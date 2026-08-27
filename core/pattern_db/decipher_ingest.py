"""
Decipher Ingest — takes structured output from the LLM and populates
the deciphered pattern database.

This is step ③ in the Engine flow:
  Buffer full → LLM Router → LLM → Decipher Ingest → Deciphered DB
"""

from __future__ import annotations

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
    PatternDB,
    PatternMeta,
    ServerConfig,
    ServerResponse,
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
        self, device_id: str, vendor: str = "", device_type: str = ""
    ) -> PatternDB:
        """Export deciphered patterns from the device DB to portable format."""
        client_endpoints = []
        server_responses = []
        state_vars = []
        sensors = []

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
