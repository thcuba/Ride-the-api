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
- [Cloud Independence](#cloud-independence)

---

## Health Check

### `GET /health`

Checks the service status.

**Response `200 OK`**

```json
{
  "status": "healthy",
  "service": "local-cloud-replacement-proxy",
  "version": "0.2.0",
  "adapters": ["example"]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `"healthy"` if the service is operational |
| `service` | string | Service name |
| `version` | string | Current version |
| `adapters` | array[string] | List of registered vendors/adapters |

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

**Status codes**: `200` OK, `503` If metrics are not enabled

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
      "database_url": "sqlite+aiosqlite:///./data/devices/ip-192-168-1-42.db",
      "ip_addresses": ["192.168.1.42"],
      "created_at": "2025-06-15T10:30:00Z"
    }
  ]
}
```

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
    "current_buffer_size_bytes": 128000
  }
}
```

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
  "model_id": "llama3.1:8b",
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
the JSON schema before importing.

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

### TLS Devices: `GET /api/tls/device-ports`

IP → device mapping for all active TLS connections.

---

### Unidentified: `GET /api/tls/unidentified`

List of devices with IDs beginning with `ip-` (auto-created
by the TLS handler, not yet renamed).

---

### List TLS Ports: `GET /api/tls/ports`

Currently active TLS listening ports.

**Response** `{"ports": [443, 8883, 8443], "enabled": true}`

---

### Add TLS Port: `POST /api/tls/ports`

Dynamically adds a TLS listening port (persists in config.yaml).

**JSON Body** `{"port": 8443}`

**Response** `{"status": "ok", "port": 8443, "listen_ports": [443, 8883, 8443]}`

---

### Remove TLS Port: `DELETE /api/tls/ports/{port}`

Removes a TLS listening port.

**Response** `{"status": "ok", "port": 8443, "listen_ports": [443, 8883]}`

---

### List Certificates: `GET /api/tls/certs`

List of all imported and auto-generated certificates.

---

### Certificate Info: `GET /api/tls/certs/{hostname}`

Details of a certificate for a specific hostname.

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

### `GET /api/llm/profiles`

List of system LLM profiles (from `config.yaml`).

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

Starts a specific protocol server.

### `POST /api/protocol-servers/{name}/stop`

Stops a protocol server.

### `GET /api/protocol-servers/{name}/config`

Configuration of a specific protocol server.

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