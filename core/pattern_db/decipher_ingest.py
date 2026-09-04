"""
Decipher Ingest — takes structured output from the LLM and populates
the deciphered pattern database.

This is step ③ in the Engine flow:
  Buffer full → LLM Router → LLM → Decipher Ingest → Deciphered DB
"""

from __future__ import annotations

import hashlib
import logging
from uuid import uuid4

from sqlalchemy import select

from core.database import (
    DatabaseManager,
    FieldMapping,
    MatchStats,
    RequestPattern,
    ResponseTemplate,
)
from core.pattern_db.schemas import (
    ClientConfig,
    ClientEndpoint,
    Command,
    DeviceModel,
    Observation,
    PatternDB,
    PatternMeta,
    ProtocolInfo,
    ServerConfig,
    ServerResponse,
    StateVariable,
    VirtualSensor,
)
from core.pattern_db.schemas import (
    FieldMapping as SchemaFieldMapping,
)
from core.pattern_db.validator import ValidationError, validate_pattern

logger = logging.getLogger(__name__)


def _safe_float(value, default: float = 0.5) -> float:
    """Parse a float from untrusted LLM output without raising.

    Bare ``float(...)`` on values like ``"high"`` or ``"90%"`` raises and would
    abort the entire ingest transaction, discarding the learn batch.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%")
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


class DecipherIngest:
    """
    Takes LLM decipher output and saves it as structured patterns
    in the device-specific database.
    """

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager

    async def ingest(self, device_id: str, llm_output: dict) -> int:
        """
        Process LLM decipher output and save patterns to the device DB.

        Args:
            device_id: Target device
            llm_output: Structured dict from LLM analysis (see LLM prompt format)

        Returns:
            Number of patterns created
        """
        patterns = llm_output.get("patterns", [])
        if not patterns:
            logger.warning("No patterns found in LLM output for %s", device_id)
            return 0

        count = 0
        async with self.db_manager.device_session(device_id) as session:
            for pat in patterns:
                pattern_id = pat.get("pattern_id", f"pat_{uuid4().hex[:8]}")

                # Idempotency guard: skip patterns already present, so a
                # re-import of the same LLM output does not raise a UNIQUE
                # constraint violation that would roll back the whole batch.
                exists = await session.execute(
                    select(RequestPattern).where(RequestPattern.pattern_id == pattern_id)
                )
                if exists.scalar_one_or_none():
                    logger.warning("Pattern %s already exists, skipping", pattern_id)
                    continue

                # Create RequestPattern
                request_pattern = RequestPattern(
                    pattern_id=pattern_id,
                    method=pat.get("method", "GET"),
                    path_pattern=pat.get("path_pattern", pat.get("path", "/")),
                    protocol=pat.get("protocol", "http"),
                    required_headers=pat.get("required_headers", []),
                    body_schema=pat.get("body_schema", {}),
                    query_param_keys=pat.get("query_param_keys", []),
                    intent=pat.get("intent", "unknown"),
                    confidence=_safe_float(pat.get("confidence", 0.5)),
                )
                session.add(request_pattern)

                # Create ResponseTemplate
                response = pat.get("response", {})
                template_id = f"tpl_{pattern_id}"
                mappings = pat.get("field_mappings", [])
                response_template = ResponseTemplate(
                    template_id=template_id,
                    pattern_id=pattern_id,
                    status_code=response.get("status_code", 200),
                    headers_template=response.get("headers", {}),
                    body_template=response.get("body", {}),
                    field_mappings={m.get("source", ""): m.get("target", "") for m in mappings},
                    expected_variables=[
                        m.get("target", "")
                        for m in mappings
                        if m.get("target", "").startswith("result.")
                    ],
                    confidence=_safe_float(pat.get("confidence", 0.5)),
                )
                session.add(response_template)

                # Create FieldMappings
                for m in mappings:
                    field_mapping = FieldMapping(
                        mapping_id=f"map_{uuid4().hex[:8]}",
                        request_field=m.get("source", ""),
                        request_type=m.get("source_type", "string"),
                        response_field=m.get("target", ""),
                        response_type=m.get("target_type", "string"),
                        transform=m.get("transform", "direct"),
                        enum_values=m.get("mapping"),
                        intent=pat.get("intent", "unknown"),
                        confidence=_safe_float(m.get("confidence", 0.5)),
                    )
                    session.add(field_mapping)

                count += 1

            # Update stats (create the row on first ingest — it is not otherwise
            # materialized, so `if stats:` alone would leave counters at 0).
            result = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats = result.scalar_one_or_none()
            if stats is None:
                stats = MatchStats(
                    device_id=device_id,
                    total_requests=0,
                    local_hits=0,
                    cloud_misses=0,
                    errors=0,
                    match_rate_pct=0.0,
                    recent_results=[],
                    patterns_learned=0,
                    templates_created=0,
                    buffer_flushes=0,
                    current_buffer_size_bytes=0,
                )
                session.add(stats)
            stats.patterns_learned = (stats.patterns_learned or 0) + count
            stats.templates_created = (stats.templates_created or 0) + count

        logger.info("Ingested %d patterns for device %s", count, device_id)
        return count

    # ── PatternDB export / import ──────────────────────────────────────────────

    async def export_patterns(
        self,
        device_id: str,
        vendor: str = "",
        device_type: str = "",
        applied: PatternDB | None = None,
    ) -> PatternDB:
        """Export deciphered patterns from the device DB to portable format.

        ``applied`` is the engine's in-memory applied PatternDB for this device
        (optional). Its ``state_variables`` and ``virtual_sensors`` are not
        persisted in SQL - they only live in the applied in-memory config - so
        this carries them into the export instead of dropping them.
        """
        client_endpoints = []
        server_responses = []
        state_vars = list(applied.server.state_variables) if applied and applied.server else []
        sensors = list(applied.server.virtual_sensors) if applied and applied.server else []

        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(select(RequestPattern))
            patterns = result.scalars().all()

            for pat in patterns:
                # Build client endpoint
                ep = ClientEndpoint(
                    id=pat.pattern_id,
                    intent=pat.intent,
                    method=pat.method,
                    path=pat.path_pattern,
                    path_pattern=pat.path_pattern,
                    headers={"required": pat.required_headers or []},
                    query_params=pat.query_param_keys or [],
                    body_schema=pat.body_schema or None,
                )
                client_endpoints.append(ep)

                # Build server response
                tmpl_result = await session.execute(
                    select(ResponseTemplate).where(ResponseTemplate.pattern_id == pat.pattern_id)
                )
                tmpl = tmpl_result.scalar_one_or_none()
                if tmpl:
                    mappings_result = await session.execute(
                        select(FieldMapping).where(FieldMapping.intent == pat.intent)
                    )
                    mappings = mappings_result.scalars().all()
                    srv_resp = ServerResponse(
                        id=tmpl.template_id,
                        triggers=[pat.intent],
                        status_code=tmpl.status_code,
                        headers_template=tmpl.headers_template or {},
                        body_template=tmpl.body_template or {},
                        field_mappings=[
                            SchemaFieldMapping(
                                source=m.request_field,
                                target=m.response_field,
                                transform=m.transform or "direct",
                                mapping=m.enum_values,
                            )
                            for m in mappings
                        ],
                    )
                    server_responses.append(srv_resp)

        return PatternDB(
            meta=PatternMeta(
                pattern_id=f"{device_id}-patterns",
                vendor=vendor or "unknown",
                device_type=device_type or "unknown",
            ),
            client=ClientConfig(endpoints=client_endpoints),
            server=ServerConfig(
                responses=server_responses,
                state_variables=state_vars,
                virtual_sensors=sensors,
            ),
        )

    async def import_patterns(self, device_id: str, pattern_db: PatternDB) -> int:
        """Import patterns from a portable PatternDB into the device DB."""
        # Validate against the portable JSON Schema before importing
        result = validate_pattern(pattern_db.model_dump(by_alias=True, exclude_none=True))
        if not result.valid:
            raise ValidationError(result=result)

        count = 0
        async with self.db_manager.device_session(device_id) as session:
            for ep in pattern_db.client.endpoints:
                pattern_id = ep.id
                request_pattern = RequestPattern(
                    pattern_id=pattern_id,
                    method=ep.method,
                    path_pattern=ep.path_pattern or ep.path,
                    protocol=pattern_db.client.protocols[0]
                    if pattern_db.client.protocols
                    else "http",
                    required_headers=ep.headers.get("required", []),
                    body_schema=ep.body_schema or {},
                    query_param_keys=ep.query_params,
                    intent=ep.intent,
                    confidence=0.9,
                )
                session.add(request_pattern)

                # Match server responses
                for resp in pattern_db.server.responses:
                    if ep.intent in resp.triggers:
                        template_id = f"tpl_{pattern_id}"
                        response_template = ResponseTemplate(
                            template_id=template_id,
                            pattern_id=pattern_id,
                            status_code=resp.status_code,
                            headers_template=resp.headers_template,
                            body_template=resp.body_template,
                            field_mappings={m.source: m.target for m in resp.field_mappings},
                            expected_variables=[
                                m.target
                                for m in resp.field_mappings
                                if m.target.startswith("result.")
                            ],
                            confidence=0.9,
                        )
                        session.add(response_template)

                        for fm in resp.field_mappings:
                            field_mapping = FieldMapping(
                                mapping_id=f"map_{uuid4().hex[:8]}",
                                request_field=fm.source,
                                request_type="string",
                                response_field=fm.target,
                                response_type="string",
                                transform=fm.transform,
                                enum_values=fm.mapping,
                                intent=ep.intent,
                                confidence=0.9,
                            )
                            session.add(field_mapping)
                        break

                count += 1

        logger.info("Imported %d patterns for device %s", count, device_id)
        return count

    # -- DeviceModel (v2) export / import ----------------------------------
    #
    # The v1 PatternDB export above is SQL-faithful but drops state_variables /
    # virtual_sensors (they live only in the engine's in-memory applied config)
    # and has no home for ProtocolInfo or observation_history. These v2 methods
    # round-trip the full portable DeviceModel: SQL (commands/responses/
    # interactions) + applied in-memory state + DeviceMeta protocol.

    async def export_device_model(
        self,
        device_id: str,
        vendor: str = "",
        device_type: str = "",
        applied: PatternDB | None = None,
        observations: list[Observation] | None = None,
    ) -> DeviceModel:
        """Export the full portable v2 DeviceModel for a device.

        Combines three sources:
          * SQL tables (RequestPattern / ResponseTemplate / FieldMapping) for
            commands, responses and interactions;
          * the engine's in-memory applied PatternDB (when given) for
            state_variables / virtual_sensors, which have no SQL table;
          * the persisted DeviceMeta header for ProtocolInfo.

        ``observations`` (optional) are carried as ``observation_history`` so a
        second install has grounding when no exact command matches.
        """
        commands: list[Command] = []
        responses: list[ServerResponse] = []
        interactions: list[SchemaFieldMapping] = []

        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(select(RequestPattern))
            patterns = result.scalars().all()

            for pat in patterns:
                commands.append(
                    Command(
                        id=pat.pattern_id,
                        kind=pat.intent or pat.pattern_id,
                        protocol=pat.protocol or "http",
                        method=pat.method or "GET",
                        path=pat.path_pattern or "",
                        path_pattern=pat.path_pattern or "",
                        headers={"required": pat.required_headers or []},
                        query_params=pat.query_param_keys or [],
                        body_schema=pat.body_schema or None,
                        confidence=_safe_float(pat.confidence),
                    )
                )

                tmpl_result = await session.execute(
                    select(ResponseTemplate).where(
                        ResponseTemplate.pattern_id == pat.pattern_id
                    )
                )
                tmpl = tmpl_result.scalar_one_or_none()
                if tmpl:
                    mappings_result = await session.execute(
                        select(FieldMapping).where(FieldMapping.intent == pat.intent)
                    )
                    mappings = mappings_result.scalars().all()
                    fms = [
                        SchemaFieldMapping(
                            source=m.request_field,
                            target=m.response_field,
                            transform=m.transform or "direct",
                            mapping=m.enum_values,
                        )
                        for m in mappings
                    ]
                    responses.append(
                        ServerResponse(
                            id=tmpl.template_id,
                            triggers=[pat.intent] if pat.intent else [],
                            status_code=tmpl.status_code,
                            headers_template=tmpl.headers_template or {},
                            body_template=tmpl.body_template or {},
                            field_mappings=fms,
                        )
                    )
                    interactions.extend(fms)

        protocol = ProtocolInfo()
        meta = await self.db_manager.read_device_meta(device_id)
        if meta:
            protocols = meta.get("protocols") or []
            if protocols:
                protocol.protocol = protocols[0]
            protocol.handler = meta.get("connection_mode", "auto")
            protocol.transport = meta.get("transport", "")
            protocol.security = meta.get("security", "")
            protocol.proprietary = bool(meta.get("proprietary", False))
            protocol.identity = meta.get("identity") or meta.get("model", "")
            protocol.ports = list(meta.get("ports") or [])
            protocol.confidence = _safe_float(meta.get("confidence"), 0.0)

        # state_variables / virtual_sensors have no SQL table: their canonical
        # home is the persisted header (DeviceMeta). Fall back to the engine's
        # in-memory applied config when the header has none (legacy pre-v2).
        state_variables: list = []
        virtual_sensors: list = []
        if meta and (meta.get("state_variables") or meta.get("virtual_sensors")):
            state_variables = [
                StateVariable(**sv) for sv in meta.get("state_variables", [])
            ]
            virtual_sensors = [
                VirtualSensor(**vs) for vs in meta.get("virtual_sensors", [])
            ]
        elif applied and applied.server:
            state_variables = list(applied.server.state_variables)
            virtual_sensors = list(applied.server.virtual_sensors)

        return DeviceModel(
            meta=PatternMeta(
                pattern_id=f"{device_id}-patterns",
                vendor=vendor or "unknown",
                device_type=device_type or "unknown",
                model=meta.get("model", "") if meta else "",
            ),
            protocol=protocol,
            commands=commands,
            responses=responses,
            interactions=interactions,
            state_variables=state_variables,
            virtual_sensors=virtual_sensors,
            observation_history=list(observations or []),
        )

    async def import_device_model(self, device_id: str, model: DeviceModel) -> int:
        """Import a portable v2 DeviceModel into this device DB.

        Writes the v1 SQL tables (RequestPattern / ResponseTemplate /
        FieldMapping) via :meth:`DeviceModel.to_pattern_db`, and persists the
        identification header (protocol) into DeviceMeta so routing can consume
        it. ``observation_history`` is preserved in the model object (and any
        re-export) but is handed to the buffer/observation layer, not SQL.
        """
        header = {
            "protocols": [model.protocol.protocol] if model.protocol.protocol else [],
            "connection_mode": model.protocol.handler or "auto",
            "vendor": model.meta.vendor,
            "device_type": model.meta.device_type,
            "model": model.protocol.identity or model.meta.model,
            "transport": model.protocol.transport,
            "security": model.protocol.security,
            "proprietary": model.protocol.proprietary,
            "identity": model.protocol.identity,
            "ports": model.protocol.ports,
            "confidence": model.protocol.confidence,
            "state_variables": [
                sv.model_dump(exclude_none=True) for sv in model.state_variables
            ],
            "virtual_sensors": [
                vs.model_dump(exclude_none=True) for vs in model.virtual_sensors
            ],
        }
        try:
            await self.db_manager.write_device_meta(device_id, header)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to persist device header for %s: %s", device_id, e)

        pattern_db = model.to_pattern_db()
        return await self.import_patterns(device_id, pattern_db)



    async def merge_device_model(self, device_id: str, model: DeviceModel) -> int:  # noqa: C901, PLR0912, PLR0915
        """Idempotently merge a v2 model delta into an existing device DB.

        This is the *learning update* path (subsequent LLM flushes, C1):
        unlike :meth:`import_device_model` (fresh-install import that always
        ``session.add``), every row here is keyed by a deterministic id and
        upserted, so re-learning a device converges on the single row of truth
        instead of raising UNIQUE or leaving stale rows behind.

        ``commands`` ? RequestPattern (id = ``{pattern_id}`` when the command
        carries an explicit id, else ``{device_id}_{kind}_{md5(path|topic)[:8]}``
        so a command emitted again maps to the same row). Responses/interactions
        follow the same stable keys used by the v1 SQL export. The protocol
        header is merged (only carried protocol fields overwrite the existing,
        so a partial delta never blanks the identity).

        Returns the number of command rows merged (created + updated).
        """
        current = await self.db_manager.read_device_meta(device_id) or {}

        # ?? merge protocol header (only when the delta carries it) ????????????
        if model.protocol and model.protocol.protocol:
            merged = dict(current)
            merged["vendor"] = model.meta.vendor or current.get("vendor", "unknown")
            merged["device_type"] = (
                model.meta.device_type or current.get("device_type", "unknown")
            )
            merged["protocols"] = [model.protocol.protocol]
            merged["connection_mode"] = model.protocol.handler or current.get(
                "connection_mode", "auto"
            )
            merged["model"] = model.protocol.identity or current.get("model", "")
            merged["transport"] = model.protocol.transport or current.get(
                "transport", ""
            )
            merged["security"] = model.protocol.security or current.get(
                "security", ""
            )
            merged["proprietary"] = model.protocol.proprietary or current.get(
                "proprietary", False
            )
            merged["identity"] = model.protocol.identity or current.get(
                "identity", ""
            )
            merged["ports"] = list(model.protocol.ports) or list(
                current.get("ports") or []
            )
            merged["confidence"] = model.protocol.confidence or current.get(
                "confidence", 0.0
            )
            try:
                await self.db_manager.write_device_meta(device_id, merged)
            except Exception as e:  # noqa: BLE001 - header merge is best-effort
                logger.warning("Failed to merge device header for %s: %s", device_id, e)

        updated = 0
        async with self.db_manager.device_session(device_id) as session:
            for cmd in model.commands:
                path = cmd.path or cmd.path_pattern or cmd.topic or ""
                if cmd.id and not cmd.id.startswith(f"{device_id}_"):
                    pattern_id = cmd.id
                else:
                    path_digest = hashlib.md5(path.encode("utf-8")).hexdigest()[:8]
                    pattern_id = f"{device_id}_{cmd.kind}_{path_digest}"

                existing = await session.execute(
                    select(RequestPattern).where(RequestPattern.pattern_id == pattern_id)
                )
                pattern = existing.scalar_one_or_none()
                if pattern is None:
                    pattern = RequestPattern(
                        pattern_id=pattern_id,
                        method=cmd.method or "GET",
                        path_pattern=path,
                        protocol=cmd.protocol or "http",
                        required_headers=cmd.headers.get("required", []),
                        body_schema=cmd.body_schema or {},
                        query_param_keys=cmd.query_params or [],
                        intent=cmd.kind,
                        confidence=_safe_float(cmd.confidence, 0.5),
                    )
                    session.add(pattern)
                else:
                    pattern.method = cmd.method or "GET"
                    pattern.path_pattern = path or pattern.path_pattern
                    pattern.protocol = cmd.protocol or "http"
                    pattern.required_headers = cmd.headers.get("required", [])
                    pattern.body_schema = cmd.body_schema or {}
                    pattern.query_param_keys = cmd.query_params or []
                    pattern.intent = cmd.kind
                    pattern.confidence = _safe_float(cmd.confidence, 0.5)

                template_id = f"tpl_{pattern_id}"
                existing_tpl = await session.execute(
                    select(ResponseTemplate).where(
                        ResponseTemplate.template_id == template_id
                    )
                )
                tpl = existing_tpl.scalar_one_or_none()
                resp = next(
                    (
                        r
                        for r in (model.responses or [])
                        if r.triggers and cmd.kind in r.triggers
                    ),
                    None,
                )
                if tpl is None:
                    tpl = ResponseTemplate(
                        template_id=template_id,
                        pattern_id=pattern_id,
                        status_code=resp.status_code if resp else 200,
                        headers_template=resp.headers_template if resp else {},
                        body_template=resp.body_template if resp else {},
                        field_mappings={
                            m.source: m.target
                            for m in (resp.field_mappings if resp else [])
                        },
                        expected_variables=[],
                    )
                    session.add(tpl)
                elif resp:
                    tpl.status_code = resp.status_code
                    tpl.headers_template = resp.headers_template or {}
                    tpl.body_template = resp.body_template or {}
                    tpl.field_mappings = {
                        m.source: m.target for m in resp.field_mappings
                    }
                if resp:
                    tpl.expected_variables = [
                        m.target
                        for m in resp.field_mappings
                        if m.target.startswith("result.")
                    ]

                # interactions: deterministic mapping ids so re-flush converges.
                for fm in (resp.field_mappings if resp else []) or (
                    model.interactions or []
                ):
                    request_field = fm.source or fm.mapping or ""
                    if not request_field:
                        continue
                    mapping_id = (
                        f"map_{device_id}_{cmd.kind}_"
                        f"{request_field.replace('.', '_')}"
                    )
                    existing_map = await session.execute(
                        select(FieldMapping).where(
                            FieldMapping.mapping_id == mapping_id
                        )
                    )
                    mp = existing_map.scalar_one_or_none()
                    if mp is None:
                        session.add(
                            FieldMapping(
                                mapping_id=mapping_id,
                                request_field=request_field,
                                request_type="string",
                                response_field=fm.target,
                                response_type="string",
                                transform=fm.transform or "direct",
                                enum_values=fm.mapping,
                                intent=cmd.kind,
                                confidence=0.5,
                            )
                        )
                    else:
                        mp.response_field = fm.target or mp.response_field
                        mp.transform = fm.transform or "direct"
                        mp.enum_values = fm.mapping
                updated += 1

            # stats
            result = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats = result.scalar_one_or_none()
            if stats is None:
                stats = MatchStats(
                    device_id=device_id,
                    total_requests=0,
                    local_hits=0,
                    cloud_misses=0,
                    errors=0,
                    match_rate_pct=0.0,
                    recent_results=[],
                    patterns_learned=0,
                    templates_created=0,
                    buffer_flushes=0,
                    current_buffer_size_bytes=0,
                )
                session.add(stats)
            stats.patterns_learned = (stats.patterns_learned or 0) + updated
            stats.templates_created = (stats.templates_created or 0) + updated

        logger.info("Merged %d model rows for device %s", updated, device_id)
        return updated
