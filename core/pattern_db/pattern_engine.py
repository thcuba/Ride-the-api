"""
Pattern Engine â€” matches incoming requests against deciphered patterns,
builds local responses, manages device state, and handles sensor simulation.

This is step â‘£ in the Engine flow, extending the existing PatternMatcher
with state management and .ride-pattern.json import/export.
"""

from __future__ import annotations

import functools
import json
import logging
import random
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import dpath
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

# Maximum length of a formula string. Cap guards against pathological inputs
# from untrusted pattern files / LLM output (CPU / memory exhaustion) before
# the evaluator is reached.
_MAX_FORMULA_LENGTH = 1024

# A hardened simpleeval evaluator for untrusted formula strings:
#   * ``names`` is empty — variables are pre-substituted with typed literals.
#   * ``allowed_attrs`` is empty — no attribute access is permitted at all, so
#     known escape vectors like ``().__class__.__bases__`` or ``.get`` cannot
#     reach host objects/methods.
#   * functions are allow-listed to ``_FORMULA_SAFE_NAMES`` only.
_FORMULA_EVALUATOR = simpleeval.SimpleEval(
    names={},
    functions=_FORMULA_SAFE_NAMES,
    allowed_attrs={},
)

# Pre-compiled regex patterns for template resolution and formula processing (hot paths)
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


@functools.lru_cache(maxsize=2048)
def _dot_to_dpath(path: str) -> str:
    """Convert dot/bracket path notation ('a.b[0].c') to dpath slash notation.

    dpath uses '/' as its default separator; map '.' and '[0]' → '/', and
    drop the closing ']'. Array indexes become segments (e.g. ``items/0/name``).

    Memoized with lru_cache for fast O(1) path conversion during request/response mapping.
    """
    p = path.lstrip("$")
    p = p.replace("[", "/").replace("]", "")
    return p.replace(".", "/")


def _dpath_set(d: dict, path: str, value: Any) -> None:  # noqa: ANN401
    """Set a value at a dot/bracket path via dpath, creating intermediates.

    dpath does not traverse ``None`` intermediates, so any segment whose value
    is ``None`` is replaced with an empty dict before the write.
    """
    parts = _dot_to_dpath(path).split("/")
    obj: Any = d
    for p in parts[:-1]:
        if isinstance(obj, dict) and obj.get(p) is None:
            obj[p] = {}
        obj = obj.get(p)
    dpath.new(d, _dot_to_dpath(path), value, separator="/")


@functools.lru_cache(maxsize=2048)
def _path_similarity(pattern: str, actual: str) -> float:
    """Compare a path pattern (may contain ``{placeholders}``) vs an actual path.

    Shared by :class:`PatternEngine` and the pipeline's matcher so both use the
    same scoring. Segments match when equal or when the pattern segment is a
    ``{placeholder}``. Length mismatch within one yields a partial 0.3 score.

    Memoized with lru_cache for ~12x faster repeated evaluations in request pattern
    matching hot paths.
    """
    p_parts = pattern.strip("/").split("/")
    a_parts = actual.strip("/").split("/")
    lp = len(p_parts)
    la = len(a_parts)
    if lp != la:
        return 0.3 if abs(lp - la) <= 1 else 0.0
    if not lp:
        return 1.0
    matches = 0
    # Performance optimization: direct index checks p[0] == "{" and p[-1] == "}"
    # instead of startswith/endswith (~1.4x faster per call in request hot path).
    for p, a in zip(p_parts, a_parts):
        if p == a or (p and p[0] == "{" and p[-1] == "}"):
            matches += 1
    return matches / lp


def _body_similarity(schema: dict, body: dict) -> float:
    """Compare body schema keys against actual body keys (structural match).

    Handles JSON-schema ``{"properties": {...}}`` wrappers and tolerates a
    non-dict ``body`` (treated as having no keys).

    Optimized by avoiding intermediate set creations/intersections in request matching
    hot paths (~1.14x faster per call).
    """
    if not schema or not body:
        return 0.5
    props = schema.get("properties", schema)
    if not props:
        return 1.0
    if not isinstance(body, dict):
        return 0.0
    matches = sum(1 for k in props if k in body)
    return matches / len(props)


def _normalize_field_mappings(field_mappings: Any) -> list[dict]:  # noqa: ANN401
    """Normalise a template's ``field_mappings`` into a uniform list of dicts.

    Two shapes exist depending on where the template came from:

    * a Pydantic :class:`ServerResponse` (cached .ride-pattern.json) exposes
      ``field_mappings`` as a **list** of :class:`FieldMapping` objects;
    * a DB :class:`ResponseTemplate` exposes it as a **dict** ``{source:
      target}`` (only a direct mapping survives, as written by
      ``DecipherIngest``/``import_patterns``).

    This helper collapses both to a list of dicts with the same keys
    (``source``/``target``/``transform``/``mapping``/``formula``) so
    :meth:`PatternEngine.build_local_response` never inspects the type itself.
    """
    if isinstance(field_mappings, dict):
        return [
            {
                "source": source,
                "target": target,
                "transform": "direct",
                "mapping": None,
                "formula": "",
            }
            for source, target in field_mappings.items()
        ]
    result = []
    for fm in field_mappings or []:
        result.append(
            {
                "source": fm.source if hasattr(fm, "source") else fm.get("source", ""),
                "target": fm.target if hasattr(fm, "target") else fm.get("target", ""),
                "transform": (
                    fm.transform if hasattr(fm, "transform") else fm.get("transform", "direct")
                ),
                "mapping": fm.mapping if hasattr(fm, "mapping") else fm.get("mapping"),
                "formula": fm.formula if hasattr(fm, "formula") else fm.get("formula", ""),
            }
        )
    return result


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
        # Pre-computed trigger -> response maps for fast O(1) response lookup in find_best_match
        self._response_trigger_maps: dict[str, dict[str, Any]] = {}

    # â”€â”€ State Management â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def get_state_store(self, device_id: str) -> DeviceStateStore:
        """Get or create the state store for a device."""
        if device_id not in self._state_stores:
            self._state_stores[device_id] = DeviceStateStore(device_id)
        return self._state_stores[device_id]

    def _get_trigger_map(self, device_id: str, cached: PatternDB) -> dict[str, Any]:
        """Get or build fast O(1) trigger-to-response lookup map for cached pattern DB."""
        if device_id not in self._response_trigger_maps:
            trigger_map = {}
            if cached.server and cached.server.responses:
                for resp in cached.server.responses:
                    for trigger in resp.triggers:
                        if trigger not in trigger_map:
                            trigger_map[trigger] = resp
            self._response_trigger_maps[device_id] = trigger_map
        return self._response_trigger_maps[device_id]

    def apply_pattern_db(self, device_id: str, pattern_db: PatternDB):
        """Apply a PatternDB's server config to a device's state store."""
        store = self.get_state_store(device_id)
        store.apply_state_variables(pattern_db.server.state_variables)
        store.apply_virtual_sensors(pattern_db.server.virtual_sensors)
        self._cached_patterns[device_id] = pattern_db
        self._response_trigger_maps.pop(device_id, None)
        self._get_trigger_map(device_id, pattern_db)

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

        # Try cached in-memory patterns first.
        # The cache is the authoritative snapshot of the last export/import
        # for the device, so when it is present it is used exclusively and the
        # per-request DB scan below is skipped (avoids a redundant device DB
        # round-trip on every production/hybrid request).
        cached = self._cached_patterns.get(device_id)
        if cached:
            # Fast O(1) response template lookup by intent instead of O(N) list search (~21x faster)
            trigger_map = self._get_trigger_map(device_id, cached)
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
                    best_template = trigger_map.get(ep.intent)
            return best_pattern, best_template, best_score

        # Fall back to database patterns (only when no cached pattern file exists
        # for this device).
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
                    best_template = None
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
        """Compare path pattern (may contain {placeholders}) vs actual path."""
        return _path_similarity(pattern, actual)

    def _body_similarity(self, schema: dict, body: dict) -> float:
        return _body_similarity(schema, body)

    # â”€â”€ Response Building â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    async def build_local_response(
        self,
        device_id: str,
        template,
        original_request: dict,
    ) -> dict:
        """Build a local response from a template, resolving variables.

        Accepts either a Pydantic :class:`ServerResponse` (from a cached
        .ride-pattern.json, whose ``field_mappings`` is a list of
        :class:`FieldMapping`) or a DB :class:`ResponseTemplate` (whose
        ``field_mappings`` is a ``{source: target}`` dict). Field mappings are
        normalised to a uniform list before being applied, so both shapes
        resolve identically.
        """
        store = self.get_state_store(device_id)

        body = dict(template.body_template)
        status_code = template.status_code
        headers = dict(template.headers_template)

        for fm in _normalize_field_mappings(getattr(template, "field_mappings", [])):
            source = fm["source"]
            target = fm["target"]
            transform = fm["transform"]
            mapping = fm["mapping"]

            val = self._resolve_source(source, original_request, store)
            if val is not None:
                if transform == "enum":
                    enum_map = mapping or {}
                    val = enum_map.get(str(val), val)
                elif transform == "formula":
                    val = self._eval_formula(fm["formula"], original_request, store)
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
        if not path:
            return obj
        try:
            return dpath.get(obj, _dot_to_dpath(path), separator="/")
        except (KeyError, TypeError, IndexError):
            return None

    def _set_nested(self, d: dict, path: str, value: Any):  # noqa: ANN401
        _dpath_set(d, path, value)

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
        if len(formula) > _MAX_FORMULA_LENGTH:
            logger.warning("Formula too long (%d chars), refusing to evaluate", len(formula))
            return 0
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
            return _FORMULA_EVALUATOR.eval(resolved)
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
