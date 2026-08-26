"""
Pattern Engine â€” matches incoming requests against deciphered patterns,
builds local responses, manages device state, and handles sensor simulation.

This is step â‘£ in the Engine flow, extending the existing PatternMatcher
with state management and .ride-pattern.json import/export.
"""

from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import simpleeval
from sqlalchemy import select

from core.atomic_io import write_json
from core.database import (
    DatabaseManager,
    DeviceState,
    RequestPattern,
    ResponseTemplate,
)
from core.pattern_db.schemas import PatternDB
from core.pattern_db.state_manager import DeviceStateStore

logger = logging.getLogger(__name__)

# Allowed names exposed inside formulas (safe math helpers only).
# Passed to simpleeval as the ``functions`` whitelist.
_FORMULA_SAFE_NAMES = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
}

# Pre-compiled regex patterns for template resolution and formula processing (hot paths)
_RE_JSON_PATH_SPLIT = re.compile(r"[\.\[\]]+")
_RE_TEMPLATE_VAR = re.compile(r"\{state\.(\w+)\}|\{request\.(\w+(?:\.\w+)*)\}|\{uuid\}")
_RE_FORMULA_VAR = re.compile(r"\{state\.(\w+)\}|\{request\.(\w+(?:\.\w+)*)\}")
_RE_FORMULA_RANDOM = re.compile(r"random\(([^,]+),\s*([^)]+)\)")


def _as_formula_literal(value: Any) -> str:  # noqa: ANN401
    """Render a runtime value as a safe literal inside a formula string.

    Numbers are emitted as-is so arithmetic still works. Any other value
    (notably strings sourced from request/state data) is emitted as an escaped
    JSON string literal, so ``simpleeval`` treats it as a literal string
    instead of re-parsing it as formula code. This is the F1 fix: a
    stored value like ``"1+1"`` used to be spliced raw into the formula and
    evaluated as ``1+1 = 2`` instead of being treated as the string ``"1+1"``,
    letting attacker-controlled device data fabricate results.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value)


class PatternEngine:
    """
    Matches incoming requests against learned patterns and builds local responses.

    Extends the original PatternMatcher with:
    - In-memory pattern caching from .ride-pattern.json
    - Device state management (state_variables)
    - Virtual sensor simulation
    - Template variable resolution (state, request fields, constants)
    """

    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db_manager = db_manager
        self._state_stores: dict[str, DeviceStateStore] = {}
        self._cached_patterns: dict[str, PatternDB] = {}

    # â”€â”€ State Management â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def get_state_store(self, device_id: str) -> DeviceStateStore:
        """Get or create the state store for a device."""
        if device_id not in self._state_stores:
            self._state_stores[device_id] = DeviceStateStore(device_id)
        return self._state_stores[device_id]

    def apply_pattern_db(self, device_id: str, pattern_db: PatternDB):
        """Apply a PatternDB's server config to a device's state store."""
        store = self.get_state_store(device_id)
        store.apply_state_variables(pattern_db.server.state_variables)
        store.apply_virtual_sensors(pattern_db.server.virtual_sensors)
        self._cached_patterns[device_id] = pattern_db

    async def load_state(self, device_id: str) -> None:
        """Restore a device's persisted state variables into its store."""
        store = self.get_state_store(device_id)
        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(
                select(DeviceState).where(DeviceState.device_id == device_id)
            )
            row = result.scalar_one_or_none()
        if row is not None and row.state:
            store.restore({"variables": row.state})
        store.clear_dirty()

    async def persist_state(self, device_id: str) -> bool:
        """Persist a device's state variables if they changed since last save.

        Writes are done through the device DB session, so the snapshot is kept
        durably (WAL + transactional) and survives a restart. Returns True when
        something was written.
        """
        store = self.get_state_store(device_id)
        if not store.is_dirty:
            return False
        variables = store.snapshot()["variables"]
        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(
                select(DeviceState).where(DeviceState.device_id == device_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                session.add(DeviceState(device_id=device_id, state=variables))
            else:
                row.state = variables
        store.clear_dirty()
        return True

    # â”€â”€ Pattern Matching â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def find_best_match(  # noqa: PLR0913, PLR0912
        self,
        device_id: str,
        method: str,
        path: str,
        headers: dict,
        body: Any,  # noqa: ANN401
        query_params: dict,
    ) -> tuple:
        """Find best matching pattern. Returns (pattern, response_template, score)."""
        best_score = 0.0
        best_pattern = None
        best_template = None

        # Try cached in-memory patterns first
        cached = self._cached_patterns.get(device_id)
        if cached:
            for ep in cached.client.endpoints:
                score = self._calculate_similarity(
                    method,
                    ep.method,
                    ep.path_pattern or ep.path,
                    path,
                    ep.headers.get("required", []),
                    headers,
                    ep.body_schema or {},
                    body,
                    ep.query_params,
                    query_params,
                )
                if score > best_score:
                    best_score = score
                    best_pattern = ep
                    # Find matching server response
                    for resp in cached.server.responses:
                        if ep.intent in resp.triggers:
                            best_template = resp
                            break

        # Fall back to database patterns
        async with self.db_manager.device_session(device_id) as session:
            result = await session.execute(select(RequestPattern))
            db_patterns = result.scalars().all()

            for pat in db_patterns:
                score = self._calculate_similarity(
                    method,
                    pat.method,
                    pat.path_pattern,
                    path,
                    pat.required_headers or [],
                    headers,
                    pat.body_schema or {},
                    body,
                    pat.query_param_keys or [],
                    query_params,
                )
                if score > best_score:
                    best_score = score
                    best_pattern = pat
                    tmpl_result = await session.execute(
                        select(ResponseTemplate).where(
                            ResponseTemplate.pattern_id == pat.pattern_id
                        )
                    )
                    best_template = tmpl_result.scalar_one_or_none()

        return best_pattern, best_template, best_score

    def _calculate_similarity(  # noqa: PLR0913
        self,
        method_a: str,
        method_b: str,
        path_pattern: str,
        actual_path: str,
        required_headers: list,
        actual_headers: dict,
        body_schema: dict,
        actual_body: Any,  # noqa: ANN401
        query_param_keys: list,
        actual_query: dict,
    ) -> float:
        """Calculate similarity score (0.0 to 1.0)."""
        score = 0.0
        total_weight = 0.0

        # Method match
        total_weight += 30.0
        if method_a == method_b:
            score += 30.0

        # Path match
        total_weight += 30.0
        score += 30.0 * self._path_similarity(path_pattern, actual_path)

        # Headers
        total_weight += 15.0
        if required_headers:
            present = sum(1 for h in required_headers if h in actual_headers)
            score += 15.0 * (present / len(required_headers))

        # Query params
        total_weight += 10.0
        if query_param_keys:
            present = sum(1 for q in query_param_keys if q in actual_query)
            score += 10.0 * (present / len(query_param_keys))

        # Body
        if actual_body and body_schema:
            total_weight += 15.0
            score += 15.0 * self._body_similarity(body_schema, actual_body)
        elif not actual_body and not body_schema:
            total_weight += 15.0
            score += 15.0

        return score / total_weight if total_weight > 0 else 0.0

    def _path_similarity(self, pattern: str, actual: str) -> float:
        p_parts = pattern.strip("/").split("/")
        a_parts = actual.strip("/").split("/")
        if len(p_parts) != len(a_parts):
            return 0.3 if abs(len(p_parts) - len(a_parts)) <= 1 else 0.0
        matches = 0
        for p, a in zip(p_parts, a_parts):
            if p.startswith("{") and p.endswith("}") or p == a:
                matches += 1
        return matches / len(p_parts) if p_parts else 1.0

    def _body_similarity(self, schema: dict, body: dict) -> float:
        if not schema or not body:
            return 0.5
        schema_keys = set(schema.get("properties", schema).keys())
        body_keys = set(body.keys()) if isinstance(body, dict) else set()
        if not schema_keys:
            return 1.0
        intersection = schema_keys & body_keys
        return len(intersection) / len(schema_keys)

    # â”€â”€ Response Building â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def build_local_response(
        self,
        device_id: str,
        template,
        original_request: dict,
    ) -> dict:
        """Build a local response from a template, resolving variables."""
        store = self.get_state_store(device_id)

        # Determine if we have a ServerResponse (Pydantic) or DB ResponseTemplate
        if hasattr(template, "body_template"):
            body = dict(template.body_template)
            field_mappings = getattr(template, "field_mappings", [])
            status_code = template.status_code
            headers = dict(template.headers_template)
        else:
            body = dict(template.body_template)
            field_mappings = []
            status_code = template.status_code
            headers = dict(template.headers_template)

        # If we have Pydantic field_mappings, use those
        if field_mappings:
            for fm in field_mappings:
                source = fm.source if hasattr(fm, "source") else fm.get("source", "")
                target = fm.target if hasattr(fm, "target") else fm.get("target", "")
                transform = (
                    fm.transform if hasattr(fm, "transform") else fm.get("transform", "direct")
                )
                mapping = fm.mapping if hasattr(fm, "mapping") else fm.get("mapping")

                val = self._resolve_source(source, original_request, store)
                if val is not None:
                    if transform == "enum":
                        enum_map = mapping or {}
                        val = enum_map.get(str(val), val)
                    elif transform == "formula":
                        val = self._eval_formula(
                            fm.formula if hasattr(fm, "formula") else fm.get("formula", ""),
                            original_request,
                            store,
                        )
                    # Field mappings targeting state.* mutate the persistent
                    # device state store (survives restart via persist_state).
                    if target.startswith("state."):
                        store.set(target[6:], val)
                    else:
                        self._set_nested(body, target, val)

        # Resolve template variables {state.xxx} in body
        body = self._resolve_template_vars(body, store, original_request)

        return {
            "status_code": status_code,
            "headers": headers,
            "body": body,
        }

    def _resolve_source(self, source: str, request: dict, store: DeviceStateStore) -> Any:  # noqa: ANN401
        """Resolve a source reference like 'request.body.x.y' or 'state.varname'."""
        if source.startswith("request."):
            # e.g. request.body.commands[0].value -> resolve "body.commands[0].value" in request
            path = source[8:]  # strip "request." prefix
            return self._resolve_json_path(request, path)
        if source.startswith("state."):
            return store.get(source[6:])
        if source.startswith("constant."):
            return source.split(".", 1)[1] if "." in source else None
        return None

    def _resolve_json_path(self, obj: Any, path: str) -> Any:  # noqa: ANN401
        """Resolve a path like 'commands[0].value' in a JSON object."""
        parts = _RE_JSON_PATH_SPLIT.split(path)
        parts = [p for p in parts if p]
        current = obj
        for p in parts:
            if isinstance(current, dict):
                current = current.get(p)
            elif isinstance(current, list):
                try:
                    idx = int(p)
                    current = current[idx] if 0 <= idx < len(current) else None
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return current

    def _set_nested(self, d: dict, path: str, value: Any):  # noqa: ANN401
        parts = path.split(".")
        for p in parts[:-1]:
            if p not in d or d[p] is None:
                d[p] = {}
            d = d[p]
        d[parts[-1]] = value

    def _resolve_template_vars(self, obj: Any, store: DeviceStateStore, request: dict) -> Any:  # noqa: ANN401
        """Recursively resolve {state.x} and {request.x} placeholders in a template."""
        if isinstance(obj, str):
            if "{state." in obj or "{request." in obj or "{uuid}" in obj:

                def _replacer(m: re.Match) -> str:
                    if m.group(1):  # state.<name>
                        return str(store.get(m.group(1), ""))
                    if m.group(2):  # request.<path>
                        val = self._resolve_source(f"request.{m.group(2)}", request, store)
                        return str(val or "")
                    # {uuid}  # noqa: ERA001
                    return str(uuid4())

                obj = _RE_TEMPLATE_VAR.sub(_replacer, obj)
            return obj
        if isinstance(obj, dict):
            return {k: self._resolve_template_vars(v, store, request) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._resolve_template_vars(item, store, request) for item in obj]
        return obj

    def _eval_formula(self, formula: str, request: dict, store: DeviceStateStore) -> Any:  # noqa: ANN401
        """Evaluate a simple formula expression via simpleeval (restricted, no eval)."""
        try:
            # Replace variable references with re.sub (single pass, no intermediate strings)
            def _var_replacer(m: re.Match) -> str:
                if m.group(1):  # state.<name>
                    return _as_formula_literal(store.get(m.group(1), 0))
                if m.group(2):  # request.<path>
                    val = self._resolve_source(f"request.{m.group(2)}", request, store)
                    return _as_formula_literal(val or 0)
                return m.group(0)

            resolved = _RE_FORMULA_VAR.sub(_var_replacer, formula)
            # Replace function calls
            resolved = _RE_FORMULA_RANDOM.sub(
                lambda m: str(random.uniform(float(m.group(1)), float(m.group(2)))),
                resolved,
            )
            return simpleeval.simple_eval(resolved, functions=_FORMULA_SAFE_NAMES)
        except Exception as e:
            logger.warning("Formula eval failed: %s (%s)", formula, e)
            return 0

    # â”€â”€ Pattern DB File I/O â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def load_pattern_file(self, device_id: str, filepath: str) -> PatternDB:
        """Load a .ride-pattern.json file and cache it for a device."""
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Pattern file not found: {filepath}")
        data = json.loads(path.read_text(encoding="utf-8"))
        pattern_db = PatternDB.model_validate(data)
        self.apply_pattern_db(device_id, pattern_db)
        return pattern_db

    def save_pattern_file(self, pattern_db: PatternDB, filepath: str):
            """Save a PatternDB to a .ride-pattern.json file (atomically)."""
            path = Path(filepath)
            data = pattern_db.model_dump(by_alias=True, exclude_none=True)
            write_json(path, data)
            logger.info("Saved pattern DB to %s", filepath)
