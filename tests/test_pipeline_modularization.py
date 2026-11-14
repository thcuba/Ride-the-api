"""Unit tests verifying the modularized core.pipeline architecture and backward compatibility."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import core.pipeline as pipeline_pkg
import core.pipeline.orchestrator as orch_mod
import core.pipeline.pipeline as pipe_mod
from core.pipeline import (
    FlushCoordinator,
    LearningOrchestrator,
    LearningPipeline,
    PipelineMode,
    ScoringInput,
    get_orchestrator,
)


def test_pipeline_package_exports() -> None:
    """Verify core.pipeline package exports all expected public and internal symbols."""
    expected_symbols = [
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
        "_pattern_headers",
        "_pattern_query_keys",
        "_redact_pairs_for_prompt",
        "_status_code",
        "get_db_manager",
        "get_orchestrator",
        "time",
    ]
    for symbol in expected_symbols:
        assert hasattr(pipeline_pkg, symbol), f"core.pipeline missing symbol {symbol}"


def test_direct_pipeline_module_import() -> None:
    """Verify direct import from core.pipeline.pipeline."""
    assert pipe_mod.LearningPipeline is LearningPipeline
    assert pipe_mod.FlushCoordinator is FlushCoordinator
    assert pipe_mod.PipelineMode is PipelineMode
    assert pipe_mod.ScoringInput is ScoringInput


def test_direct_orchestrator_module_import() -> None:
    """Verify direct import from core.pipeline.orchestrator."""
    assert orch_mod.LearningOrchestrator is LearningOrchestrator
    assert orch_mod.get_orchestrator is get_orchestrator


def test_no_circular_dependencies() -> None:
    """Verify pipeline module does not import orchestrator module."""
    # LearningPipeline should be importable without orchestrator module
    assert not hasattr(pipe_mod, "LearningOrchestrator")
    assert "core.pipeline.orchestrator" not in pipe_mod.__dict__


def test_orchestrator_singleton_dynamic_db_manager() -> None:
    """Verify get_orchestrator dynamically resolves get_db_manager."""
    mock_db = MagicMock()
    with patch("core.pipeline.get_db_manager", return_value=mock_db):
        # Reset orchestrator singleton for this test
        orig = orch_mod._orchestrator
        try:
            orch_mod._orchestrator = None
            orch = get_orchestrator()
            assert isinstance(orch, LearningOrchestrator)
            assert orch.db_manager is mock_db
        finally:
            orch_mod._orchestrator = orig
