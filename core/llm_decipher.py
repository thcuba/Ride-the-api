"""
LLM Deciphering Service - Uses configurable LLMs to analyze and decipher
intercepted request/response pairs for protocol understanding and field mapping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import httpx

from core.config import get_config_manager

logger = logging.getLogger(__name__)


@dataclass
class LLMProfile:
    """Configuration for an LLM provider."""
    name: str
    base_url: str
    api_key: str
    model_id: str
    prompt_template: str
    enabled: bool = True
    timeout: int = 30
    max_retries: int = 2


@dataclass
class DecipherResult:
    """Result of LLM deciphering."""
    pair_id: str
    device_id: str
    vendor: str
    intent: str
    fields: dict[str, Any]
    confidence: float
    suggested_dp_codes: dict[str, Any] = field(default_factory=dict)
    protocol_notes: str = ""
    raw_response: str = ""
    success: bool = True
    error: str | None = None
    processing_time_ms: float = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)


class LLMDecipherService:
    """
    Service that uses LLMs to decipher intercepted device communications.
    Supports multiple LLM providers (OpenAI-compatible APIs, local Ollama, etc.)
    """
    
    def __init__(self, config_manager=None):
        self.config_manager = config_manager or get_config_manager()
        self._config = None
        self._profiles: dict[str, LLMProfile] = {}
        self._default_profile = "default"
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, DecipherResult] = {}  # Simple in-memory cache
        self._cache_ttl = 3600  # 1 hour
        
        self._load_config()
    
    def _load_config(self):
        """Load LLM deciphering configuration."""
        config = self.config_manager.config
        self._config = getattr(config, 'llm_decipher', None)
        
        if not self._config:
            logger.warning("No LLM decipher config found, using defaults")
            return
        
        self._default_profile = getattr(self._config, 'default_profile', 'default')
        
        # Load profiles
        profiles_config = getattr(self._config, 'profiles', {})
        for name, profile_config in profiles_config.items():
            profile = LLMProfile(
                name=name,
                        base_url=getattr(profile_config, 'base_url', 'https://api.openai.com/v1'),
                        api_key=self._resolve_api_key(getattr(profile_config, 'api_key', '')),
                        model_id=getattr(profile_config, 'model_id', 'gpt-4o-mini'),
                        prompt_template=getattr(profile_config, 'prompt_template', ''),
                        enabled=getattr(profile_config, 'enabled', True),
                        timeout=getattr(profile_config, 'timeout', 30),
                        max_retries=getattr(profile_config, 'max_retries', 2),
            )
            self._profiles[name] = profile
        
        # Register for config changes
        self.config_manager.register_callback(self._on_config_change)
        
        logger.info(f"Loaded {len(self._profiles)} LLM profiles: {list(self._profiles.keys())}")
    
    def _resolve_api_key(self, api_key: str) -> str:
        """Resolve API key from environment variable if it starts with ${}."""
        if api_key.startswith('${') and api_key.endswith('}'):
            env_var = api_key[2:-1]
            return os.environ.get(env_var, '')
        return api_key
    
    def _on_config_change(self, new_config):
        """Reload config on change."""
        logger.info("LLM decipher config changed, reloading")
        self._load_config()
    
    def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client
    
    def get_profile(self, name: str | None = None) -> LLMProfile | None:
        """Get LLM profile by name, or default."""
        profile_name = name or self._default_profile
        return self._profiles.get(profile_name)
    
    def list_profiles(self) -> list[str]:
        """List available profile names."""
        return [name for name, p in self._profiles.items() if p.enabled]
    
    async def decipher_with_params(
        self,
            context: dict,
            base_url: str | None = None,
            model_id: str | None = None,
            profile_name: str | None = None,
        ) -> dict:
            """Decipher using on-the-fly overridden parameters. Used by pipeline."""
            profile = self.get_profile(profile_name)
            if not profile:
                return {"success": False, "error": f"Profile not found: {profile_name}"}

            # Override base_url/model_id if provided
            effective = profile
            if base_url or model_id:
                effective = LLMProfile(
                    name=profile.name,
                    base_url=base_url or profile.base_url,
                    api_key=profile.api_key,
                    model_id=model_id or profile.model_id,
                    prompt_template=profile.prompt_template,
                    enabled=profile.enabled,
                    timeout=profile.timeout,
                    max_retries=profile.max_retries,
                )

            pairs_text = json.dumps(context.get("pairs", []), indent=2, default=str)
            prompt = effective.prompt_template
            replacements = {
                "{vendor}": context.get("vendor", "unknown"),
                "{device_type}": context.get("device_type", "unknown"),
                "{pairs}": pairs_text,
                "{device_id}": context.get("device_id", "unknown"),
            }
            for placeholder, value in replacements.items():
                prompt = prompt.replace(placeholder, value)

            try:
                result = await self.call_llm(effective, prompt)
                if result["success"]:
                    content = result["content"]
                    if "```json" in content:
                        content = content.split("```json")[1].split("```")[0]
                    elif "```" in content:
                        content = content.split("```")[1].split("```")[0]
                    return {"success": True, "analysis": json.loads(content)}
                return {"success": False, "error": result.get("error")}
            except Exception as e:
                return {"success": False, "error": str(e)}

    def _build_prompt(
            self,
            profile: LLMProfile,
            pair: RequestResponsePair,
            db_schema: str,
            recent_patterns: list[dict],
        ) -> str:
        """Build the prompt for the LLM."""
        # Format request/response pairs
        req = pair.request
        resp = pair.response

        pairs_text = f"""
Request:
  Method: {req.method}
  Path: {req.path}
  Headers: {json.dumps(req.headers, indent=2)}
  Query Params: {json.dumps(req.query_params, indent=2)}
  Body: {json.dumps(req.body, indent=2) if req.body else 'empty'}

Response:
  Status: {resp.status_code}
  Headers: {json.dumps(resp.headers, indent=2)}
  Body: {json.dumps(resp.body, indent=2) if resp.body else 'empty'}
  Latency: {resp.latency_ms:.1f}ms
"""

        # Use safe replacement to avoid issues with literal braces in JSON content
        prompt = profile.prompt_template
        replacements = {
            "{vendor}": pair.vendor,
            "{device_type}": pair.request.metadata.get("device_type", "unknown"),
            "{db_schema}": db_schema,
            "{recent_patterns}": json.dumps(recent_patterns, indent=2),
            "{pairs}": pairs_text,
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt

    async def decipher_pair(
        self,
        pair: RequestResponsePair,
        profile_name: str | None = None,
    ) -> DecipherResult:
        """
        Send a request/response pair to LLM for deciphering.
        Returns structured analysis of the protocol.
        """
        start_time = time.time()
        
        profile = self.get_profile(profile_name)
        if not profile:
            return DecipherResult(
                pair_id=pair.pair_id,
                device_id=pair.device_id,
                vendor=pair.vendor,
                intent="unknown",
                fields={},
                confidence=0.0,
                success=False,
                error=f"Profile not found: {profile_name or self._default_profile}",
                processing_time_ms=(time.time() - start_time) * 1000,
            )
        
        # Check cache
        cache_key = f"{pair.pair_id}:{profile.name}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if (datetime.now(timezone.utc) - cached.timestamp).total_seconds() < self._cache_ttl:
                logger.debug(f"Cache hit for pair {pair.pair_id}")
                return cached
        
        # Get database schema (simplified)
        db_schema = self._get_db_schema(pair.vendor)
        
        # Get recent patterns for this vendor/device_type
        recent_patterns = self._get_recent_patterns(pair.vendor, pair.request.metadata.get('device_type'))
        
        # Build prompt
        prompt = self._build_prompt(profile, pair, db_schema, recent_patterns)
        
        # Call LLM
        result = await self.call_llm(profile, prompt)
        
        processing_time = (time.time() - start_time) * 1000
        
        if not result['success']:
            return DecipherResult(
                pair_id=pair.pair_id,
                device_id=pair.device_id,
                vendor=pair.vendor,
                intent="unknown",
                fields={},
                confidence=0.0,
                success=False,
                error=result['error'],
                processing_time_ms=processing_time,
                raw_response=result.get('raw', ''),
            )
        
        # Parse LLM response
        try:
            analysis = json.loads(result['content'])
        except json.JSONDecodeError:
            # Try to extract JSON from markdown
            content = result['content']
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0]
            elif '```' in content:
                content = content.split('```')[1].split('```')[0]
            try:
                analysis = json.loads(content)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse LLM response: {e}")
                return DecipherResult(
                    pair_id=pair.pair_id,
                    device_id=pair.device_id,
                    vendor=pair.vendor,
                    intent="unknown",
                    fields={},
                    confidence=0.0,
                    success=False,
                    error=f"Failed to parse LLM response: {e}",
                    processing_time_ms=processing_time,
                    raw_response=result['content'],
                )
        
        # Create result
        decipher_result = DecipherResult(
            pair_id=pair.pair_id,
            device_id=pair.device_id,
            vendor=pair.vendor,
            intent=analysis.get('intent', 'unknown'),
            fields=analysis.get('fields', {}),
            confidence=float(analysis.get('confidence', 0.0)),
            suggested_dp_codes=analysis.get('suggested_dp_codes', {}),
            protocol_notes=analysis.get('protocol_notes', ''),
            raw_response=result['content'],
            success=True,
            processing_time_ms=processing_time,
        )
        
        # Cache result
        self._cache[cache_key] = decipher_result
        
        logger.info(f"Deciphered pair {pair.pair_id}: intent={decipher_result.intent}, confidence={decipher_result.confidence:.2f}")
        return decipher_result
    
    async def call_llm(self, profile: LLMProfile, prompt: str) -> dict:
        """Call the LLM API."""
        client = self._get_client()
        
        headers = {
            "Authorization": f"Bearer {profile.api_key}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "model": profile.model_id,
            "messages": [
                {"role": "system", "content": "You are a protocol analysis expert. Analyze IoT device communications and output structured JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
            "response_format": {"type": "json_object"},
        }
        
        for attempt in range(profile.max_retries + 1):
            try:
                response = await client.post(
                    f"{profile.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=profile.timeout,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    content = data['choices'][0]['message']['content']
                    return {'success': True, 'content': content}
                else:
                    error = f"LLM API error: {response.status_code} - {response.text}"
                    logger.warning(f"LLM call failed (attempt {attempt+1}/{profile.max_retries+1}): {error}")
                    
            except httpx.TimeoutException:
                error = f"LLM request timeout after {profile.timeout}s"
                logger.warning(f"LLM timeout (attempt {attempt+1}/{profile.max_retries+1})")
            except Exception as e:
                error = f"LLM call failed: {e}"
                logger.error(f"LLM call error (attempt {attempt+1}/{profile.max_retries+1}): {e}")
            
            if attempt < profile.max_retries:
                await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff
        
        return {'success': False, 'error': error, 'raw': ''}
    
    def _get_db_schema(self, vendor: str) -> str:
        """Get database schema for vendor (simplified)."""
        # In production, this would query the actual schema
        return f"""
Vendor: {vendor}
Tables:
- devices (device_id, name, type, capabilities, config)
- readings (device_id, timestamp, temp_target, temp_actual, humidity, power_watts, mode, fan_speed)
- commands (device_id, timestamp, command_type, params, success, response)
- models (vendor, device_type, model_path, accuracy, version)
- policies (vendor, device_type, rules_json)
- intercepted_requests (device_id, protocol, method, path, body, parsed_intent, parsed_params)
"""
    
    def _get_recent_patterns(self, vendor: str, device_type: str | None) -> list[dict]:
            """Get recent successful deciphering patterns for context."""
            return []
            # TODO: Load from device database patterns table
            # patterns = await db.get_recent_patterns(vendor, limit=10)
    
    async def decipher_batch(
        self,
        pairs: list[RequestResponsePair],
        profile_name: str | None = None,
    ) -> list[DecipherResult]:
        """Decipher multiple pairs in parallel."""
        tasks = [self.decipher_pair(pair, profile_name) for pair in pairs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        deciphered = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Batch decipher error for pair {pairs[i].pair_id}: {result}")
                deciphered.append(DecipherResult(
                    pair_id=pairs[i].pair_id,
                    device_id=pairs[i].device_id,
                    vendor=pairs[i].vendor,
                    intent="error",
                    fields={},
                    confidence=0.0,
                    success=False,
                    error=str(result),
                ))
            else:
                deciphered.append(result)
        
        return deciphered
    
    async def close(self):
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# Global instance
_llm_decipher: LLMDecipherService | None = None


def get_llm_decipher() -> LLMDecipherService:
    """Get or create global LLM decipher service instance."""
    global _llm_decipher
    if _llm_decipher is None:
        _llm_decipher = LLMDecipherService()
    return _llm_decipher

