# Architecture of Ride-the-API

> **Architectural Overview** — on-premise cloud proxy substitute that intercepts, learns, and serves
> IoT device protocols locally via LLM analysis.
>
> Document version: 1.0 — based on `core/` and `adapters/` code.

---

## 1. General Architectural Flow

The system is designed as a six-stage pipeline that transforms encrypted IoT traffic into
autonomous local responses. Each request passes through the following stages:

```
┌──────────┐     ┌──────────────────────────────────────────────────────────────────────────────┐
│  IoT     │     │  Ride-the-API Proxy                                                          │
│  Device  │     │                                                                              │
│          │     │  ┌──────────┐    ┌────────────────┐    ┌───────────────────────────┐          │
│  ──TLS───│────▶│  │  TLS MITM │───▶│  Traffic       │───▶│  LearningOrchestrator     │          │
│  HTTPS   │     │  │  Server   │    │  Selector      │    │  (Pipeline)               │          │
│          │     │  │           │    │                │    │                           │          │
│          │     │  │ ● SNI     │    │ ● CIDR match   │    │  ┌─────────────────┐     │          │
│          │     │  │   extract │    │ ● Hostname      │    │  │ BufferManager   │     │          │
│          │     │  │ ● Dynamic │    │   match         │    │  │ (sliding window)│     │          │
│          │     │  │   cert    │    │ ● Vendor match  │    │  └──────┬──────────┘     │          │
│          │     │  │   gen     │    │ ● Device ID     │    │         │               │          │
│          │     │  │ ● Multi-  │    │   match         │    │  ┌──────▼──────────┐     │          │
│          │     │  │   port    │    │ ● Priority-     │    │  │ LLMRouter       │     │          │
│          │     │  │   listen  │    │   based eval    │    │  │ (LLMDecipher-   │     │          │
│          │     │  └──────────┘    └────────┬───────┘    │  │  │ Service)        │     │          │
│          │     │                           │             │  └──────┬──────────┘     │          │
│          │     │              ┌────────────┘             │         │               │          │
│          │     │              ▼                          │  ┌──────▼──────────┐     │          │
│          │     │      Passthrough (forward to cloud)     │  │ DecipherIngest  │     │          │
│          │     │      ──► UpstreamResolver ──► Cloud     │  └──────┬──────────┘     │          │
│          │     │                                           │         │               │          │
│          │     │              INTERCEPT                    │  ┌──────▼──────────┐     │          │
│          │     │      ◄─────────────────────────►          │  │ PatternEngine   │     │          │
│          │     │                                           │  │ (match + build  │     │          │
│          │     │                                           │  │  local resp.)   │     │          │
│          │     │                                           │  └────────┬───────┘     │          │
│          │     │                                           └───────────┼──────────────┘          │
│          │     │                                                       │                        │
│          │     │                                              ┌────────▼────────┐                │
│          │     │                                              │  Response       │                │
│          │     │                                              │  Local  ──► IoT │                │
│          │     │                                              │  Or             │                │
│          │     │                                              │  Forward Cloud  │                │
│          │     │                                              │  (via nginx /   │                │
│          │     │                                              │   UpstreamRes.) │                │
│          │     │                                              └─────────────────┘                │
│          │     └──────────────────────────────────────────────────────────────────────────────┘
│          │
│          ▼
│     Vendor Cloud
│     (learning phase only)
```

---

## 2. Stages in Detail

### 2.1 TLS MITM Server (`core/tls_mitm.py`)

**Role**: TLS termination for all IoT devices, regardless of vendor.

- **Multi-port listening**: listens on configurable ports (default 8443, 9443, 10443, …),
  so that nginx (reverse proxy sidecar) forwards traffic destined for different cloud ports.
- **SNI Extraction**: analyzes the TLS `ClientHello` without completing the handshake to extract
  the target hostname. This enables dynamic generation of the appropriate certificate.
- **CertManager** (`core/cert_manager.py`): automatically generates a root CA (RSA 4096, SHA-256)
  on first startup, and for each new SNI hostname generates a leaf certificate signed by the CA and
  caches it on disk in `./certs/`.
- **IP-first device routing**: device identity is determined by the **source IP address**,
  not by port or hostname. Unknown IPs are auto-registered with a dedicated database and
  `passthrough=ON`.
- **REST API**: allows managing ports, viewing statistics, listing unidentified devices,
  and downloading the CA certificate for manual installation on devices.
- **Frida script injection**: provides a dynamic instrumentation script for devices that
  implement certificate pinning (`GET /api/tls/frida/script.js`).
- **HTTP/1.1 parsing/serialization**: decrypted traffic is parsed and re-serialized with the
  [`h11`](https://github.com/python-hyper/h11) state machine (`parse_decrypted_http_request`,
  `serialize_http_response`) — no regex-based HTTP parsers are used.
- **Forward contract**: when the pipeline does not serve a local response, the MITM mirrors the
  documented forward contract (`docs/nginx-architecture.md`): `action=forward` → **502** +
  `X-Action: forward` + `X-Original-Host` (the SNI); `action=no_fallback` → **501**; anything else →
  generic 502. A downstream nginx (or proxy) catches the `X-Action: forward` signal and re-routes
  loop-free.

### 2.2 TrafficSelector (`core/traffic_selector.py`)

**Role**: decides whether a request should be intercepted (processed by the local pipeline) or
passed through (forwarded directly to the cloud).

- **Priority-based rule evaluation**: rules are ordered by priority (descending). The first
  matching rule determines the action.
- **Match types**:
  - `CIDR`: match on IP range (for local traffic).
  - `HOSTNAME`: match on hostname pattern with wildcards (for external traffic).
  - `VENDOR`: match on vendor code (e.g. `ty`, `tl`, `zh`, `hr`).
  - `DEVICE_ID`: match on specific device ID.
- **Available actions**:
  - `INTERCEPT`: traffic is processed by the local edge AI pipeline.
  - `PASSTHROUGH`: traffic is forwarded directly to the cloud without processing.
- **Default action**: configurable; in the absence of matching rules, the default action applies
  (usually `INTERCEPT`).
- **Hot-reload**: rules are automatically reloaded when the configuration changes.
- **Wildcard matching**: hostname wildcard patterns are translated to regex with
  [`fnmatch.translate`](https://docs.python.org/3/library/fnmatch.html) (stdlib) rather than a
  hand-rolled `*` → `.*` conversion.

### 2.3 LearningOrchestrator (Pipeline) (`core/pipeline.py`)

**Role**: central orchestrator that manages the learning and production cycle for each
device. Coordinates BufferManager → LLMRouter → DecipherIngest → PatternEngine.

- **Operational modes**:
  - `LEARNING`: intercepts requests and responses, buffers them, sends them to the LLM for analysis,
    saves learned patterns.
  - `PRODUCTION`: matches incoming requests against learned patterns; if the match is
    sufficient, serves a local response; otherwise forwards to the cloud (with different strategies).
  - `HYBRID`: combines learning and production simultaneously.
- **Auto-switch**: a background scheduler checks the match rate every 60 seconds. When
  it reaches ≥ 99% (with at least 10 patterns and 50 total requests), it automatically switches
  the device to production mode.
- **Rollback**: if the match rate drops below 90% in production, it automatically reverts to
  learning mode.
- **Real-time match rate tracking**: `hits / (hits + misses) × 100%`.

#### 2.3.1 BufferManager (`core/pattern_db/buffer_manager.py`)

**Role**: accumulates correlated request/response pairs in a sliding window buffer until
the configured capacity is reached, then signals a flush to the LLM.

- **Accumulation**: receives already-correlated pairs from the pipeline and stores them in the
  device's database (`LLMContextBuffer` table) with an estimated byte size.
- **Sequence**: each pair receives a progressive sequence number to maintain chronological
  request order.
- **Configurable capacity**: each device has a configurable maximum buffer limit
  (default 512 KB) settable in `DeviceRegistry.context_buffer_size`.
- **Flush**: when the buffer exceeds the maximum capacity, `add_pair()` returns `True` to
  signal that it's time to send to the LLM. The `flush()` method marks all entries as
  processed and resets the counter.
- **Export/Import**: supports the portable `.ride-capture.json` format via
  `export_capture()` and `import_capture()`, validated against a JSON schema via `Validator`.
  This allows sharing traffic traces between users without exposing sensitive data.
- **Cache cleanup**: after flushing, the device's `SessionCache` is cleared for the
  next learning cycle.

#### 2.3.2 LLMRouter (LLMDecipherService) (`core/llm_decipher.py`)

**Role**: sends buffered request/response pairs to a configurable LLM for protocol
analysis and field deciphering.

- **Multi-provider**: supports any OpenAI-compatible API (OpenAI, local Ollama,
  vLLM, etc.) via configurable profiles.
- **LLM Profiles** (`LLMProfile`): each profile specifies `base_url`, `api_key`, `model_id`,
  `prompt_template`, `timeout` and `max_retries`. The `api_key` can be resolved from environment
  variables using `${VAR_NAME}` syntax.
- **Single and batch deciphering**: `decipher_pair()` analyzes a single pair; `decipher_batch()`
  analyzes multiple pairs in parallel with `asyncio.gather()`.
- **Prompt construction**: the prompt is built from a template that includes the
  request, response, vendor database schema, recent patterns, and user context notes
  (`llm_context_notes`).
- **Response format**: the LLM must return structured JSON with `intent`, `fields`,
  `confidence`, `suggested_dp_codes` and `protocol_notes`. The JSON is extracted from
  markdown responses (```json ... ``` fences) and repaired when malformed/truncated with the
  [`json_repair`](https://github.com/mangiucugna/json_repair) library (see `_parse_llm_json`).
- **Cache**: deciphered results are cached in memory (TTL: 1 hour) to
  avoid redundant LLM calls.
- **Retry with backoff**: in case of timeout or HTTP error, retries up to `max_retries` times with
  exponential backoff (1s, 2s, …) driven by the [`tenacity`](https://github.com/jd/tenacity)
  library (shared helpers in `core/retry.py`).

#### 2.3.3 DecipherIngest (`core/pattern_db/decipher_ingest.py`)

**Role**: takes the structured LLM output and transforms it into persistent patterns in the
device-specific database.

- **Input**: structured dictionary with key `"patterns"` containing a list of patterns.
- **Operation**: for each pattern, creates:
  - A `RequestPattern` with method, path pattern, protocol, required headers, body schema,
    query param keys, intent, and confidence.
  - A `ResponseTemplate` linked to the pattern, with status code, header/body template,
    field mappings, and expected variables.
  - One or more `FieldMapping` entries that link request fields (`source`) to response
    fields (`target`), with transformation type (direct, enum, formula) and confidence.
- **Statistics update**: increments `patterns_learned` and `templates_created` in the
  device's `MatchStats`.
- **Export/Import**: `export_patterns()` produces a portable `PatternDB` (`.ride-pattern.json`
  format); `import_patterns()` loads patterns from a portable file, validating them
  against a JSON schema via `Validator`.

#### 2.3.4 PatternEngine (`core/pattern_db/pattern_engine.py`)

**Role**: matches incoming requests against learned patterns and builds local responses
with state variable resolution and safe formula evaluation.

- **Pattern matching**: `find_best_match()` computes a similarity score (0.0–1.0) between the
  incoming request and each known pattern, based on:
  - **Method match** (30%): HTTP method equality.
  - **Path similarity** (30%): path matching with `{id}` placeholder support.
  - **Required headers** (15%): how many of the required headers are present.
  - **Query params** (10%): how many of the expected parameters are present.
  - **Body schema** (15%): key correspondence between the body and the expected schema.
- **Local response construction**: `build_local_response()` starts from a `ResponseTemplate` and:
  1. Applies `field_mappings` to transfer values from the request or device state to the response.
  2. Resolves template variables `{state.variable_name}`, `{request.path.some_field}` and `{uuid}`.
  3. Supports transformations: `direct` (direct copy), `enum` (value mapping), `formula`
     (safe arithmetic expressions evaluated via AST walker).
- **State Management**: each device has a `DeviceStateStore` that maintains persistent
  state variables (e.g. current temperature, operating mode) and virtual sensors.
- **Virtual sensors**: simulated sensors that aggregate state data and apply formulas (e.g.
  moving average, unit conversion).
- **Formula safety**: pattern formulas are evaluated via the
  `simpleeval` library (restricted to arithmetic operations, comparisons,
  and basic math functions — no `eval`/`exec`), replacing the hand-rolled
  AST interpreter — no attribute access, imports, or arbitrary calls, so code injection is prevented.
- **Caching**: patterns can be loaded from `.ride-pattern.json` files into memory for
  ultra-fast matching without touching the database.

---

## 3. Request/Response Correlation

Correlation occurs in the pipeline (`pipeline.py`) and links a device request
to the corresponding cloud response. The mechanism is designed to work with HTTP/1.1,
HTTP/2, WebSocket, CoAP, MQTT, and other protocols.

**Correlation strategies**:

1. **Connection tracking**: requests and responses traversing the same TCP connection
   are temporally associated.
2. **Sequence numbers**: for protocols that support them, sequence numbers are extracted
   and used as the correlation key.
3. **Correlation IDs**: `X-Request-ID`, `X-Correlation-ID` or equivalent headers are used
   to match requests and responses.
4. **Wait timeout**: if a response does not arrive within a configurable timeout (default 30s),
   the request is considered orphaned and discarded.

**Data structure**: each correlated pair is stored in `SessionCache` as:

| Field | Description |
|---|---|
| `correlation_key` | Unique key for matching (connection + seq or correlation ID) |
| `method`, `path`, `headers`, `body` | Original request |
| `response_status`, `response_headers`, `response_body` | Correlated response |
| `correlated` | Boolean flag indicating whether the pair is complete |
| `in_buffer` | Boolean flag indicating whether it has already been sent to the BufferManager |

After correlation, the pair is passed to `BufferManager.add_pair()` for accumulation.
The `SessionCache` is cleared on every buffer flush.

---

## 4. Per-Device Database

**Foundational principle**: each IoT device has its own dedicated protocol database.
This completely isolates learned patterns, preventing interference between different devices
(even from the same vendor) and ensuring each device operates independently.

### 4.1 Architecture

```
┌─────────────────────────────────────────────┐
│  Core Database (SQLite / PostgreSQL)         │
│                                               │
│  DeviceRegistry        ModelRegistry          │
│  ┌─────────────────┐  ┌──────────────────┐   │
│  │ device_id: str   │  │ model_id: str    │   │
│  │ vendor: str      │  │ device_id: str   │   │
│  │ device_type: str │  │ version: str     │   │
│  │ ip_addresses: [] │  │ framework: str   │   │
│  │ mode: str         │  │ model_path: str │   │
│  │ database_url: str?│  │ input_schema:   │   │
│  │ ...              │  │ output_schema:   │   │
│  └─────────────────┘  └──────────────────┘   │
├──────────────────────────────────────────────┤
│  Device DB #1 (per-device, e.g. 192.168.1.42)│
│                                               │
│  RequestPattern   ResponseTemplate            │
│  FieldMapping     LLMContextBuffer            │
│  SessionCache     MatchStats                  │
│  InterceptedRequest                           │
│                                               │
├──────────────────────────────────────────────┤
│  Device DB #2 (per-device, e.g. 192.168.1.77)│
│  ... same tables ...                          │
└──────────────────────────────────────────────┘
```

### 4.2 Per-device database components

| Table | Purpose |
|---|---|
| **DeviceRegistry** | (Core) Central registry. Maps each `device_id` to its vendor, type, IP, operating mode, match threshold, LLM config override, and buffer size. |
| **RequestPattern** | Learned request patterns. Includes method, path pattern, required headers, body schema, deciphered intent, and confidence. |
| **ResponseTemplate** | Local response template. Links to a pattern, specifies status code, header/body template, field mappings, and expected variables. |
| **FieldMapping** | Field-to-field mapping between request and response. Supports transformations: `direct`, `enum` (with value map), `formula` (arithmetic expressions). |
| **LLMContextBuffer** | Sliding window buffer of request/response pairs not yet sent to the LLM. Each entry has an estimated byte size and a `flushed` flag. |
| **SessionCache** | Temporary cache for request/response correlation. Cleared after each buffer flush. |
| **MatchStats** | Real-time statistics: total requests, local hits, cloud misses, errors, match rate percentage, patterns learned, current buffer size. |
| **InterceptedRequest** | Raw history of intercepted requests. Used for audit, debugging, and training on-device ML models. |

### 4.3 IP-based Routing

`DatabaseManager.resolve_device_id(ip_address)` determines which device a given source IP
belongs to by searching the `ip_addresses` list of each `DeviceRegistry`. This is the
central mechanism for IP-first routing: **the source IP is the key to the device's
database**. IP validation and classification (private/loopback) use the stdlib
[`ipaddress`](https://docs.python.org/3/library/ipaddress.html) module.

### 4.4 Custom Databases

Each device can have a completely separate database (custom URL) via the
`DeviceRegistry.database_url` field. This allows physically isolating data from different
devices (e.g. on separate volumes or PostgreSQL clusters). By default, all devices share
a single SQLite database with per-device tables.

---

## 5. Failure Management and Resilience (`core/resilience.py`)

The resilience module verifies that devices can operate independently of the vendor's
cloud. Key features:

- **Cloud Independence Verifier**: REST API that tests whether a device can operate without the
  vendor's cloud, analyzing the completeness of learned patterns and match statistics.
- **Auto-switch scheduler**: runs every 60 seconds and evaluates the match rate of each device.
- **Configurable thresholds**:
  - `AUTO_SWITCH_MATCH_RATE` = 99% (switch to production).
  - `ROLLBACK_MATCH_RATE` = 90% (revert to learning).
  - `MIN_PATTERNS_FOR_SWITCH` = 10 (minimum patterns to consider switching).
  - `MIN_TOTAL_REQUESTS` = 50 (minimum requests for reliable statistics).
- **Forwarding loop prevention**: the `UpstreamResolver` resolves cloud names directly via
  public DNS (8.8.8.8 / 1.1.1.1, dual-stack IPv4+IPv6), bypassing local DNS
  (dnsmasq/Pi-hole/AdGuard Home) to prevent the proxy from re-inserting itself. Resolution
  results are cached with a TTL by the [`cachetools`](https://github.com/tkem/cachetools)
  `TTLCache` (in `core/upstream_resolver.py`).

---

## 6. On-the-Fly Modification (`core/modification.py`)

Real-time modification engine that allows altering requests and responses on the fly according to
configurable rules. It is **active in the HTTP serving path**: before the orchestrator local-match,
`process_request()` runs the rules on the intercepted request (mutating it in place so the pipeline
sees the transformed request); when a local response is served, `process_response()` runs the rules
on it and the result is normalised back to the `{status_code, headers, body}` wrapper. Supports:

- Header, body, query parameter modification.
- JavaScript/CSS injection for debugging.
- Selective logging of modified traffic (`modifications.jsonl` audit log).
- Rules based on regex patterns, HTTP methods, and specific paths.
- JSON field access and updates via dot/bracket paths (`a.b[0].c`) are read/written with the
  [`dpath`](https://github.com/akesterson/dpath-python) library (`_get_json_path`/`_set_json_path`).

---

## 7. Specialized Protocol Adapters (`adapters/`)

The system includes adapters for non-HTTP IoT protocols, each in a dedicated directory:

| Adapter | Protocol | Purpose |
|---|---|---|
| `mqtt/` | MQTT | Pub/sub bridge for cloud-based MQTT devices |
| `coap/` | CoAP | Constrained Application Protocol devices |
| `shelly/` | Shelly API | Shelly-specific device adapter |
| `zigbee/` | Zigbee | Bridge for Zigbee coordinators |
| `zwave/` | Z-Wave | Bridge for Z-Wave controllers |
| `thread_matter/` | Thread / Matter | Bridge for Matter over Thread devices |
| `modbus/` | Modbus | Bridge for industrial Modbus TCP devices |
| `base/` | — | Abstract base class for all adapters |
| `example/` | — | Template for creating new adapters |

Each adapter implements the `InterceptedRequest` interface to normalize requests and responses
from different protocols, allowing the core to process them uniformly.

---

## 8. Portable Formats

The system supports two portable formats for sharing and reusing learning data
between users:

### 8.1 `.ride-capture.json` (CaptureDB)

Contains raw intercepted traffic traces (request/response pairs) in JSON format.
Used for:
- Sharing anonymized traces between users.
- Starting learning on a device without having to intercept live traffic.
- Offline debugging and analysis.

### 8.2 `.ride-pattern.json` (PatternDB)

Contains deciphered patterns, response templates, field mappings, and state configuration.
Used for:
- Distributing already-learned patterns to new devices of the same model.
- Backing up and restoring learning state.
- Collaborative pattern validation among community users.

Both formats are validated via JSON schema and support automatic obfuscation of
sensitive data (device IDs, MAC addresses, serial numbers).

### 8.3 DeviceModel v2 (portable superset of PatternDB)

Since the v2 work, `.ride-pattern.json` is the serialized form of the canonical
**`DeviceModel`** (`core/pattern_db/schemas.py`), a portable superset of the v1
`PatternDB`. It is sufficient to clone a device on a second installation without
re-learning it (no LLM/cloud required within the limits of the contained
knowledge). The v2 root keeps the v1 building blocks at the top level
(`commands` + `responses` + `interactions` + `state_variables` +
`virtual_sensors`) and adds:

- **`protocol`** (`ProtocolInfo`): transport, security, proprietary flag,
  identity, ports, handler and confidence — produced by the first-flush LLM
  identification (`mode="auto"`), not a protocol handler.
- **`observation_history`**: learned traffic history used as grounding for
  synthesising replies to requests never seen before.

```
TRAFFIC → PROTOCOL LAYER → OBSERVATION → AUTO / PROTOCOL IDENTIFICATION
        → LLM LEARNING (batch) → DEVICE MODEL → DEVICE DB
        → COMPILED RUNTIME → LOCAL RESPONSE
```

**First flush (AUTO):** the buffer's first flush instructs the LLM to return a
structured `protocol_info` object that identifies transport / protocol /
security / proprietary-vs-standard / identity / handler / confidence. This is
persisted to the device header and recovered by `export_device_model`.

**Subsequent flushes (model delta):** later flushes ask the LLM for a JSON
*delta* over `commands` / `responses` / `interactions` /
`state_variables` / `virtual_sensors`; `merge_device_model`
(`core/pattern_db/decipher_ingest.py`) upserts it idempotently into the device
DB by deterministic IDs. The LLM is never on the runtime critical path (flush
is async) and produces a structured model update, not raw SQL rows.

**Runtime:** the `PatternEngine` matches on the v1 `PatternDB` projection
(`DeviceModel.to_pattern_db()`); the in-memory compiled model is what answers
requests, the Device DB is persistence, and the DB is not queried per request.

**Export path:** when a device has a learned protocol header, both the runtime
sync (`pipeline._export_and_sync_patterns`) and the HTTP API export
(`GET /api/devices/{id}/patterns/export`) emit the v2 `DeviceModel` (goal #11:
`.ride-pattern.json` = complete clone). Legacy devices that never completed a
first-flush AUTO identification fall back to the v1 `PatternDB` shape. The
HTTP import route dispatches a v2 body to `import_device_model` (preserving
v2-only fields) instead of dropping them via the v1 `PatternDB` parser.

The LLM must learn **semantics and behaviour** of the device; for standard
protocols (Modbus TCP, MQTT, CoAP, HTTP, WebSocket) the protocol handler
interprets the wire format while the LLM learns the meaning; for unknown or
proprietary protocols the LLM analyses Observation batches directly.

---

## 9. Request Lifecycle Summary

```
1. IoT Device → [TLS MITM Server]                    (TLS termination, SNI extraction)
2. [TLS MITM Server] → [TrafficSelector]              (decide: intercept or pass?)
3. [TrafficSelector] → PASSTHROUGH → [UpstreamResolver] → Cloud
                   ↘ INTERCEPT → [Pipeline (LearningOrchestrator)]

   If INTERCEPT and in LEARNING mode:
4. [Pipeline] → correlate observations using protocol-native identifiers
5. [Pipeline] → Buffer claim/snapshot               (per-device batch ownership)
6. [Pipeline] → LLMDecipherService.decipher_batch() (first batch: AUTO ID;
                                                       later batches: model delta)
7. [Pipeline] → merge canonical DeviceModel → compile → atomic runtime swap
8. [Pipeline] → acknowledge claimed batch only after the commit succeeds
10. The original cloud response is forwarded to the device (transparent)

   If INTERCEPT and in PRODUCTION mode:
4'. [Dispatcher] → standard native handler OR proprietary compiled runtime
5'. [Compiled runtime] → deterministic command match → local response
6'. If score < threshold and production_no_fallback → 501 (conclusive local-only)
7'. If score < threshold and signal_forward_to_cloud → nginx forwards to cloud
8'. If score < threshold and no flag → forward + learning from the miss

   Auto-switch (60s scheduler):
     match_rate ≥ 99% → LEARNING → PRODUCTION
     match_rate < 90% → PRODUCTION → LEARNING
```
