"""
Backwards-compatibility shim for core.llm.
Re-exports LLMDecipherService, models, and helpers from the modular core.llm package.
"""

from __future__ import annotations

from openai import AsyncOpenAI  # Re-exported for test mocks / patching

from core.llm import (
    DecipherResult,
    LLMCallError,
    LLMClientPool,
    LLMDecipherAnalysis,
    LLMDecipherService,
    LLMProfile,
    RequestResponsePair,
    _is_retryable_llm_error,
    _parse_llm_json,
    _raw_secret,
    _strict_true,
    build_decipher_prompt,
    build_params_prompt,
    build_validation_prompt,
    call_single_profile,
    execute_profile_chain,
    get_available_profiles,
    get_default_db_schema,
    get_llm_decipher,
    get_recent_patterns,
    order_profiles,
    validate_task_profiles,
)
from core.llm.service import _llm_decipher
from core.llm_settings import load_llm_settings, save_llm_settings

__all__ = [
    "AsyncOpenAI",
    "DecipherResult",
    "LLMCallError",
    "LLMClientPool",
    "LLMDecipherAnalysis",
    "LLMDecipherService",
    "LLMProfile",
    "RequestResponsePair",
    "_is_retryable_llm_error",
    "_llm_decipher",
    "_parse_llm_json",
    "_raw_secret",
    "_strict_true",
    "build_decipher_prompt",
    "build_params_prompt",
    "build_validation_prompt",
    "call_single_profile",
    "execute_profile_chain",
    "get_available_profiles",
    "get_default_db_schema",
    "get_llm_decipher",
    "get_recent_patterns",
    "load_llm_settings",
    "order_profiles",
    "save_llm_settings",
    "validate_task_profiles",
]
