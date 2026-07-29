"""
Resilience Module — ensures brand-cloud independence.
Verifies that devices can function without vendor cloud APIs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from core.database import DatabaseManager, DeviceRegistry, get_db_manager
from core.pipeline import LearningOrchestrator, PatternMatcher
from sqlalchemy import select, func

logger = logging.getLogger(__name__)


class CloudIndependenceVerifier:
    """Verifies and ensures devices can operate without vendor cloud APIs."""

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

    async def check_cloud_independence(self, device_id: str) -> dict:
        """Check if a device can operate independently of its vendor cloud.

        Returns:
            A dict with independence status, including:
            - independent: bool — can the device function without cloud?
            - patterns_learned: int — how many patterns are available
            - match_rate: float — current match rate percentage
            - last_cloud_contact: datetime or None
            - reason: str — explanation
        """
        async with self.db_manager.device_session(device_id) as session:
            from core.database import MatchStats, RequestPattern, ResponseTemplate

            result = await session.execute(
                select(MatchStats).where(MatchStats.device_id == device_id)
            )
            stats = result.scalar_one_or_none()

            patterns = await session.execute(select(RequestPattern))
            patterns_list = patterns.scalars().all()

            templates = await session.execute(
                select(ResponseTemplate).where(ResponseTemplate.confidence >= 0.7)
            )
            templates_list = templates.scalars().all()

        async with self.db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                return {"independent": False, "reason": "device_not_found"}

        match_rate = stats.match_rate_pct if stats else 0.0
        has_patterns = len(patterns_list) > 0
        has_templates = len(templates_list) > 0
        has_high_confidence = any(t.confidence >= 0.85 for t in templates_list)
        has_good_match_rate = match_rate >= 80.0
        is_learning = device.mode == "learning"

        # Production mode with high confidence = independent
        if not is_learning and has_patterns and has_templates and has_high_confidence:
            return {
                "independent": True,
                "patterns_learned": len(patterns_list),
                "templates_created": len(templates_list),
                "match_rate": match_rate,
                "mode": "production",
                "reason": "Device is in production mode with learned patterns",
            }

        # Learning mode with enough data to switch
        if has_patterns and has_good_match_rate and has_high_confidence:
            return {
                "independent": True,
                "patterns_learned": len(patterns_list),
                "templates_created": len(templates_list),
                "match_rate": match_rate,
                "mode": device.mode,
                "reason": "Device has sufficient patterns to operate independently",
                "suggested_action": "Switch to production mode",
            }

        # Still learning
        return {
            "independent": False,
            "patterns_learned": len(patterns_list),
            "templates_created": len(templates_list),
            "match_rate": match_rate,
            "mode": device.mode,
            "reason": f"Device {device_id} is still learning. "
                      f"Needs more patterns or higher match rate. "
                      f"Current: {len(patterns_list)} patterns, {match_rate}% match rate.",
        }

    async def verify_all_devices(self) -> list[dict]:
        """Check independence status for all devices."""
        devices = await self.db_manager.list_devices()
        results = []
        for device in devices:
            status = await self.check_cloud_independence(device["device_id"])
            results.append({
                "device_id": device["device_id"],
                "name": device.get("name", "unknown"),
                **status,
            })
        return results

    async def auto_switch_to_production(self, device_id: str,
                                          min_patterns: int = 10,
                                          min_match_rate: float = 80.0) -> bool:
        """Automatically switch a device to production mode if conditions are met."""
        status = await self.check_cloud_independence(device_id)
        if (status.get("patterns_learned", 0) >= min_patterns
                and status.get("match_rate", 0) >= min_match_rate):
            await self.db_manager.update_device_mode(device_id, "production")
            logger.info(f"Auto-switched {device_id} to production mode "
                         f"({status['patterns_learned']} patterns, {status['match_rate']}% match rate)")
            return True
        return False

    async def export_device_patterns(self, device_id: str) -> dict:
        """Export learned patterns for backup/sharing."""
        from core.database import RequestPattern, ResponseTemplate, FieldMapping
        async with self.db_manager.device_session(device_id) as session:
            patterns = await session.execute(select(RequestPattern))
            templates = await session.execute(select(ResponseTemplate))
            mappings = await session.execute(select(FieldMapping))

        return {
            "device_id": device_id,
            "exported_at": datetime.utcnow().isoformat(),
            "format_version": "1.0",
            "patterns": [
                {
                    "pattern_id": p.pattern_id,
                    "method": p.method,
                    "path_pattern": p.path_pattern,
                    "protocol": p.protocol,
                    "required_headers": p.required_headers,
                    "body_schema": p.body_schema,
                    "intent": p.intent,
                    "confidence": p.confidence,
                }
                for p in patterns.scalars().all()
            ],
            "templates": [
                {
                    "template_id": t.template_id,
                    "pattern_id": t.pattern_id,
                    "status_code": t.status_code,
                    "headers_template": t.headers_template,
                    "body_template": t.body_template,
                    "field_mappings": t.field_mappings,
                    "expected_variables": t.expected_variables,
                    "confidence": t.confidence,
                }
                for t in templates.scalars().all()
            ],
            "field_mappings": [
                {
                    "mapping_id": m.mapping_id,
                    "request_field": m.request_field,
                    "response_field": m.response_field,
                    "transform": m.transform,
                    "intent": m.intent,
                    "confidence": m.confidence,
                }
                for m in mappings.scalars().all()
            ],
        }

    async def import_device_patterns(self, device_id: str, data: dict) -> int:
        """Import previously exported patterns (for sharing between devices)."""
        from core.database import RequestPattern, ResponseTemplate, FieldMapping
        count = 0
        async with self.db_manager.device_session(device_id) as session:
            for p_data in data.get("patterns", []):
                existing = await session.execute(
                    select(RequestPattern).where(
                        RequestPattern.pattern_id == p_data["pattern_id"]
                    )
                )
                if not existing.scalar_one_or_none():
                    pattern = RequestPattern(**p_data)
                    session.add(pattern)
                    count += 1

            for t_data in data.get("templates", []):
                existing = await session.execute(
                    select(ResponseTemplate).where(
                        ResponseTemplate.template_id == t_data["template_id"]
                    )
                )
                if not existing.scalar_one_or_none():
                    template = ResponseTemplate(**t_data)
                    session.add(template)

            for m_data in data.get("field_mappings", []):
                existing = await session.execute(
                    select(FieldMapping).where(
                        FieldMapping.mapping_id == m_data["mapping_id"]
                    )
                )
                if not existing.scalar_one_or_none():
                    mapping = FieldMapping(**m_data)
                    session.add(mapping)

        logger.info(f"Imported {count} patterns for device {device_id}")
        return count


# API endpoint handlers for resilience endpoints

def register_resilience_routes(app, get_db, get_orch):
    """Register resilience-related API routes."""

    @app.get("/api/independence/{device_id}")
    async def check_device_independence(device_id: str):
        """Check if a device can operate without vendor cloud."""
        db = get_db()
        if not db:
            return {"error": "Service not ready"}, 503
        verifier = CloudIndependenceVerifier(db)
        status = await verifier.check_cloud_independence(device_id)
        return {"device_id": device_id, "independence": status}

    @app.get("/api/independence")
    async def check_all_independence():
        """Check independence for all devices."""
        db = get_db()
        if not db:
            return {"error": "Service not ready"}, 503
        verifier = CloudIndependenceVerifier(db)
        results = await verifier.verify_all_devices()
        return {"devices": results}

    @app.post("/api/independence/{device_id}/auto-switch")
    async def auto_switch_device(device_id: str):
        """Auto-switch device to production if conditions are met."""
        db = get_db()
        if not db:
            return {"error": "Service not ready"}, 503
        verifier = CloudIndependenceVerifier(db)
        switched = await verifier.auto_switch_to_production(device_id)
        return {"device_id": device_id, "switched": switched}

    @app.get("/api/independence/{device_id}/export")
    async def export_device_patterns(device_id: str):
        """Export learned patterns for backup/sharing."""
        db = get_db()
        if not db:
            return {"error": "Service not ready"}, 503
        verifier = CloudIndependenceVerifier(db)
        data = await verifier.export_device_patterns(device_id)
        return data

    @app.post("/api/independence/{device_id}/import")
    async def import_device_patterns(device_id: str, request):
        """Import previously exported patterns."""
        db = get_db()
        if not db:
            return {"error": "Service not ready"}, 503
        body = await request.json()
        verifier = CloudIndependenceVerifier(db)
        count = await verifier.import_device_patterns(device_id, body)
        return {"device_id": device_id, "patterns_imported": count}