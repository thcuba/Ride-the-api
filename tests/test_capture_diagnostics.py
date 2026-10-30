"""Why a device produces no pairs — and the counters that prove a capture happened.

A healthy proxy can still capture nothing for a device, and every cause is a different
switch: the IP is in per-IP bypass, TLS passthrough is on (configured, or set automatically
because the device refused our certificate), or the device mode is passthrough. Those three
live in different places, and reading the wrong one is exactly how an empty buffer looks
like a broken pipeline — so the API reports the state (`capture`) and the learning path
counts its requests.

Both halves are behavioural: the mapping is a pure function, and the counters are asserted
through the orchestrator, not through the tracker in isolation (calling `record_result`
directly is what the old tests did, which is why nobody noticed production never called it).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from core.capture_state import (
    BYPASSED,
    CAPTURING,
    PASSTHROUGH,
    RELAYED,
    describe_capture,
)
from core.database import DatabaseManager, DeviceRegistry
from core.pipeline import LearningOrchestrator

#: How many refusals each test records, named so the assertions read as facts.
REFUSALS_REPORTED = 3
REFUSALS_RECORDED = 2


@pytest_asyncio.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


async def _set_attributes(
    db_manager, device_id: str, attributes: dict, ips: list[str] | None = None
) -> None:
    """Write ``extra_attributes`` (and optionally the bound IPs) on a device."""
    async with db_manager.core_session() as session:
        result = await session.execute(
            select(DeviceRegistry).where(DeviceRegistry.device_id == device_id)
        )
        device = result.scalar_one()
        extra = dict(device.extra_attributes or {})
        extra.update(attributes)
        device.extra_attributes = extra
        if ips is not None:
            device.ip_addresses = list(ips)
        session.add(device)
        await session.commit()
    db_manager.invalidate_device_cache(device_id)


class TestDescribeCapture:
    def test_nothing_in_the_way_is_capturing(self):
        state = describe_capture(mode="learning")

        assert state["state"] == CAPTURING
        assert state["reason"] == ""
        assert state["hint"]

    def test_bypass_wins_over_the_other_switches(self):
        """Bypass short-circuits before the handshake and before the mode is read."""
        state = describe_capture(
            bypass=True,
            mode="passthrough",
            tls_passthrough=True,
            untrusted_reason="client_refused_certificate",
        )

        assert state["state"] == BYPASSED
        assert state["reason"] == "ip_bypass"

    def test_a_relayed_device_is_relayed_in_learning_mode_too(self):
        """The relay is settled during the handshake, so the mode never gets a say."""
        state = describe_capture(mode="learning", tls_passthrough=True)

        assert state["state"] == RELAYED
        assert state["reason"] == "configured"

    def test_the_relay_reason_names_certificate_pinning(self):
        state = describe_capture(
            mode="learning",
            tls_passthrough=True,
            untrusted_reason="client_refused_certificate",
        )

        assert state["state"] == RELAYED
        assert state["reason"] == "client_refused_certificate"
        assert "does not trust the Ride CA" in state["hint"]

    def test_an_unknown_relay_reason_still_explains_itself(self):
        state = describe_capture(tls_passthrough=True, untrusted_reason="something_new")

        assert state["reason"] == "something_new"
        assert state["hint"]

    def test_passthrough_mode(self):
        assert describe_capture(mode="passthrough")["state"] == PASSTHROUGH

    def test_mode_is_optional_and_case_insensitive(self):
        assert describe_capture(mode=None)["state"] == CAPTURING
        assert describe_capture(mode="Passthrough")["state"] == PASSTHROUGH

    def test_production_is_not_reported_as_blocked(self):
        """Production captures only unmatched requests: it is not a "no pairs" state."""
        assert describe_capture(mode="production")["state"] == CAPTURING

    def test_a_counted_refusal_is_reported_alongside_the_reason(self):
        """How many times, not only why: one refusal and twenty are different situations."""
        described = describe_capture(
            tls_passthrough=True,
            untrusted_reason="client_refused_certificate",
            refusals=3,
            last_refusal_at="2026-10-30T12:44:07+00:00",
        )

        assert described["state"] == RELAYED
        assert described["refusals"] == REFUSALS_REPORTED
        assert described["last_refusal_at"] == "2026-10-30T12:44:07+00:00"

    def test_an_uncounted_device_keeps_the_three_field_payload(self):
        """No count, no extra field: the ordinary payload must keep its shape."""
        described = describe_capture(
            tls_passthrough=True, untrusted_reason="client_refused_certificate"
        )

        assert set(described) == {"state", "reason", "hint"}


class TestLearningCounters:
    @pytest.mark.asyncio
    async def test_a_learning_request_is_counted(self, db_manager):
        """The counters used to stay at zero in the mode that captures pairs."""
        await db_manager.get_or_create_device("dev-learn", "unknown")
        orchestrator = LearningOrchestrator(db_manager)
        orchestrator.initialize(MagicMock())

        result = await orchestrator.handle_request(
            "dev-learn",
            "unknown",
            "https",
            "POST",
            "/v1/status",
            {"host": "ats.iot.example"},
            {"state": "on"},
            {},
        )

        assert result["action"] == "forward"
        assert result["correlation_key"]
        stats = await orchestrator.tracker.get_stats("dev-learn")
        assert stats["total_requests"] == 1
        assert stats["cloud_misses"] == 1
        assert stats["recent_results"][-1]["result"] == "cloud_miss"

    @pytest.mark.asyncio
    async def test_a_forward_without_a_response_is_an_error(self, db_manager):
        await db_manager.get_or_create_device("dev-fail", "unknown")
        orchestrator = LearningOrchestrator(db_manager)
        orchestrator.initialize(MagicMock())
        result = await orchestrator.handle_request(
            "dev-fail", "unknown", "https", "POST", "/v1/status", {}, {}, {}
        )

        assert await orchestrator.discard_request("dev-fail", result["correlation_key"]) is True
        stats = await orchestrator.tracker.get_stats("dev-fail")
        assert stats["errors"] == 1


class TestDeviceStatsExplainThemselves:
    @pytest.mark.asyncio
    async def test_a_relayed_device_reports_the_reason(self, db_manager):
        await db_manager.get_or_create_device("dev-pin", "unknown")
        await _set_attributes(
            db_manager,
            "dev-pin",
            {
                "tls_passthrough": True,
                "tls_untrusted_reason": "client_refused_certificate",
                "seen_hostnames": ["ats.iot.example"],
            },
        )
        orchestrator = LearningOrchestrator(db_manager)
        orchestrator.initialize(MagicMock())

        stats = await orchestrator.get_device_stats("dev-pin")

        assert stats["tls_untrusted_reason"] == "client_refused_certificate"
        assert stats["seen_hostnames"] == ["ats.iot.example"]
        assert stats["capture"]["state"] == RELAYED
        assert stats["capture"]["reason"] == "client_refused_certificate"

    @pytest.mark.asyncio
    async def test_a_bypassed_device_reports_bypass(self, db_manager, monkeypatch):
        await db_manager.get_or_create_device("dev-byp", "unknown")
        # The bypass state is per-IP, so the device needs an address for it to apply.
        await _set_attributes(db_manager, "dev-byp", {}, ips=["192.168.178.99"])

        async def _always_bypassed(_ip: str) -> bool:
            return True

        monkeypatch.setattr(db_manager, "is_ips_bypassed", _always_bypassed)
        orchestrator = LearningOrchestrator(db_manager)
        orchestrator.initialize(MagicMock())

        stats = await orchestrator.get_device_stats("dev-byp")

        assert stats["bypass"] is True
        assert stats["capture"]["state"] == BYPASSED

    @pytest.mark.asyncio
    async def test_a_ready_device_needs_no_explanation(self, db_manager):
        await db_manager.get_or_create_device("dev-ok", "unknown")
        orchestrator = LearningOrchestrator(db_manager)
        orchestrator.initialize(MagicMock())

        stats = await orchestrator.get_device_stats("dev-ok")

        assert stats["capture"]["state"] == CAPTURING
        assert stats["tls_untrusted_reason"] is None
        assert stats["seen_hostnames"] == []

    @pytest.mark.asyncio
    async def test_refusal_count_reaches_the_device_payload(self, db_manager):
        """The count and its timestamps belong to the device view, not only to extra_attributes."""
        await db_manager.get_or_create_device("dev-refused", "unknown")
        await _set_attributes(
            db_manager,
            "dev-refused",
            {
                "tls_passthrough": True,
                "tls_untrusted_reason": "client_refused_certificate",
                "tls_refusals": 2,
                "tls_first_refusal_at": "2026-10-30T12:44:07+00:00",
                "tls_last_refusal_at": "2026-10-30T17:32:30+00:00",
            },
        )
        orchestrator = LearningOrchestrator(db_manager)
        orchestrator.initialize(MagicMock())

        stats = await orchestrator.get_device_stats("dev-refused")

        assert stats["tls_refusals"] == REFUSALS_RECORDED
        assert stats["tls_first_refusal_at"] == "2026-10-30T12:44:07+00:00"
        assert stats["tls_last_refusal_at"] == "2026-10-30T17:32:30+00:00"
        assert stats["capture"]["refusals"] == REFUSALS_RECORDED
        assert stats["capture"]["last_refusal_at"] == "2026-10-30T17:32:30+00:00"

    @pytest.mark.asyncio
    async def test_a_device_that_never_refused_reports_zero(self, db_manager):
        """Absence of refusals is a fact worth reporting, not a missing key."""
        await db_manager.get_or_create_device("dev-never-refused", "unknown")
        orchestrator = LearningOrchestrator(db_manager)
        orchestrator.initialize(MagicMock())

        stats = await orchestrator.get_device_stats("dev-never-refused")

        assert stats["tls_refusals"] == 0
        assert stats["tls_first_refusal_at"] is None
        assert stats["tls_last_refusal_at"] is None
        assert "refusals" not in stats["capture"]

    @pytest.mark.asyncio
    async def test_a_corrupted_count_reads_as_zero(self, db_manager):
        """A device view must not 500 because free-form JSON holds a bad count."""
        await db_manager.get_or_create_device("dev-corrupt", "unknown")
        await _set_attributes(db_manager, "dev-corrupt", {"tls_refusals": "molti"})
        orchestrator = LearningOrchestrator(db_manager)
        orchestrator.initialize(MagicMock())

        stats = await orchestrator.get_device_stats("dev-corrupt")

        assert stats["tls_refusals"] == 0
