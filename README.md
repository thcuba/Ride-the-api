# Ride-the-API

A DNS interception proxy that sits between IoT devices and their cloud APIs, learns device protocols via LLM analysis, and serves responses locally — with the goal of keeping devices working if the vendor shuts down its cloud servers.

## Project status

**Ride-the-API is experimental protocol emulation**, not a turnkey solution, and it does not
claim that any specific device keeps working after its vendor cloud shuts down.

**Demonstrated, and reproducible in CI** — `tests/test_complete_path.py` walks one complete,
truthful request/response path in order, against a *real* HTTP vendor on a socket (not a mock):

1. a fresh install (empty database directory) comes up and creates its store;
2. forwarding preserves **bytes, status and headers in both directions**: a vendor `429` with
   its `retry-after` and a non-JSON body reaches the device unchanged, and the vendor receives
   the device's own bytes;
3. learning **captures the real vendor response** — the stored pair carries the `429` and its
   headers, not a fabricated `200`;
4. with the vendor **shut down**, the learned pattern serves the request locally, and it still
   does after the database manager is rebuilt on the same directory — a restart / container
   recreation with a persistent volume.

**And the piece that makes interception reachable at all**: `core/dns_server.py` answers the names
listed in `dns.intercept` with the proxy's own address and **forwards every other name verbatim** to
the real resolvers — so the proxy can be the DNS for a network without hijacking anything by
accident (`tests/test_dns_server.py`, real socket round trip included). When listing domains one by
one is not enough, `dns.intercept_all` widens that to every name, with `dns.exclude` drawing the
boundary (clock, OCSP/CRL and local names stay out), and every query is logged in
`dns_query_log` so the next name to intercept is read off real traffic instead of guessed
(`tests/test_dns_discovery.py`).

That test states its own limits: the pattern *extraction* step is fed a canned analysis instead
of calling a real model, so model quality is not measured. Capture, correlation, persistence,
matching and serving are production code, and the network exchange is real.

**Verified live on hardware** — on a a proxy host 4, deployed from source, with a **real public
vendor** reached over the internet: a vendor `429` arrives as `429`; a non-JSON body keeps its
`content-type` and its bytes; the vendor receives the device's own bytes unchanged; control-plane
errors carry no internals; and an unreachable vendor fails loudly (`502`) instead of claiming
success. Exact commands and results: [`docs/verification.md`](docs/verification.md).
Running the real path also found a defect the suite had missed — the forward ignored the
request's `Host` header and used the configured hostname instead (fixed in #408).

**Not yet verified:**

- a run against **one named physical device and firmware version** — DNS interception, the
  device's own certificate handling and retry logic, and its behaviour across a real reboot;
- pattern extraction by a real LLM from real vendor traffic, end to end.

A device is only labelled **cloud independent** when it is in production mode *and* its measured
match rate reaches `AUTO_SWITCH_MATCH_RATE` (`core/resilience.py`) — recognising request shapes
is not treated as proof.

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

### Prebuilt binaries (all platforms)

Every GitHub **release ships prebuilt bundles** for Linux (x64/arm64), macOS
(arm64) and Windows. Install/upgrade with the one-shot scripts (they download
the **latest** release by default and rerunning them upgrades **in place,
preserving `config/`, `data/`, `certs/` and `logs/`**):

```bash
# Linux (x64) or a proxy host (arm64) / macOS (arm64)
curl -sSL https://raw.githubusercontent.com/thcuba/Ride-the-api/main/deploy/rideapi -o rideapi
bash rideapi                    # → ~/ride-the-api
bash rideapi --systemd          # optional: run as a user service
# Optionally choose dir/version:  bash rideapi --dir /opt/ride-the-api --version 1.1
```

```powershell
# Windows (x64) — in PowerShell
Invoke-WebRequest https://raw.githubusercontent.com/thcuba/Ride-the-api/main/deploy/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1          # latest, GUI installer
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Silent  # silent install
```

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
| [Deployment Windows](docs/deployment-windows.md) | Native Windows server + installer service |
| [API Reference](docs/api.md) | All REST endpoints |
| [Pattern DB](docs/portable-pattern-database.md) | .ride-pattern.json / .ride-capture.json format |
| [Nginx Architecture](docs/nginx-architecture.md) | Reverse proxy + DNS loop prevention |
| [Protocol Servers](docs/protocol-servers.md) | MQTT, CoAP, Modbus, WebSocket, Raw TCP, bridges |
| [Onboarding a device](docs/onboarding-a-device.md) | Take one real device under the proxy, step by step, with the checks |
| [Contributing](CONTRIBUTING.md) | Dev setup, tests/linters, PR workflow |

## Key Features

- **Automatic Learning** — captures traffic, correlates request/response pairs, LLM analysis generates patterns
- **Local Response** — matches incoming requests against learned patterns, responds without cloud
- **Auto-Switch** — transitions from learning to production when match rate ≥ 99%
- **Per-IP Bypass** — forward specific IPs straight to the real cloud with no analysis (no buffer/LLM/learning)
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

For local Ollama (use a model that is **actually installed**: check with `ollama list`):
```yaml
    local_ollama:
      base_url: "http://localhost:11434/v1"
      api_key: "ollama"
      model_id: "llama3.2:1b"
```

## License

MIT

## Third-Party Libraries

Ride-the-API is built on a small set of mature, battle-tested open-source
libraries. Wherever the project once had hand-rolled code (DNS caching, HTTP
parsing, safe expression evaluation, JSON path access, LLM output parsing,
API-key handling), it now delegates to these tested building blocks. The
shared helpers that remain are thin adaptation layers on top of them.

| Area | Library | Used for |
|---|---|---|
| HTTP/1.1 parsing | [`h11`](https://github.com/python-hyper/h11) | State-machine parsing/serialization of decrypted HTTP in the MITM proxy (no regex parsers) |
| Retry / backoff | [`tenacity`](https://github.com/jd/tenacity) | Resilient outbound calls (LLM, cloud forward) with shared helpers in `core/retry.py` |
| Structured logging | [`structlog`](https://github.com/hynek/structlog) | `core/logging_config.py` pipeline replacing bare `basicConfig` |
| Safe formula eval | [`simpleeval`](https://github.com/danthedeckie/simpleeval) | Restricted arithmetic/comparison evaluation in the pattern engine |
| JSON path access | [`dpath`](https://github.com/akesterson/dpath-python) | Dot/bracket JSON navigation in `modification.py`, `pattern_engine.py`, `pipeline.py` |
| IP address validation | [`ipaddress`](https://docs.python.org/3/library/ipaddress.html) (stdlib) | Robust IPv4/IPv6 validation |
| DNS cache | [`cachetools`](https://github.com/tkem/cachetools) | TTL cache for upstream DNS resolution |
| LLM JSON extraction | [`json_repair`](https://github.com/mangiucugna/json_repair) | Tolerant parsing of LLM output (code fences, malformed/truncated JSON) |
| API-key secrets | [`pydantic-settings`](https://github.com/pydantic/pydantic-settings) | `SecretStr`-based API-key handling |
| Wildcard→regex | [`fnmatch`](https://docs.python.org/3/library/fnmatch.html) (stdlib) | Wildcard pattern translation in `traffic_selector.py` |
| HTTP status phrases | [`http.HTTPStatus`](https://docs.python.org/3/library/http.html#http.HTTPStatus) (stdlib) | Canonical reason phrases for responses in `tls_mitm.py` |

Dependency versions are pinned in [`pyproject.toml`](pyproject.toml) and
bundled into renewal groups in [`.github/dependabot.yml`](.github/dependabot.yml)
so interlinked libraries stay version-compatible. All libraries are
distributed under permissive (MIT/BSD/Apache) licenses.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, running tests/linters,
coding conventions, and the PR workflow. The test suite is self-contained and
lightweight: it needs **no GPU, no live LLM calls, and no external network**
(`onnxruntime`/`numpy` are dead scaffolding — an optional `inference` extra, not
imported by any module and untested — LLM clients are mocked, and
socket tests use loopback) — `pip install -e ".[dev]"` is all you need to
run `pytest`.

yes it is all vibecoded 
contributions! Here's how you can help:

🍴 Fork the repository
🔧 Create your feature branch
💾 Commit your changes
📤 Push to the branch 
🎉 Open a Pull Request