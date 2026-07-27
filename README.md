# Ride-the-API - DNS Interception Proxy for Multi-Vendor HVAC Edge AI

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Repository:** https://github.com/thcuba/Ride-the-api

## Overview

Ride-the-API is a local edge server that intercepts communications between HVAC devices (air conditioners, heat pumps, ventilators) and their vendor cloud services. It uses DNS interception to transparently replace vendor cloud endpoints with a local edge server, enabling:

- **Local AI inference** for autonomous control (temperature, mode, fan speed, swing)
- **Privacy-preserving** operation (all data stays on your local network)
- **Offline capability** (continues working without internet connection)
- **Multi-vendor support** (TY, TL, ZH, HR - extensible plugin architecture)
- **Fallback to cloud** when edge confidence is low
- **Training data collection** from real device-cloud interactions
- **Request/response correlation** with LLM-powered protocol deciphering
- **On-the-fly request/response modification** for custom behavior
- **Traffic selection** (intercept vs passthrough) via UI-managed rules

## Architecture

```
┌─────────────┐     DNS locale      ┌─────────────────────────────────┐
│  HVAC Dev.  │  mqtt.ty.com        │  Ride-the-API Edge Server :8911 │
│  TY/TL/ZH/HR│  api.tl.com ───────▶│                                 │
└─────────────┘   (redirection)     │  ┌─────────────────────────┐   │
       ▲                            │  │ Traffic Selector        │   │
       │ HTTPS/MQTT/                │  │ (Intercept/Passthrough) │   │
       │ CoAP (device thinks        │  └───────────┬─────────────┘   │
       │ it's talking to cloud)     │              │                │
       │                            │  ┌───────────▼─────────────┐   │
       │                            │  │ Protocol Adapters       │   │
       │                            │  │ (TY, TL, ZH, HR)        │   │
       │                            │  └───────────┬─────────────┘   │
       │                            │              │                │
       │                            │  ┌───────────▼─────────────┐   │
       │                            │  │ Correlation Engine      │   │
       │                            │  │ (Req/Resp Matching)     │   │
       │                            │  └───────────┬─────────────┘   │
       │                            │              │                │
       │                            │  ┌───────────▼─────────────┐   │
       │                            │  │ LLM Deciphering         │   │
       │                            │  │ (Protocol Analysis)     │   │
       │                            │  └───────────┬─────────────┘   │
       │                            │              │                │
       │                            │  ┌───────────▼─────────────┐   │
       │                            │  │ Safety Engine           │   │
       │                            │  │ (Hard Limits: 16-30°C,  │   │
       │                            │  │  3500W, 3°C/h, 15min)   │   │
       │                            │  └───────────┬─────────────┘   │
       │                            │              │                │
       │                            │  ┌───────────▼─────────────┐   │
       │                            │  │ Modification Engine     │   │
       │                            │  │ (On-the-fly Changes)    │   │
       │                            │  └───────────┬─────────────┘   │
       │                            │              │                │
       │                            │  ┌───────────▼─────────────┐   │
       └────────────────────────────│  │ Edge AI Inference       │   │
         Response (compatible)      │  │ + Control Logic         │   │
         (from edge or cloud)       │  └───────────┬─────────────┘   │
                                    │              │                │
                                    │  ┌───────────▼─────────────┐   │
                                    │  │ Vendor Cloud Forward    │   │
                                    │  │ (Fallback / Passthrough)│   │
                                    │  └─────────────────────────┘   │
                                    └─────────────────────────────────┘
                                             │
                                    ┌────────▼────────┐
                                    │  Per-Vendor DB  │
                                    │  (ty.db,        │
                                    │   tl.db,        │
                                    │   zh.db,        │
                                    │   hr.db)        │
                                    │  + Core DB      │
                                    └─────────────────┘
```

## Quick Start

### 1. Prerequisites

- Python 3.11+
- Local DNS server (dnsmasq, Pi-hole, AdGuard Home, Technitium, Unbound)
- HVAC devices configured for vendor cloud (TY, TL, ZH, HR)
- Optional: NVIDIA GPU for faster inference

### 2. Installation

```bash
# Clone and install
git clone https://github.com/thcuba/Ride-the-api.git
cd Ride-the-api
pip install -e ".[dev]"

# Or with GPU support (CUDA)
pip install -e ".[gpu]"

# Or with TensorFlow Lite
pip install -e ".[tflite]"
```

### 3. Configuration

```bash
# Copy example config
cp config/config.yaml config/config.local.yaml

# Edit config.local.yaml with your settings:
# - Database paths
# - Vendor API credentials (for cloud fallback)
# - Model paths
# - Safety limits
# - LLM provider (OpenAI, Ollama, vLLM, custom)
```

### 4. DNS Setup (Critical)

Configure your local DNS to redirect vendor hostnames to the edge server (port 8911):

**dnsmasq** (`/etc/dnsmasq.d/ride-the-api.conf`):
```conf
# TY (Tuya) EU
address=/mqtt.tuyaeu.com/192.168.1.100
address=/api.tuyaeu.com/192.168.1.100
address=/openapi.tuyaeu.com/192.168.1.100

# TL (TP-Link)
address=/api.kasacloud.com/192.168.1.100
address=/iot.tplinkcloud.com/192.168.1.100
address=/use1-api.tplinkcloud.com/192.168.1.100

# ZH (Zehnder)
address=/api.zehndercloud.com/192.168.1.100

# HR (Haier)
address=/api.haier.com/192.168.1.100
```

Then restart: `sudo systemctl restart dnsmasq`

**Pi-hole**: Admin UI → Local DNS → Custom DNS → Add entries

**AdGuard Home**: Filters → DNS Rewrites → Add rules

**Technitium DNS**: Settings → Conditional Forwarders / Hosts

**Unbound**: Add `local-zone` and `local-data` entries

### 5. TLS Certificates (for HTTPS interception)

Generate certificates trusted by your devices:

```bash
# Using mkcert (recommended for local development)
mkcert -install
mkcert -cert-file certs/ride-the-api.pem -key-file certs/ride-the-api.key \
  "*.tuyaeu.com" "*.tuyaus.com" "*.tplinkcloud.com" \
  "*.zehndercloud.com" "*.haier.com" \
  localhost 127.0.0.1 ::1
```

Or use your local CA (step-ca, smallstep, HashiCorp Vault, etc.).

**Note**: Some devices use certificate pinning. See `config/config.yaml` → `tls_decrypt` for mitigation strategies (cert repinning, Frida, mitmproxy).

### 6. Run

```bash
# Development
python -m core.server

# Production (Docker)
docker-compose -f deploy/docker-compose.yml up -d

# Production (systemd)
sudo cp deploy/ride-the-api.service /etc/systemd/system/
sudo systemctl enable --now ride-the-api
```

## Configuration

See `config/config.yaml` for all options. Key sections:

| Section | Description |
|---------|-------------|
| `core` | Database settings, vendor DB directory |
| `proxy` | Server host/port, TLS, vendor routing prefixes |
| `vendors` | Per-vendor cloud endpoints and adapter config |
| `models` | Model registry, inference settings, hot-reload |
| `control` | Policy engine, safety limits, online learning |
| `observability` | Logging, metrics, tracing, health checks |
| `dns` | DNS integration helpers |
| `traffic_selection` | UI-managed intercept/passthrough rules |
| `llm_decipher` | LLM provider config (base_url, api_key, model_id) |
| `tls_decrypt` | TLS interception methods per vendor |
| `modification` | On-the-fly request/response transformation rules |
| `correlation` | Request/response matching configuration |

## Extending for New Vendors

1. Create new adapter in `adapters/<code>/__init__.py` (e.g., `adapters/xyz/`)
2. Implement `ProtocolAdapter` interface (see `adapters/base/__init__.py`)
3. Register in `adapters/__init__.py`
4. Add vendor config to `config/config.yaml` under `vendors:`
5. Add DNS entries for vendor hostnames
6. Test with real device

### Vendor Code Convention

Use 2-letter codes to avoid trademark issues:
- **TY** - Tuya
- **TL** - TP-Link (Kasa/Tapo)
- **ZH** - Zehnder
- **HR** - Haier
- **XX** - Your new vendor

## Project Structure

```
Ride-the-api/
├── core/                      # Core engine
│   ├── config.py             # Configuration management (hot-reload)
│   ├── database.py           # Multi-vendor SQL databases (SQLAlchemy)
│   ├── server.py             # FastAPI proxy server (main pipeline)
│   ├── safety.py             # Safety engine (hard limits)
│   ├── traffic_analysis.py   # Response comparison, device compliance
│   ├── traffic_selector.py   # Intercept vs passthrough rules engine
│   ├── correlation.py        # Request/response correlation
│   ├── llm_decipher.py       # LLM protocol analysis (OpenAI/Ollama/vLLM)
│   ├── modification.py       # On-the-fly modification rules
│   └── __init__.py
├── adapters/                  # Vendor protocol adapters
│   ├── base/                 # Base interfaces & registry
│   ├── ty/                   # TY (Tuya) - full implementation
│   ├── tl/                   # TL (TP-Link) - stub
│   ├── zh/                   # ZH (Zehnder) - stub
│   ├── hr/                   # HR (Haier) - stub
│   └── __init__.py           # Auto-registration
├── models/                    # ML model serving (ONNX/TFLite)
├── config/                    # Configuration files
│   ├── config.yaml           # Main config (commit this)
│   └── config.local.yaml     # Local overrides (gitignored)
├── deploy/                    # Deployment configs
│   ├── Dockerfile            # Multi-stage build
│   ├── docker-compose.yml    # Full stack
│   └── ride-the-api.service  # systemd unit
├── tests/                     # Unit & integration tests
├── scripts/                   # Utility scripts
└── docs/                      # Documentation
```

## Database Schema

Each vendor gets their own SQLite database (`data/vendors/<code>.db`):

| Table | Purpose |
|-------|---------|
| `devices` | Device registry with capabilities, config |
| `readings` | Time-series sensor data (temp, humidity, power, mode) |
| `commands` | Command log with source tracking (edge/manual/cloud) |
| `models` | Trained model metadata (path, accuracy, version) |
| `policies` | Per-vendor control policies (JSON) |
| `intercepted_requests` | Raw request/response for training |

Core database (`data/core.db`):

| Table | Purpose |
|-------|---------|
| `device_registry` | Maps device_id → vendor DB |
| `model_registry` | Global model index |
| `global_policies` | Safety limits applied to all vendors |
| `cloud_providers` | Fallback cloud configuration |

## Pipeline Stages

1. **Traffic Selection** - Rules engine decides intercept vs passthrough (CIDR, hostname, vendor, device_id)
2. **Protocol Parsing** - Vendor adapter extracts intent (DP codes, commands, queries)
3. **Correlation** - Matches requests to responses across HTTP/MQTT/CoAP
4. **LLM Deciphering** - Sends pairs to LLM for protocol analysis (cached)
5. **Safety Check** - Hard limits enforced before any device command
6. **Modification** - On-the-fly request/response transformations
7. **Edge Execution** - Local AI inference or deterministic control
8. **Cloud Fallback** - Forward to real cloud if needed
9. **Traffic Analysis** - Compare edge vs cloud responses, track compliance

## Safety

Hard safety limits are **always enforced** before sending commands to devices:

- Temperature range: 16-30°C (configurable)
- Max power: 3500W (configurable)  
- Max temp change rate: 3°C/hour
- Emergency stop on communication loss > 15min
- Min/max humidity: 30-70%

These **cannot be overridden** by AI models or modifications.

## LLM Integration

Configure any OpenAI-compatible provider:

```yaml
llm_decipher:
  enabled: true
  default_profile: "local_ollama"
  profiles:
    local_ollama:
      base_url: "http://localhost:11434/v1"
      api_key: "ollama"  # dummy
      model_id: "llama3.1:8b"
      prompt_template: |
       There are requests and replies from a {vendor} {device_type} device.
       Decipher them and make correlation on the same the database I give to you.
        
       Database schema:
       {db_schema}
        
       Recent patterns from this device type:
       {recent_patterns}
        
       Request/Response pairs to analyze:
       {pairs}
        
       Output JSON with:
       - intent: the device intent (get_state, set_temp, set_mode, firmware_check, etc.)
       - fields: object mapping field names to values with types
       - confidence: 0.0-1.0 confidence score
       - suggested_dp_codes: object mapping DP code names to values
       - protocol_notes: any protocol-specific observations
```

Supported: OpenAI, Azure OpenAI, Ollama, vLLM, LiteLLM, custom OpenAI-compatible endpoints.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/metrics` | GET | Prometheus metrics |
| `/{vendor}/{path}` | ANY | Main proxy (vendor routes) |
| `/mqtt/{vendor}/publish` | POST | MQTT message proxy |
| `/api/devices` | GET | List all devices |
| `/api/devices/{id}` | GET | Device details + readings |
| `/api/devices/{id}/command` | POST | Send manual command |
| `/api/traffic/rules` | GET/POST | Traffic selection rules |
| `/api/correlation/pairs` | GET | Request/response pairs |
| `/api/llm/decipher` | POST | Trigger LLM analysis |
| `/api/modification/rules` | GET/POST | Modification rules |

## Monitoring

- **Grafana Dashboards**: See `deploy/grafana/` (TODO)
- **Prometheus**: `GET /metrics` on port 9090
- **Jaeger/OTLP**: Configured via `observability.tracing`
- **Structured Logs**: JSON format, configurable level

## Deployment

### Docker

```yaml
# docker-compose.yml
services:
  ride-the-api:
    build: .
    ports:
      - "8911:8911"
      - "9090:9090"
      - "8080:8080"
    volumes:
      - ./config:/app/config
      - ./data:/app/data
      - ./certs:/app/certs
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
```

### Kubernetes

```yaml
# k8s/deployment.yaml (TODO)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ride-the-api
spec:
  replicas: 2
  selector:
    matchLabels:
      app: ride-the-api
  template:
    spec:
      containers:
      - name: ride-the-api
       image: ghcr.io/thcuba/ride-the-api:latest
       ports:
       - containerPort: 8911
       env:
       - name: OPENAI_API_KEY
         valueFrom:
           secretKeyRef:
             name: ride-the-api-secrets
             key: openai-api-key
```

## Testing

```bash
# Unit tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=core --cov=adapters --cov-report=html

# Integration tests (requires devices)
pytest tests/integration/ -v -k "not slow"
```

## Roadmap

- [ ] Full TP-Link (TL) adapter implementation
- [ ] Full Zehnder (ZH) adapter (Modbus + Cloud)
- [ ] Full Haier (HR) adapter implementation  
- [ ] Model training pipeline (intercepted_requests → ONNX/TFLite)
- [ ] PID/RL control policy engine
- [ ] Grafana dashboard templates
- [ ] Device compliance alerting
- [ ] Firmware update passthrough detection
- [ ] Multi-tenant support
- [ ] Web UI for rule management

## Contributing

1. Fork the repository
2. Create feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open Pull Request

## Security

- Never commit API keys or secrets to git
- Use environment variables for credentials (`${ENV_VAR}` in config)
- TLS certificates should be managed by your local CA
- Safety engine provides last line of defense
- Report vulnerabilities via GitHub Security Advisories

## License

MIT License - see [LICENSE](LICENSE) file

## Acknowledgments

- Tuya IoT developers for protocol documentation
- TP-Link/Kasa/Tapo reverse engineering community
- Zehnder Modbus protocol documentation
- Haier U+ Smart Life protocol researchers
- All contributors to local-first IoT movement

---

**Built for privacy, reliability, and vendor independence.** 🏠🔧