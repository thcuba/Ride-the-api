# Ride-the-API

A DNS interception proxy that sits between IoT devices and their cloud APIs, learns device protocols via LLM analysis, and serves responses locally — making devices fully functional even if the vendor shuts down its cloud servers.

```
IoT Device ──▶ nginx/TLS MITM ──▶ Ride-the-API ──▶ Vendor Cloud (learning only)
                                        │
                                        └── Buffer → LLM → Pattern Engine → Local Response
```

## Why

IoT devices become bricked when vendors shut down their cloud servers. Ride-the-API intercepts cloud-bound traffic, analyzes it with an LLM, and learns to respond locally.

## Quick Start

```bash
git clone https://github.com/thcuba/Ride-the-api.git
cd Ride-the-api
pip install -e .
# Configure your LLM in config/config.yaml
python -m core.server
```

Open `http://localhost:8911/` — dashboard + pattern editor.

### Docker (with nginx sidecar)

```bash
docker compose -f deploy/docker-compose.yml up -d
```

nginx on port 443 → loop-free cloud forwarding via dedicated DNS (8.8.8.8/1.1.1.1).

## Documentation

| Document | Content |
|---|---|
| [Architecture](docs/architecture.md) | Components, flows, learning/production modes |
| [Quick Start](docs/quickstart.md) | Step-by-step installation guide |
| [Configuration](docs/configuration.md) | Full config.yaml reference |
| [Deployment](docs/deployment.md) | Docker, systemd, production setup |
| [API Reference](docs/api.md) | All REST endpoints |
| [Pattern DB](docs/portable-pattern-database.md) | .ride-pattern.json / .ride-capture.json format |
| [Nginx Architecture](docs/nginx-architecture.md) | Reverse proxy + DNS loop prevention |
| [Protocol Servers](docs/protocol-servers.md) | MQTT, CoAP, Modbus, WebSocket, Raw TCP, bridges |

## Key Features

- **Automatic Learning** — captures traffic, correlates request/response pairs, LLM analysis generates patterns
- **Local Response** — matches incoming requests against learned patterns, responds without cloud
- **Auto-Switch** — transitions from learning to production when match rate ≥ 99%
- **TLS MITM** — multi-port TLS interception with SNI extraction and dynamic certificate generation
- **Multi-Protocol** — HTTP, MQTT, CoAP, Modbus, WebSocket, Raw TCP, HTTP/2, Zigbee, Z-Wave, Matter
- **Portable Patterns** — export/import `.ride-pattern.json` and `.ride-capture.json`
- **Virtual Sensors** — simulated sensors with drift, periodic, and random behaviors
- **Persistent State** — device state variables (power, mode, temperature) persist across requests
- Web dashboard + built-in pattern editor
- Automatic resilience and retry

## Quick LLM Setup

```yaml
# config/config.yaml
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

## License

MIT