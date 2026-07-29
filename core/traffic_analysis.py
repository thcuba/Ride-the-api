"""
Traffic Analysis Engine - Compares local edge responses vs cloud responses,
tracks device compliance, and provides analytics for dashboard.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class TrafficSource(str, Enum):
    """Source of the request."""
    LOCAL_NETWORK = "local_network"      # Device on same LAN
    INTERNET = "internet"                # Device via internet/remote


class ProcessingMode(str, Enum):
    """How the request was processed."""
    LOCAL_EDGE = "local_edge"            # Handled by edge AI
    CLOUD_PASSTHROUGH = "cloud_passthrough"  # Forwarded to vendor cloud
    LOCAL_PASSTHROUGH = "local_passthrough"  # Local device, passthrough to cloud
    HYBRID = "hybrid"                    # Edge + cloud comparison


class ResponseMatchType(str, Enum):
    """Type of response match."""
    IDENTICAL = "identical"              # Byte-for-byte identical
    SEMANTIC_EQUIVALENT = "semantic_equivalent"  # Same meaning, different format
    PARTIAL_MATCH = "partial_match"      # Some fields match
    NO_MATCH = "no_match"                # Completely different
    ERROR = "error"                      # One or both errored


@dataclass
class RequestContext:
    """Context for a single request."""
    request_id: str
    device_id: str
    vendor: str
    device_type: str
    source: TrafficSource
    timestamp: datetime
    protocol: str
    method: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any] | None
    query_params: dict[str, str]


@dataclass
class ResponseRecord:
    """Record of a response from edge or cloud."""
    source: str  # "edge" or "cloud"
    status_code: int
    headers: dict[str, str]
    body: dict[str, Any] | None
    latency_ms: float
    timestamp: datetime
    error: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ComparisonResult:
    """Result of comparing edge vs cloud response."""
    request_id: str
    device_id: str
    vendor: str
    match_type: ResponseMatchType
    similarity_score: float  # 0.0 to 1.0
    edge_response: ResponseRecord
    cloud_response: ResponseRecord | None
    differences: list[dict[str, Any]] = field(default_factory=list)
    processing_mode: ProcessingMode = ProcessingMode.LOCAL_EDGE
    compared_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class DeviceCommandRecord:
    """Record of a command sent to device and its actual response."""
    command_id: str
    device_id: str
    vendor: str
    timestamp: datetime
    command_sent: dict[str, Any]  # What we sent to device
    expected_state: dict[str, Any]  # What state should result
    actual_response: dict[str, Any] | None  # What device actually responded
    actual_state: dict[str, Any] | None  # Actual device state after
    compliance: bool = False
    compliance_score: float = 0.0
    latency_ms: float = 0.0
    error: str | None = None


class ResponseComparator:
    """Compares edge and cloud responses for similarity."""
    
    def __init__(self, vendor_normalizers: dict[str, Callable] | None = None):
        self.vendor_normalizers = vendor_normalizers or {}
        self._default_normalizer = self._default_normalize
    
    def _default_normalize(self, response: dict[str, Any]) -> dict[str, Any]:
        """Default normalization: sort keys, remove timestamps, metadata."""
        if not isinstance(response, dict):
            return {"_raw": str(response)}
        
        normalized = {}
        for k, v in sorted(response.items()):
            # Skip timestamp/metadata fields that vary
            if k.lower() in ("t", "tid", "time", "timestamp", "request_id", "trace_id", "bid"):
                continue
            if isinstance(v, dict):
                normalized[k] = self._default_normalize(v)
            elif isinstance(v, list):
                normalized[k] = [self._default_normalize(item) if isinstance(item, dict) else item for item in v]
            else:
                normalized[k] = v
        return normalized
    
    def normalize(self, vendor: str, response: dict[str, Any]) -> dict[str, Any]:
        """Normalize response for comparison."""
        normalizer = self.vendor_normalizers.get(vendor, self._default_normalizer)
        return normalizer(response)
    
    def compare(self, edge_response: dict[str, Any], cloud_response: dict[str, Any], vendor: str) -> ComparisonResult:
        """Compare two responses and return match details."""
        norm_edge = self.normalize(vendor, edge_response)
        norm_cloud = self.normalize(vendor, cloud_response)
        
        # Generate hashes for quick identical check
        edge_hash = hashlib.sha256(json.dumps(norm_edge, sort_keys=True).encode()).hexdigest()
        cloud_hash = hashlib.sha256(json.dumps(norm_cloud, sort_keys=True).encode()).hexdigest()
        
        if edge_hash == cloud_hash:
            return ComparisonResult(
                request_id="",
                device_id="",
                vendor=vendor,
                match_type=ResponseMatchType.IDENTICAL,
                similarity_score=1.0,
                edge_response=ResponseRecord(
                    source="edge", status_code=200, headers={}, body=edge_response,
                    latency_ms=0, timestamp=datetime.utcnow()
                ),
                cloud_response=ResponseRecord(
                    source="cloud", status_code=200, headers={}, body=cloud_response,
                    latency_ms=0, timestamp=datetime.utcnow()
                ),
            )
        
        # Semantic comparison
        similarity = self._calculate_similarity(norm_edge, norm_cloud)
        differences = self._find_differences(norm_edge, norm_cloud)
        
        if similarity >= 0.95:
            match_type = ResponseMatchType.SEMANTIC_EQUIVALENT
        elif similarity >= 0.5:
            match_type = ResponseMatchType.PARTIAL_MATCH
        else:
            match_type = ResponseMatchType.NO_MATCH
        
        return ComparisonResult(
            request_id="",
            device_id="",
            vendor=vendor,
            match_type=match_type,
            similarity_score=similarity,
            edge_response=ResponseRecord(
                source="edge", status_code=200, headers={}, body=edge_response,
                latency_ms=0, timestamp=datetime.utcnow()
            ),
            cloud_response=ResponseRecord(
                source="cloud", status_code=200, headers={}, body=cloud_response,
                latency_ms=0, timestamp=datetime.utcnow()
            ),
            differences=differences,
        )
    
    def _calculate_similarity(self, a: dict[str, Any], b: dict[str, Any]) -> float:
        """Calculate Jaccard-like similarity between two dicts."""
        keys_a = set(a.keys())
        keys_b = set(b.keys())
        
        if not keys_a and not keys_b:
            return 1.0
        if not keys_a or not keys_b:
            return 0.0
        
        # Key overlap
        common_keys = keys_a & keys_b
        all_keys = keys_a | keys_b
        key_similarity = len(common_keys) / len(all_keys)
        
        # Value similarity for common keys
        value_similarities = []
        for key in common_keys:
            val_a = a[key]
            val_b = b[key]
            if val_a == val_b:
                value_similarities.append(1.0)
            elif isinstance(val_a, (int, float)) and isinstance(val_b, (int, float)):
                # Numeric similarity
                max_val = max(abs(val_a), abs(val_b), 1)
                diff = abs(val_a - val_b) / max_val
                value_similarities.append(max(0.0, 1.0 - diff))
            elif isinstance(val_a, dict) and isinstance(val_b, dict):
                value_similarities.append(self._calculate_similarity(val_a, val_b))
            elif isinstance(val_a, list) and isinstance(val_b, list):
                # List similarity (simplified)
                value_similarities.append(1.0 if val_a == val_b else 0.5)
            else:
                value_similarities.append(0.0)
        
        value_similarity = sum(value_similarities) / len(value_similarities) if value_similarities else 0.0
        
        return (key_similarity + value_similarity) / 2
    
    def _find_differences(self, a: dict[str, Any], b: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
        """Find specific differences between two dicts."""
        diffs = []
        all_keys = set(a.keys()) | set(b.keys())
        
        for key in all_keys:
            path = f"{prefix}.{key}" if prefix else key
            val_a = a.get(key)
            val_b = b.get(key)
            
            if key not in a:
                diffs.append({"path": path, "type": "missing_in_edge", "edge": None, "cloud": val_b})
            elif key not in b:
                diffs.append({"path": path, "type": "missing_in_cloud", "edge": val_a, "cloud": None})
            elif val_a != val_b:
                if isinstance(val_a, dict) and isinstance(val_b, dict):
                    diffs.extend(self._find_differences(val_a, val_b, path))
                else:
                    diffs.append({"path": path, "type": "value_mismatch", "edge": val_a, "cloud": val_b})
        
        return diffs


class TrafficAnalyzer:
    """Main traffic analysis engine."""
    
    def __init__(
        self,
        comparator: ResponseComparator | None = None,
        db_manager: Any = None,  # DatabaseManager from core
    ):
        self.comparator = comparator or ResponseComparator()
        self.db_manager = db_manager
        
        # In-memory buffers for real-time analytics
        self._comparison_buffer: list[ComparisonResult] = []
        self._command_buffer: list[DeviceCommandRecord] = []
        self._stats: dict[str, Any] = defaultdict(lambda: defaultdict(int))
        
        # Configuration
        self.buffer_size = 1000
        self.flush_interval = 60  # seconds
        self._flush_task: asyncio.Task | None = None
    
    async def start(self):
        """Start background flush task."""
        self._flush_task = asyncio.create_task(self._periodic_flush())
    
    async def stop(self):
        """Stop background tasks and flush buffers."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush()
    
    async def analyze_request(
        self,
        context: RequestContext,
        edge_response: ResponseRecord,
        cloud_response: ResponseRecord | None = None,
    ) -> ComparisonResult:
        """Analyze a request: compare edge vs cloud response."""
        
        # Determine processing mode
        if context.source == TrafficSource.LOCAL_NETWORK:
            if cloud_response:
                mode = ProcessingMode.HYBRID
            else:
                mode = ProcessingMode.LOCAL_EDGE
        else:
            if cloud_response:
                mode = ProcessingMode.CLOUD_PASSTHROUGH
            else:
                mode = ProcessingMode.LOCAL_EDGE
        
        # Compare responses if both available
        if cloud_response:
            comparison = self.comparator.compare(
                edge_response.body or {},
                cloud_response.body or {},
                context.vendor
            )
        else:
            comparison = ComparisonResult(
                request_id=context.request_id,
                device_id=context.device_id,
                vendor=context.vendor,
                match_type=ResponseMatchType.ERROR,
                similarity_score=0.0,
                edge_response=edge_response,
                cloud_response=None,
            )
        
        comparison.request_id = context.request_id
        comparison.device_id = context.device_id
        comparison.processing_mode = mode
        
        # Buffer for analytics
        self._comparison_buffer.append(comparison)
        self._update_stats(comparison)
        
        # Persist to database
        if self.db_manager:
            await self._persist_comparison(context, comparison)
        
        # Flush if buffer full
        if len(self._comparison_buffer) >= self.buffer_size:
            await self.flush()
        
        return comparison
    
    async def track_device_command(
        self,
        device_id: str,
        vendor: str,
        command_sent: dict[str, Any],
        expected_state: dict[str, Any],
        actual_response: dict[str, Any] | None = None,
        actual_state: dict[str, Any] | None = None,
        latency_ms: float = 0.0,
        error: str | None = None,
    ) -> DeviceCommandRecord:
        """Track a command sent to device and its compliance."""
        
        # Calculate compliance
        compliance, score = self._calculate_compliance(expected_state, actual_state or actual_response or {})
        
        record = DeviceCommandRecord(
            command_id=str(uuid4()),
            device_id=device_id,
            vendor=vendor,
            timestamp=datetime.utcnow(),
            command_sent=command_sent,
            expected_state=expected_state,
            actual_response=actual_response,
            actual_state=actual_state,
            compliance=compliance,
            compliance_score=score,
            latency_ms=latency_ms,
            error=error,
        )
        
        self._command_buffer.append(record)
        self._update_command_stats(record)
        
        if self.db_manager:
            await self._persist_command_record(record)
        
        if len(self._command_buffer) >= self.buffer_size:
            await self.flush()
        
        return record
    
    def _calculate_compliance(
        self,
        expected: dict[str, Any],
        actual: dict[str, Any],
    ) -> tuple[bool, float]:
        """Calculate if device complied with command."""
        if not expected or not actual:
            return False, 0.0
        
        matches = 0
        total = 0
        
        for key, expected_val in expected.items():
            total += 1
            actual_val = actual.get(key)
            
            if actual_val == expected_val:
                matches += 1
            elif isinstance(expected_val, (int, float)) and isinstance(actual_val, (int, float)):
                # Numeric tolerance
                tolerance = 0.5 if key in ("temp_target", "temp_actual", "temperature") else 0
                if abs(expected_val - actual_val) <= tolerance:
                    matches += 1
        
        score = matches / total if total > 0 else 0.0
        return score >= 0.8, score  # 80% threshold for compliance
    
    def _update_stats(self, comparison: ComparisonResult):
        """Update real-time statistics."""
        key = f"{comparison.vendor}:{comparison.processing_mode.value}"
        self._stats[key]["total"] += 1
        self._stats[key][comparison.match_type.value] += 1
        self._stats[key]["similarity_sum"] += comparison.similarity_score
    
    def _update_command_stats(self, record: DeviceCommandRecord):
        """Update command compliance statistics."""
        key = f"{record.vendor}:commands"
        self._stats[key]["total"] += 1
        if record.compliance:
            self._stats[key]["compliant"] += 1
        self._stats[key]["score_sum"] += record.compliance_score
    
    def get_stats(self, vendor: str | None = None) -> dict[str, Any]:
        """Get current statistics."""
        result = {}
        for key, stats in self._stats.items():
            if vendor and not key.startswith(vendor):
                continue
            
            total = stats.get("total", 0)
            if total == 0:
                continue
            
            result[key] = {
                "total": total,
                "identical_pct": stats.get("identical", 0) / total * 100,
                "semantic_equiv_pct": stats.get("semantic_equivalent", 0) / total * 100,
                "partial_match_pct": stats.get("partial_match", 0) / total * 100,
                "no_match_pct": stats.get("no_match", 0) / total * 100,
                "avg_similarity": stats.get("similarity_sum", 0) / total,
                "compliance_pct": stats.get("compliant", 0) / max(total, 1) * 100,
                "avg_compliance_score": stats.get("score_sum", 0) / max(total, 1),
            }
        return result
    
    async def flush(self):
        """Flush buffers to database."""
        # In production, batch insert to DB
        self._comparison_buffer.clear()
        self._command_buffer.clear()
    
    async def _periodic_flush(self):
        """Periodic flush task."""
        while True:
            await asyncio.sleep(self.flush_interval)
            await self.flush()
    
    async def _persist_comparison(self, context: RequestContext, comparison: ComparisonResult):
        """Persist comparison to vendor database."""
        if not self.db_manager:
            return
        try:
            async with self.db_manager.vendor_session(context.vendor) as session:
                from core.database import VendorInterceptedRequest
                
                log_entry = VendorInterceptedRequest(
                    device_id=context.device_id,
                    protocol=context.protocol,
                    method=context.method,
                    path=context.path,
                    headers=context.headers,
                    body=context.body,
                    query_params=context.query_params,
                    response_status=comparison.edge_response.status_code,
                    response_headers=comparison.edge_response.headers,
                    response_body=comparison.edge_response.body,
                    response_latency_ms=int(comparison.edge_response.latency_ms),
                    processed=True,
                    processed_at=datetime.utcnow(),
                    edge_action=f"{comparison.match_type.value}:{comparison.similarity_score:.2f}",
                    model_id="comparison",
                )
                session.add(log_entry)
                await session.commit()
        except Exception as e:
            # Log but don't fail request
            import logging
            logging.getLogger(__name__).warning(f"Failed to persist comparison: {e}")
    
    async def _persist_command_record(self, record: DeviceCommandRecord):
        """Persist command compliance record."""
        if not self.db_manager:
            return
        try:
            async with self.db_manager.vendor_session(record.vendor) as session:
                from core.database import VendorCommand
                
                cmd = VendorCommand(
                    device_id=record.device_id,
                    command="compliance_check",
                    params=record.command_sent,
                    source="edge_auto",
                    status="acked" if record.compliance else "failed",
                    response=record.actual_response,
                    error=record.error,
                )
                session.add(cmd)
                await session.commit()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to persist command record: {e}")


# Default normalizer (identity)
def default_normalizer(response: dict[str, Any]) -> dict[str, Any]:
    """Default normalizer — returns response as-is."""
    return response

# Registry of vendor normalizers — users add their own
VENDOR_NORMALIZERS: dict[str, callable] = {}