"""DNS discovery: wide interception with exclusions, and the query log.

Two rules this file protects:

1. ``exclude`` wins over everything but an explicit adapter claim. A name there is
   always forwarded, even when ``intercept`` lists it too or ``intercept_all`` is
   on — hijacking NTP, OCSP or local names only breaks devices without teaching
   the proxy anything. The one exception is a name a registered adapter claims
   (``shelly-*.local`` sits inside the ``*.local`` default exclusion): shipping an
   adapter must be enough to make its service interceptable.
2. Recording what devices ask for must never slow down or break a DNS answer:
   the query path only counts in memory, and persistence happens in batches.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import dns.message
import dns.rdatatype
import httpx
import pytest
from sqlalchemy import select

import core.server as server_mod
from adapters import register_all_adapters
from core.config import Config, ConfigManager
from core.database import DatabaseManager, DNSQueryLog
from core.dns_discovery import (
    DNSDiscoveryRecorder,
    set_dns_recorder,
)
from core.dns_server import DNSInterceptor, set_dns_interceptor

PROXY_IP = "192.168.1.200"
CLIENT_IP = "192.168.1.77"
DEVICE_HOST = "ats.iot.vendor-cloud.example"
PASSWORD = "testpass"

CONFIG_YAML = f"""
security:
  auth_enabled: true
  password: "{PASSWORD}"
dns:
  enabled: true
  listen_host: 127.0.0.1
  answer_ip: {PROXY_IP}
  intercept:
    - {DEVICE_HOST}
"""


def _config(**overrides) -> Config:
    config = Config()
    dns = config.dns
    dns.enabled = True
    dns.answer_ip = PROXY_IP
    dns.intercept = list(overrides.get("intercept", [DEVICE_HOST]))
    dns.exclude = list(overrides.get("exclude", []))
    dns.intercept_all = bool(overrides.get("intercept_all", False))
    return config


class _FakeSink:
    """Stands in for ``DatabaseManager``: records what the recorder hands over."""

    def __init__(self, *, fail: bool = False, fail_prune: bool = False) -> None:
        self.observations: list[dict] = []
        self.pruned: list[int | None] = []
        self.clear_calls = 0
        self.fail = fail
        self.fail_prune = fail_prune

    async def record_dns_queries(self, observations: list[dict]) -> int:
        if self.fail:
            raise RuntimeError("sink unavailable")
        self.observations.extend(observations)
        return len(observations)

    async def prune_dns_queries(self, max_rows: int | None = None) -> int:
        self.pruned.append(max_rows)
        if self.fail_prune:
            raise RuntimeError("prune unavailable")
        return 0

    async def clear_dns_queries(self) -> int:
        self.clear_calls += 1
        return 3


# ── interception policy: exclude / intercept / intercept_all ─────────────────


def test_exclude_beats_the_intercept_list():
    interceptor = DNSInterceptor(_config(intercept=["*.vendor-cloud.example"], exclude=["ntp.*"]))

    assert interceptor.classify("ntp.vendor-cloud.example") == "excluded"
    assert interceptor.matches("ntp.vendor-cloud.example") is False
    assert interceptor.classify("ats.vendor-cloud.example") == "intercepted"
    assert interceptor.classify("cloud.other.example") == "forwarded"


def test_intercept_all_takes_everything_but_the_exclusions():
    interceptor = DNSInterceptor(
        _config(intercept=[], exclude=["ntp.*", "ocsp.*"], intercept_all=True)
    )

    assert interceptor.classify("anything.at.all.example") == "intercepted"
    assert interceptor.classify("random.device.example") == "intercepted"
    # The exclusions survive the wide-open mode, which is the whole point of
    # having them.
    assert interceptor.classify("ntp.pool.example") == "excluded"
    assert interceptor.classify("ocsp.digicert.example") == "excluded"


def test_default_exclusions_never_hijack_the_clock_or_local_names():
    """The shipped defaults are what make ``intercept_all`` safe to switch on."""
    interceptor = DNSInterceptor(Config())  # untouched defaults, intercept_all on
    interceptor.intercept_all = True

    for name in (
        "ntp.iot.versuni.com",
        "time.apple.com",
        "ocsp.digicert.com",
        "crl.example.com",
        "homeassistant.local",
        "printer.lan",
    ):
        assert interceptor.classify(name) == "excluded", name
    assert interceptor.classify("ats.prod.eu-da.iot.versuni.com") == "intercepted"


def test_an_adapter_claim_wins_over_a_default_exclusion():
    """A shipped adapter stays interceptable: ``shelly-*.local`` lives inside ``*.local``.

    The default exclusions must not neutralise an adapter that declares exactly the
    names of the service it implements -- but they keep protecting every other
    local name.
    """
    interceptor = DNSInterceptor(Config())  # untouched defaults

    claimed = [h for h in register_all_adapters().list_hostnames() if "*.local" in h]
    if not claimed:
        pytest.skip("nessun adapter dichiara un nome .local in questa configurazione")
    assert interceptor.classify(claimed[0].replace("*", "abc123")) == "intercepted"
    assert interceptor.classify("printer.lan") == "excluded"
    assert interceptor.classify("homeassistant.local") == "excluded"


def test_apply_settings_reloads_without_restarting():
    interceptor = DNSInterceptor(_config(intercept=[]))
    assert interceptor.classify(DEVICE_HOST) == "forwarded"

    updated = _config(intercept=["*.vendor-cloud.example"], exclude=["ntp.*"], intercept_all=False)
    interceptor.apply_settings(updated)

    assert interceptor.classify(DEVICE_HOST) == "intercepted"
    assert interceptor.classify("ntp.vendor-cloud.example") == "excluded"
    assert interceptor.answer_ip == PROXY_IP


# ── recorder: batched, never on the DNS hot path ─────────────────────────────


@pytest.mark.asyncio
async def test_recorder_aggregates_a_pair_and_flushes_it():
    sink = _FakeSink()
    recorder = DNSDiscoveryRecorder(sink, flush_interval_s=3600)

    recorder.note(CLIENT_IP, DEVICE_HOST.upper() + ".", "a", "intercepted")
    recorder.note(CLIENT_IP, DEVICE_HOST, "A", "intercepted")

    assert recorder.pending_pairs == 1  # same pair, not two
    written = await recorder.flush()

    assert written == 1
    assert recorder.pending_pairs == 0
    observation = sink.observations[0]
    assert observation["hostname"] == DEVICE_HOST  # case and trailing dot normalised
    assert observation["hit_count"] == 2  # noqa: PLR2004 - two notes, one pair
    assert observation["status"] == "intercepted"
    assert observation["client_ip"] == CLIENT_IP
    assert sink.pruned == [recorder.max_rows]


@pytest.mark.asyncio
async def test_recorder_keeps_counting_when_persistence_fails():
    """A broken sink must not lose the observation nor raise into the DNS path."""
    sink = _FakeSink(fail=True)
    recorder = DNSDiscoveryRecorder(sink, flush_interval_s=3600)

    recorder.note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")
    assert await recorder.flush() == 0

    assert recorder.flush_errors == 1
    assert recorder.pending_pairs == 1  # still counted, ready for the next flush


@pytest.mark.asyncio
async def test_recorder_without_a_sink_still_counts():
    recorder = DNSDiscoveryRecorder(sink=None)

    recorder.note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")
    assert await recorder.flush() == 0
    assert recorder.pending_pairs == 1


@pytest.mark.asyncio
async def test_recorder_stop_writes_what_is_pending():
    sink = _FakeSink()
    recorder = DNSDiscoveryRecorder(sink, flush_interval_s=3600)
    await recorder.start()

    recorder.note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")
    await recorder.stop()

    assert sink.observations
    assert recorder.pending_pairs == 0
    assert recorder._task is None  # noqa: SLF001 - the loop must be gone


@pytest.mark.asyncio
async def test_recorder_clear_empties_memory_and_storage():
    sink = _FakeSink()
    recorder = DNSDiscoveryRecorder(sink)

    recorder.note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")
    removed = await recorder.clear()

    assert recorder.pending_pairs == 0
    assert sink.clear_calls == 1
    assert removed == 3  # noqa: PLR2004 - what the fake sink reported


@pytest.mark.asyncio
async def test_recorder_disabled_records_nothing():
    recorder = DNSDiscoveryRecorder(_FakeSink(), enabled=False)

    recorder.note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")
    assert recorder.pending_pairs == 0
    assert await recorder.flush() == 0


# ── the database side of the log ─────────────────────────────────────────────


@pytest.fixture
async def db_manager(tmp_path):
    dm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dm.initialize()
    yield dm
    await dm.close()


def _observation(hostname: str, *, hits: int, seen: datetime, status: str = "forwarded") -> dict:
    return {
        "client_ip": CLIENT_IP,
        "hostname": hostname,
        "qtype": "A",
        "status": status,
        "hit_count": hits,
        "first_seen": seen,
        "last_seen": seen,
    }


@pytest.mark.asyncio
async def test_same_pair_is_upserted_not_duplicated(db_manager):
    now = datetime.now(UTC)
    await db_manager.record_dns_queries([_observation(DEVICE_HOST, hits=1, seen=now)])
    await db_manager.record_dns_queries(
        [_observation(DEVICE_HOST, hits=4, seen=now + timedelta(minutes=1), status="intercepted")]
    )

    rows = await db_manager.list_dns_queries()
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 5  # noqa: PLR2004 - 1 + 4 from the two upserts
    assert rows[0]["status"] == "intercepted"
    assert rows[0]["hostname"] == DEVICE_HOST


@pytest.mark.asyncio
async def test_log_lists_the_newest_pair_first(db_manager):
    now = datetime.now(UTC)
    await db_manager.record_dns_queries(
        [
            _observation("old.example", hits=1, seen=now),
            _observation("new.example", hits=1, seen=now + timedelta(seconds=30)),
        ]
    )

    rows = await db_manager.list_dns_queries()
    assert [row["hostname"] for row in rows] == ["new.example", "old.example"]


@pytest.mark.asyncio
async def test_prune_drops_the_oldest_pairs(db_manager):
    now = datetime.now(UTC)
    await db_manager.record_dns_queries(
        [
            _observation(f"host{index}.example", hits=1, seen=now + timedelta(seconds=index))
            for index in range(5)
        ]
    )

    removed = await db_manager.prune_dns_queries(max_rows=2)

    assert removed == 3  # noqa: PLR2004
    rows = await db_manager.list_dns_queries()
    assert [row["hostname"] for row in rows] == ["host4.example", "host3.example"]


@pytest.mark.asyncio
async def test_prune_is_a_noop_below_the_cap(db_manager):
    await db_manager.record_dns_queries([_observation(DEVICE_HOST, hits=1, seen=datetime.now(UTC))])

    assert await db_manager.prune_dns_queries(max_rows=10) == 0
    assert len(await db_manager.list_dns_queries()) == 1


@pytest.mark.asyncio
async def test_clear_removes_every_row(db_manager):
    await db_manager.record_dns_queries([_observation(DEVICE_HOST, hits=1, seen=datetime.now(UTC))])

    assert await db_manager.clear_dns_queries() == 1
    assert await db_manager.list_dns_queries() == []
    async with db_manager.core_session() as session:
        assert (await session.execute(select(DNSQueryLog))).scalars().all() == []


# ── the API the dashboard drives ─────────────────────────────────────────────


@pytest.fixture
async def api(tmp_path):
    """A running API with a real config file, database, interceptor and recorder."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_YAML, encoding="utf-8")
    manager = ConfigManager(config_path=config_path)
    manager.load()

    dbm = DatabaseManager(
        core_db_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        device_db_dir=tmp_path / "device_dbs",
    )
    await dbm.initialize()

    interceptor = DNSInterceptor(manager.config)
    set_dns_interceptor(interceptor)
    recorder = DNSDiscoveryRecorder(dbm, flush_interval_s=3600)
    set_dns_recorder(recorder)
    interceptor.query_recorder = recorder

    previous_manager = server_mod.config_manager
    previous_db = server_mod.db_manager
    server_mod.config_manager = manager
    server_mod.db_manager = dbm
    transport = httpx.ASGITransport(app=server_mod.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": PASSWORD}
    ) as client:
        yield {
            "client": client,
            "manager": manager,
            "config_path": Path(config_path),
            "interceptor": interceptor,
            "recorder": recorder,
        }

    server_mod.config_manager = previous_manager
    server_mod.db_manager = previous_db
    set_dns_interceptor(None)
    set_dns_recorder(None)
    await dbm.close()


@pytest.mark.asyncio
async def test_discovery_lists_pending_queries_with_their_state(api):
    api["recorder"].note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")
    api["recorder"].note(CLIENT_IP, "new.device.example", "A", "forwarded")

    response = await api["client"].get("/api/dns/discovery")

    assert response.status_code == 200  # noqa: PLR2004
    body = response.json()
    by_host = {entry["hostname"]: entry for entry in body["entries"]}
    assert set(by_host) == {DEVICE_HOST, "new.device.example"}
    # The configured name is reported as already covered, the other one as a
    # candidate -- that difference is what the dashboard's button acts on.
    assert by_host[DEVICE_HOST]["intercepted"] is True
    assert by_host[DEVICE_HOST]["suggested"] is False
    assert by_host["new.device.example"]["suggested"] is True
    assert body["intercept"] == [DEVICE_HOST]
    assert body["pending_pairs"] == 2  # noqa: PLR2004


@pytest.mark.asyncio
async def test_promoting_a_discovered_name_persists_and_applies_it(api):
    response = await api["client"].post(
        "/api/dns/intercept", json={"hostname": "*.iot.dreame.tech"}
    )

    assert response.status_code == 200  # noqa: PLR2004
    assert api["config_path"].read_text(encoding="utf-8").count("iot.dreame.tech") == 1
    assert api["manager"].config.dns.intercept == [DEVICE_HOST, "*.iot.dreame.tech"]
    # Hot reload: no restart, the next query already follows the new rule.
    assert api["interceptor"].classify("eu.iot.dreame.tech") == "intercepted"
    assert api["interceptor"].patterns == [DEVICE_HOST, "*.iot.dreame.tech"]


@pytest.mark.asyncio
async def test_promoting_a_name_twice_does_not_duplicate_it(api):
    await api["client"].post("/api/dns/intercept", json={"hostname": DEVICE_HOST})

    assert api["manager"].config.dns.intercept == [DEVICE_HOST]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "not a name", "http://x/", "192.168.1.77", 42])
async def test_promoting_something_that_is_not_a_name_is_refused(api, bad):
    response = await api["client"].post("/api/dns/intercept", json={"hostname": bad})

    assert response.status_code == 400  # noqa: PLR2004
    assert api["manager"].config.dns.intercept == [DEVICE_HOST]


@pytest.mark.asyncio
async def test_removing_an_intercepted_name_stops_hijacking_it(api):
    response = await api["client"].delete(f"/api/dns/intercept/{DEVICE_HOST}")

    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["removed"] == 1
    assert api["manager"].config.dns.intercept == []
    assert api["interceptor"].classify(DEVICE_HOST) == "forwarded"


@pytest.mark.asyncio
async def test_settings_can_flip_intercept_all_and_the_exclusions(api):
    response = await api["client"].put(
        "/api/dns/settings",
        json={"intercept_all": True, "exclude": ["ntp.*", "*.local"]},
    )

    assert response.status_code == 200  # noqa: PLR2004
    assert response.json() == {
        "intercept": [DEVICE_HOST],
        "intercept_all": True,
        "exclude": ["ntp.*", "*.local"],
    }
    assert api["interceptor"].intercept_all is True
    assert api["interceptor"].classify("unknown.cloud.example") == "intercepted"
    assert api["interceptor"].classify("ntp.cloud.example") == "excluded"
    # The intercept list was not resent, so it must be untouched.
    assert api["manager"].config.dns.intercept == [DEVICE_HOST]


@pytest.mark.asyncio
async def test_settings_reject_a_non_boolean_intercept_all(api):
    response = await api["client"].put("/api/dns/settings", json={"intercept_all": "yes"})

    assert response.status_code == 400  # noqa: PLR2004
    assert api["interceptor"].intercept_all is False


@pytest.mark.asyncio
async def test_clearing_the_log_empties_both_memory_and_database(api):
    api["recorder"].note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")
    await api["recorder"].flush()

    response = await api["client"].delete("/api/dns/discovery")

    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["removed"] == 1
    assert api["recorder"].pending_pairs == 0
    listed = await api["client"].get("/api/dns/discovery")
    assert listed.json()["entries"] == []


@pytest.mark.asyncio
async def test_dns_endpoints_require_the_control_plane_password(api):  # noqa: ARG001 - the fixture wires the server under test
    unauthenticated = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_mod.app), base_url="http://test"
    )
    async with unauthenticated as client:
        response = await client.get("/api/dns/discovery")

    assert response.status_code == 401  # noqa: PLR2004


@pytest.mark.asyncio
async def test_the_service_records_queries_on_the_dns_path(api):
    """End of the chain: a query answered by the interceptor reaches the log."""
    query = dns.message.make_query("brand-new.device.example", dns.rdatatype.A)

    await api["interceptor"].handle_query(query.to_wire(), client=CLIENT_IP)

    assert api["recorder"].pending_pairs == 1
    observation = api["recorder"].snapshot()[0]
    assert observation["hostname"] == "brand-new.device.example"
    assert observation["status"] == "forwarded"
    await asyncio.sleep(0)  # let any fire-and-forget task settle


# ── failure and shutdown paths (what the review of this feature was about) ───


@pytest.mark.asyncio
async def test_a_failed_prune_is_not_taken_for_a_failed_write():
    """The counter must not be flushed twice: that would double every hit."""
    sink = _FakeSink(fail_prune=True)
    recorder = DNSDiscoveryRecorder(sink, flush_interval_s=3600)

    recorder.note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")
    written = await recorder.flush()

    assert written == 1
    assert len(sink.observations) == 1  # the pair did reach the sink
    assert recorder.pending_pairs == 0  # and was not put back in memory
    assert recorder.flush_errors == 1  # the prune failure is visible


@pytest.mark.asyncio
async def test_the_log_is_pruned_at_most_once_per_interval():
    sink = _FakeSink()
    recorder = DNSDiscoveryRecorder(sink, flush_interval_s=3600, prune_interval_s=3600)

    recorder.note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")
    await recorder.flush()
    recorder.note(CLIENT_IP, "other.example", "A", "forwarded")
    await recorder.flush()

    assert sink.pruned == [recorder.max_rows]  # one COUNT for two flushes, not two


@pytest.mark.asyncio
async def test_a_cancelled_flush_keeps_the_counters():
    """Shutdown must not lose observations the sink never got."""
    started = asyncio.Event()

    class _BlockingSink(_FakeSink):
        async def record_dns_queries(self, observations: list[dict]) -> int:  # noqa: ARG002
            started.set()
            await asyncio.sleep(60)
            return 1

    recorder = DNSDiscoveryRecorder(_BlockingSink(), flush_interval_s=3600)
    recorder.note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")

    task = asyncio.create_task(recorder.flush())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert recorder.pending_pairs == 1  # back in memory, not silently dropped


@pytest.mark.asyncio
async def test_a_duplicated_pair_in_one_batch_is_merged_not_rejected(db_manager):
    """SQLite refuses one statement touching a row twice: collapse before sending."""
    now = datetime.now(UTC)
    first = _observation(DEVICE_HOST, hits=2, seen=now)
    second = _observation(DEVICE_HOST, hits=3, seen=now + timedelta(seconds=5))

    assert await db_manager.record_dns_queries([first, second]) == 2  # noqa: PLR2004

    rows = await db_manager.list_dns_queries()
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 5  # noqa: PLR2004
    # SQLite stores the row naive; the API hands it back as explicit UTC.
    assert rows[0]["last_seen"] == second["last_seen"].isoformat()


@pytest.mark.asyncio
async def test_settings_refuse_a_field_that_is_not_a_list(api):
    response = await api["client"].put("/api/dns/settings", json={"intercept": "a.example"})

    assert response.status_code == 400  # noqa: PLR2004
    assert "must be a list" in response.json()["error"]
    assert api["manager"].config.dns.intercept == [DEVICE_HOST]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not a name", "http://x/", 42, ""])
async def test_settings_name_the_value_they_refuse(api, bad):
    """A typo must fail loudly: silently dropping it looks like it was saved."""
    response = await api["client"].put(
        "/api/dns/settings", json={"exclude": ["ntp.*", bad, "*.local"]}
    )

    assert response.status_code == 400  # noqa: PLR2004
    assert "not names" in response.json()["error"]
    # Nothing was written: the list still holds exactly the shipped defaults.
    assert api["manager"].config.dns.exclude == Config().dns.exclude


@pytest.mark.asyncio
async def test_settings_collapse_duplicate_names(api):
    response = await api["client"].put(
        "/api/dns/settings",
        json={"intercept": [DEVICE_HOST, DEVICE_HOST.upper() + ".", "*.x.test"]},
    )

    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["intercept"] == [DEVICE_HOST, "*.x.test"]
    assert api["interceptor"].patterns == [DEVICE_HOST, "*.x.test"]


@pytest.mark.asyncio
async def test_settings_reject_a_non_list_exclude_without_touching_the_rest(api):
    response = await api["client"].put(
        "/api/dns/settings", json={"intercept_all": True, "exclude": "ntp.*"}
    )

    assert response.status_code == 400  # noqa: PLR2004
    # The whole body is refused, so the flip that was valid in it is not applied
    # either: a half-applied policy is worse than none.
    assert api["interceptor"].intercept_all is False
    assert api["manager"].config.dns.intercept_all is False


def test_the_reason_a_name_is_not_interceptable_is_reported():
    """The UI needs the reason, not only the state: promoting an excluded name is a no-op."""
    interceptor = DNSInterceptor(_config(intercept=[], exclude=["ntp.*"]))

    assert interceptor.matching_exclusion("NTP.pool.example.") == "ntp.*"
    assert interceptor.matching_exclusion("cloud.other.example") is None

    claimed = register_all_adapters().list_hostnames()
    if not claimed:
        pytest.skip("nessun adapter registrato in questa configurazione")
    assert interceptor.adapter_claim(claimed[0].replace("*", "abc123")) == claimed[0]
    assert interceptor.adapter_claim("cloud.other.example") is None


@pytest.mark.parametrize("name", ["ntp.pool.example", "cloud.other.example", "time.europa.example"])
def test_classify_and_the_explainers_never_drift_apart(name):
    interceptor = DNSInterceptor(_config(intercept=[], exclude=["ntp.*", "time.*"]))

    excluded_by_a_pattern = interceptor.matching_exclusion(name) is not None

    assert (interceptor.classify(name) == "excluded") == excluded_by_a_pattern


@pytest.mark.asyncio
async def test_discovery_explains_why_a_name_is_not_intercepted(api):
    api["recorder"].note(CLIENT_IP, "ntp.pool.example", "A", "excluded")
    api["recorder"].note(CLIENT_IP, "promote-me.example", "A", "forwarded")

    body = (await api["client"].get("/api/dns/discovery")).json()
    by_host = {entry["hostname"]: entry for entry in body["entries"]}

    assert by_host["ntp.pool.example"]["excluded_by"] == "ntp.*"
    assert by_host["ntp.pool.example"]["suggested"] is False
    assert by_host["promote-me.example"]["excluded_by"] is None
    assert by_host["promote-me.example"]["claimed_by"] is None
    assert by_host["promote-me.example"]["suggested"] is True


@pytest.mark.asyncio
async def test_discovery_names_the_adapter_that_claims_a_name(api):
    claimed = register_all_adapters().list_hostnames()
    if not claimed:
        pytest.skip("nessun adapter registrato in questa configurazione")
    hostname = claimed[0].replace("*", "abc123")

    api["recorder"].note(CLIENT_IP, hostname, "A", "intercepted")
    body = (await api["client"].get("/api/dns/discovery")).json()
    entry = next(e for e in body["entries"] if e["hostname"] == hostname)

    assert entry["claimed_by"] == claimed[0]
    assert entry["intercepted"] is True


# ── the proxy never captures its own resolution ──────────────────────────────


def test_intercept_all_does_not_capture_the_proxys_own_queries():
    """Redirecting our own resolution to ourselves is a loop, not a capture."""
    interceptor = DNSInterceptor(_config(intercept=[], exclude=[], intercept_all=True))

    assert interceptor.classify("anything.at.all.example", from_self=True) == "forwarded"
    assert interceptor.classify("anything.at.all.example") == "intercepted"


def test_a_listed_name_is_still_intercepted_for_a_local_probe():
    """A list entry is a deliberate choice: `dig @127.0.0.1` must see it applied."""
    interceptor = DNSInterceptor(_config(intercept=[DEVICE_HOST], intercept_all=True))

    assert interceptor.classify(DEVICE_HOST, from_self=True) == "intercepted"


@pytest.mark.asyncio
async def test_a_loopback_query_is_forwarded_and_not_recorded(monkeypatch):
    interceptor = DNSInterceptor(_config(intercept=[], exclude=[], intercept_all=True))
    recorder = DNSDiscoveryRecorder(sink=None)
    interceptor.query_recorder = recorder
    forwarded: list[str] = []

    async def _fake_forward(query):
        forwarded.append(query.question[0].name.to_text())
        return b"from-the-upstream"

    monkeypatch.setattr(interceptor, "_forward", _fake_forward)
    query = dns.message.make_query("brand-new.example", dns.rdatatype.A)

    answer = await interceptor.handle_query(query.to_wire(), client="127.0.0.1")

    assert answer == b"from-the-upstream"  # not answered with our own address
    assert forwarded == ["brand-new.example."]
    assert recorder.pending_pairs == 0  # a self-query is not a device to discover
    assert interceptor.stats["intercepted"] == 0


@pytest.mark.asyncio
async def test_a_device_query_is_still_intercepted_and_recorded(monkeypatch):
    """The same rule, on the other side: a real client keeps the wide behaviour."""
    interceptor = DNSInterceptor(_config(intercept=[], exclude=[], intercept_all=True))
    recorder = DNSDiscoveryRecorder(sink=None)
    interceptor.query_recorder = recorder

    async def _never_called(_query):  # pragma: no cover - must not be reached
        raise AssertionError("an intercepted name must not be forwarded")

    monkeypatch.setattr(interceptor, "_forward", _never_called)
    query = dns.message.make_query("brand-new.example", dns.rdatatype.A)

    answer = dns.message.from_wire(
        await interceptor.handle_query(query.to_wire(), client=CLIENT_IP)
    )

    assert [rdata.address for rrset in answer.answer for rdata in rrset] == [PROXY_IP]
    assert recorder.pending_pairs == 1


@pytest.mark.asyncio
async def test_the_discovery_log_hands_out_utc_timestamps(db_manager):
    """A caller must not have to guess the zone of a timestamp it is given."""
    await db_manager.record_dns_queries([_observation(DEVICE_HOST, hits=1, seen=datetime.now(UTC))])

    row = (await db_manager.list_dns_queries())[0]

    assert row["last_seen"].endswith("+00:00")
    assert row["first_seen"].endswith("+00:00")


@pytest.mark.asyncio
async def test_discovery_reports_a_failing_recorder(api):
    """A recorder that cannot reach the database must not fail silently.

    The log line is easy to miss; the panel counter is not.
    """
    failing = DNSDiscoveryRecorder(_FakeSink(fail=True), flush_interval_s=3600)
    failing.note(CLIENT_IP, DEVICE_HOST, "A", "forwarded")
    await failing.flush()  # the sink refuses: one error, and the counter is kept
    set_dns_recorder(failing)
    try:
        body = (await api["client"].get("/api/dns/discovery")).json()
    finally:
        set_dns_recorder(api["recorder"])

    assert body["flush_errors"] == 1
    assert body["pending_pairs"] == 1
    assert body["flushed_pairs"] == 0


@pytest.mark.asyncio
async def test_a_hand_edited_config_reaches_the_running_interceptor(tmp_path):
    """The file and the panel must mean the same thing, without a restart."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_YAML, encoding="utf-8")
    manager = ConfigManager(config_path=config_path)
    manager.load()
    interceptor = DNSInterceptor(manager.config)
    set_dns_interceptor(interceptor)
    previous = server_mod.config_manager
    server_mod.config_manager = manager
    # Exactly what the lifespan does with the discovery wiring.
    manager.register_callback(server_mod._apply_dns_settings)
    try:
        assert interceptor.classify("anything.example") == "forwarded"

        config_path.write_text(CONFIG_YAML + "  intercept_all: true\n", encoding="utf-8")
        manager.load()
        manager._notify_callbacks()  # noqa: SLF001 - the watcher's own path

        assert interceptor.intercept_all is True
        assert interceptor.classify("anything.example") == "intercepted"
    finally:
        server_mod.config_manager = previous
        set_dns_interceptor(None)
