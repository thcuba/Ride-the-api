# Quick Start — Ride-the-API

Local cloud replacement proxy that intercepts IoT traffic, learns protocols via LLM, and serves responses locally.

---

## 1. Prerequisites

- **Python ≥ 3.11**
- **Git**
- **DNS server** (dnsmasq, Pi-hole, or AdGuard Home) for device routing
- **LLM API** (OpenAI, local Ollama, vLLM, etc.)
- **Optional — Docker**: for the full stack (nginx + ride-the-api)

---

## 2. Clone the repository

```bash
git clone https://github.com/thcuba/Ride-the-api.git
cd Ride-the-api
```

---

## 3. Install dependencies

### With pip (recommended)

```bash
pip install -e .
```

### With uv (faster alternative)

```bash
uv pip install -e .
```

### Optional dependencies

- **Dev tools** (ruff, mypy, pytest): `pip install -e ".[dev]"`
- **GPU ONNX**: `pip install -e ".[gpu]"`
- **TFLite**: `pip install -e ".[tflite]"`

---

## 4. Configure config.yaml

Copy and customize the configuration file:

```bash
cp config/config.yaml config/config.local.yaml
# or edit config/config.yaml directly
```

### 4.1 — LLM (required)

Set the LLM profile in the `llm_decipher` section:

```yaml
llm_decipher:
  enabled: true
  default_profile: "default"

  profiles:
    default:
      base_url: "https://api.openai.com/v1"
      api_key: "${OPENAI_API_KEY}"            # or put the key in plain text
      model_id: "gpt-4o-mini"
```

> **Local Ollama:** change `base_url` to `http://localhost:11434/v1`, `api_key` to `"ollama"`, and `model_id` to `"llama3.1:8b"` (or your preferred model).

Export the environment variable:

```bash
export OPENAI_API_KEY="sk-..."
```

### 4.2 — Database (optional)

For development with SQLite (default), no changes needed:

```yaml
core:
  database_url: "sqlite+aiosqlite:///./data/core.db"
  device_db_dir: "./data/devices"
```

For production, set a PostgreSQL URL:

```yaml
core:
  database_url: "postgresql+asyncpg://user:pass@localhost/ride_api"
```

### 4.3 — Learning/Production mode

```yaml
learning:
  enabled: true
  default_mode: "learning"        # learning | production | hybrid
  default_match_threshold: 0.85
  auto_switch_to_production: false  # true for automatic switch at 99% match rate
```

### 4.4 — DNS routing (dnsmasq example)

Create `/etc/dnsmasq.d/ride-api.conf`:

```
# Replace 192.168.1.100 with the IP of the ride-the-api server
address=/mqtt.example.com/192.168.1.100
address=/api.example.com/192.168.1.100
address=/openapi.example.com/192.168.1.100
```

Restart dnsmasq:

```bash
sudo systemctl restart dnsmasq
```

---

## 5. Start the server

### Directly with Python

```bash
python -m core.server
```

The server starts on `http://0.0.0.0:8911` (default).

### With Docker Compose (recommended for production)

```bash
docker compose -f deploy/docker-compose.yml up -d
```

The stack starts:
- **nginx** on ports 80/443 (TLS) and 8883 (MQTT over TLS)
- **Ride-the-API** on internal port 8911

### As a systemd service (Linux)

```bash
sudo cp deploy/ride-the-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ride-the-api
sudo systemctl start ride-the-api
```

---

## 6. Connect an IoT device

### 6.1 — Configure the device DNS

- On your router/DHCP, set the primary DNS to the ride-the-api server IP
- Or statically configure DNS on the IoT device
- Or, for a quick test, add a line in the device's `/etc/hosts`:

```
192.168.1.100   mqtt.example.com
192.168.1.100   api.example.com
```

### 6.2 — (Optional) Intercept TLS

If the device uses HTTPS, the MITM proxy must be active:

```yaml
tls_decrypt:
  enabled: true
  listen_ports:
    - 443
    - 8883
```

Download the CA certificate from `http://<server-ip>:8911/api/tls/ca-cert` and install it on the device as a trusted authority.

### 6.3 — Verify the connection

Turn on the IoT device. The device will start talking to the cloud — ride-the-api intercepts the traffic and begins learning.

---

## 7. Verification

### Web dashboard

Open in your browser:

```
http://<server-ip>:8911/
```

You will see:
- List of detected devices
- Match rate and number of learned patterns for each device
- Buttons to switch between learning/production modes
- Buffer fill level

### API health check

```bash
curl http://localhost:8911/health
```

Expected response: `{"status": "ok"}` (or similar).

### TLS status

```bash
curl http://localhost:8911/api/tls/ports
```

Shows the TLS listening ports and active certificates.

### Real-time logs

```bash
# If started manually — logs go to stdout
tail -f data/core.log
```

---

## 8. Next steps

| What to do | Documentation |
|---|---|
| Understand the full architecture | `docs/nginx-architecture.md` |
| Portable pattern database format | `docs/portable-pattern-database.md` |
| Edit patterns via web UI | `http://localhost:8911/patterns/{device_id}` |
| Export/import patterns | REST API: `GET/POST /api/patterns/export` |
| Configure direct protocol servers (MQTT, CoAP, Modbus…) | `protocol_servers` section in `config.yaml` |

---

## 9. Quick Troubleshooting

| Problem | Likely cause | Solution |
|---|---|---|
| Device not detected | DNS does not point to proxy | Verify `nslookup <cloud-hostname>` from the device |
| `Connection refused` on :8911 | Server not started | Check `python -m core.server` and the logs |
| TLS handshake fails | CA certificate not installed on device | Download and install CA from `/api/tls/ca-cert` |
| Match rate at 0% | No patterns learned yet | Wait for a few requests in learning mode |
| Forwarding loop | DNS resolves to the proxy itself | Use `signal_forward_to_cloud: true` with nginx |
| `OPENAI_API_KEY` not found | Environment variable not set | `export OPENAI_API_KEY="sk-..."` |