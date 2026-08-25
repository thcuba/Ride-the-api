# Complete Configuration Reference — ride-the-api

> YAML configuration file (default: `config/config.yaml`).  
> All values shown are the **defaults** applied by the Pydantic `Config` model.

---

## Index

1. [core](#core) — Database and context
2. [proxy](#proxy) — HTTP proxy, TLS and fallback
3. [vendors](#vendors) — IoT vendor configuration
4. [models](#models) — ML models and inference
5. [control](#control) — Control, policy and online learning
6. [observability](#observability) — Logging, metrics, tracing and health check
7. [dns](#dns) — DNS integration (Pi-hole, AdGuard)
8. [traffic_selection](#traffic_selection) — Traffic selection rules
9. [llm_decipher](#llm_decipher) — LLM-based deciphering
10. [modification](#modification) — Request/response modification rules
11. [correlation](#correlation) — Request-response correlation
12. [learning](#learning) — Learning and production modes
13. [tls_decrypt](#tls_decrypt) — TLS decryption/MITM
14. [protocol_servers](#protocol_servers) — Multi-protocol servers

---

## core

Database and per-device context configuration.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `database_url` | `string` | `"sqlite+aiosqlite:///./data/core.db"` | Core database connection URL (SQLite with aiosqlite) |
| `device_db_dir` | `string` | `"./data/devices"` | Directory for per-device databases |
| `device_databases` | `dict[string, string]` | `{}` | Device-name → database path map, to override the default path |
| `default_context_buffer_size` | `integer` | `524288` | Default context buffer size in bytes (default 512 KB). Possible values from enum `ContextBufferSizes`: `131072` (128 KB), `262144` (256 KB), `524288` (512 KB), `1048576` (1 MB), `2097152` (2 MB), `5242880` (5 MB), `10485760` (10 MB) |

Example:

```yaml
core:
  database_url: "sqlite+aiosqlite:///./data/core.db"
  device_db_dir: "./data/devices"
  device_databases:
    termostato_soggiorno: "./data/custom/termostato.db"
  default_context_buffer_size: 1048576
```

---

  ## buffer

  Transient capture buffer storage backend, independently configurable for each device.

  | Field | Type | Default | Description |
  |-------|------|---------|-------------|
  | `backend` | `string` | `"disk"` | Where the raw capture buffer lives until it is flushed to the LLM. `disk` keeps pairs on durable storage (per-device DB); `memory` keeps them in a process-shared in-memory SQLite engine (RAM). Can be switched at runtime from the dashboard toggle or `PUT /api/settings/buffer-backend` |

  Example:

  ```yaml
  buffer:
    backend: "disk"  # or "memory" for in-process RAM buffering
  ```

  Notes:

  - In `memory` mode the buffered pairs are **volatile** — they are lost on process restart. Already-learned patterns and durable stats remain on disk.
  - Both `BufferManager` (export/import) and `ContextBuffer` (learning pipeline) share the same in-process RAM buffer, so exports still see buffered pairs while RAM mode is active.
  - RAM mode is process-local: the buffer is not shared between separate processes (e.g. multiple replicas).

  ---

  ## proxy

Main HTTP proxy configuration, listening TLS and fallback behavior.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | `string` | `"0.0.0.0"` | Address the proxy listens on |
| `port` | `integer` | `8911` | Main proxy port |
| `tls` | `TLSConfig` | — | Proxy TLS configuration |
| `request_timeout` | `integer` | `30` | Request handling timeout in seconds |
| `max_request_size` | `integer` | `1048576` | Maximum request size in bytes (1 MB) |
| `fallback` | `FallbackConfig` | — | Vendor cloud fallback configuration |

### proxy.tls

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable TLS on the inbound proxy |
| `cert_file` | `string` | `"./certs/ride-api.pem"` | Path to the proxy TLS certificate |
| `key_file` | `string` | `"./certs/ride-api.key"` | Path to the proxy TLS private key |

### proxy.fallback

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable automatic fallback to vendor cloud |
| `timeout` | `integer` | `10` | Fallback timeout in seconds |
| `retry_count` | `integer` | `2` | Number of fallback retries |
| `confidence_threshold` | `float` | `0.7` | Confidence threshold below which fallback is triggered (0.0 – 1.0) |

Example:

```yaml
proxy:
  host: "0.0.0.0"
  port: 8911
  tls:
    enabled: true
    cert_file: "./certs/ride-api.pem"
    key_file: "./certs/ride-api.key"
  request_timeout: 30
  max_request_size: 1048576
  fallback:
    enabled: true
    timeout: 10
    retry_count: 2
    confidence_threshold: 0.7
```

---

## vendors

Configuration for each supported IoT vendor. It is a key-value dictionary where the key is the vendor name (e.g. `"shelly"`, `"mqtt"`, `"coap"`).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable the vendor |
| `cloud` | `CloudConfig` | — | Vendor cloud endpoint |
| `adapter` | `AdapterConfig` | — | Python adapter to load for the vendor |

### vendors.\<vendor\>.cloud

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `api_endpoint` | `string` | `""` | Vendor cloud REST API endpoint |
| `mqtt_endpoint` | `string` | `""` | Vendor cloud MQTT endpoint |
| `mqtt_port` | `integer` | `8883` | Vendor cloud MQTT port |

### vendors.\<vendor\>.adapter

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `class` | `string` | `""` | Fully qualified adapter class name (e.g. `"adapters.shelly.ShellyAdapter"`) |
| `extra` | `dict` | `{}` | Vendor-specific extra configuration (passed to the adapter) |

Example:

```yaml
vendors:
  shelly:
    enabled: true
    cloud:
      api_endpoint: "https://api.shelly.cloud"
      mqtt_endpoint: "mqtt.shelly.cloud"
      mqtt_port: 8883
    adapter:
      class: "adapters.shelly.ShellyAdapter"
      extra:
        auth_token: "..."
  mqtt:
    enabled: true
    adapter:
      class: "adapters.mqtt.MQTTAdapter"
```

---

## models

ML model (ONNX) configuration for local inference.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `registry_path` | `string` | `"models"` | Directory containing registered ONNX models |
| `defaults` | `ModelDefaults` | — | Default models for each device type |
| `inference` | `InferenceConfig` | — | ONNX inference runtime configuration |
| `hot_reload` | `HotReloadConfig` | — | Model hot-reloading |

### models.defaults

Device-name → ONNX model filename dictionary.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `ac` | `string` | `"example_ac_v1.onnx"` | Default model for air conditioners |
| `heat_pump` | `string` | `"example_hp_v1.onnx"` | Default model for heat pumps |

### models.inference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `batch_size` | `integer` | `1` | Batch size for inference |
| `intra_op_threads` | `integer` | `2` | ONNX Runtime intra-operation threads |
| `inter_op_threads` | `integer` | `2` | ONNX Runtime inter-operation threads |
| `execution_providers` | `array[string]` | `["CPUExecutionProvider"]` | ONNX execution providers (e.g. `CPUExecutionProvider`, `CUDAExecutionProvider`) |

### models.hot_reload

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable hot-reloading of models |
| `check_interval` | `integer` | `30` | Interval in seconds between update checks |

Example:

```yaml
models:
  registry_path: "models"
  defaults:
    ac: "example_ac_v1.onnx"
    heat_pump: "example_hp_v1.onnx"
  inference:
    batch_size: 1
    intra_op_threads: 2
    inter_op_threads: 2
    execution_providers:
      - "CPUExecutionProvider"
  hot_reload:
    enabled: true
    check_interval: 30
```

---

## control

Control system configuration: active policy and online learning.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `policy` | `PolicyConfig` | — | Control policy configuration |
| `online_learning` | `OnlineLearningConfig` | — | Online pattern learning |

### control.policy

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `evaluation_interval` | `integer` | `60` | Interval in seconds between policy evaluations |
| `default_policy` | `string` | `"pid_thermal"` | Default policy name (e.g. `pid_thermal`, `rule_based`) |

### control.online_learning

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable online learning |
| `buffer_size` | `integer` | `1000` | Maximum sample buffer size before update |
| `update_interval` | `integer` | `3600` | Interval in seconds between model updates (1 hour) |
| `min_samples_for_update` | `integer` | `100` | Minimum number of samples required to start an update |

Example:

```yaml
control:
  policy:
    evaluation_interval: 60
    default_policy: "pid_thermal"
  online_learning:
    enabled: true
    buffer_size: 1000
    update_interval: 3600
    min_samples_for_update: 100
```

---

## observability

System observability: structured logging, Prometheus metrics, distributed tracing and health check.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `logging` | `LoggingConfig` | — | Logging configuration |
| `metrics` | `MetricsConfig` | — | Prometheus metrics exposure |
| `tracing` | `TracingConfig` | — | Distributed tracing (OpenTelemetry) |
| `health_check` | `HealthCheckConfig` | — | HTTP health check endpoint |

### observability.logging

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `level` | `string` | `"INFO"` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `format` | `string` | `"json"` | Log format (`json`, `text`) |
| `output` | `string` | `"stdout"` | Log destination (`stdout`, `stderr`, file path) |

### observability.metrics

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable Prometheus metrics exposure |
| `port` | `integer` | `9090` | Metrics server port |
| `path` | `string` | `"/metrics"` | Metrics endpoint path |

### observability.tracing

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable distributed tracing |
| `exporter` | `string` | `"console"` | Tracing exporter (`console`, `otlp`) |
| `otlp_endpoint` | `string` | `"http://localhost:4317"` | OTLP gRPC endpoint for trace export |
| `sample_rate` | `float` | `0.1` | Trace sampling rate (0.0 – 1.0) |

### observability.health_check

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable health check endpoint |
| `port` | `integer` | `8080` | Health check server port |
| `path` | `string` | `"/health"` | Health check endpoint path |

Example:

```yaml
observability:
  logging:
    level: "INFO"
    format: "json"
    output: "stdout"
  metrics:
    enabled: true
    port: 9090
    path: "/metrics"
  tracing:
    enabled: true
    exporter: "console"
    otlp_endpoint: "http://localhost:4317"
    sample_rate: 0.1
  health_check:
    enabled: true
    port: 8080
    path: "/health"
```

---

## dns

DNS integration for local resolution and rewriting.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pihole_custom_dns` | `string` | `""` | Pi-hole API URL for custom DNS (empty = disabled) |
| `adguard_rewrites` | `string` | `""` | AdGuard Home API URL for DNS rewrites (empty = disabled) |

Example:

```yaml
dns:
  pihole_custom_dns: "http://192.168.1.100/admin/api.php"
  adguard_rewrites: "http://192.168.1.101:80/control/rewrite"
```

---

## traffic_selection

Rules for selecting which traffic to intercept, forward, or block.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_action` | `string` | `"intercept"` | Default action for traffic not covered by rules (`intercept`, `forward`, `block`) |
| `rules` | `array[TrafficRule]` | `[]` | List of traffic selection rules |

### traffic_selection.rules[]

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `string` | `""` | Descriptive rule name |
| `scope` | `string` | `"local"` | Rule scope (`local`, `cloud`) |
| `match_type` | `string` | `"cidr"` | Match type (`cidr`, `hostname`, `port`) |
| `match_value` | `string` | `""` | Value to match (e.g. CIDR, hostname, port) |
| `action` | `string` | `"intercept"` | Action to apply (`intercept`, `forward`, `block`) |
| `priority` | `integer` | `0` | Rule priority (higher values take precedence) |
| `enabled` | `boolean` | `true` | Enable/disable the rule |

Example:

```yaml
traffic_selection:
  default_action: "intercept"
  rules:
    - name: "Shelly cloud traffic"
      scope: "cloud"
      match_type: "hostname"
      match_value: "*.shelly.cloud"
      action: "intercept"
      priority: 10
      enabled: true
    - name: "Trusted local traffic"
      scope: "local"
      match_type: "cidr"
      match_value: "10.0.0.0/8"
      action: "forward"
      priority: 5
      enabled: true
```

---

## llm_decipher

Deciphering unknown protocols or encrypted payloads via Large Language Model.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable LLM-based deciphering |
| `default_profile` | `string` | `"default"` | Default LLM profile name |
| `profiles` | `dict[string, LLMDecipherProfile]` | `{}` | Configurable LLM profiles (key = profile name) |

### llm_decipher.profiles.\<name\>

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `base_url` | `string` | `""` | LLM API base URL (e.g. `https://api.openai.com/v1`) |
| `api_key` | `string` | `""` | API key for LLM access |
| `model_id` | `string` | `""` | LLM model identifier (e.g. `gpt-4o`, `claude-3`) |
| `prompt_template` | `string` | `""` | Prompt template to guide deciphering |

Example:

```yaml
llm_decipher:
  enabled: true
  default_profile: "default"
  profiles:
    default:
      base_url: "https://api.openai.com/v1"
      api_key: "${OPENAI_API_KEY}"
      model_id: "gpt-4o"
      prompt_template: "Decode the following IoT payload into JSON format:\n{payload}"
```

---

## modification

Rules to modify, block, inject, replace, redirect, or delay requests/responses.

Available actions (enum `ModificationAction`):

| Action | Description |
|--------|-------------|
| `modify` | Modify the target field value |
| `block` | Block the request/response |
| `inject` | Inject a new field or payload |
| `replace` | Completely replace the payload |
| `redirect` | Redirect the request to another endpoint |
| `delay` | Delay request processing |

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable the modification system |
| `rules` | `array[ModificationRule]` | `[]` | List of modification rules |

### modification.rules[]

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `string` | `""` | Descriptive rule name |
| `scope` | `string` | `"local"` | Rule scope (`local`, `cloud`) |
| `match_type` | `string` | `"hostname"` | Match type (`hostname`, `path`, `header`, `payload`) |
| `match_value` | `string` | `""` | Value to match |
| `action` | `string` | `"modify"` | Action to perform (`modify`, `block`, `inject`, `replace`, `redirect`, `delay`) |
| `target_field` | `string` | `""` | Payload field to apply the action on |
| `target_value` | `string` | `""` | Value to set for the target field |
| `priority` | `integer` | `0` | Rule priority (higher values take precedence) |
| `enabled` | `boolean` | `true` | Enable/disable the rule |

Example:

```yaml
modification:
  enabled: true
  rules:
    - name: "Override temperature"
      scope: "local"
      match_type: "hostname"
      match_value: "living-room-thermostat"
      action: "modify"
      target_field: "target_temp"
      target_value: "22.0"
      priority: 10
      enabled: true
    - name: "Block dangerous command"
      scope: "local"
      match_type: "path"
      match_value: "/cmd/reboot"
      action: "block"
      priority: 100
      enabled: true
```

---

## correlation

Correlation between requests and responses to reconstruct device-cloud dialog.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable request-response correlation |
| `http` | `CorrelationHTTPConfig` | — | Correlation configuration for HTTP |
| `mqtt` | `CorrelationMQTTConfig` | — | Correlation configuration for MQTT |
| `coap` | `CorrelationCoAPConfig` | — | Correlation configuration for CoAP |
| `store_pairs` | `boolean` | `true` | Enable persistence of correlated pairs to database |
| `max_pairs_per_device` | `integer` | `10000` | Maximum number of pairs kept per device |
| `pair_ttl_hours` | `integer` | `168` | Pair time-to-live in hours (default 7 days) |

### correlation.http

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `method` | `string` | `"connection"` | Correlation method (`connection`, `header`) |
| `correlation_header` | `string` | `"X-Request-ID"` | Name of the header used for correlation |
| `keep_alive_timeout` | `integer` | `30` | Keep-alive timeout in seconds for connection-based correlation |

### correlation.mqtt

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `method` | `string` | `"topic_sequence"` | Correlation method (`topic_sequence`, `packet_id`) |
| `qos_tracking` | `boolean` | `true` | Track MQTT packet QoS for correlation |
| `retain_handling` | `string` | `"include"` | Retained message handling (`include`, `exclude`) |

### correlation.coap

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `method` | `string` | `"message_id"` | Correlation method (`message_id`, `token`) |
| `confirmable_timeout` | `integer` | `5` | Timeout in seconds for CoAP confirmable messages |

Example:

```yaml
correlation:
  enabled: true
  http:
    method: "connection"
    correlation_header: "X-Request-ID"
    keep_alive_timeout: 30
  mqtt:
    method: "topic_sequence"
    qos_tracking: true
    retain_handling: "include"
  coap:
    method: "message_id"
    confirmable_timeout: 5
  store_pairs: true
  max_pairs_per_device: 10000
  pair_ttl_hours: 168
```

---

## learning

Learning modes and thresholds for automatic transition from learning to production.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable the learning system |
| `default_mode` | `string` | `"learning"` | Default operating mode: `learning` (learns), `production` (serves local responses), `hybrid` (mixed) |
| `default_match_threshold` | `float` | `0.85` | Default similarity threshold to consider a pattern matching (0.0 – 1.0) |
| `auto_switch_to_production` | `boolean` | `false` | Automatically switch to production mode when requirements are met |
| `min_patterns_for_production` | `integer` | `10` | Minimum number of patterns required to enter production mode |
| `min_match_rate_for_production` | `float` | `80.0` | Minimum match rate percentage required to enter production (0.0 – 100.0) |
| `production_no_fallback` | `boolean` | `false` | If `true`, in production mode requests with no local response return an error instead of falling back to the cloud |
| `signal_forward_to_cloud` | `boolean` | `false` | If `true`, requests with no response in production/hybrid mode return an `X-Action: forward` header instead of calling `adapter.forward_to_cloud()` internally. Intended for deployments behind a reverse proxy |

Example:

```yaml
learning:
  enabled: true
  default_mode: "learning"
  default_match_threshold: 0.85
  auto_switch_to_production: false
  min_patterns_for_production: 10
  min_match_rate_for_production: 80.0
  production_no_fallback: false
  signal_forward_to_cloud: false
```

---

## tls_decrypt

TLS decryption (MITM) to intercept encrypted traffic.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Enable TLS decryption (disabled by default) |
| `listen_ports` | `array[integer]` | `[443, 8883, 5684, 8443]` | Ports on which to intercept TLS traffic |
| `ca_cert_path` | `string` | `"./certs/ca.pem"` | Path to the CA certificate used to sign MITM certificates |
| `ca_key_path` | `string` | `"./certs/ca.key"` | Path to the CA private key |
| `device_certs_dir` | `string` | `"./data/device_certs"` | Directory for per-device generated certificates |
| `external_certs_dir` | `string` | `"./data/external_certs"` | Directory for imported external certificates |
| `pinning_bypass` | `dict[string, PinningBypassConfig]` | `{}` | Certificate pinning bypass strategies per vendor (key = vendor name) |
| `min_tls_version` | `string` | `"TLSv1.2"` | Minimum accepted TLS version |
| `max_tls_version` | `string` | `"TLSv1.3"` | Maximum accepted TLS version |

### tls_decrypt.pinning_bypass.\<vendor\>

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `strategy` | `string` | `"mitm_proxy"` | Bypass strategy: `mitm_proxy` (standard MITM proxy), `frida` (Frida hook), `disable_pin_check` (disable verification) |

Example:

```yaml
tls_decrypt:
  enabled: false
  listen_ports:
    - 443
    - 8883
    - 5684
    - 8443
  ca_cert_path: "./certs/ca.pem"
  ca_key_path: "./certs/ca.key"
  device_certs_dir: "./data/device_certs"
  external_certs_dir: "./data/external_certs"
  pinning_bypass:
    shelly:
      strategy: "mitm_proxy"
    nest:
      strategy: "frida"
  min_tls_version: "TLSv1.2"
  max_tls_version: "TLSv1.3"
```

---

## protocol_servers

Servers for additional protocols beyond HTTP. Each server is disabled by default.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mqtt` | `MQTTServerConfig` | — | MQTT broker/server |
| `coap` | `CoAPServerConfig` | — | CoAP server |
| `modbus` | `ModbusServerConfig` | — | Modbus TCP server |
| `websocket` | `WebSocketServerConfig` | — | WebSocket server |
| `raw_tcp` | `RawTCPServerConfig` | — | Raw TCP server |
| `http2` | `HTTP2ServerConfig` | — | HTTP/2 server (h2/h2c) |
| `zigbee_bridge` | `ZigbeeBridgeConfig` | — | Zigbee bridge (Zigbee2MQTT) |
| `zwave_bridge` | `ZWaveBridgeConfig` | — | Z-Wave bridge (Z-Wave JS UI) |
| `matter_bridge` | `MatterBridgeConfig` | — | Matter bridge (Matter.js) |

---

### protocol_servers.mqtt

Embedded MQTT broker server.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Enable the MQTT server |
| `host` | `string` | `"0.0.0.0"` | Listen address |
| `port` | `integer` | `1883` | Unencrypted MQTT port |
| `port_tls` | `integer` | `8883` | MQTT TLS port |
| `tls_enabled` | `boolean` | `false` | Enable TLS for MQTT |
| `max_packet_size` | `integer` | `268435` | Maximum MQTT packet size in bytes (256 KB) |
| `topic_filters` | `array[string]` | `["#"]` | MQTT topic filters to intercept (all by default) |

---

### protocol_servers.coap

CoAP (Constrained Application Protocol) server.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Enable the CoAP server |
| `host` | `string` | `"0.0.0.0"` | Listen address |
| `port` | `integer` | `5683` | Unencrypted CoAP port |
| `dtls_enabled` | `boolean` | `false` | Enable DTLS for CoAP |
| `dtls_port` | `integer` | `5684` | DTLS port |
| `max_pdu_size` | `integer` | `1024` | Maximum CoAP PDU size in bytes |

---

### protocol_servers.modbus

Modbus TCP server.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Enable the Modbus server |
| `host` | `string` | `"0.0.0.0"` | Listen address |
| `port` | `integer` | `502` | Modbus TCP port |
| `unit_id` | `integer` | `1` | Default Modbus unit ID |
| `tls_enabled` | `boolean` | `false` | Enable Modbus over TLS (Modbus Security) |
| `tls_port` | `integer` | `802` | Modbus Security port |
| `holding_registers` | `dict[string, integer]` | `{}` | Name → address map for default holding registers |
| `coil_registers` | `dict[string, integer]` | `{}` | Name → address map for default coil registers |

---

### protocol_servers.websocket

WebSocket server.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Enable the WebSocket server |
| `host` | `string` | `"0.0.0.0"` | Listen address |
| `port` | `integer` | `9000` | WebSocket port |
| `path` | `string` | `"/ws"` | WebSocket endpoint path |
| `max_message_size` | `integer` | `1048576` | Maximum WebSocket message size in bytes (1 MB) |

---

### protocol_servers.raw_tcp

Raw TCP server for non-standard protocols.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Enable the raw TCP server |
| `host` | `string` | `"0.0.0.0"` | Listen address |
| `port` | `integer` | `9100` | Raw TCP port |
| `buffer_size` | `integer` | `4096` | Read buffer size in bytes |
| `idle_timeout` | `integer` | `300` | Idle timeout in seconds (5 minutes) |
| `protocol_detect` | `boolean` | `true` | Attempt automatic protocol detection |

---

### protocol_servers.http2

HTTP/2 server (supports both h2 and h2c).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Enable the HTTP/2 server |
| `host` | `string` | `"0.0.0.0"` | Listen address |
| `port` | `integer` | `443` | HTTP/2 with TLS (h2) port |
| `cleartext_port` | `integer` | `8080` | HTTP/2 cleartext (h2c) port |
| `tls_enabled` | `boolean` | `true` | Enable TLS for HTTP/2 |

---

### protocol_servers.zigbee_bridge

Bridge for Zigbee devices via Zigbee2MQTT.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Enable the Zigbee bridge |
| `mqtt_host` | `string` | `"localhost"` | Zigbee2MQTT MQTT host |
| `mqtt_port` | `integer` | `1883` | Zigbee2MQTT MQTT port |
| `mqtt_user` | `string` | `""` | MQTT user (empty = no authentication) |
| `mqtt_pass` | `string` | `""` | MQTT password |
| `topic_prefix` | `string` | `"zigbee2mqtt"` | Zigbee2MQTT MQTT topic prefix |
| `reconnect_interval` | `integer` | `10` | Reconnection interval in seconds |

---

### protocol_servers.zwave_bridge

Bridge for Z-Wave devices via Z-Wave JS UI.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Enable the Z-Wave bridge |
| `connection_type` | `string` | `"mqtt"` | Connection type: `mqtt` or `ws` (WebSocket) |
| `host` | `string` | `"localhost"` | Z-Wave JS UI server host |
| `port` | `integer` | `1883` | MQTT port (used if `connection_type: mqtt`) |
| `ws_port` | `integer` | `3000` | WebSocket port (used if `connection_type: ws`) |
| `mqtt_user` | `string` | `""` | MQTT user (empty = no authentication) |
| `mqtt_pass` | `string` | `""` | MQTT password |

---

### protocol_servers.matter_bridge

Bridge for Matter devices via Matter.js.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Enable the Matter bridge |
| `controller_port` | `integer` | `5540` | Matter.js controller port |
| `fabric_id` | `integer` | `1` | Matter fabric ID |
| `vendor_id` | `integer` | `65521` | Matter vendor ID (0xFFF1 = test/development) |

---

Complete `protocol_servers` example:

```yaml
protocol_servers:
  mqtt:
    enabled: false
    host: "0.0.0.0"
    port: 1883
    port_tls: 8883
    tls_enabled: false
    max_packet_size: 268435
    topic_filters:
      - "#"
  coap:
    enabled: false
    host: "0.0.0.0"
    port: 5683
    dtls_enabled: false
    dtls_port: 5684
    max_pdu_size: 1024
  modbus:
    enabled: false
    host: "0.0.0.0"
    port: 502
    unit_id: 1
    tls_enabled: false
    tls_port: 802
    holding_registers: {}
    coil_registers: {}
  websocket:
    enabled: false
    host: "0.0.0.0"
    port: 9000
    path: "/ws"
    max_message_size: 1048576
  raw_tcp:
    enabled: false
    host: "0.0.0.0"
    port: 9100
    buffer_size: 4096
    idle_timeout: 300
    protocol_detect: true
  http2:
    enabled: false
    host: "0.0.0.0"
    port: 443
    cleartext_port: 8080
    tls_enabled: true
  zigbee_bridge:
    enabled: false
    mqtt_host: "localhost"
    mqtt_port: 1883
    mqtt_user: ""
    mqtt_pass: ""
    topic_prefix: "zigbee2mqtt"
    reconnect_interval: 10
  zwave_bridge:
    enabled: false
    connection_type: "mqtt"
    host: "localhost"
    port: 1883
    ws_port: 3000
    mqtt_user: ""
    mqtt_pass: ""
  matter_bridge:
    enabled: false
    controller_port: 5540
    fabric_id: 1
    vendor_id: 65521
```

---

## Complete example

```yaml
# config/config.yaml — ride-the-api
core:
  database_url: "sqlite+aiosqlite:///./data/core.db"
  device_db_dir: "./data/devices"
  device_databases: {}
  default_context_buffer_size: 524288

proxy:
  host: "0.0.0.0"
  port: 8911
  tls:
    enabled: true
    cert_file: "./certs/ride-api.pem"
    key_file: "./certs/ride-api.key"
  request_timeout: 30
  max_request_size: 1048576
  fallback:
    enabled: true
    timeout: 10
    retry_count: 2
    confidence_threshold: 0.7

vendors: {}

models:
  registry_path: "models"
  defaults:
    ac: "example_ac_v1.onnx"
    heat_pump: "example_hp_v1.onnx"
  inference:
    batch_size: 1
    intra_op_threads: 2
    inter_op_threads: 2
    execution_providers:
      - "CPUExecutionProvider"
  hot_reload:
    enabled: true
    check_interval: 30

control:
  policy:
    evaluation_interval: 60
    default_policy: "pid_thermal"
  online_learning:
    enabled: true
    buffer_size: 1000
    update_interval: 3600
    min_samples_for_update: 100

observability:
  logging:
    level: "INFO"
    format: "json"
    output: "stdout"
  metrics:
    enabled: true
    port: 9090
    path: "/metrics"
  tracing:
    enabled: true
    exporter: "console"
    otlp_endpoint: "http://localhost:4317"
    sample_rate: 0.1
  health_check:
    enabled: true
    port: 8080
    path: "/health"

dns:
  pihole_custom_dns: ""
  adguard_rewrites: ""

traffic_selection:
  default_action: "intercept"
  rules: []

llm_decipher:
  enabled: true
  default_profile: "default"
  profiles: {}

modification:
  enabled: true
  rules: []

correlation:
  enabled: true
  http:
    method: "connection"
    correlation_header: "X-Request-ID"
    keep_alive_timeout: 30
  mqtt:
    method: "topic_sequence"
    qos_tracking: true
    retain_handling: "include"
  coap:
    method: "message_id"
    confirmable_timeout: 5
  store_pairs: true
  max_pairs_per_device: 10000
  pair_ttl_hours: 168

learning:
  enabled: true
  default_mode: "learning"
  default_match_threshold: 0.85
  auto_switch_to_production: false
  min_patterns_for_production: 10
  min_match_rate_for_production: 80.0
  production_no_fallback: false
  signal_forward_to_cloud: false

tls_decrypt:
  enabled: false
  listen_ports:
    - 443
    - 8883
    - 5684
    - 8443
  ca_cert_path: "./certs/ca.pem"
  ca_key_path: "./certs/ca.key"
  device_certs_dir: "./data/device_certs"
  external_certs_dir: "./data/external_certs"
  pinning_bypass: {}
  min_tls_version: "TLSv1.2"
  max_tls_version: "TLSv1.3"

protocol_servers:
  mqtt:
    enabled: false
    host: "0.0.0.0"
    port: 1883
    port_tls: 8883
    tls_enabled: false
    max_packet_size: 268435
    topic_filters: ["#"]
  coap:
    enabled: false
    host: "0.0.0.0"
    port: 5683
    dtls_enabled: false
    dtls_port: 5684
    max_pdu_size: 1024
  modbus:
    enabled: false
    host: "0.0.0.0"
    port: 502
    unit_id: 1
    tls_enabled: false
    tls_port: 802
    holding_registers: {}
    coil_registers: {}
  websocket:
    enabled: false
    host: "0.0.0.0"
    port: 9000
    path: "/ws"
    max_message_size: 1048576
  raw_tcp:
    enabled: false
    host: "0.0.0.0"
    port: 9100
    buffer_size: 4096
    idle_timeout: 300
    protocol_detect: true
  http2:
    enabled: false
    host: "0.0.0.0"
    port: 443
    cleartext_port: 8080
    tls_enabled: true
  zigbee_bridge:
    enabled: false
    mqtt_host: "localhost"
    mqtt_port: 1883
    mqtt_user: ""
    mqtt_pass: ""
    topic_prefix: "zigbee2mqtt"
    reconnect_interval: 10
  zwave_bridge:
    enabled: false
    connection_type: "mqtt"
    host: "localhost"
    port: 1883
    ws_port: 3000
    mqtt_user: ""
    mqtt_pass: ""
  matter_bridge:
    enabled: false
    controller_port: 5540
    fabric_id: 1
    vendor_id: 65521
```

---

> Document automatically generated from the `Config` model in `/config/ride-the-api/core/config.py`.
> Last updated: based on `core/config.py` — all Pydantic classes and their defaults.