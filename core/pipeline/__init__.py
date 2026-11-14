"""Pipeline package: pattern matching engine and multi-device orchestrator."""

from __future__ import annotations

import time
from typing import Any

from core.database import get_db_manager
from core.pipeline.orchestrator import (
    LearningOrchestrator,
    get_orchestrator,
)
from core.pipeline.pipeline import (
    _FLUSH_RETRY_COOLDOWN_S,
    ContextBuffer,
    CorrelatedPair,
    FlushCoordinator,
    LearningPipeline,
    MatchRateTracker,
    MatchResult,
    PipelineMode,
    ScoringInput,
    _as_key_list,
    _confidence,
    _listed_patterns,
    _llm_task_profile,
    _llm_validate_weak_match,
    _older_than,
    _pattern_headers,
    _pattern_query_keys,
    _redact_pairs_for_prompt,
    _status_code,
)


def __getattr__(name: str) -> Any:  # noqa: ANN401
    if name == "_orchestrator":
        from core.pipeline import orchestrator  # noqa: PLC0415

        return orchestrator._orchestrator
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
    "ContextBuffer",
    "CorrelatedPair",
    "FlushCoordinator",
    "LearningOrchestrator",
    "LearningPipeline",
    "MatchRateTracker",
    "MatchResult",
    "PipelineMode",
    "ScoringInput",
    "_FLUSH_RETRY_COOLDOWN_S",
    "_as_key_list",
    "_confidence",
    "_listed_patterns",
    "_llm_task_profile",
    "_llm_validate_weak_match",
    "_older_than",
    "_orchestrator",
    "_pattern_headers",
    "_pattern_query_keys",
    "_redact_pairs_for_prompt",
    "_status_code",
    "get_db_manager",
    "get_orchestrator",
    "time",
]
