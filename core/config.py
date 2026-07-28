"""
Configuration management with hot-reload support.
"""

from __future__ import annotations

import threading
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from watchfiles import watch, Change


class CoreConfig(BaseModel):
    database_url: str = "sqlite+aiosqlite:///./data/core.db"
    vendor_db_dir: str = "./data/vendors"
    vendor_databases: dict[str, str] = Field(default_factory=dict)


class TLSConfig(BaseModel):
    enabled: bool = True
    cert_file: str = "./certs/edge-hvac.pem"
    key_file: str = "./certs/edge-hvac.key"


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
    vendor_routes: dict[str, str] = Field(default_factory=lambda: {
        "ty": "/ty",
        "tl": "/tl",
        "zh": "/zh",
        "hr": "/hr",
    })
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)


class CloudConfig(BaseModel):
    api_endpoint: str = ""
    mqtt_endpoint: str = ""
    mqtt_port: int = 8883


class AdapterConfig(BaseModel):
    class_name: str = ""
    # Vendor-specific extra config
    extra: dict[str, Any] = Field(default_factory=dict)


class VendorConfig(BaseModel):
    enabled: bool = True
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    adapter: AdapterConfig = Field(default_factory=AdapterConfig)


class ModelDefaults(BaseModel):
    ty: dict[str, str] = Field(default_factory=lambda: {
        "ac": "ty_ac_v1.onnx",
        "heat_pump": "ty_hp_v1.onnx",
    })
    tl: dict[str, str] = Field(default_factory=lambda: {
        "ac": "tl_ac_v1.onnx",
    })
    zh: dict[str, str] = Field(default_factory=lambda: {
        "ventilator": "zh_vent_v1.onnx",
    })
    hr: dict[str, str] = Field(default_factory=lambda: {
        "ac": "hr_ac_v1.onnx",
    })


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


class DNSConfig(BaseModel):
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


class LLMDecipherConfig(BaseModel):
    enabled: bool = True
    default_profile: str = "default"
    profiles: dict[str, LLMDecipherProfile] = Field(default_factory=dict)


class ModificationAction(str, Enum):
    modify = "modify"
    block = "block"
    inject = "inject"
    replace = "replace"
    redirect = "redirect"
    delay = "delay"


class ModificationRule(BaseModel):
    name: str = ""
    scope: str = "local"
    match_type: str = "hostname"
    match_value: str = ""
    action: ModificationAction = ModificationAction.modify
    target_field: str = ""
    target_value: str = ""
    priority: int = 0
    enabled: bool = True


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


class Config(BaseModel):
    core: CoreConfig = Field(default_factory=CoreConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    vendors: dict[str, VendorConfig] = Field(default_factory=dict)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    dns: DNSConfig = Field(default_factory=DNSConfig)
    traffic_selection: TrafficSelectionConfig = Field(default_factory=TrafficSelectionConfig)
    llm_decipher: LLMDecipherConfig = Field(default_factory=LLMDecipherConfig)
    modification: ModificationConfig = Field(default_factory=ModificationConfig)
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)


class ConfigManager:
    """Manages configuration with hot-reload support."""
    
    def __init__(self, config_path: str | Path = "config/config.yaml"):
        self.config_path = Path(config_path)
        self._config: Config | None = None
        self._lock = threading.RLock()
        self._callbacks: list[callable] = []
        self._watch_thread: threading.Thread | None = None
        self._stop_watch = threading.Event()
    
    def load(self) -> Config:
        """Load configuration from file."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        
        config = Config(**data)
        
        with self._lock:
            self._config = config
        
        return config
    
    @property
    def config(self) -> Config:
        """Get current configuration (loads if not loaded)."""
        with self._lock:
            if self._config is None:
                return self.load()
            return self._config
    
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
            for callback in self._callbacks:
                try:
                    callback(self._config)
                except Exception as e:
                    # Log but don't crash
                    import logging
                    logging.getLogger(__name__).error(f"Config callback error: {e}")
    
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
                        import logging
                        logging.getLogger(__name__).info(f"Config file changed: {path}")
                        try:
                            self.load()
                            self._notify_callbacks()
                        except Exception as e:
                            logging.getLogger(__name__).error(f"Failed to reload config: {e}")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Config watch error: {e}")


# Global config manager instance
_config_manager: ConfigManager | None = None


def get_config_manager(config_path: str | Path | None = None) -> ConfigManager:
    """Get global config manager instance."""
    global _config_manager
    if _config_manager is None:
        path = config_path or "config/config.yaml"
        _config_manager = ConfigManager(path)
    return _config_manager


def get_config(config_path: str | Path | None = None) -> Config:
    """Get current configuration."""
    return get_config_manager(config_path).config