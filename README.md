# Ride-the-API — Local Cloud Replacement Proxy

A DNS interception proxy that sits between IoT devices and their cloud APIs, **learns the device protocol** via LLM analysis, and **serves responses locally** — making devices fully functional even if the vendor shuts down its cloud servers.

## Architecture

```
┌──────────────┐     ┌──────────────────────────────────────────────────────────────────────────────┐       ┌──────────┐
│  IoT Device  │────▶│  nginx (reverse proxy sidecar) / TLS MITM Server       ┌──────────────────┐  │────▶│  Vendor  │
│  (Any Brand) │     │  ┌──────────────────────────────────────────────────── │  Ride-the-API    │  │      │  Cloud   │
│              │     │  │  ● nginx: port 443 (TLS) ↔ port 8911 (internal)     │  (FastAPI)       │  │      └──────────┘
│              │     │  │  ● TLS MITM Server: multi-port TLS interception     │  Core Pipeline   │  │
│              │     │  │    with SNI extraction + dynamic cert generation     │  ┌───────────┐  │  │
│              │     │  │  ● Dual-stack DNS resolver: 8.8.8.8/1.1.1.1+IPv6    │  │ resilience │  │  │
│              │     │  │  ● 502 + X-Action: forward → loop-free forwarding   │  │ .py        │  │  │
│              │     │  └──────────────────────────────────────────────────── │  └───────────┘  │  │
│              │     │                                                       │                 │  │
│              │     │  ┌─────────────────────────────────────────────────── │  ┌───────────┐  │  │
│              │     │  │            LEARNING MODE                           │  │ pattern_db│  │  │
│              │     │  │  Request → Correlate → Buffer → LLM → Patterns     │  │ engine    │  │  │
│              │     │  └─────────────────────────────────────────────────── │  └───────────┘  │  │
│              │     │                                                       │                 │  │
│              │     │  ┌─────────────────────────────────────────────────── │  ┌───────────┐  │  │
│              │     │  │           PRODUCTION MODE                          │  │ protocol  │  │  │
│              │     │  │  Request → Match Pattern → Local Response          │  │ _servers  │  │  │
│              │     │  │     ↓ (if no match + no_fallback)                  │  └───────────┘  │  │
│              │     │  │  Return 501 (conclusive local-only response)       │                 │  │
│              │     │  └─────────────────────────────────────────────────── │                 │  │
│              │     │                                                       │                 │  │
│              │     │  ┌─────────────────────────────────────────────────── │  ┌───────────┐  │  │
│              │     │  │  Auto-Switch ── Match Rate ≥ 99% → Production     │  │ cert      │  │  │
│              │     │  │  Rollback ──── Match Rate < 90% → Learning        │  │ _manager  │  │  │
│              │     │  │  Real-time tracking: hits / (hits + misses) * 100  │  └───────────┘  │  │
│              │     │  └───────────────────────────────────────────────────┴──────────────────┘  │
│              │     └──────────────────────────────────────────────────────────────────────────────┘
└──────────────┘
```

## Key Concept

### No vendor lock-in, no brand references.
- **Device-specific databases** — every device gets its own protocol DB
- **Protocol learning** — the system watches device↔cloud communication and deciphers the protocol
- **Local response** — once learned, the device talks to the local proxy instead of the cloud
- **Resilience** — works even if the vendor shuts down its servers or blocks your region

## Features

### Learning Pipeline
- Intercepts device requests and cloud responses (via HTTP proxy + TLS MITM Server)
- Correlates request/response pairs via connection tracking, sequence numbers, and correlation IDs
- Buffers pairs in a configurable sliding window (128KB to 10MB)
- Sends batch to LLM for protocol analysis
- Saves learned patterns (request schemas, response templates, field mappings)

### Production Pipeline
- Matches incoming requests against learned patterns
- Calculates similarity score (path, method, headers, body, query params)
- If score ≥ threshold → serves response from local database with **state-aware pattern engine**
- If score < threshold and `production_no_fallback` is enabled → returns 501 (conclusive local-only response)
- If score < threshold and `signal_forward_to_cloud` is enabled → signals nginx to forward to the real cloud via a dual-stack resolver (8.8.8.8/1.1.1.1 + IPv6), bypassing local DNS to prevent forwarding loops
- If score < threshold and neither flag is set → forwards to cloud via `adapter.forward_to_cloud()`, captures and learns from the miss
- Real-time match rate tracking: `hits / (hits + misses) * 100%`

### Auto-Switch & Rollback
- **Auto-switch**: background scheduler checks every 60s — when match rate reaches 99% (≥ 10 patterns, ≥ 50 total requests), automatically switches device to production mode
- **Rollback**: if match rate drops below 90% in production, automatically reverts to learning mode
- **Cloud Independence Verifier**: REST API to check if a device can function without vendor cloud
- Per-device toggle to enable/disable auto-switch, configurable thresholds

### TLS MITM Server
- **Multi-port TLS interception**: listens on configurable ports (default 8443, 9443, 10443, 11443, 12443, 13443, 14443, 15443, 16443, 17443, 18443, 19443, 443)
- **SNI extraction**: parses raw TLS ClientHello to extract the target hostname without completing the handshake
- **Dynamic certificate generation**: generates per-hostname certificates on-the-fly via CertManager
- **IP-first device routing**: device identity is determined by source IP, not port or hostname
- **Auto-registration**: unknown IPs are auto-registered with a dedicated device DB + passthrough=ON
- **REST API**: manage ports, view stats, list unidentified devices, download CA cert
- **Frida script integration**: dynamic instrumentation script for devices that pin certificates (`GET /api/tls/frida/script.js`)

### Portable Pattern Database
- Two portable databases per device: **Buffer DB** (raw captures) and **Deciphered DB** (learned patterns)
- **Client section**: what the device sends (endpoints, body schemas, auth, firmware variants)
- **Server section**: response templates, field mappings, state variables, virtual sensors
- **State variables**: persistent device state across requests (power, mode, temperature)
- **Virtual sensors**: simulated data — static, drift (random walk), periodic (sine wave), random
- **Template variables**: `{state.varname}`, `{request.body.path}`, `{uuid}` resolved at response time
- **Field transforms**: direct, enum (value mapping), formula (eval context-aware)
- **Export/import**: `.ride-capture.json` and `.ride-pattern.json` portable format — shareable, LLM-agnostic, cross-hardware
- **JSON Schema validation** — imported files are validated against `capture-schema-v1.json` and `pattern-schema-v1.json` with protocol-aware checks for all 11 supported protocols (HTTP, MQTT, CoAP, Modbus, WebSocket, Raw TCP, HTTP/2, Zigbee, Z-Wave, Matter)
- See [design doc](docs/portable-pattern-database.md) for full schema and examples

### Configurable LLM
- Per-device LLM configuration: `base_url`, `model_id`, optional `profile_name`
- Supports OpenAI-compatible APIs, local Ollama, vLLM, etc.
- Context buffer size configurable per device

### Multi-Protocol Server Platform
- Direct protocol listeners for **MQTT** (port 1883 / 8883 TLS), **CoAP** (5683 / 5684 DTLS), **Modbus TCP** (502 / 802 TLS), **WebSocket** (9000), **Raw TCP** (9100), and **HTTP/2** (443 / 8080 h2c)
- Protocol bridge plugins for **Zigbee** (via Zigbee2MQTT), **Z-Wave** (via Z-Wave JS UI), and **Matter** (via Matter.js)
- All servers managed via `ProtocolServerManager` — unified start/stop/status lifecycle
- REST API for runtime protocol server control
- Example protocol adapters: **CoAP**, **Modbus**, **Shelly**

### TLS Certificate Management
- **Certificate upload**: import PEM cert+key pairs per hostname via file upload or JSON
- **Certificate lifecycle**: list, inspect, delete, and rotate TLS certificates
- **MITM CA download**: retrieve the root CA certificate for device trust configuration
- External certs stored at `data/external_certs/{hostname}/`

### Dashboard
- Real-time web UI at `http://localhost:8911/`
- Per-device match rate, pattern count, buffer fill level
- Switch between learning and production mode
- Configure LLM settings per device

### Pattern Editor
- Web UI at `http://localhost:8911/patterns/{device_id}`
- View, create, edit, and delete request patterns with a dark-theme editor
- Expandable cards showing method, path, headers, body schema, query params, response template, and field mappings
- Tag-style inputs for headers, query params, expected variables
- JSON editors for body schema, body template, headers template
- Real-time save feedback with toast notifications

## Quick Start

### Prerequisites
- Python 3.11+
- DNS server (dnsmasq, Pi-hole, or AdGuard Home) for device routing
- An LLM API (OpenAI, Ollama, etc.)

### DNS Interception Setup

```bash
# /etc/dnsmasq.d/ride-api.conf
address=/mqtt.example.com/192.168.1.100
address=/api.example.com/192.168.1.100
```

Restart your DNS server: `sudo systemctl restart dnsmasq`

### Docker Deployment (recommended)

Run the full stack (nginx sidecar + Ride-the-API) with Docker Compose:

```bash
docker compose -f deploy/docker-compose.yml up -d
```

This starts:
- **nginx** on ports 80/443 (TLS) and 8883 (MQTT over TLS) — the stable entry point for devices
- **Ride-the-API** on internal port 8911 (not exposed publicly)

nginx resolves cloud hostnames via a dedicated dual-stack resolver (8.8.8.8 / 1.1.1.1, IPv6 enabled), bypassing the local DNS to prevent forwarding loops.

### Manual Deployment

```bash
git clone https://github.com/thcuba/Ride-the-api.git
cd Ride-the-api
pip install -e .
# Edit config/config.yaml — set LLM profile, enable production mode, configure traffic rules
python -m core.server
```

Open `http://localhost:8911/` in your browser to see the dashboard.

> When running without nginx, forward-to-cloud behavior depends on `signal_forward_to_cloud` and `production_no_fallback` in `config.yaml`. With the Docker stack, nginx handles loop-free cloud forwarding automatically.

### Systemd Service (Linux)

```bash
sudo cp deploy/ride-the-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ride-the-api
sudo systemctl start ride-the-api
```

### Configure LLM

Edit `config/config.yaml` to set your LLM API endpoint:

```yaml
llm_decipher:
  profiles:
    default:
      base_url: "https://api.openai.com/v1"
      api_key: "${OPENAI_API_KEY}"
      model_id: "gpt-4o-mini"
```

For local Ollama:
```yaml
    local_ollama:
      base_url: "http://localhost:11434/v1"
      api_key: "ollama"
      model_id: "llama3.1:8b"
```

### Protocol Server Configuration

Protocol servers are opt-in (disabled by default). Enable them in `config/config.yaml`:

```yaml
protocol_servers:
  mqtt:
    enabled: true
    host: "0.0.0.0"
    port: 1883
  coap:
    enabled: true
    host: "0.0.0.0"
    port: 5683
  modbus:
    enabled: true
    host: "0.0.0.0"
    port: 502
  websocket:
    enabled: true
    host: "0.0.0.0"
    port: 9000
  raw_tcp:
    enabled: true
    host: "0.0.0.0"
    port: 9100
  http2:
    enabled: true
    host: "0.0.0.0"
    port: 443
    cleartext_port: 8080
  # Bridge plugins (connect via external software)
  zigbee_bridge:
    enabled: true
    mqtt_host: "localhost"
    mqtt_port: 1883
    topic_prefix: "zigbee2mqtt"
  zwave_bridge:
    enabled: true
    connection_type: "mqtt"
    host: "localhost"
    port: 1883
  matter_bridge:
    enabled: true
    controller_port: 5540
```

## How It Works

### 1. Learning Mode (default)
Every request from the device is forwarded to the real cloud. The proxy:
1. Registers the outgoing request with a correlation key
2. Captures the cloud response and matches it to the request
3. Adds the correlated pair to the context buffer
4. When the buffer reaches the configured size, flushes to LLM for batch analysis
5. LLM decodes the protocol patterns and saves them to the device database
6. Buffer and correlation cache are cleared

### 2. Production Mode

The proxy handles requests locally:

1. Incoming request is matched against learned patterns
2. Similarity score is calculated (method, path, headers, body, query params)
3. If score ≥ threshold (default 85%): response is built from template with field mappings
4. If score < threshold:
   - `production_no_fallback` enabled → returns HTTP 501 (conclusive local-only response)
   - `signal_forward_to_cloud` enabled → returns HTTP 502 + `X-Action: forward` header; nginx catches the 502 and proxies to the real cloud via a dedicated dual-stack resolver (8.8.8.8 / 1.1.1.1 + IPv6), avoiding DNS loops
   - Neither flag set → forward via `adapter.forward_to_cloud()`, capture the miss, learn from it
5. Match rate is updated in real-time

### Context Buffer
- Configurable per device: 128KB, 256KB, 512KB, 1MB, 2MB, 5MB, 10MB
- Acts as a sliding window — accumulates pairs until threshold is reached
- On flush: all pairs sent to LLM for analysis, then buffer cleared
- Correlation cache is also cleared after flush

### 3. TLS MITM Interception

Ride-the-API includes a built-in TLS MITM Server for intercepting encrypted device traffic without requiring a separate nginx proxy:

1. **Listen**: starts on configurable TLS ports (default: 8443, 9443, …)
2. **SNI extraction**: parses the TLS ClientHello to extract the target hostname
3. **Dynamic cert generation**: generates a per-hostname certificate signed by the local CA
4. **Termination**: completes the TLS handshake with the device using the generated cert
5. **Routing**: passes the decrypted HTTP request to the pipeline, where device identity is determined by **source IP**
6. **Auto-registration**: unknown IPs are automatically registered with a device DB in passthrough mode

### 4. Auto-Switch & Rollback

In the background, every 60 seconds the system evaluates all devices:

- **Auto-switch to production**: devices in learning mode with `auto_switch_enabled=True`, match rate ≥ 99%, ≥ 10 patterns, and ≥ 50 total requests are automatically switched to production
- **Rollback to learning**: devices in production mode whose match rate drops below 90% are reverted to learning
- **Independence check**: REST API (`GET /api/independence/{id}`) evaluates whether a device can function without vendor cloud

## Project Structure

```
├── adapters/
│   ├── base/              # Abstract ProtocolAdapter interface
│   ├── example/           # Reference implementation
│   ├── coap/              # CoAP protocol adapter example
│   ├── modbus/            # Modbus protocol adapter example
│   ├── shelly/            # Shelly smart home adapter
│   └── __init__.py        # Adapter registry
├── core/
│   ├── server.py          # FastAPI server + dashboard + API endpoints
│   ├── config.py          # Configuration management
│   ├── cert_manager.py    # TLS CA + device cert generation + external cert management
│   ├── database.py        # Device-specific protocol DB models + manager
│   ├── pipeline.py        # Learning/production orchestrator, correlator, buffer, matcher
│   ├── llm_decipher.py    # LLM analysis service
│   ├── modification.py    # Request/response modification
│   ├── traffic_selector.py # Intercept/passthrough rules
│   ├── upstream_resolver.py # Dual-stack upstream DNS resolver (loop-free)
│   ├── resilience.py      # Auto-switch scheduler + cloud independence verifier
│   ├── tls_mitm.py        # Multi-port TLS MITM interception server
│   ├── protocol_servers/  # IoT/industrial protocol server plugins
│   │   ├── __init__.py    # ProtocolServerPlugin base + ProtocolServerManager
│   │   ├── mqtt_server.py      # MQTT broker plugin
│   │   ├── coap_server.py      # CoAP server plugin
│   │   ├── modbus_server.py    # Modbus TCP server plugin
│   │   ├── websocket_server.py # WebSocket server plugin
│   │   ├── raw_tcp_server.py   # Raw TCP server plugin
│   │   ├── http2_server.py     # HTTP/2 server plugin
│   │   ├── zigbee_bridge.py    # Zigbee2MQTT bridge
│   │   ├── zwave_bridge.py     # Z-Wave JS UI bridge
│   │   └── matter_bridge.py    # Matter.js bridge
│   └── pattern_db/        # Portable pattern database engine
│       ├── __init__.py    # Package init + Pydantic schemas
│       ├── schemas.py     # CaptureDB & PatternDB Pydantic models
│       ├── schemas/       # JSON Schema files for portable format validation
│       │   ├── capture-schema-v1.json
│       │   └── pattern-schema-v1.json
│       ├── validator.py   # JSON Schema validation for import
│       ├── state_manager.py   # Device state + virtual sensor simulation
│       ├── buffer_manager.py  # Buffer accumulation + export/import
│       ├── decipher_ingest.py # LLM output → pattern DB records
│       └── pattern_engine.py  # Pattern matching + state-aware response builder
├── config/
│   └── config.yaml        # Main configuration
├── tests/
├── deploy/
│   ├── nginx.conf          # nginx reverse proxy configuration
│   ├── docker-compose.yml  # Docker Compose (nginx + ride-the-api)
│   ├── Dockerfile          # Multi-stage Dockerfile (CPU + GPU)
│   └── ride-the-api.service # systemd service unit
└── docs/
```

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web dashboard |
| `GET /health` | Health check |
| `GET /api/devices` | List all devices |
| `GET /api/devices/{id}` | Device details + stats |
| `GET /api/devices/{id}/stats` | Real-time match statistics |
| `GET /api/devices/{id}/match-rate` | Match rate percentage |
| `GET /api/devices/by-ip/{ip}` | Find device by source IP |
| `POST /api/devices/{id}/mode` | Switch learning/production |
| `POST /api/devices/{id}/ip` | Update device IP mapping |
| `PUT /api/devices/{id}/llm` | Configure LLM for device |
| `PUT /api/devices/{id}/auto-switch` | Enable/disable auto-switch per device |
| `PUT /api/devices/{id}/tls-config` | Update device TLS interception config |
| `POST /api/devices/{id}/database` | Create/manage device protocol DB |
| `GET /api/devices/{id}/database` | Get device database info |
| `GET /api/databases` | List all device databases |
| `GET /api/llm/profiles` | Available LLM profiles |
| `GET /patterns/{id}` | Web UI for manual pattern editing |
| `GET /api/devices/{id}/patterns` | Learned patterns |
| `GET /api/devices/{id}/patterns/{pid}` | Pattern detail + field mappings |
| `PUT /api/devices/{id}/patterns/{pid}` | Create or full-update a pattern (upsert) |
| `PATCH /api/devices/{id}/patterns/{pid}` | Partial update of a pattern |
| `DELETE /api/devices/{id}/patterns/{pid}` | Delete pattern + response template + field mappings |
| `GET /api/devices/{id}/patterns/export` | Export deciphered patterns (.ride-pattern.json) |
| `POST /api/devices/{id}/patterns/import` | Import patterns from .ride-pattern.json |
| `GET /api/devices/{id}/capture/export` | Export raw buffer (.ride-capture.json) |
| `POST /api/devices/{id}/capture/import` | Import raw pairs from .ride-capture.json |
| `GET /api/protocol-servers` | List all protocol servers with status |
| `POST /api/protocol-servers/{name}/start` | Start a protocol server |
| `POST /api/protocol-servers/{name}/stop` | Stop a protocol server |
| `GET /api/protocol-servers/{name}/config` | Get protocol server configuration |
| `GET /api/tls/ca-cert` | Download MITM CA certificate (PEM) |
| `GET /api/tls/stats` | TLS interception statistics |
| `GET /api/tls/ports` | List configured TLS listen ports |
| `POST /api/tls/ports` | Add a TLS listen port |
| `DELETE /api/tls/ports/{port}` | Remove a TLS listen port |
| `GET /api/tls/device-ports` | Device-to-port mapping |
| `GET /api/tls/unidentified` | List unidentified devices (unknown IPs) |
| `GET /api/tls/frida/script.js` | Frida dynamic instrumentation script |
| `POST /api/tls/certs/upload` | Upload TLS certificate + key (multipart) |
| `POST /api/tls/certs/upload-json` | Upload TLS certificate + key (JSON) |
| `GET /api/tls/certs` | List all imported TLS certificates |
| `GET /api/tls/certs/{hostname}` | Inspect a TLS certificate |
| `DELETE /api/tls/certs/{hostname}` | Delete an imported certificate |
| `POST /api/tls/certs/{hostname}/rotate` | Regenerate a device leaf certificate |
| `POST /api/tls/root-ca/download` | Download the MITM root CA certificate |
| `GET /api/independence` | Check cloud independence for all devices |
| `GET /api/independence/{id}` | Check cloud independence for a device |
| `POST /api/independence/{id}/auto-switch` | Trigger auto-switch to production |
| `GET /api/independence/{id}/export` | Export learned patterns for backup |
| `POST /api/independence/{id}/import` | Import patterns from backup |
| `/{vendor}/{path:path}` | Proxy endpoint for device traffic |

YES. it is all vibe coded i'm searching for people who like this idea and would like to help implement it
## License

MIT
