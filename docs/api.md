# REST API Reference — Ride-the-API

> All REST endpoints exposed by the proxy for device management, patterns, buffer, and observability.

- **Default Base URL**: `http://localhost:8911`
- **Request/Response Format**: JSON
- **Interactive Documentation**: `http://localhost:8911/docs` (OpenAPI / Swagger UI)

---

## Table of Contents

- [Health Check](#health-check-get-health)
- [Metrics](#metrics-get-metrics)
- [Devices](#devices)
  - [List Devices](#list-devices-get-apidevices)
  - [Device Detail](#device-detail-get-apidevicesdevice_id)
  - [Match Statistics](#match-statistics-get-apidevicesdevice_idstats)
  - [Match Rate](#match-rate-get-apidevicesdevice_idmatch-rate)
  - [Device Mode](#device-mode-post-apidevicesdevice_idmode)
  - [Auto-switch](#auto-switch-getput-apidevicesdevice_idauto-switch)
  - [LLM Configuration](#llm-configuration-put-apidevicesdevice_idllm)
  - [TLS Configuration](#tls-configuration-put-apidevicesdevice_idtls-config)
  - [Context Notes](#context-notes-getput-apidevicesdevice_idcontext)
  - [Database Assignment](#database-assignment-post-apidevicesdevice_iddatabase)
  - [IP Resolution](#ip-resolution-get-apidevicesby-ipip_address)
  - [IP Registration](#ip-registration-post-apidevicesdevice_idip)
  - [Bypass mode](#bypass-mode-get-apiip-profilesip_addressbypass)
- [Buffer](#buffer)
  - [List Buffer](#list-buffer-get-apidevicesdevice_idbuffer)
  - [Delete Buffer Entry](#delete-buffer-entry-delete-apidevicesdevice_idbufferentry_id)
  - [Flush to LLM](#flush-to-llm-post-apidevicesdevice_idllmflush)
  - [LLM Preview](#llm-preview-post-apidevicesdevice_idllmpreview)
- [Patterns](#patterns)
  - [List Patterns](#list-patterns-get-apidevicesdevice_idpatterns)
  - [Pattern Detail](#pattern-detail-get-apidevicesdevice_idpatternspattern_id)
  - [Update Pattern](#update-pattern-put-apidevicesdevice_idpatternspattern_id)
  - [Partial Update Pattern](#partial-update-pattern-patch-apidevicesdevice_idpatternspattern_id)
  - [Delete Pattern](#delete-pattern-delete-apidevicesdevice_idpatternspattern_id)
  - [Export Patterns](#export-patterns-get-apidevicesdevice_idpatternsexport)
  - [Import Patterns](#import-patterns-post-apidevicesdevice_idpatternsimport)
- [Capture](#capture)
  - [Export Capture](#export-capture-get-apidevicesdevice_idcaptureexport)
  - [Import Capture](#import-capture-post-apidevicesdevice_idcaptureimport)
- [TLS / MITM](#tls--mitm)
  - [Download CA](#download-ca-get-apitlsca-cert)
  - [TLS Statistics](#tls-statistics-get-apitlsstats)
  - [TLS Devices](#tls-devices-get-apitlsdevice-ports)
  - [Unidentified](#unidentified-get-apitlsunidentified)
  - [List TLS Ports](#list-tls-ports-get-apitlsports)
  - [Add TLS Port](#add-tls-port-post-apitlsports)
  - [Remove TLS Port](#remove-tls-port-delete-apitlsportsport)
  - [List Certificates](#list-certificates-get-apitlscerts)
  - [Certificate Info](#certificate-info-get-apitlscertshostname)
  - [Upload Certificate](#upload-certificate-post-apitlscertsupload)
  - [Upload Certificate JSON](#upload-certificate-json-post-apitlscertsupload-json)
  - [Delete Certificate](#delete-certificate-delete-apitlscertshostname)
  - [Rotate Certificate](#rotate-certificate-post-apitlscertshostnamerotate)
  - [Download Root CA](#download-root-ca-post-apitlsroot-cadownload)
  - [Frida Script](#frida-script-get-apitlsfridascriptjs)
- [LLM Profiles](#llm-profiles)
- [Protocol Servers](#protocol-servers)
- [DNS Interception / Discovery](#dns-interception--discovery)
  - [DNS Settings](#dns-settings-getput-apidnssettings)
  - [DNS Discovery](#dns-discovery-get-apidnsdiscovery)
  - [Promote Intercept](#promote-intercept-post-apidnsintercept)
  - [Remove Intercept](#remove-intercept-delete-apidnsintercepthostname)
  - [Clear Discovery](#clear-discovery-delete-apidnsdiscovery)
- [Cloud Independence](#cloud-independence)

---

## Authentication

Every `/api/*` route requires the control-plane **password** when
`security.auth_enabled` is `true` (default). The password grants full access
(reads and writes) — there is no read-only vs admin distinction.

Send it via the `X-API-Key` header or `Authorization: Bearer <password>`:

```bash
curl -H "X-API-Key: your-password" http://<server>:8911/api/devices
```

If auth is enabled but `security.password` is empty in `config.yaml`, the
control plane is **locked**: every `/api/*` request returns `503` with the
header `X-Auth-Setup: required`, so the UI knows a password must be created
before anything else.

### First-run password setup: `POST /api/auth/password`

On a fresh install (auth enabled, `security.password` empty) this is the **only**
route reachable without credentials, so the UI can guide the operator to create
the control-plane password from the browser instead of editing `config.yaml`.

Because the endpoint has no credential to validate against yet, the first-run
window is protected by a **one-time setup token**: it is generated at startup
while no password exists, printed in the server logs and written to
`ridebase/setup-token` (mode `0600`). Requests must carry it in the
`X-Setup-Token` header — without it anyone able to reach the port could claim
administration before the operator configures the instance.

```bash
curl -X POST http://<server>:8911/api/auth/password \
  -H "Content-Type: application/json" \
  -H "X-Setup-Token: <token from the logs>" \
  -d '{"password": "<new password>"}'
```

Body: `{"password": "<new password>"}` — must be at least 8 characters.

| Case | Response |
|------|----------|
| No password set yet, valid body **+ setup token** | `200` `{"status":"ok","config_updated":true}` |
| No password set yet, missing/wrong setup token | `403` + `X-Auth-Setup: token-required` |
| Empty / too-short password | `400` |
| A password is already configured | `403` "A password is already set" |
| Non-JSON body | `400` |

Once a password exists, this endpoint requires the normal password like every
other `/api/*` route (it is not a bypass) and the setup token is no longer used.

---

## Health Check

### `GET /health`

Checks the service status. The `version` field reports the **installed release
tag**: the deploy scripts (`deploy/rideapi`, `bootstrap.sh`, `update.sh`) write
the installed tag to `<install-dir>/VERSION`, which the server reads from the
process CWD (the install root in both source and frozen layouts). Falls back to
the static app version when no `VERSION` file exists (dev checkout).

**Response `200 OK`**

```json
{
  "status": "healthy",
  "service": "local-cloud-replacement-proxy",
  "version": "1.2.15",
  "adapters": ["example"],
  "ports": {
    "proxy": {"port": 8911, "listening": true},
    "dns": {"enabled": true, "host": "0.0.0.0", "port": 53, "udp": true, "tcp": true,
            "udp_v6": true, "tcp_v6": false},
    "tls_mitm": {"requested": [443, 8883, 5684, 8443], "bound": [8883, 5684, 8443], "missing": [443]}
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `"healthy"` if the service is operational |
| `service` | string | Service name |
| `version` | string | Current version |
| `adapters` | array[string] | List of registered vendors/adapters |
| `ports` | object | What is actually bound: `proxy`, `dns` (`udp`/`tcp`/`udp_v6`/`tcp_v6`) and `tls_mitm` (`requested`/`bound`/`missing`) |

The `ports` block is the first thing to read when a device "does not work": a listener that
never came up (a port below 1024 without `CAP_NET_BIND_SERVICE`) is the usual cause, and it is
otherwise invisible. A port that cannot be bound is reported, never fatal — the rest of the
service keeps running.

**Status codes**: `200` OK, `503` Service Unavailable

---

## Metrics

### `GET /metrics`

Exposes metrics in Prometheus format. Configurable in `config.yaml`:

```yaml
observability:
  metrics:
    enabled: true
    port: 9090
    path: "/metrics"
```

**Note**: Metrics are served on port `9090` (not on the proxy's 8911)
when enabled. Use Prometheus or any scraper to collect them.

**Response**: Text in Prometheus exposition format (`text/plain; version=0.0.4`).

Exposed metrics (from `prometheus-client` and custom metrics):

| Name | Type | Description |
|------|------|-------------|
| `ride_api_requests_total` | Counter | Total requests processed |
| `ride_api_local_hits_total` | Counter | Local responses served |
| `ride_api_cloud_misses_total` | Counter | Requests forwarded to the cloud |
| `ride_api_device_count` | Gauge | Registered devices |
| `ride_api_buffer_size_bytes` | Gauge | Total buffer size |
| `python_*` | — | Standard Python runtime metrics |

**Status codes**: `200` OK. When `observability.metrics.enabled` is `false`,
the metrics server is not started, so the port is closed (the endpoint is not
served).

---

## Devices

### List Devices: `GET /api/devices`

Returns all registered devices.

**Response `200 OK`**

```json
{
  "devices": [
    {
      "device_id": "ip-192-168-1-42",
      "vendor": "example",
      "name": "Living Room AC",
      "mode": "learning",
      "auto_switch_enabled": false,
      "database_url": "sqlite+aiosqlite:///./ridebase/devices/ip-192-168-1-42.db",
      "ip_addresses": ["192.168.1.42"],
      "last_seen": "2025-06-15T11:30:00Z",
      "created_at": "2025-06-15T10:30:00Z"
    }
  ]
}
```

`last_seen` is when the proxy last saw the device present itself on a TLS port.
It is **stored in the registry**, so it survives a restart. Because a device may
reconnect continuously, the row is rewritten only once every 60 seconds per
device: the timestamp is accurate to that interval, and a write per connection
would wear the disk for a difference nobody can perceive.

**Status codes**: `200` OK, `503` Service not ready

---

### Device Detail: `GET /api/devices/{device_id}`

Returns complete details and statistics for a device.

**Path parameters**

| Parameter | Type | Description |
|-----------|------|-------------|
| `device_id` | string | Device ID (e.g. `ip-192-168-1-42`) |

**Response `200 OK`**

```json
{
  "device": {
    "device_id": "ip-192-168-1-42",
    "vendor": "example",
    "mode": "learning",
    "match_rate_pct": 87.5,
    "total_requests": 240,
    "local_hits": 210,
    "cloud_misses": 30,
    "patterns_learned": 12,
    "buffer_flushes": 8,
    "current_buffer_size_bytes": 128000,
    "bypass": false,
    "tls_passthrough": false,
    "tls_untrusted_reason": null,
    "tls_refusals": 0,
    "tls_first_refusal_at": null,
    "tls_last_refusal_at": null,
    "seen_hostnames": ["ats.iot.vendor-cloud.example"],
    "capture": {
      "state": "capturing",
      "reason": "",
      "hint": "requests are analysed and buffered. ..."
    }
  }
}
```

**Why a device produces no pairs.** `capture` answers that question in one field, because the
causes otherwise live in three different switches — and reading the wrong one is how an empty
buffer looks like a broken pipeline:

| `capture.state` | Meaning |
|-----------------|---------|
| `capturing` | nothing is in the way: the device *can* produce pairs, so an empty buffer means it is not reaching the proxy at all |
| `bypassed` | the IP is in per-IP bypass: traffic goes straight to the cloud, no analysis, no buffering |
| `relayed` | TLS passthrough is on: either the operator enabled it, or the device refused our certificate (`tls_untrusted_reason`, e.g. `client_refused_certificate`) and the proxy relays it encrypted from then on |
| `passthrough` | the device mode is `passthrough`: forwarded without analysis or buffering |

`capture.reason` carries the machine-readable cause (`ip_bypass`, `mode_passthrough`, the relay
reason, or empty) and `capture.hint` is one sentence ready for a human. The states are evaluated
in the order the runtime hits them: bypass, then relay (settled during the handshake), then mode.
`seen_hostnames` lists the SNI values the device has asked for (evidence of what it talks to) and
`tls_untrusted_reason` is why its TLS is relayed, if it is.

**How many times a device refused.** `tls_refusals` counts the handshakes the device aborted, with
`tls_first_refusal_at` and `tls_last_refusal_at` (ISO 8601, UTC) saying when it started and when it
last did. A relayed device that refused once and is forwarded ever since reads `1`; a count that
keeps growing means the device is still opening connections that reach the handshake, which is a
different situation from a settled one. When a count exists it is repeated inside `capture` as
`refusals` (plus `last_refusal_at`), so a client reading only `capture` still sees whether the
relay was a one-off. A device that never refused reports `tls_refusals: 0`.

**Status codes**: `200` OK, `404` Device not found, `503` Service not ready

---

### Match Statistics: `GET /api/devices/{device_id}/stats`

Real-time statistics for a device (alias of `/api/devices/{device_id}`).

**Response** `{"stats": { ... }}` — same fields as `/api/devices/{device_id}`.

---

### Match Rate: `GET /api/devices/{device_id}/match-rate`

Local match percentage, useful for monitoring learning progress.

**Response `200 OK`**

```json
{
  "device_id": "ip-192-168-1-42",
  "match_rate_pct": 87.5,
  "total_requests": 240,
  "local_hits": 210,
  "cloud_misses": 30
}
```

---

### Device Mode: `POST /api/devices/{device_id}/mode`

Changes the operating mode of a device.

**JSON Body**

```json
{
  "mode": "production"
}
```

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `mode` | string | `learning`, `production`, `hybrid` | Operating mode |

**Response `200 OK`** `{"device_id": "...", "mode": "production"}`

**Status codes**: `200` OK, `400` Invalid mode, `404` Device not found, `503` Service not ready

---

### Auto-switch: `GET /api/devices/{device_id}/auto-switch`

Reads the auto-switch state for a device.

**Response `200 OK`**

```json
{
  "device_id": "ip-192-168-1-42",
  "auto_switch_enabled": false
}
```

### Auto-switch: `PUT /api/devices/{device_id}/auto-switch`

Enables or disables automatic switching from learning to production.

**JSON Body**

```json
{
  "enabled": true
}
```

**Response `200 OK`** `{"device_id": "...", "auto_switch_enabled": true}`

**Notes**: When enabled, the `AutoSwitchScheduler` automatically switches the
device to `production` when the match rate reaches 99% with at least
10 learned patterns and 50 total requests. If the rate drops below 90%, it reverts
to `learning`.

---

### LLM Configuration: `PUT /api/devices/{device_id}/llm`

Configures the LLM profile for a specific device (overrides the default profile).

**JSON Body**

```json
{
  "base_url": "http://localhost:11434/v1",
  "model_id": "llama3.2:1b",
  "profile_name": "local_ollama"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `base_url` | string | OpenAI-compatible API base URL |
| `model_id` | string | Model identifier |
| `profile_name` | string | Profile name (optional) |

**Response `200 OK`** `{"device_id": "...", "status": "updated"}`

---

### TLS Configuration: `PUT /api/devices/{device_id}/tls-config`

Updates name, vendor, passthrough, and pinning bypass for a device.

**JSON Body**

```json
{
  "name": "Kitchen Thermostat",
  "vendor": "example",
  "passthrough": true,
  "pinning_bypass": "mitm_proxy"
}
```

---

### Context Notes: `GET /api/devices/{device_id}/context`

Reads contextual notes for a device (useful as hints for the LLM).

**Response** `{"device_id": "...", "context_notes": "Thermostat model XYZ"}`

### Context Notes: `PUT /api/devices/{device_id}/context`

Updates contextual notes.

**JSON Body** `{"context_notes": "Thermostat model ABC-123, firmware 2.4"}`

---

### Database Assignment: `POST /api/devices/{device_id}/database`

Assigns a specific database to a device (SQLite or PostgreSQL).

**JSON Body**

```json
{
  "database_name": "my_ac_protocol"
}
```

OR

```json
{
  "database_url": "postgresql://localhost/my_ac_protocol"
}
```

---

### Database Inventory: `GET /api/databases`

Lists every device database that exists, taken from the device registry, so a
database is listed as soon as its device is known — including right after a
restart, before anything has touched it. `is_active` says whether that device's
engine is currently open in this process.

```json
{
  "databases": [
    {
      "device_id": "ip-192-168-1-42",
      "database_url": "sqlite+aiosqlite:///./ridebase/devices/ip-192-168-1-42.db",
      "is_active": true
    }
  ]
}
```

### Database Rename: `POST /api/devices/{device_id}/database/rename`

Renames a device's database file so it follows the device name. The name is
normalized into a filename segment (whitespace and separators become hyphens)
and the `.db` suffix is added when missing. The device keeps working: its engine
is closed, the file together with its SQLite `-wal`/`-shm` sidecars is moved, and
the device is repointed at the new file.

This is also what happens automatically when a device is given a name through
`PUT /api/devices/{device_id}/tls-config`: a device named after the fact stops
being stored under a bare address. If the target name is already taken by
another device's file, the device is renamed anyway and only the file keeps its
name.

**JSON Body**

```json
{
  "name": "Purifier Soggiorno"
}
```

**Response**

```json
{
  "device_id": "ip-192-168-1-42",
  "name": "Purifier Soggiorno",
  "database_url": "sqlite+aiosqlite:///./ridebase/devices/Purifier-Soggiorno.db"
}
```

Errors: `400` when no name is given, `404` when the device is unknown, `409` when
another device already uses that file name.

---

### IP Resolution: `GET /api/devices/by-ip/{ip_address}`

Looks up a device by IP address.

**Response `200 OK`**

```json
{
  "device_id": "ip-192-168-1-42",
  "ip_address": "192.168.1.42"
}
```

---

### Bypass mode: `GET /api/ip-profiles/{ip_address}/bypass`

Returns the per-IP bypass mode (from `ip_profiles` config, default `false`).

**Response `200 OK`**

```json
{
  "ip_address": "192.168.1.42",
  "bypass": false
}
```

### Bypass mode: `PUT /api/ip-profiles/{ip_address}/bypass`

Enables/disables bypass for an IP, persisting to the config file. A bypassed IP
still transits the gateway but is forwarded straight to the real cloud with no
analysis (no buffer/LLM/local match).

**Request body**

```json
{ "bypass": true }
```

**Response `200 OK`**

```json
{ "ip_address": "192.168.1.42", "bypass": true }
```

---

### IP Registration: `POST /api/devices/{device_id}/ip`

Associates an IP address with a device.

**JSON Body** `{"ip_address": "192.168.1.100"}`

---

## Buffer

### List Buffer: `GET /api/devices/{device_id}/buffer`

Lists buffer entries not yet processed by the LLM.

**Response `200 OK`**

```json
{
  "device_id": "ip-192-168-1-42",
  "entries": [
    {
      "id": 1,
      "pair": {
        "method": "GET",
        "path": "/v1/device/status",
        "request_headers": {"Host": "api.example.com"},
        "response_status": 200
      },
      "size": 2048
    }
  ],
  "total_entries": 15,
  "total_size_bytes": 128000
}
```

---

### Delete Buffer Entry: `DELETE /api/devices/{device_id}/buffer/{entry_id}`

Removes a single entry from the buffer.

| Parameter | Type | Description |
|-----------|------|-------------|
| `entry_id` | int | Numeric entry ID |

**Response `200 OK** `{"device_id": "...", "entry_id": 1, "status": "deleted"}`

---

### Flush to LLM: `POST /api/devices/{device_id}/llm/flush`

Sends selected buffer entries to the LLM for analysis and learning.

**JSON Body**

```json
{
  "pair_ids": [1, 2, 3, 4, 5],
  "context_notes": "The device sends a heartbeat every 60 seconds"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `pair_ids` | array[int] | (Optional) Specific IDs to flush. If omitted, full flush |
| `context_notes` | string | (Optional) Contextual notes to guide the LLM |

**Response `200 OK`**

```json
{
  "success": true,
  "flushed": 5,
  "patterns_found": 3,
  "patterns_saved": 3
}
```

---

### LLM Preview: `POST /api/devices/{device_id}/llm/preview`

Analyzes entries without saving patterns. Used to validate before importing.

**JSON Body**: Same structure as `/llm/flush`.

**Response**: LLM analysis without persistence.

### Buffer Backend: `GET /api/settings/buffer-backend`

Returns the active buffer storage backend (`disk` or `memory`).

**Response `200 OK`** `{"backend": "disk"}`

---

### Buffer Backend: `PUT /api/settings/buffer-backend`

Switches the active buffer backend at runtime and persists the choice for the next start.

**JSON Body** `{"backend": "sqlite"}`

| Value | Aliases | Description |
|-------|---------|-------------|
| `disk` | `sqlite` | Durable per-device buffer database — **captured pairs survive a restart** (default) |
| `memory` | `ram` | In-process RAM buffer: faster, and gone when the process ends |

**Switching is lossless.** Whatever has not been flushed yet is copied into the new store
before the old one is marked flushed, and every live buffer is repointed at the new store, so
no capture is lost in one direction and none is buffered twice in the other. An unknown name
is refused with `400` naming the accepted ones — never a silent fallback, which is how an
operator ends up believing captures are durable while the proxy runs the volatile backend.

**Response `200 OK`**

```json
{ "backend": "disk", "from": "memory", "devices": 1, "pairs": 3 }
```

| Field | Description |
|-------|-------------|
| `backend` | The backend now in force |
| `from` | The backend that was in force |
| `devices` | How many devices had unflushed pairs carried over |
| `pairs` | How many pairs were carried over |

**Status codes**: `200` OK, `400` invalid body or unknown backend, `503` Service not ready

---

## Patterns

### List Patterns: `GET /api/devices/{device_id}/patterns`

Returns all learned patterns for a device, sorted by confidence
in descending order.

**Response `200 OK`**

```json
{
  "device_id": "ip-192-168-1-42",
  "patterns": [
    {
      "pattern_id": "abc123",
      "method": "GET",
      "path": "/v1/device/status",
      "path_pattern": "/v1/device/status",
      "protocol": "http",
      "intent": "get_status",
      "confidence": 0.95,
      "hit_count": 42,
      "required_headers": ["Host"],
      "body_schema": {},
      "query_param_keys": [],
      "response_template": {
        "status_code": 200,
        "body_template": {"temperature": 22.5, "mode": "cool"},
        "headers_template": {"content-type": "application/json"},
        "field_mappings": {}
      }
    }
  ]
}
```

---

### Pattern Detail: `GET /api/devices/{device_id}/patterns/{pattern_id}`

Full detail of a pattern, including field mappings.

**Response `200 OK`**: Includes `pattern`, `response_template` and `field_mappings`.

---

### Update Pattern: `PUT /api/devices/{device_id}/patterns/{pattern_id}`

Full update (upsert) of a pattern, response template, and field mappings.

**JSON Body**: Same format as `GET patterns/{pattern_id}` response, with
an additional `field_mappings` array.

If the `pattern_id` does not exist, it is created (upsert).

---

### Partial Update Pattern: `PATCH /api/devices/{device_id}/patterns/{pattern_id}`

Partial update — only the fields present in the body are modified.

---

### Delete Pattern: `DELETE /api/devices/{device_id}/patterns/{pattern_id}`

Deletes the pattern, its associated response template, and all field mappings
for that intent.

**Response `200 OK`** `{"status": "deleted", "pattern_id": "abc123"}`

---

### Export Patterns: `GET /api/devices/{device_id}/patterns/export`

Exports all patterns of a device in the portable `.ride-pattern.json` format.

> **v2**: when the device has a learned protocol (first-flush `mode="auto"`),
> the export is a **`DeviceModel` v2** document (see
> `docs/portable-pattern-database.md`): the v1 `client`/`server` shape is kept
> at the root as `commands`/`responses` plus `protocol` (`ProtocolInfo`) and
> `observation_history`. It is sufficient to clone the device on a second
> installation without re-learning.

**Response `200 OK`**: JSON document conforming to the `PatternDB` schema containing:

```json
{
  "meta": {
    "pattern_id": "...",
    "vendor": "example",
    "device_type": "ac",
    "format_version": "1.0",
    "exported_at": "2025-06-15T10:30:00Z"
  },
  "client": {
    "version": "0.1.0",
    "endpoints": [
      {
        "intent": "get_status",
        "method": "GET",
        "path": "/v1/device/status",
        "headers": {"required": ["Host"]},
        "body_schema": {},
        "query_params": [],
        "confidence": 0.95
      }
    ]
  },
  "server": {
    "state_variables": [],
    "virtual_sensors": [],
    "responses": [
      {
        "status_code": 200,
        "headers_template": {"content-type": "application/json"},
        "body_template": {"temperature": 22.5, "mode": "cool"},
        "expected_variables": [],
        "triggers": ["get_status"]
      }
    ]
  }
}
```

---

### Import Patterns: `POST /api/devices/{device_id}/patterns/import`

Imports patterns from a `.ride-pattern.json` file. Patterns are validated against
the JSON schema before importing. v1 `PatternDB` and v2 `DeviceModel` documents
are both accepted (`$schema`-aware validation, see `core/pattern_db/validator.py`).
A v2 `DeviceModel` body is routed to `import_device_model` (persists
`commands`/`responses`/`protocol`/`state_variables`/`virtual_sensors`), so
importing a full clone does **not** silently drop v2-only fields.

**JSON Body**: `PatternDB` document (same format as export).

**Response `200 OK`**

```json
{
  "imported": 12,
  "device_id": "ip-192-168-1-42",
  "warnings": []
}
```

**Status codes**: `200` OK, `422` Validation error (with details), `400` Bad request

**Note**: The `.ride-pattern.json` format is designed for sharing between
installations and for backup. Imported patterns are applied to the device
state and configured virtual variables/sensors.

**Device registration**: Importing patterns for a device that has not yet
sent traffic **registers it** in the core device registry (vendor and
`device_type` are taken from the pattern `meta`), so its mode can be set to
`production` and requests can be served immediately — no need to wait for the
device to appear via intercepted traffic first.

---

## Capture

Captures contain **raw** request/response pairs (not yet analyzed
by the LLM), in the `.ride-capture.json` format.

### Export Capture: `GET /api/devices/{device_id}/capture/export`

Exports the raw buffer in a portable format.

**Response `200 OK`**: JSON document conforming to the `CaptureDB` schema:

```json
{
  "meta": {
    "capture_id": "ip-192-168-1-42-20250615103000",
    "vendor": "example",
    "device_type": "ac",
    "capture_date": "2025-06-15T10:30:00Z"
  },
  "device_info": {
    "device_id": "ip-192-168-1-42"
  },
  "sessions": [
    {
      "session_id": "export_001",
      "timestamp_start": "2025-06-15T10:30:00Z",
      "pairs": [
        {
          "pair_id": "uuid-...",
          "timestamp": "2025-06-15T10:30:00Z",
          "protocol": "http",
          "method": "GET",
          "path": "/v1/device/status",
          "headers": {"Host": "api.example.com"},
          "body": null,
          "response": {
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body": {"temperature": 22.5}
          }
        }
      ]
    }
  ]
}
```

---

### Import Capture: `POST /api/devices/{device_id}/capture/import`

Imports raw pairs into a device's buffer. JSON Schema validation before
import.

**JSON Body**: `CaptureDB` document (same format as export).

**Response `200 OK`**

```json
{
  "imported": 25,
  "device_id": "ip-192-168-1-42",
  "warnings": []
}
```

**Status codes**: `200` OK, `422` Validation error (with details), `400` Bad request

**Note**: Imported captures are added to the existing buffer. When the buffer
reaches maximum capacity (default 512 KB), it is reported and can be
manually flushed to the LLM via `POST /api/devices/{device_id}/llm/flush`.

---

## TLS / MITM

### Download CA: `GET /api/tls/ca-cert`

Downloads the CA certificate in PEM format for installation on devices.

**Response**: `application/x-pem-file` with `Content-Disposition: attachment; filename=ride-the-api-ca.pem`

**Status codes**: `200` OK, `503` Cert manager not ready

---

### TLS Statistics: `GET /api/tls/stats`

MITM server status and certificate statistics.

```json
{
  "cert_manager": {
    "ca_loaded": true,
    "total_certs": 5,
    "expiring_certs": 0
  },
  "mitm_server": true,
  "listen_ports": [443, 8883, 8443],
  "open_relays": 1,
  "device_ports": [
    {
      "ip": "192.168.1.42",
      "port": 443,
      "device_id": "ip-192-168-1-42",
      "first_seen": "2025-06-15T10:30:00",
      "last_seen": "2025-06-15T11:30:00"
    }
  ]
}
```

---

### Relayed Connections: `GET /api/tls/relayed`

Connections forwarded to the vendor cloud **without terminating TLS**, newest first. These come from
devices that pin their vendor's certificate: they refuse any certificate the proxy generates, so
their payload is never decrypted and cannot be read. The contact itself is still recorded, and for
such a device it is the only evidence of activity.

A contact is written **when the connection ends**, and a device can hold one session open for hours
(measured: ~4 h with ~32 KB up and ~22 KB down). The list below would therefore look empty for
exactly as long as the device is busy, so the connections being forwarded at this instant are listed
separately in `in_progress` (`open` is how many there are). One call answers both "has it ever
connected" and "is it connected now".

Query parameters:

| parameter | default | description |
| --- | --- | --- |
| `limit` | 50 | how many entries to return (1-500) |

```json
{
  "relayed": [
    {
      "device_id": "ip-192.168.1.50",
      "client_ip": "192.168.1.50",
      "server_name": "ats.iot.vendor-cloud.example",
      "destination_port": 443,
      "upstream_ip": "203.0.113.10",
      "bytes_to_cloud": 444,
      "bytes_to_device": 3981,
      "duration_ms": 62,
      "outcome": "relayed",
      "started_at": "2026-01-01T12:00:00+00:00"
    }
  ],
  "total": 1,
  "open": 1,
  "in_progress": [
    {
      "client_ip": "192.168.1.50",
      "server_name": "ats.iot.vendor-cloud.example",
      "destination_port": 443,
      "upstream_ip": "203.0.113.10",
      "bytes_to_cloud": 32626,
      "bytes_to_device": 22014,
      "duration_ms": 8130000,
      "started_at": "2026-01-01T08:44:07+00:00",
      "in_progress": true
    }
  ]
}
```

`in_progress` entries carry the same shape as a recorded contact (same field names, so a client can
render them with the same code) plus `in_progress: true`, and they have no `device_id`: resolving
the device would mean a database read on the forwarding path, so the client maps `client_ip`
itself. Their byte counts and `duration_ms`
are the values **so far** — they grow while the session lives. `started_at` here is a real start
time; for recorded contacts it is when the record was written, i.e. the moment the session ended
(compute the start as `started_at - duration_ms`).

`outcome` is one of `relayed` (the two ends were connected and bytes moved both ways),
`no_resolver` (nothing wired to resolve hostnames), `unresolvable` (the name did not resolve),
`unreachable` (resolved but the connection could not be established). Failed attempts are recorded
too: an attempt that never got through is still an attempt, and it dates the device.

The count per device is also exposed by `GET /api/devices` as `relayed_connections`, which is what
the dashboard shows on each device card. `GET /api/tls/stats` exposes the live aggregate as
`open_relays`.

### TLS Devices: `GET /api/tls/device-ports`

IP → device mapping for all active TLS connections.

---

### Unidentified: `GET /api/tls/unidentified`

List of devices with IDs beginning with `ip-` (auto-created
by the TLS handler, not yet renamed).

---

### List TLS Ports: `GET /api/tls/ports`

Currently active TLS listening ports. **Read-only**: the monitored set is
configured *only* in `config.yaml` (`tls_decrypt.listen_ports`) and is never
added/removed at runtime — edit config.yaml and restart the service to change it.

**Response** `{"ports": [443, 8443, 8883, 5684, 802], "enabled": true}`

> Removed: `POST /api/tls/ports` and `DELETE /api/tls/ports/{port}` (runtime
> port management). All listening ports are controlled from config.yaml only.

---

### List Certificates: `GET /api/tls/certs`

List of all imported and auto-generated certificates.

---

### Certificate Info: `GET /api/tls/certs/{hostname}`

Details of a certificate for a specific hostname.

> **Error responses** (these endpoints and the other certificate routes). A rejected request —
> invalid payload, unsafe hostname, unreadable certificate — returns `400` with a **stable
> message**: the exception text is deliberately **not** echoed, because it can carry filesystem
> paths or other internals. The detail is written to the server log as
> `Rejected request (<message>): <detail>`. Unexpected failures return
> `500 {"error": "An internal error occurred"}`. The same applies to the pattern and capture
> import endpoints.

---

### Upload Certificate: `POST /api/tls/certs/upload`

Uploads certificate + private key in PEM format (multipart form).

| Field | Type | Description |
|-------|------|-------------|
| `hostname` | string | Host name (form) |
| `cert` | file | PEM certificate file |
| `key` | file | PEM private key file |

---

### Upload Certificate JSON: `POST /api/tls/certs/upload-json`

Same operation but in JSON with base64-encoded PEM.

**JSON Body**

```json
{
  "hostname": "api.example.com",
  "cert_base64": "LS0tLS1CRUdJTiBDRV...",
  "key_base64": "LS0tLS1CRUdJTiBSU0..."
}
```

---

### Delete Certificate: `DELETE /api/tls/certs/{hostname}`

Removes an imported certificate (reverts to auto-generated certificate).

---

### Rotate Certificate: `POST /api/tls/certs/{hostname}/rotate`

Replaces a certificate without downtime. Same format as `upload-json`.

---

### Download Root CA: `POST /api/tls/root-ca/download`

Alias of `GET /api/tls/ca-cert`.

---

### Frida Script: `GET /api/tls/frida/script.js`

Returns a Frida JavaScript script to bypass certificate pinning
on Android devices. Usage:

```bash
frida -U -l script.js <app-name>
```

**Response**: `application/javascript`

---

## LLM Profiles

Endpoints for managing user-saved LLM profiles.

### `GET /api/llm/settings` · `PUT /api/llm/settings`

The **single source of truth** for the system LLM profiles used by the decipher
(`enabled`, `default_profile`, `profiles`, `task_profiles`). `PUT` validates the
payload and persists it to the runtime override file, so a change takes effect
immediately.

`task_profiles` binds a **purpose** to a profile: `correlation` (validate a weak match against
the learned patterns), `flush` (analyse the buffer and learn) and `learning_context` (the flush
grounded with the already-learned model). An empty value leaves the purpose unbound, so the
device profile — and then `default_profile` — applies.

`llm_decipher` is therefore **owned by this endpoint**, which is one of two
entry points onto the same store: the LLM page (dashboard modal) and the
Configuration page's "LLM Decipher" section, which loads the effective values
and saves them through this API.

A generic `PUT /api/config` can never change them: because a config save always
sends the whole tree, the server stamps the *effective* (runtime) LLM section
back in — present or not — so a save can neither overwrite nor drop the profiles
and `config.yaml` stays aligned with the runtime.

### `GET /api/llm/profiles`

List of system LLM profiles (from `config.yaml`).

### `POST /api/llm/profiles/test`

Sends a small "hello" completion to a provider and reports whether it answers — this is
the dashboard's per-profile **Test** button. The body carries the (possibly unsaved) form
values: `base_url` and `model_id` are required, `api_key` is optional (empty for local
providers that ignore auth), `timeout` defaults to 30 and `max_retries` to 2.

Returns `200 {"success": true, ...}` on a non-empty completion, or `502` with
`{"success": false, "error": ...}` when the provider rejects the call.

Each test builds a **throwaway client** from the request's own values — deliberately
**not** taken from the shared per-profile client cache — and closes it afterwards. A
cached client must never be reused here: caching the throwaway profile under a fixed
name would pin the first test's `base_url`/`api_key`, so every later test would keep
using stale credentials and a corrected key would appear to have no effect.

### `GET /api/llm/user-profiles`

List of saved user profiles.

### `POST /api/llm/user-profiles`

Creates a new user profile.

**JSON Body**

```json
{
  "name": "my_analysis_profile",
  "prompt_template": "Analyze the following messages...",
  "model_id": "gpt-4o-mini",
  "base_url": "https://api.openai.com/v1"
}
```

### `GET /api/llm/user-profiles/{name}`

Detail of a user profile.

### `PUT /api/llm/user-profiles/{name}`

Updates a user profile.

### `DELETE /api/llm/user-profiles/{name}`

Deletes a user profile.

---

## Protocol Servers

### `GET /api/protocol-servers`

Status of all protocol servers (MQTT, CoAP, Modbus, WebSocket, etc.).

```json
{
  "servers": [
    {"name": "mqtt", "running": true, "port": 1883},
    {"name": "coap", "running": false, "port": 5683}
  ]
}
```

### `POST /api/protocol-servers/{name}/start`

Starts a specific protocol server **and persists `enabled: true`** in
`config.yaml`, so it auto-starts on the next boot and the dashboard "config"
view reflects it.

**Response `200 OK`**

```json
{ "status": "ok", "server": "mqtt", "running": true, "persisted": true }
```

### `POST /api/protocol-servers/{name}/stop`

Stops a protocol server **and persists `enabled: false`** in `config.yaml`, so
it stays stopped after a restart.

**Response `200 OK`**

```json
{ "status": "ok", "server": "mqtt", "running": false, "persisted": true }
```

### `GET /api/protocol-servers/{name}/config`

Configuration of a specific protocol server.

---

## DNS Interception / Discovery

The DNS interception server and its discovery log. The policy precedence is
**an exclusion you wrote > adapter claim > the shipped default exclusions >
`intercept` > `intercept_all`**: a hand-typed exclusion wins over everything (adapter
claims included), while the exclusions shipped by default yield to a name an adapter
explicitly claims — see `configuration.md` for why both tiers exist.

### `GET /api/dns/settings`

The full interception policy plus the ports actually bound.

```json
{
  "enabled": true, "listen_host": "0.0.0.0", "listen_port": 53,
  "answer_ip": "192.168.1.200", "answer_ttl": 60,
  "dns_servers": ["8.8.8.8", "1.1.1.1"],
  "intercept": ["ats.iot.vendor-cloud.example"], "intercept_all": false,
  "exclude": ["ntp.*", "time.*", "ocsp.*", "crl.*", "*.local", "*.lan", "*.localdomain"],
  "listen": {"host": "0.0.0.0", "port": 53, "udp": true, "tcp": true}
}
```

### `PUT /api/dns/settings`

Updates the policy and applies it **without a restart**. Only the fields present in
the body are touched, so one switch can be flipped without resending the others.

```json
{ "intercept_all": true, "exclude": ["ntp.*", "*.local"] }
```

`intercept` and `exclude` are lists of names or `*.domain` patterns; `intercept_all`
must be a boolean. Anything else is refused with `400`.

A body that carries a field which is not a list, or a value that is not a name
(`not a name`, `http://x/`, an IP literal), is refused with `400` and the message
names the offending values: a typo must not look like it was saved. The whole body
is rejected together, so a valid switch sent next to an invalid name is not applied
either -- a half-applied policy is worse than none. Duplicates collapse, so the
stored list is a set of rules.

### `GET /api/dns/discovery`

What each client asked the DNS for, and what the proxy does with it now --
including the names still being forwarded, which are the candidates for
`intercept`. Counters not yet flushed to the database are merged in.

Query parameter `limit` (default 500, max 5000).

```json
{
  "enabled": true, "intercept": ["ats.iot.vendor-cloud.example"],
  "intercept_all": false, "exclude": ["ntp.*"],
  "listen": {"host": "0.0.0.0", "port": 53, "udp": true, "tcp": true},
  "stats": {"intercepted": 3, "forwarded": 12, "excluded": 4},
  "pending_pairs": 2, "flushed_pairs": 118, "flush_errors": 0, "dropped_pairs": 0,
  "entries": [
    {"client_ip": "192.168.1.77", "hostname": "ats.iot.vendor.example", "qtype": "A",
     "status": "forwarded", "hit_count": 12, "first_seen": "2026-01-01T10:00:00+00:00",
     "last_seen": "2026-01-01T11:30:00+00:00",
     "intercepted": false, "excluded": false, "suggested": true,
     "excluded_by": null, "claimed_by": null}
  ]
}
```

`pending_pairs` counts the pairs still in memory (a query just made is already in this
response), `flushed_pairs` how many have reached the database, `flush_errors` how many
writes failed and `dropped_pairs` how many names the in-memory working set refused because
it was full — the last two are zero in a healthy service and say why the log is not
growing when they are not.

`excluded_by` is the exclusion pattern keeping the name out (e.g. `ntp.*`) and
`claimed_by` the hostname a registered adapter declares for it; both are `null`
when they do not apply. When `excluded_by` is set it is the rule that decides — an
exclusion you wrote beats an adapter claim — while `claimed_by` then only records who
else was interested in the name. They exist so a client can explain the state instead of
offering an action that would change nothing: promoting a name held out by an
exclusion pattern, or one an adapter already claims, is a no-op. `suggested`
(=`forwarded`) is the only state where `POST /api/dns/intercept` has an effect.

### `POST /api/dns/intercept`

Promotes a discovered name (or pattern) to the intercept list: persisted to
`dns.intercept` and applied live.

```json
{ "hostname": "*.iot.vendor.example" }
```

`400` for anything that is not a plausible name (an IP literal is refused).

```json
{ "hostname": "*.iot.vendor.example", "intercept": ["*.iot.vendor.example"], "changed": true }
```

`changed` is `false` when the name was already listed: the call then changes nothing
at all, config file included.

### `DELETE /api/dns/intercept/{hostname}`

Removes a name from the intercept list: it goes back to being forwarded (and
discovered). The response carries `removed`, which is `0` when the name was not
listed -- in that case nothing is written.

### `DELETE /api/dns/discovery`

Forgets every recorded query, in memory and in the `dns_query_log` table.

```json
{ "removed": 25 }
```

---

## Cloud Independence

Endpoints for verifying and managing device independence from the cloud vendor.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/independence/{device_id}` | Checks if a device can work without the cloud |
| `GET` | `/api/independence/` | Check for all devices |
| `POST` | `/api/independence/{device_id}/auto-switch` | Forces auto-switch to production |
| `GET` | `/api/independence/{device_id}/export` | Exports patterns for backup |
| `POST` | `/api/independence/{device_id}/import` | Imports patterns from backup |

---

## References

- [Deployment](deployment.md) — direct execution, Docker, Docker Compose
- [Configuration](configuration.md) — full `config.yaml` reference
- [nginx Architecture](nginx-architecture.md) — reverse proxy and DNS loop prevention
- [Portable Pattern Database](portable-pattern-database.md) — `.ride-pattern.json` / `.ride-capture.json` format