"""
Configuration management with hot-reload support.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from watchfiles import Change, watch

from core.backend_names import normalise_backend
from core.modification import ModificationAction

logger = logging.getLogger(__name__)


class ContextBufferSizes(int, Enum):
    KB_128 = 131072
    KB_256 = 262144
    KB_512 = 524288
    MB_1 = 1048576
    MB_2 = 2097152
    MB_5 = 5242880
    MB_10 = 10485760


class CoreConfig(BaseModel):
    database_url: str = "sqlite+aiosqlite:///./ridebase/core.db"
    device_db_dir: str = "./ridebase/devices"
    device_databases: dict[str, str] = Field(default_factory=dict)
    default_context_buffer_size: int = 524288  # 512KB default

    # Per-IP overrides: how each known device IP is treated on ingress.
    # Key = source IP (IPv4/v6 string). Values control which database the
    # device uses and how its connection is handled (``auto`` detects whether
    # the traffic should be TLS-decrypted or handled by a known protocol).
    ip_profiles: dict[str, IpProfileConfig] = Field(default_factory=dict)


class ConnectionType(StrEnum):
    """How a device connection is handled on ingress.

    - AUTO: detect on first bytes — if it looks like a TLS ClientHello it is
      TLS to be decrypted (MITM); if it matches a known protocol (HTTP, MQTT,
      CoAP, Modbus...) the matching handler is used.
    - TLS: always decrypt (MITM).
    - Any known protocol name (http, mqtt, coap, modbus, ...) forces that handler
      without decryption.
    """

    AUTO = "auto"
    TLS = "tls"
    HTTP = "http"
    MQTT = "mqtt"
    COAP = "coap"
    MODBUS = "modbus"


class IpProfileConfig(BaseModel):
    """Per-IP override for database and connection handling."""

    database: str | None = None  # URL override; None -> default per-device DB
    connection: ConnectionType = ConnectionType.AUTO
    bypass: bool = False  # True -> forward this IP to the cloud without analysis


class BufferConfig(BaseModel):
    """Transient capture buffer storage backend.

    ``disk`` (default) keeps buffered pairs on durable storage (device DB);
    ``memory`` keeps them in a process-shared in-memory SQLite engine (RAM).
    The active backend can also be switched at runtime via the settings API /
    dashboard toggle, which persists the choice for the next start.
    """

    backend: str = "disk"

    @field_validator("backend")
    @classmethod
    def _normalise_backend(cls, value: str) -> str:
        """Accept the documented spellings, and refuse an unknown one by name.

        A silent fallback here is how an operator ends up believing captures survive a
        restart while the proxy quietly runs the volatile backend.
        """
        canonical = normalise_backend(value)
        if canonical is None:
            raise ValueError(
                f"buffer.backend must be 'disk' (SQLite, survives a restart) or 'memory' "
                f"(RAM), got {value!r}"
            )
        return canonical


class TLSConfig(BaseModel):
    enabled: bool = True
    cert_file: str = "./certs/ride-api.pem"
    key_file: str = "./certs/ride-api.key"


class FallbackConfig(BaseModel):
    enabled: bool = True
    timeout: int = 10
    retry_count: int = 2
    confidence_threshold: float = 0.7


class ProxyConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8911
    tls: TLSConfig = Field(default_factory=TLSConfig)
    request_timeout: int = 30
    max_request_size: int = 1048576
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)


class SecurityConfig(BaseModel):
    """Control-plane authentication and hardening.

    When ``auth_enabled`` is true (default), every ``/api/*`` route requires
    the control-plane ``password``. It is sent via the ``X-API-Key`` header or
    ``Authorization: Bearer ***`` and grants full access (reads and writes) —
    there is no read-only vs admin distinction.

    If ``password`` is left empty while auth is enabled, the control plane is
    locked and the API and UI ask the operator to set ``security.password`` in
    ``config.yaml`` and restart — no ephemeral keys are generated.
    """

    auth_enabled: bool = True
    password: str = ""


class CloudConfig(BaseModel):
    api_endpoint: str = ""
    mqtt_endpoint: str = ""
    mqtt_port: int = 8883


class AdapterConfig(BaseModel):
    class_name: str = Field(default="", alias="class")
    # Vendor-specific extra config
    extra: dict[str, Any] = Field(default_factory=dict)


class VendorConfig(BaseModel):
    enabled: bool = True
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    adapter: AdapterConfig = Field(default_factory=AdapterConfig)


class ModelDefaults(BaseModel):
    example: dict[str, str] = Field(
        default_factory=lambda: {
            "ac": "example_ac_v1.onnx",
            "heat_pump": "example_hp_v1.onnx",
        }
    )


class InferenceConfig(BaseModel):
    batch_size: int = 1
    intra_op_threads: int = 2
    inter_op_threads: int = 2
    execution_providers: list[str] = Field(default_factory=lambda: ["CPUExecutionProvider"])


class HotReloadConfig(BaseModel):
    enabled: bool = True
    check_interval: int = 30


class ModelsConfig(BaseModel):
    registry_path: str = "models"
    defaults: ModelDefaults = Field(default_factory=ModelDefaults)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    hot_reload: HotReloadConfig = Field(default_factory=HotReloadConfig)


class OnlineLearningConfig(BaseModel):
    enabled: bool = True
    buffer_size: int = 1000
    update_interval: int = 3600
    min_samples_for_update: int = 100


class PolicyConfig(BaseModel):
    evaluation_interval: int = 60
    default_policy: str = "pid_thermal"


class ControlConfig(BaseModel):
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    online_learning: OnlineLearningConfig = Field(default_factory=OnlineLearningConfig)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    output: str = "stdout"


class MetricsConfig(BaseModel):
    enabled: bool = True
    port: int = 9090
    path: str = "/metrics"


class TracingConfig(BaseModel):
    enabled: bool = True
    exporter: str = "console"
    otlp_endpoint: str = "http://localhost:4317"
    sample_rate: float = 0.1


class HealthCheckConfig(BaseModel):
    enabled: bool = True
    port: int = 8080
    path: str = "/health"


class ObservabilityConfig(BaseModel):
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)


#: Exclusions shipped as defaults. They are **soft**: they yield to a name a
#: registered adapter explicitly claims, so shipping an adapter is still enough to
#: make its service interceptable (the ``shelly-*.local`` family lives inside
#: ``*.local``). An exclusion written by the operator is hard and wins over
#: everything, adapter claims included, because overriding what someone typed by
#: hand is exactly the kind of silence this project avoids.
DEFAULT_DNS_EXCLUSIONS: tuple[str, ...] = (
    "ntp.*",
    "time.*",
    "ocsp.*",
    "crl.*",
    "*.local",
    "*.lan",
    "*.localdomain",
)


class DNSConfig(BaseModel):
    """DNS interception server and the upstream resolvers it forwards to."""

    enabled: bool = Field(
        default=False,
        description="Run the DNS interception server (opt-in: binds a privileged port)",
    )
    listen_host: str = "0.0.0.0"
    listen_port: int = Field(
        default=53,
        description="Port 53 needs CAP_NET_BIND_SERVICE (setcap) or root",
    )
    intercept: list[str] = Field(
        default_factory=list,
        description=(
            "Names answered with this proxy's address: exact names or '*.domain' "
            "wildcards. Anything not listed is forwarded to the upstream resolvers."
        ),
    )
    intercept_all: bool = Field(
        default=False,
        description=(
            "Answer EVERY name with this proxy's address, so every device that uses "
            "this host as its resolver reaches the proxy (the way to fill the capture "
            "buffer without listing domains one by one). Off by default: it must be a "
            "deliberate choice, and 'exclude' is what keeps it from touching names "
            "that have nothing to do with device learning. It never applies to the "
            "proxy's own queries (loopback client): answering our own resolution with "
            "our own address would be a loop, not a capture."
        ),
    )
    exclude: list[str] = Field(
        default_factory=lambda: list(DEFAULT_DNS_EXCLUSIONS),
        description=(
            "Names never answered by the proxy and always forwarded. The ones you write "
            "here win over everything ('intercept', 'intercept_all' and even a name a "
            "registered adapter claims), because an exclusion typed by hand must mean "
            "what it says. The values shipped by default are soft instead: they yield "
            "to a name an adapter explicitly claims, so shipping an adapter is still "
            "enough to make its service interceptable -- the 'shelly-*.local' family "
            "lives inside the default '*.local'. Time/OCSP/CRL and local names are here "
            "by default: hijacking them only breaks devices without teaching anything."
        ),
    )
    answer_ip: str = Field(
        default="",
        description="Address returned for intercepted names; empty = detect the LAN address",
    )
    answer_ttl: int = Field(default=60, description="TTL of the answers we synthesise")
    forward_timeout: float = Field(default=5.0, description="Per-upstream timeout, seconds")

    dns_servers: list[str] = Field(
        default_factory=lambda: ["8.8.8.8", "1.1.1.1"],
        description="Upstream IPv4 DNS servers (tried in order)",
    )
    dns_servers_v6: list[str] = Field(
        default_factory=lambda: [
            "2001:4860:4860::8888",
            "2606:4700:4700::1111",
        ],
        description="Upstream IPv6 DNS servers (tried in order)",
    )
    pihole_custom_dns: str = ""
    adguard_rewrites: str = ""


class TrafficRule(BaseModel):
    name: str = ""
    scope: str = "local"
    match_type: str = "cidr"
    match_value: str = ""
    action: str = "intercept"
    priority: int = 0
    enabled: bool = True


class TrafficSelectionConfig(BaseModel):
    default_action: str = "intercept"
    rules: list[TrafficRule] = Field(default_factory=list)


class LLMDecipherProfile(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model_id: str = ""
    prompt_template: str = ""
    max_tokens: int = Field(
        default=2048,
        description=(
            "Ceiling on the tokens a single completion may generate. A model that does not "
            "honour the requested JSON format keeps generating until the context or the "
            "client timeout stops it, so every call costs the timeout plus its retries. "
            "0 = do not send the parameter."
        ),
    )


class LLMDecipherConfig(BaseModel):
    enabled: bool = True
    default_profile: str = "default"
    profiles: dict[str, LLMDecipherProfile] = Field(default_factory=dict)
    # Purpose → profile binding. Each purpose (see ``LLMDecipherService.TASKS``)
    # may be served by a dedicated profile — e.g. a cheap local model for the
    # buffer flush and a stronger one to validate weak matches. An empty or
    # missing entry falls back to ``default_profile``. Normally managed at
    # runtime through the LLM settings API, which both UIs edit.
    task_profiles: dict[str, str] = Field(default_factory=dict)


class ModificationRule(BaseModel):
    """A single modification rule.

    Supports both the legacy field set (``match_type``/``match_value``/
    ``target_field``/``target_value``) and the engine-native field set
    (``match_vendor``/``match_intent``/``action_params``...). The engine
    translates legacy rules to the native format at load time. All fields
    are stored as a superset so the config round-trip preserves every value.
    """

    name: str = ""
    scope: str = "local"
    match_type: str = "hostname"
    match_value: Any = None
    action: ModificationAction = ModificationAction.MODIFY
    target_field: str = ""
    target_value: Any = None
    priority: int = 0
    enabled: bool = True

    # Engine-native fields
    match_vendor: str | None = None
    match_device_type: str | None = None
    match_intent: str | None = None
    match_field_path: str | None = None
    match_headers: dict[str, str] | None = None
    match_method: str | None = None
    match_path_pattern: str | None = None
    action_params: dict[str, Any] = Field(default_factory=dict)
    direction: str = "request"

    model_config = ConfigDict(extra="allow")


class ModificationConfig(BaseModel):
    enabled: bool = True
    rules: list[ModificationRule] = Field(default_factory=list)


class CorrelationHTTPConfig(BaseModel):
    method: str = "connection"
    correlation_header: str = "X-Request-ID"
    keep_alive_timeout: int = 30


class CorrelationMQTTConfig(BaseModel):
    method: str = "topic_sequence"
    qos_tracking: bool = True
    retain_handling: str = "include"


class CorrelationCoAPConfig(BaseModel):
    method: str = "message_id"
    confirmable_timeout: int = 5


class CorrelationConfig(BaseModel):
    enabled: bool = True
    http: CorrelationHTTPConfig = Field(default_factory=CorrelationHTTPConfig)
    mqtt: CorrelationMQTTConfig = Field(default_factory=CorrelationMQTTConfig)
    coap: CorrelationCoAPConfig = Field(default_factory=CorrelationCoAPConfig)
    store_pairs: bool = True
    max_pairs_per_device: int = 10000
    pair_ttl_hours: int = 168


class LearningConfig(BaseModel):
    enabled: bool = True
    default_mode: str = "learning"  # learning | production | hybrid
    default_match_threshold: float = 0.85
    auto_switch_to_production: bool = False
    min_patterns_for_production: int = 10
    min_match_rate_for_production: float = 80.0
    # When True, production mode serves responses exclusively from the local
    # database — no implicit cloud fallback. Unmatched requests return a
    # conclusive error instead of forwarding to the vendor cloud.
    production_no_fallback: bool = False
    # When True, unmatched requests in production/hybrid mode return a special
    # "forward to cloud" signal (X-Action: forward) instead of calling
    # adapter.forward_to_cloud() internally. Intended for deployments behind a
    # reverse proxy (nginx) that routes the request to the real cloud directly.
    signal_forward_to_cloud: bool = False


class PinningBypassConfig(BaseModel):
    """Per-vendor guidance for manual certificate-pinning handling.

    The value is configuration metadata; it does not alter client-side TLS
    validation or make the proxy decrypt a rejected certificate.
    """

    strategy: str = "mitm_proxy"  # metadata: mitm_proxy | frida | disable_pin_check


class TLSDecryptConfig(BaseModel):
    """TLS Decryption / MITM engine configuration."""

    enabled: bool = False  # disabled by default
    listen_ports: list[int] = Field(default_factory=lambda: [443, 8443])
    ca_cert_path: str = "./certs/ca.pem"
    ca_key_path: str = "./certs/ca.key"
    device_certs_dir: str = "./data/device_certs"
    external_certs_dir: str = "./data/external_certs"
    pinning_bypass: dict[str, PinningBypassConfig] = Field(default_factory=dict)
    min_tls_version: str = "TLSv1.2"
    max_tls_version: str = "TLSv1.3"


# ── Protocol Server Configs ─────────────────────────────────────────────────────


class MQTTServerConfig(BaseModel):
    """MQTT broker server configuration."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 1883
    port_tls: int = 8883
    tls_enabled: bool = False
    max_packet_size: int = 268435  # 256KB
    topic_filters: list[str] = Field(default_factory=lambda: ["#"])


class CoAPServerConfig(BaseModel):
    """CoAP server configuration."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 5683
    dtls_enabled: bool = False
    dtls_port: int = 5684
    max_pdu_size: int = 1024


class ModbusServerConfig(BaseModel):
    """Modbus TCP server configuration."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 502
    unit_id: int = 1
    tls_enabled: bool = False
    tls_port: int = 802
    holding_registers: dict[str, int] = Field(default_factory=dict)
    coil_registers: dict[str, int] = Field(default_factory=dict)


class WebSocketServerConfig(BaseModel):
    """WebSocket server configuration."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9000
    path: str = "/ws"
    max_message_size: int = 1048576  # 1MB


class RawTCPServerConfig(BaseModel):
    """Raw TCP server configuration."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9100
    buffer_size: int = 4096
    idle_timeout: int = 300
    protocol_detect: bool = True


class HTTP2ServerConfig(BaseModel):
    """HTTP/2 server configuration."""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 443
    cleartext_port: int = 8080  # h2c
    tls_enabled: bool = True


class ZigbeeBridgeConfig(BaseModel):
    """Zigbee bridge (Zigbee2MQTT) configuration."""

    enabled: bool = False
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_pass: str = ""
    topic_prefix: str = "zigbee2mqtt"
    reconnect_interval: int = 10


class ZWaveBridgeConfig(BaseModel):
    """Z-Wave bridge (Z-Wave JS UI) configuration."""

    enabled: bool = False
    connection_type: str = "mqtt"  # mqtt | ws
    host: str = "localhost"
    port: int = 1883
    ws_port: int = 3000
    mqtt_user: str = ""
    mqtt_pass: str = ""


class MatterBridgeConfig(BaseModel):
    """Matter bridge (Matter.js) configuration."""

    enabled: bool = False
    host: str = "0.0.0.0"
    controller_port: int = 5540
    fabric_id: int = 1
    vendor_id: int = 65521


class ProtocolServersConfig(BaseModel):
    """All protocol server configurations."""

    mqtt: MQTTServerConfig = Field(default_factory=MQTTServerConfig)
    coap: CoAPServerConfig = Field(default_factory=CoAPServerConfig)
    modbus: ModbusServerConfig = Field(default_factory=ModbusServerConfig)
    websocket: WebSocketServerConfig = Field(default_factory=WebSocketServerConfig)
    raw_tcp: RawTCPServerConfig = Field(default_factory=RawTCPServerConfig)
    http2: HTTP2ServerConfig = Field(default_factory=HTTP2ServerConfig)
    zigbee_bridge: ZigbeeBridgeConfig = Field(default_factory=ZigbeeBridgeConfig)
    zwave_bridge: ZWaveBridgeConfig = Field(default_factory=ZWaveBridgeConfig)
    matter_bridge: MatterBridgeConfig = Field(default_factory=MatterBridgeConfig)


class ConnectionWatchdogConfig(BaseModel):
    """Auto-heal when the process can no longer make outgoing HTTP calls.

    Long-running installs occasionally get into a state where every outbound
    HTTP request fails with ``Connection error.`` (httpx.TransportError) while
    local listeners keep serving and the same calls succeed from a shell. Only a
    process restart clears it. The watchdog periodically probes an outbound URL
    with a FRESH client and, after ``max_failures`` consecutive failures, restarts
    itself via ``restart_command`` (then cools down for ``cooldown`` seconds).

    ``restart_command`` defaults to the standard systemd user unit; set it to an
    empty list to disable the restart (the watchdog then only logs) or point it
    at any non-interactive restart command.
    """

    enabled: bool = True
    interval: int = 60  # seconds between probes
    timeout: int = 10  # per-probe HTTP timeout
    max_failures: int = 5  # consecutive transport failures before restart
    probe_url: str = "http://api.openai.com/v1/models"
    restart_command: list[str] = Field(
        default_factory=lambda: ["systemctl", "--user", "restart", "ride-the-api"]
    )
    cooldown: int = 600  # seconds to wait before allowing another restart


class Config(BaseModel):
    core: CoreConfig = Field(default_factory=CoreConfig)
    buffer: BufferConfig = Field(default_factory=BufferConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    vendors: dict[str, VendorConfig] = Field(default_factory=dict)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    dns: DNSConfig = Field(default_factory=DNSConfig)
    traffic_selection: TrafficSelectionConfig = Field(default_factory=TrafficSelectionConfig)
    llm_decipher: LLMDecipherConfig = Field(default_factory=LLMDecipherConfig)
    modification: ModificationConfig = Field(default_factory=ModificationConfig)
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    tls_decrypt: TLSDecryptConfig = Field(default_factory=TLSDecryptConfig)
    protocol_servers: ProtocolServersConfig = Field(default_factory=ProtocolServersConfig)
    connection_watchdog: ConnectionWatchdogConfig = Field(default_factory=ConnectionWatchdogConfig)


class ConfigManager:
    """Manages configuration with hot-reload support."""

    def __init__(self, config_path: str | Path = "config/config.yaml") -> None:
        self.config_path = Path(config_path)
        self._config: Config | None = None
        self._lock = threading.RLock()
        self._callbacks: list[callable] = []
        self._watch_thread: threading.Thread | None = None
        self._stop_watch = threading.Event()

    def load(self) -> Config:
        """Load configuration from file."""
        with self._lock:
            if not self.config_path.exists():
                raise FileNotFoundError(f"Config file not found: {self.config_path}")

            with self.config_path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)

            config = Config(**data)
            self._config = config
            return config

    def set_ip_profile_bypass(self, ip: str, bypass: bool) -> bool:
        """Set/clear ``ip_profiles[ip].bypass`` and persist to the config file.

        Mutates the in-memory config, writes the file atomically and leaves the
        hot-reload watcher to refresh other consumers. Returns True on success.
        """
        with self._lock:
            cfg = self.config.model_copy(deep=True)
            profile = cfg.core.ip_profiles.get(ip, IpProfileConfig())
            profile.bypass = bypass
            cfg.core.ip_profiles[ip] = profile
            return self._write(cfg)

    def set_dns_settings(
        self,
        *,
        intercept: list[str] | None = None,
        intercept_all: bool | None = None,
        exclude: list[str] | None = None,
    ) -> bool:
        """Persist the DNS interception settings (intercept list, "all", exclude).

        Called by the DNS discovery API when an operator promotes a discovered
        hostname to an intercepted one, or flips ``intercept_all``. Only the
        arguments that are not ``None`` are touched, so the UI updates one field
        without resending (and risking) the others. Mutates the in-memory config,
        writes the file atomically and leaves the hot-reload watcher to refresh
        other consumers. Returns True on success.
        """
        with self._lock:
            cfg = self.config.model_copy(deep=True)
            if intercept is not None:
                cfg.dns.intercept = [str(item).strip() for item in intercept if str(item).strip()]
            if intercept_all is not None:
                cfg.dns.intercept_all = bool(intercept_all)
            if exclude is not None:
                cfg.dns.exclude = [str(item).strip() for item in exclude if str(item).strip()]
            return self._write(cfg)

    def set_security_password(self, password: str) -> bool:
        """Persist the control-plane ``security.password`` back to the config.

        Mutates the in-memory config, writes the file atomically and leaves
        the hot-reload watcher to refresh other consumers. Returns True on
        success.
        """
        with self._lock:
            cfg = self.config.model_copy(deep=True)
            cfg.security.password = password
            return self._write(cfg)

    def set_protocol_server_enabled(self, name: str, enabled: bool) -> bool:
        """Set/clear ``protocol_servers.<name>.enabled`` and persist to disk.

        Called by the protocol-server start/stop API so a server toggled on in
        the dashboard survives a restart (``start_all()`` boots whatever has
        ``enabled: true``). Mutates the in-memory config, writes the file
        atomically and leaves the hot-reload watcher to reconcile the running
        plugins. Returns True on success.
        """
        with self._lock:
            cfg = self.config.model_copy(deep=True)
            sub = getattr(cfg.protocol_servers, name, None)
            if sub is None:
                return False
            sub.enabled = bool(enabled)
            return self._write(cfg)

    def update_config(self, data: dict) -> bool:
        """Replace the full config after validation; atomically persist.

        The control-plane ``security.password`` is preserved across updates:
        if the incoming data omits the ``security`` section or sends an empty
        password, the current password is kept so it never changes after a
        config update.
        """
        with self._lock:
            current = self.config
            if "security" not in data:
                data = {**data, "security": current.security.model_dump(mode="json")}
            else:
                security = dict(data["security"])
                if not security.get("password"):
                    security["password"] = current.security.password
                data = {**data, "security": security}
            new_config = Config(**data)
            return self._write(new_config)

    def _write(self, config: Config | None = None) -> bool:
        """Atomically persist a candidate config before making it active.

        The temporary file is created with mode ``0600`` because the YAML may
        contain the control-plane password and LLM API keys. The live config is
        swapped only after the durable file replacement succeeds, so a failed
        save cannot leave this process running with settings that disappear on
        restart.
        """
        tmp_name: str | None = None
        try:
            with self._lock:
                candidate = config if config is not None else self.config
                content = yaml.safe_dump(
                    candidate.model_dump(mode="json", by_alias=False),
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(self.config_path.parent),
                    prefix=f".{self.config_path.name}.",
                    suffix=".tmp",
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                Path(tmp_name).replace(self.config_path)
                tmp_name = None
                self._config = candidate
        except Exception:  # noqa: BLE001 - persist failure should not crash
            logger.exception("ConfigManager: failed to persist config to %s", self.config_path)
            if tmp_name is not None:
                try:
                    Path(tmp_name).unlink(missing_ok=True)
                except OSError:
                    logger.warning("ConfigManager: could not remove temporary config %s", tmp_name)
            return False
        return True

    @property
    def config(self) -> Config:
        """Get current configuration (loads if not loaded).

        Uses atomic attribute read for the common case (already loaded)
        to avoid lock contention on every access.
        """
        c = self._config
        if c is None:
            with self._lock:
                if self._config is None:
                    return self.load()
                return self._config
        return c

    def get_vendor_config(self, vendor: str) -> VendorConfig | None:
        """Get vendor-specific configuration."""
        return self.config.vendors.get(vendor)

    def register_callback(self, callback: callable) -> None:
        """Register callback for config changes."""
        with self._lock:
            self._callbacks.append(callback)

    def _notify_callbacks(self) -> None:
        """Notify all callbacks of config change."""
        with self._lock:
            # Snapshot under lock, then invoke outside the lock so a slow or
            # re-entrant callback cannot block reloads or registration.
            callbacks = list(self._callbacks)
            config = self._config
        for callback in callbacks:
            try:
                callback(config)
            except Exception:
                # Log but don't crash
                logging.getLogger(__name__).exception("Config callback error")

    def start_watching(self) -> None:
        """Start watching config file for changes."""
        if self._watch_thread and self._watch_thread.is_alive():
            return

        self._stop_watch.clear()
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()

    def stop_watching(self) -> None:
        """Stop watching config file."""
        self._stop_watch.set()
        if self._watch_thread:
            self._watch_thread.join(timeout=5)

    def _watch_loop(self) -> None:
        """Watch loop for config file changes."""
        try:
            for changes in watch(str(self.config_path), stop_event=self._stop_watch):
                for change_type, path in changes:
                    if change_type in (Change.modified, Change.added):
                        logging.getLogger(__name__).info("Config file changed: %s", path)
                        try:
                            self.load()
                            self._notify_callbacks()
                        except Exception:
                            logging.getLogger(__name__).exception("Failed to reload config")
        except Exception:
            logging.getLogger(__name__).exception("Config watch error")


# Global config manager instance
_config_manager: ConfigManager | None = None


def get_config_manager(config_path: str | Path | None = None) -> ConfigManager:
    """Get global config manager instance."""
    global _config_manager  # noqa: PLW0603
    if _config_manager is None:
        path = config_path or "config/config.yaml"
        _config_manager = ConfigManager(path)
    elif config_path:
        # The singleton already exists; silently ignoring an explicit path
        # leaves callers with a different config than they requested.
        wanted = Path(config_path).resolve()
        current = Path(_config_manager.config_path).resolve()
        if wanted != current:
            logging.getLogger(__name__).warning(
                "Ignoring config path %s: singleton already loaded %s",
                config_path,
                _config_manager.config_path,
            )
    return _config_manager


def get_config(config_path: str | Path | None = None) -> Config:
    """Get current configuration."""
    return get_config_manager(config_path).config
