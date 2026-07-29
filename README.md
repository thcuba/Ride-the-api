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
- If score ≥ threshold → serves response from local database
- If score < threshold → forwards to cloud, captures and learns from the miss
- Real-time match rate tracking: `hits / (hits + misses) * 100%`

### Configurable LLM
- Per-device LLM configuration: `base_url`, `model_id`, optional `profile_name`
- Supports OpenAI-compatible APIs, local Ollama, vLLM, etc.
- Context buffer size configurable per device

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
# /etc/dnsmasq.d/edge-hvac.conf
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
│   └── __init__.py        # Adapter registry
├── core/
│   ├── server.py          # FastAPI server + dashboard + API endpoints
│   ├── config.py          # Configuration management
│   ├── database.py        # Device-specific protocol DB models + manager
│   ├── pipeline.py        # Learning/production orchestrator, correlator, buffer, matcher
│   ├── llm_decipher.py    # LLM analysis service
│   ├── modification.py    # Request/response modification
│   └── traffic_selector.py # Intercept/passthrough rules
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
| `/{vendor}/{path:path}` | Proxy endpoint for device traffic |

## Roadmap

- [ ] Auto-switch to production when match rate is sufficient
- [ ] Pattern export/import for sharing between users
- [ ] Built-in DNS server (no external dependency)
- [ ] MQTT/CoAP protocol support
- [ ] Web UI for manual pattern editing
- [ ] Community pattern database
- [ ] Encrypted traffic MITM support

YES. it is all vibe coded i'm searching for people who like this idea and would like to help implement it
## License

MIT
