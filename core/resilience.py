"""
Resilience Module — ensures brand-cloud independence.
Verifies that devices can function without vendor cloud APIs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from core.database import DatabaseManager, DeviceRegistry

logger = logging.getLogger(__name__)

# Auto-switch thresholds
AUTO_SWITCH_MATCH_RATE = 99.0    # Switch to production at 99% match rate
ROLLBACK_MATCH_RATE = 90.0       # Roll back to learning below 90% match rate
MIN_PATTERNS_FOR_SWITCH = 10     # Minimum patterns before considering switch
MIN_TOTAL_REQUESTS = 50          # Minimum total requests for reliable stats
CHECK_INTERVAL_SECONDS = 60      # How often to check auto-switch conditions


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
        total_requests = stats.total_requests if stats else 0
        has_patterns = len(patterns_list) > 0
        has_templates = len(templates_list) > 0
        has_high_confidence = any(t.confidence >= 0.85 for t in templates_list)
        has_excellent_match_rate = match_rate >= AUTO_SWITCH_MATCH_RATE
        is_learning = device.mode == "learning"
        is_production = device.mode == "production"

        # Production mode with excellent match rate = independent
        if is_production and has_patterns and has_templates and has_high_confidence:
            return {
                "independent": True,
                "patterns_learned": len(patterns_list),
                "templates_created": len(templates_list),
                "match_rate": match_rate,
                "total_requests": total_requests,
                "mode": "production",
                "auto_switch_enabled": device.auto_switch_enabled,
                "reason": "Device is in production mode with learned patterns",
            }

        # Learning mode with enough data to switch (99% threshold)
        is_switch_ready = (
            has_patterns
            and has_excellent_match_rate
            and has_high_confidence
            and total_requests >= MIN_TOTAL_REQUESTS
            and len(patterns_list) >= MIN_PATTERNS_FOR_SWITCH
        )
        if is_switch_ready:
            return {
                "independent": True,
                "patterns_learned": len(patterns_list),
                "templates_created": len(templates_list),
                "match_rate": match_rate,
                "total_requests": total_requests,
                "mode": device.mode,
                "auto_switch_enabled": device.auto_switch_enabled,
                "reason": "Device has sufficient patterns to operate independently",
                "suggested_action": "Switch to production mode",
            }

        # Still learning or production with degrading match rate
        return {
            "independent": False,
            "patterns_learned": len(patterns_list),
            "templates_created": len(templates_list),
            "match_rate": match_rate,
            "total_requests": total_requests,
            "mode": device.mode,
            "auto_switch_enabled": device.auto_switch_enabled,
            "reason": (
                f"Device {device_id} is {'in production but ' if is_production else ''}"
                f"not fully independent yet. "
                f"Current: {len(patterns_list)} patterns, {match_rate}% match rate "
                f"(need {AUTO_SWITCH_MATCH_RATE}% for auto-switch)."
            ),
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
                                          min_patterns: int = MIN_PATTERNS_FOR_SWITCH,
                                          min_match_rate: float = AUTO_SWITCH_MATCH_RATE) -> bool:
        """Automatically switch a device to production mode if conditions are met.

        Only switches if auto_switch_enabled is True for the device
        and match rate meets the threshold (default 99%).
        """
        # Check if auto-switch is enabled for this device
        async with self.db_manager.core_session() as session:
            result = await session.execute(
                select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
            )
            device = result.scalar_one_or_none()
            if not device:
                logger.warning(f"Auto-switch: device {device_id} not found")
                return False
            if not device.auto_switch_enabled:
                logger.info(f"Auto-switch disabled for device {device_id}")
                return False

        status = await self.check_cloud_independence(device_id)
        patterns = status.get("patterns_learned", 0)
        match_rate = status.get("match_rate", 0)
        total_reqs = status.get("total_requests", 0)

        if (patterns >= min_patterns
                and match_rate >= min_match_rate
                and total_reqs >= MIN_TOTAL_REQUESTS):
            await self.db_manager.update_device_mode(device_id, "production")
            logger.info(f"Auto-switched {device_id} to production mode "
                         f"({patterns} patterns, {match_rate}% match rate, "
                         f"{total_reqs} total requests)")
            return True

        logger.info(f"Auto-switch conditions not met for {device_id}: "
                     f"{patterns} patterns, {match_rate}% match rate, "
                     f"{total_reqs} requests (need {min_patterns}+ patterns, "
                     f"{min_match_rate}% match rate, {MIN_TOTAL_REQUESTS}+ requests)")
        return False

    async def should_rollback_to_learning(self, device_id: str,
                                           rollback_threshold: float = ROLLBACK_MATCH_RATE) -> bool:
        """Check if a production device should roll back to learning mode.

        Returns True if match rate has dropped below the rollback threshold (default 90%).
        """
        status = await self.check_cloud_independence(device_id)
        match_rate = status.get("match_rate", 100.0)
        total_reqs = status.get("total_requests", 0)

        if status.get("mode") != "production":
            return False

        if match_rate < rollback_threshold and total_reqs >= MIN_TOTAL_REQUESTS:
            logger.warning(f"Rollback triggered for {device_id}: "
                           f"match rate {match_rate}% below {rollback_threshold}% threshold")
            await self.db_manager.update_device_mode(device_id, "learning")
            return True

        return False

    async def export_device_patterns(self, device_id: str) -> dict:
        """Export learned patterns for backup/sharing."""
        from core.database import FieldMapping, RequestPattern, ResponseTemplate
        async with self.db_manager.device_session(device_id) as session:
            patterns = await session.execute(select(RequestPattern))
            templates = await session.execute(select(ResponseTemplate))
            mappings = await session.execute(select(FieldMapping))

        return {
            "device_id": device_id,
            "exported_at": datetime.now(UTC).isoformat(),
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
        from core.database import FieldMapping, RequestPattern, ResponseTemplate
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


class AutoSwitchScheduler:
    """Background scheduler that periodically checks and performs auto-switch to production.

    Runs every CHECK_INTERVAL_SECONDS and checks:
    - Devices with auto_switch_enabled=True in learning mode → switch to production at 99% match rate
    - Devices in production mode → rollback to learning if match rate drops below 90%
    """

    def __init__(self, db_manager):
        self.db_manager = db_manager
        self.verifier = CloudIndependenceVerifier(db_manager)
        self._task = None
        self._running = False

    async def start(self):
        """Start the background check loop."""
        if self._running:
            logger.warning("AutoSwitchScheduler already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("AutoSwitchScheduler started")

    async def stop(self):
        """Stop the background check loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("AutoSwitchScheduler stopped")

    async def _run_loop(self):
        """Main loop: check all devices periodically."""
        while self._running:
            try:
                await self._check_all_devices()
            except Exception as e:
                logger.error(f"AutoSwitchScheduler error: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def _check_all_devices(self):
        """Check all registered devices for auto-switch conditions."""
        devices = await self.db_manager.list_devices()
        for device in devices:
            device_id = device["device_id"]
            if device.get("mode") == "learning" and device.get("auto_switch_enabled", False):
                switched = await self.verifier.auto_switch_to_production(device_id)
                if switched:
                    logger.info(f"AutoSwitchScheduler: {device_id} switched to production")
            elif device.get("mode") == "production":
                rolled_back = await self.verifier.should_rollback_to_learning(device_id)
                if rolled_back:
                    logger.warning(f"AutoSwitchScheduler: {device_id} rolled back to learning")
