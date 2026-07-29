# Ride-the-API — Edge AI DNS Interception Proxy for HVAC

A DNS interception proxy that sits between HVAC devices and their cloud services, applying edge AI for autonomous control. Supports multi-protocol traffic analysis, LLM-assisted protocol deciphering, and on-the-fly request/response modification.

## Architecture

```
┌──────────────┐     ┌─────────────────────────────────────────────────────┐     ┌──────────────┐
│  HVAC Device │────▶│  Ride-the-API (DNS Interception Proxy)              │────▶│   Vendor     │
│  (Any Brand) │     │                                                     │     │   Cloud      │
│              │     │  Traffic Selector ─▶ Protocol Parser ─▶ Correlator  │     │  (Fallback)  │
│              │     │       │                       │                      │     │              │
│              │     │       ▼                       ▼                      │     │              │
│              │     │  LLM Decipher ─▶ Safety Check ─▶ Modification       │     │              │
│              │     │       │                                              │     │              │
│              │     │       ▼                                              │     │              │
│              │     │  Edge Execution (local AI inference)                │     │              │
│              │     │       │                                              │     │              │
│              │     │       ▼                                              │     │              │
│              │     │  Traffic Analysis (compare edge vs cloud)           │     │              │
│              │     └─────────────────────────────────────────────────────┘     └──────────────┘
└──────────────┘
```

## Features

### Protocol-Agnostic Architecture
- **Abstract protocol adapter interface** — implement for any device protocol
- **Reference implementation** included showcasing DP (Data Point) code patterns
- **Users/community define their own database names** — no vendor lock-in

### Traffic Interception & Analysis
- **DNS-based interception** via dnsmasq, Pi-hole, or AdGuard Home
- **Traffic selection** — rules-based intercept vs passthrough decisions
- **Response comparison** — edge vs cloud response analysis
- **Pattern discovery** — automatic protocol pattern detection

### Edge AI Pipeline
- **LLM-assisted protocol deciphering** — OpenAI-compatible API or local Ollama
- **Request/Response correlation** — intelligent pair matching
- **Safety engine** — prevents dangerous commands
- **On-the-fly modification** — transform requests/responses in real-time

### Observability
- Prometheus metrics, OpenTelemetry tracing
- Health check endpoints
- Structured JSON logging

## Quick Start

### Prerequisites
- Python 3.11+
- DNS server (dnsmasq, Pi-hole, or AdGuard Home) for device routing
- (Optional) GPU with NVIDIA CUDA for ONNX Runtime GPU inference

### Installation

```bash
# Clone the repository
git clone https://github.com/thcuba/Ride-the-api.git
cd Ride-the-api

# Install dependencies
pip install -e .

# Copy and customize configuration
cp config/config.example.yaml config/config.yaml
```

### DNS Interception Setup

Add DNS entries to redirect device traffic to the proxy:

```bash
# Example dnsmasq config (/etc/dnsmasq.d/edge-hvac.conf)
address=/mqtt.example.com/192.168.1.100
address=/api.example.com/192.168.1.100
address=/openapi.example.com/192.168.1.100
```

Restart your DNS server: `sudo systemctl restart dnsmasq`

### Run the Proxy

```bash
python -m core.server
```

The proxy starts on port 8911 by default.

## Protocol Adapters

Adapters translate device-specific protocols into standard commands. The project provides a **reference implementation** (in `adapters/example/`) that demonstrates DP (Data Point) code-based protocol patterns.

### Creating a Custom Adapter

```python
from adapters.base import ProtocolAdapter, ProtocolType, CommandResult, InterceptedRequest

class MyProtocolAdapter(ProtocolAdapter):
    @property
    def supported_protocols(self) -> list[ProtocolType]:
        return [ProtocolType.MQTT, ProtocolType.HTTPS]
    
    @property
    def vendor_hostnames(self) -> list[str]:
        return ["cloud.myprotocol.com", "mqtt.myprotocol.com"]
    
    async def parse_request(self, request: InterceptedRequest) -> InterceptedRequest:
        # Extract intent from the request
        return request
    
    async def handle_request(self, request: InterceptedRequest) -> CommandResult:
        # Handle locally via edge AI
        return CommandResult(success=True)
    
    async def forward_to_cloud(self, request: InterceptedRequest) -> CommandResult:
        # Forward to real cloud
        return CommandResult(success=False, error="Not implemented")
    
    async def build_response(self, request: InterceptedRequest, result: CommandResult) -> dict:
        return {"success": result.success}
    
    async def get_device_info(self, device_id: str):
        return None
    
    async def get_device_state(self, device_id: str):
        return None
    
    async def send_command(self, device_id: str, command):
        return CommandResult(success=True)
```

Register your adapter in `adapters/__init__.py`:

```python
from adapters.my_protocol import MyProtocolAdapter
my_adapter = MyProtocolAdapter("my_protocol", {})
registry.register(my_adapter)
```

## Project Structure

```
├── adapters/
│   ├── base/              # Abstract ProtocolAdapter interface
│   ├── example/           # Reference implementation (DP code protocol)
│   └── __init__.py        # Adapter registry (add your own here)
├── core/
│   ├── server.py          # FastAPI server + proxy endpoints
│   ├── config.py          # Configuration management
│   ├── database.py        # Core + per-protocol database models
│   ├── traffic_analysis.py # Response comparison engine
│   ├── traffic_selector.py # Intercept/passthrough rules
│   ├── correlation.py     # Request/response pair matching
│   ├── llm_decipher.py    # LLM-based protocol analysis
│   ├── modification.py    # On-the-fly request/response modification
│   └── safety.py          # Safety constraints engine
├── config/
│   └── config.yaml        # Main configuration
├── tests/
│   ├── test_adapters.py   # Adapter tests
│   └── test_database.py   # Database tests
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── ride-the-api.service
└── docs/
```

## Roadmap

- [ ] Additional protocol adapter examples
- [ ] Web UI for traffic monitoring and rule management
- [ ] Auto-discovery of device DP codes
- [ ] Community-contributed adapter database
- [ ] Enhanced LLM deciphering with fine-tuned models
- [ ] Built-in DNS server
- [ ] Mobile app for device control

## License

MIT
