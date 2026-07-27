"""
Request/Response Correlation Engine - Matches intercepted requests with their responses.
Supports HTTP (connection/keep-alive, correlation IDs), MQTT (topic/sequence), CoAP (message IDs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from core.config import get_config_manager

logger = logging.getLogger(__name__)


class CorrelationMethod(str, Enum):
    """Method used to correlate requests and responses."""
    CONNECTION = "connection"           # HTTP/1.1 keep-alive connection tracking
    CORRELATION_ID = "correlation_id"   # X-Request-ID header or similar
    SEQUENCE = "sequence"               # Sequence numbers
    TOPIC_SEQUENCE = "topic_sequence"   # MQTT topic + sequence
    MESSAGE_ID = "message_id"           # CoAP message ID
    TOKEN = "token"                     # CoAP token
    UNKNOWN = "unknown"


@dataclass
class PendingRequest:
    """A request waiting for its response."""
    request_id: str
    device_id: str
    vendor: str
    protocol: str
    method: str
    path: str
    headers: dict[str, str]
    body: Any
    query_params: dict[str, str]
    timestamp: float
    correlation_key: str  # Key used for matching
    correlation_method: CorrelationMethod
    metadata: dict = field(default_factory=dict)
    
    # For tracking
    matched: bool = False
    response: Optional[CorrelatedResponse] = None


@dataclass
class CorrelatedResponse:
    """A response correlated with a request."""
    request_id: str
    device_id: str
    vendor: str
    protocol: str
    status_code: int
    headers: dict[str, str]
    body: Any
    latency_ms: float
    timestamp: float
    correlation_key: str
    correlation_method: CorrelationMethod


@dataclass
class RequestResponsePair:
    """Complete request/response pair for analysis."""
    pair_id: str
    device_id: str
    vendor: str
    protocol: str
    request: PendingRequest
    response: CorrelatedResponse
    correlation_confidence: float
    latency_ms: float
    timestamp: datetime
    deciphered: bool = False
    llm_analysis: Optional[dict] = None
    modification_applied: bool = False
    modification_rule_id: Optional[str] = None


class CorrelationEngine:
    """
    Correlates requests with responses across multiple protocols.
    Uses different strategies per protocol type.
    """
    
    def __init__(self, config_manager=None):
        self.config_manager = config_manager or get_config_manager()
        self._config = None
        
        # Pending requests waiting for responses (keyed by correlation key)
        self._pending: dict[str, PendingRequest] = {}
        
        # Completed pairs for analysis
        self._pairs: deque[RequestResponsePair] = deque(maxlen=10000)
        
        # Per-device pending requests (for cleanup)
        self._device_pending: dict[str, set[str]] = defaultdict(set)
        
        # Cleanup task
        self._cleanup_task: asyncio.Task | None = None
        self._running = False
        
        self._load_config()
    
    def _load_config(self):
        """Load correlation configuration."""
        config = self.config_manager.config
        self._config = getattr(config, 'correlation', None)
        
        if not self._config:
            logger.warning("No correlation config found, using defaults")
            self._config = type('obj', (object,), {
                'enabled': True,
                'http': type('obj', (object,), {'method': 'connection', 'correlation_header': 'X-Request-ID', 'keep_alive_timeout': 30}),
                'mqtt': type('obj', (object,), {'method': 'topic_sequence', 'qos_tracking': True, 'retain_handling': 'include'}),
                'coap': type('obj', (object,), {'method': 'message_id', 'confirmable_timeout': 5}),
                'store_pairs': True,
                'max_pairs_per_device': 10000,
                'pair_ttl_hours': 168,
            })()
        
        # Register for config changes
        self.config_manager.add_change_callback(self._on_config_change)
    
    def _on_config_change(self, new_config):
        """Reload config on change."""
        self._load_config()
    
    async def start(self):
        """Start the correlation engine."""
        if self._running:
            return
        
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("Correlation engine started")
    
    async def stop(self):
        """Stop the correlation engine."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.info("Correlation engine stopped")
    
    async def _cleanup_loop(self):
        """Periodically clean up expired pending requests."""
        while self._running:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in correlation cleanup: {e}")
    
    async def _cleanup_expired(self):
        """Remove pending requests older than timeout."""
        now = time.time()
        http_timeout = getattr(self._config.http, 'keep_alive_timeout', 30)
        mqtt_timeout = 30  # Default MQTT timeout
        coap_timeout = getattr(self._config.coap, 'confirmable_timeout', 5) * 3
        
        expired_keys = []
        for key, pending in self._pending.items():
            age = now - pending.timestamp
            timeout = http_timeout if pending.protocol == 'http' else (mqtt_timeout if pending.protocol == 'mqtt' else coap_timeout)
            
            if age > timeout:
                expired_keys.append(key)
        
        for key in expired_keys:
            pending = self._pending.pop(key, None)
            if pending:
                self._device_pending[pending.device_id].discard(key)
                logger.debug(f"Expired pending request: {key}")
    
    def _extract_correlation_key(
        self,
        protocol: str,
        headers: dict[str, str],
        path: str,
        body: Any,
        topic: str | None = None,
    ) -> tuple[str, CorrelationMethod]:
        """Extract correlation key from request based on protocol."""
        if protocol == 'http':
            http_config = getattr(self._config, 'http', None)
            method = getattr(http_config, 'method', 'connection') if http_config else 'connection'
            corr_header = getattr(http_config, 'correlation_header', 'X-Request-ID') if http_config else 'X-Request-ID'
            
            if method == 'correlation_id' and corr_header in headers:
                return headers[corr_header], CorrelationMethod.CORRELATION_ID
            
            # Fallback: use connection-based (would need connection tracking in real implementation)
            return f"conn_{path}_{time.time()}", CorrelationMethod.CONNECTION
        
        elif protocol == 'mqtt':
            if topic and body:
                # Use topic + sequence from payload if available
                seq = body.get('sequence', body.get('seq', ''))
                return f"mqtt_{topic}_{seq}", CorrelationMethod.TOPIC_SEQUENCE
            return f"mqtt_{topic}_{time.time()}", CorrelationMethod.TOPIC_SEQUENCE
        
        elif protocol == 'coap':
            # CoAP uses message ID or token
            msg_id = headers.get('coap_message_id', headers.get('message_id', ''))
            token = headers.get('coap_token', headers.get('token', ''))
            if token:
                return f"coap_token_{token}", CorrelationMethod.TOKEN
            if msg_id:
                return f"coap_msg_{msg_id}", CorrelationMethod.MESSAGE_ID
            return f"coap_{time.time()}", CorrelationMethod.UNKNOWN
        
        return f"unknown_{time.time()}", CorrelationMethod.UNKNOWN
    
    async def register_request(
        self,
        device_id: str,
        vendor: str,
        protocol: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: Any,
        query_params: dict[str, str],
        topic: str | None = None,
        metadata: dict | None = None,
    ) -> PendingRequest:
        """Register an outgoing request waiting for response."""
        correlation_key, corr_method = self._extract_correlation_key(
            protocol, headers, path, body, topic
        )
        
        # Add device_id to correlation key for uniqueness
        full_key = f"{device_id}:{correlation_key}"
        
        pending = PendingRequest(
            request_id=str(uuid4()),
            device_id=device_id,
            vendor=vendor,
            protocol=protocol,
            method=method,
            path=path,
            headers=headers,
            body=body,
            query_params=query_params,
            timestamp=time.time(),
            correlation_key=full_key,
            correlation_method=corr_method,
            metadata=metadata or {},
        )
        
        self._pending[full_key] = pending
        self._device_pending[device_id].add(full_key)
        
        logger.debug(f"Registered pending request: {full_key} ({corr_method.value})")
        return pending
    
    async def match_response(
        self,
        device_id: str,
        vendor: str,
        protocol: str,
        status_code: int,
        headers: dict[str, str],
        body: Any,
        topic: str | None = None,
        metadata: dict | None = None,
    ) -> Optional[RequestResponsePair]:
        """Try to match an incoming response to a pending request."""
        # Try to find matching pending request
        correlation_key, corr_method = self._extract_correlation_key(
            protocol, headers, '', body, topic
        )
        
        full_key = f"{device_id}:{correlation_key}"
        
        # Try exact match first
        pending = self._pending.pop(full_key, None)
        
        if not pending:
            # Try fuzzy match for same device
            for key in list(self._device_pending.get(device_id, set())):
                if key.startswith(f"{device_id}:"):
                    # Check if protocol and path are compatible
                    p = self._pending.get(key)
                    if p and p.protocol == protocol:
                        pending = self._pending.pop(key)
                        self._device_pending[device_id].discard(key)
                        break
        
        if not pending:
            logger.debug(f"No pending request found for response: {full_key}")
            return None
        
        # Calculate latency
        latency_ms = (time.time() - pending.timestamp) * 1000
        
        response = CorrelatedResponse(
            request_id=pending.request_id,
            device_id=device_id,
            vendor=vendor,
            protocol=protocol,
            status_code=status_code,
            headers=headers,
            body=body,
            latency_ms=latency_ms,
            timestamp=time.time(),
            correlation_key=full_key,
            correlation_method=corr_method,
        )
        
        pending.matched = True
        pending.response = response
        
        # Create pair
        pair = RequestResponsePair(
            pair_id=str(uuid4()),
            device_id=device_id,
            vendor=vendor,
            protocol=protocol,
            request=pending,
            response=response,
            correlation_confidence=1.0 if corr_method != CorrelationMethod.UNKNOWN else 0.5,
            latency_ms=latency_ms,
            timestamp=datetime.utcnow(),
        )
        
        # Store pair if configured
        if getattr(self._config, 'store_pairs', True):
            self._pairs.append(pair)
            # Trim per-device limit
            max_per_device = getattr(self._config, 'max_pairs_per_device', 10000)
            device_pairs = [p for p in self._pairs if p.device_id == device_id]
            while len(device_pairs) > max_per_device:
                # Remove oldest for this device
                for i, p in enumerate(self._pairs):
                    if p.device_id == device_id:
                        del self._pairs[i]
                        break
        
        logger.debug(f"Matched request/response pair: {pair.pair_id} (latency: {latency_ms:.1f}ms)")
        return pair
    
    def get_pairs(
        self,
        device_id: str | None = None,
        vendor: str | None = None,
        limit: int = 100,
    ) -> list[RequestResponsePair]:
        """Get stored request/response pairs."""
        pairs = list(self._pairs)
        
        if device_id:
            pairs = [p for p in pairs if p.device_id == device_id]
        if vendor:
            pairs = [p for p in pairs if p.vendor == vendor]
        
        # Sort by timestamp descending
        pairs.sort(key=lambda p: p.timestamp, reverse=True)
        
        return pairs[:limit]
    
    def get_pending_count(self, device_id: str | None = None) -> int:
        """Get count of pending requests."""
        if device_id:
            return len(self._device_pending.get(device_id, set()))
        return len(self._pending)
    
    async def mark_deciphered(self, pair_id: str, llm_analysis: dict):
        """Mark a pair as deciphered with LLM analysis."""
        for pair in self._pairs:
            if pair.pair_id == pair_id:
                pair.deciphered = True
                pair.llm_analysis = llm_analysis
                return True
        return False
    
    async def mark_modified(self, pair_id: str, rule_id: str):
        """Mark a pair as modified by a rule."""
        for pair in self._pairs:
            if pair.pair_id == pair_id:
                pair.modification_applied = True
                pair.modification_rule_id = rule_id
                return True
        return False


# Global instance
_correlation_engine: CorrelationEngine | None = None


def get_correlation_engine() -> CorrelationEngine:
    """Get or create global correlation engine instance."""
    global _correlation_engine
    if _correlation_engine is None:
        _correlation_engine = CorrelationEngine()
    return _correlation_engine