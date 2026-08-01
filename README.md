# Ride-the-API — Local Cloud Replacement Proxy

A DNS interception proxy that sits between IoT devices and their cloud APIs, **learns the device protocol** via LLM analysis, and **serves responses locally** — making devices fully functional even if the vendor shuts down its cloud servers.

## Architecture

```
┌──────────────┐     ┌──────────────────────────────────────────────────────┐     ┌──────────┐
│  IoT Device  │────▶│  Ride-the-API (Local Cloud Replacement Proxy)        │────▶│  Vendor  │
│  (Any Brand) │     │                                                      │     │  Cloud   │
│              │     │  ┌─────────────────────────────────────────────────  │     │(Fallback)│
│              │     │  │            LEARNING MODE                       │  │     └──────────┘
│              │     │  │  Request → Correlate → Buffer → LLM → Patterns │  │           │
│              │     │  └─────────────────────────────────────────────────  │           │
│              │     │                                                      │           │
│              │     │  ┌─────────────────────────────────────────────────  │           │
│              │     │  │           PRODUCTION MODE                      │  │           │
│              │     │  │  Request → Match Pattern → Local Response      │  │           │
│              │     │  │     ↓ (if no match)                            │  │           │
│              │     │  │  Forward to Cloud → Learn → Improve            │  │           │
│              │     │  └─────────────────────────────────────────────────  │           │
│              │     │                                                      │           │
│              │     │  Match Rate  ◀── Real-time tracking                  │           │
│              │     └──────────────────────────────────────────────────────┘           │
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
- Intercepts device requests and cloud responses
- Correlates request/response pairs via connection tracking, sequence numbers, and correlation IDs
- Buffers pairs in a configurable sliding window (128KB to 10MB)
- Sends batch to LLM for protocol analysis
- Saves learned patterns (request schemas, response templates, field mappings)

### Production Pipeline
- Matches incoming requests against learned patterns
- Calculates similarity score (path, method, headers, body, query params)
- If score ≥ threshold → serves response from local database with **state-aware pattern engine**
- If score < threshold → forwards to cloud, captures and learns from the miss
- Real-time match rate tracking: `hits / (hits + misses) * 100%`

### Portable Pattern Database
- Two portable databases per device: **Buffer DB** (raw captures) and **Deciphered DB** (learned patterns)
- **Client section**: what the device sends (endpoints, body schemas, auth, firmware variants)
- **Server section**: response templates, field mappings, state variables, virtual sensors
- **State variables**: persistent device state across requests (power, mode, temperature)
- **Virtual sensors**: simulated data — static, drift (random walk), periodic (sine wave), random
- **Template variables**: `{state.varname}`, `{request.body.path}`, `{uuid}` resolved at response time
- **Field transforms**: direct, enum (value mapping), formula (eval context-aware)
- **Export/import**: `.ride-capture.json` and `.ride-pattern.json` portable format — shareable, LLM-agnostic, cross-hardware
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

## Quick Start

### Prerequisites
- Python 3.11+
- DNS server (dnsmasq, Pi-hole, or AdGuard Home) for device routing
- An LLM API (OpenAI, Ollama, etc.)

### Installation

```bash
git clone https://github.com/thcuba/Ride-the-api.git
cd Ride-the-api
pip install -e .
cp config/config.example.yaml config/config.yaml
```

### DNS Interception Setup

Add DNS entries to redirect device traffic to the proxy:

```bash
# /etc/dnsmasq.d/ride-api.conf
address=/mqtt.example.com/192.168.1.100
address=/api.example.com/192.168.1.100
```

Restart your DNS server: `sudo systemctl restart dnsmasq`

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

### Run the Proxy

```bash
python -m core.server
```

Open `http://localhost:8911/` in your browser to see the dashboard.

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
4. If score < threshold: forward to cloud, capture the miss, learn from it
5. Match rate is updated in real-time

### Context Buffer
- Configurable per device: 128KB, 256KB, 512KB, 1MB, 2MB, 5MB, 10MB
- Acts as a sliding window — accumulates pairs until threshold is reached
- On flush: all pairs sent to LLM for analysis, then buffer cleared
- Correlation cache is also cleared after flush

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
│       ├── state_manager.py   # Device state + virtual sensor simulation
│       ├── buffer_manager.py  # Buffer accumulation + export/import
│       ├── decipher_ingest.py # LLM output → pattern DB records
│       └── pattern_engine.py  # Pattern matching + state-aware response builder
├── config/
│   └── config.yaml        # Main configuration
├── tests/
├── deploy/
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
| `POST /api/devices/{id}/mode` | Switch learning/production |
| `PUT /api/devices/{id}/llm` | Configure LLM for device |
| `GET /api/devices/{id}/patterns` | Learned patterns |
| `GET /api/devices/{id}/patterns/{pid}` | Pattern detail + field mappings |
| `GET /api/llm/profiles` | Available LLM profiles |
| `GET /api/devices/{id}/patterns/export` | Export deciphered patterns (.ride-pattern.json) |
| `POST /api/devices/{id}/patterns/import` | Import patterns from .ride-pattern.json |
| `GET /api/devices/{id}/capture/export` | Export raw buffer (.ride-capture.json) |
| `POST /api/devices/{id}/capture/import` | Import raw pairs from .ride-capture.json |
| `GET /api/protocol-servers` | List all protocol servers with status |
| `POST /api/protocol-servers/{name}/start` | Start a protocol server |
| `POST /api/protocol-servers/{name}/stop` | Stop a protocol server |
| `GET /api/protocol-servers/{name}/config` | Get protocol server configuration |
| `POST /api/tls/certs/upload` | Upload TLS certificate + key (multipart) |
| `POST /api/tls/certs/upload-json` | Upload TLS certificate + key (JSON) |
| `GET /api/tls/certs` | List all imported TLS certificates |
| `GET /api/tls/certs/{hostname}` | Inspect a TLS certificate |
| `DELETE /api/tls/certs/{hostname}` | Delete an imported certificate |
| `POST /api/tls/certs/{hostname}/rotate` | Regenerate a device leaf certificate |
| `POST /api/tls/root-ca/download` | Download the MITM root CA certificate |
| `/{vendor}/{path:path}` | Proxy endpoint for device traffic |

## Roadmap

- [ ] Auto-switch to production when match rate is sufficient
- [x] Portable pattern database (LLM-agnostic, shareable, cross-hardware) — see [design doc](docs/portable-pattern-database.md)
- [ ] Built-in DNS server (no external dependency)
- [x] MQTT/CoAP protocol support
- [x] Modbus, WebSocket, Raw TCP, HTTP/2 protocol servers
- [x] TLS certificate management API (upload, list, delete, rotate)
- [x] Zigbee / Z-Wave / Matter bridge plugins
- [ ] Web UI for manual pattern editing
- [x] Encrypted traffic MITM support

YES. it is all vibe coded i'm searching for people who like this idea and would like to help implement it
## License

MIT
