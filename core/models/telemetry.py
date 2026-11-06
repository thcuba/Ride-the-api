"""Network telemetry, relayed connections, DNS queries, and match stats ORM models."""

from __future__ import annotations

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class InterceptedRequest(Base):
    """Raw intercepted request/response pair for audit / training data."""

    __tablename__ = "intercepted_requests"
    __table_args__ = (Index("ix_intercepted_device_ts", "device_id", "captured_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    captured_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    query_params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed: Mapped[bool] = mapped_column(default=False, nullable=False)
    processed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RelayedConnection(Base):
    """A connection forwarded to the vendor cloud without terminating TLS.

    Some devices pin their vendor's certificate and will never accept one we generate, so their
    traffic is relayed verbatim and nothing of the payload is readable. The contact itself is still
    worth keeping: which device, which host it asked for, how much was exchanged in each direction
    and how long it lasted. That is the only trace such a device leaves, and the raw material from
    which its behaviour can be understood over time.
    """

    __tablename__ = "relayed_connections"
    __table_args__ = (Index("ix_relayed_device_ts", "device_id", "started_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    client_ip: Mapped[str] = mapped_column(String(64), nullable=False)
    server_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    destination_port: Mapped[int] = mapped_column(Integer, nullable=False)
    upstream_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bytes_to_cloud: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bytes_to_device: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class DNSQueryLog(Base):
    """One (device, hostname) pair seen on the DNS interception port.

    The DNS server is the first place a device reveals what it talks to, whether
    or not the name is being intercepted: what a client asks for, how often and
    when is what lets an operator decide what to intercept next instead of
    guessing domain names. One row per (client_ip, hostname, qtype) with a hit
    counter, so the table grows with the number of distinct pairs rather than
    with the number of queries; the oldest rows are pruned past a cap.
    """

    __tablename__ = "dns_query_log"
    __table_args__ = (
        UniqueConstraint("client_ip", "hostname", "qtype", name="uq_dns_query_pair"),
        Index("ix_dns_query_last_seen", "last_seen"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_ip: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    qtype: Mapped[str] = mapped_column(String(16), nullable=False, default="A")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="forwarded")
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)


class MatchStats(Base):
    """Real-time match statistics per device."""

    __tablename__ = "match_stats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    total_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    local_hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cloud_misses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    match_rate_pct: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    recent_results: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    patterns_learned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    templates_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    buffer_flushes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_buffer_size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_flush_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
