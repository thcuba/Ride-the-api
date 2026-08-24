# Riferimento API REST — Ride-the-API

> Tutti gli endpoint REST esposti dal proxy per la gestione dei dispositivi, pattern, buffer e osservabilità.

- **Base URL predefinito**: `http://localhost:8911`
- **Formato richiesta/risposta**: JSON
- **Documentazione interattiva**: `http://localhost:8911/docs` (OpenAPI / Swagger UI)

---

## Indice

- [Health Check](#health-check-get-health)
- [Metriche](#metriche-get-metrics)
- [Dispositivi](#dispositivi)
  - [Lista dispositivi](#lista-dispositivi-get-apidevices)
  - [Dettaglio dispositivo](#dettaglio-dispositivo-get-apidevicesdevice_id)
  - [Statistiche match](#statistiche-match-get-apidevicesdevice_idstats)
  - [Tasso di match](#tasso-di-match-get-apidevicesdevice_idmatch-rate)
  - [Modalità dispositivo](#modalità-dispositivo-post-apidevicesdevice_idmode)
  - [Auto-switch](#auto-switch-getput-apidevicesdevice_idauto-switch)
  - [Configurazione LLM](#configurazione-llm-put-apidevicesdevice_idllm)
  - [Configurazione TLS](#configurazione-tls-put-apidevicesdevice_idtls-config)
  - [Context notes](#context-notes-getput-apidevicesdevice_idcontext)
  - [Assegnazione database](#assegnazione-database-post-apidevicesdevice_iddatabase)
  - [Risoluzione IP](#risoluzione-ip-get-apidevicesby-ipip_address)
  - [Registrazione IP](#registrazione-ip-post-apidevicesdevice_idip)
- [Buffer](#buffer)
  - [Lista buffer](#lista-buffer-get-apidevicesdevice_idbuffer)
  - [Elimina entry buffer](#elimina-entry-buffer-delete-apidevicesdevice_idbufferentry_id)
  - [Flush su LLM](#flush-su-llm-post-apidevicesdevice_idllmflush)
  - [Preview LLM](#preview-llm-post-apidevicesdevice_idllmpreview)
- [Pattern](#pattern)
  - [Lista pattern](#lista-pattern-get-apidevicesdevice_idpatterns)
  - [Dettaglio pattern](#dettaglio-pattern-get-apidevicesdevice_idpatternspattern_id)
  - [Aggiorna pattern](#aggiorna-pattern-put-apidevicesdevice_idpatternspattern_id)
  - [Modifica parziale pattern](#modifica-parziale-pattern-patch-apidevicesdevice_idpatternspattern_id)
  - [Elimina pattern](#elimina-pattern-delete-apidevicesdevice_idpatternspattern_id)
  - [Esporta pattern](#esporta-pattern-get-apidevicesdevice_idpatternsexport)
  - [Importa pattern](#importa-pattern-post-apidevicesdevice_idpatternsimport)
- [Capture](#capture)
  - [Esporta capture](#esporta-capture-get-apidevicesdevice_idcaptureexport)
  - [Importa capture](#importa-capture-post-apidevicesdevice_idcaptureimport)
- [TLS / MITM](#tls--mitm)
  - [Scarica CA](#scarica-ca-get-apitlsca-cert)
  - [Statistiche TLS](#statistiche-tls-get-apitlsstats)
  - [Dispositivi TLS](#dispositivi-tls-get-apitlsdevice-ports)
  - [Non identificati](#non-identificati-get-apitlsunidentified)
  - [Lista porte TLS](#lista-porte-tls-get-apitlsports)
  - [Aggiungi porta TLS](#aggiungi-porta-tls-post-apitlsports)
  - [Rimuovi porta TLS](#rimuovi-porta-tls-delete-apitlsportsport)
  - [Lista certificati](#lista-certificati-get-apitlscerts)
  - [Info certificato](#info-certificato-get-apitlscertshostname)
  - [Carica certificato](#carica-certificato-post-apitlscertsupload)
  - [Carica certificato JSON](#carica-certificato-json-post-apitlscertsupload-json)
  - [Elimina certificato](#elimina-certificato-delete-apitlscertshostname)
  - [Ruota certificato](#ruota-certificato-post-apitlscertshostnamerotate)
  - [Scarica root CA](#scarica-root-ca-post-apitlsroot-cadownload)
  - [Script Frida](#script-frida-get-apitlsfridascriptjs)
- [Profili LLM](#profili-llm)
- [Server di protocollo](#server-di-protocollo)
- [Indipendenza dal Cloud](#indipendenza-dal-cloud)

---

## Health Check

### `GET /health`

Verifica lo stato del servizio.

**Response `200 OK`**

```json
{
  "status": "healthy",
  "service": "local-cloud-replacement-proxy",
  "version": "0.2.0",
  "adapters": ["example"]
}
```

| Campo | Tipo | Descrizione |
|-------|------|-------------|
| `status` | string | `"healthy"` se il servizio è operativo |
| `service` | string | Nome del servizio |
| `version` | string | Versione corrente |
| `adapters` | array[string] | Lista dei vendor/adapter registrati |

**Codici di stato**: `200` OK, `503` Service Unavailable

---

## Metriche

### `GET /metrics`

Espone metriche in formato Prometheus. Configurabile in `config.yaml`:

```yaml
observability:
  metrics:
    enabled: true
    port: 9090
    path: "/metrics"
```

**Nota**: Le metriche sono servite sulla porta `9090` (non sulla 8911 del proxy)
quando abilitate. Usa Prometheus o un qualsiasi scraper per raccoglierle.

**Response**: Testo in formato Prometheus exposition format (`text/plain; version=0.0.4`).

Metriche esposte (da `prometheus-client` e metriche custom):

| Nome | Tipo | Descrizione |
|------|------|-------------|
| `ride_api_requests_total` | Counter | Richieste totali elaborate |
| `ride_api_local_hits_total` | Counter | Risposte locali servite |
| `ride_api_cloud_misses_total` | Counter | Richieste forwardate al cloud |
| `ride_api_device_count` | Gauge | Dispositivi registrati |
| `ride_api_buffer_size_bytes` | Gauge | Dimensione totale buffer |
| `python_*` | — | Metriche runtime Python standard |

**Codici di stato**: `200` OK, `503` Se le metriche non sono abilitate

---

## Dispositivi

### Lista dispositivi: `GET /api/devices`

Restituisce tutti i dispositivi registrati.

**Response `200 OK`**

```json
{
  "devices": [
    {
      "device_id": "ip-192-168-1-42",
      "vendor": "example",
      "name": "Condizionatore Soggiorno",
      "mode": "learning",
      "auto_switch_enabled": false,
      "database_url": "sqlite+aiosqlite:///./data/devices/ip-192-168-1-42.db",
      "ip_addresses": ["192.168.1.42"],
      "created_at": "2025-06-15T10:30:00Z"
    }
  ]
}
```

**Codici di stato**: `200` OK, `503` Service not ready

---

### Dettaglio dispositivo: `GET /api/devices/{device_id}`

Restituisce dettagli e statistiche complete per un dispositivo.

**Parametri path**

| Parametro | Tipo | Descrizione |
|-----------|------|-------------|
| `device_id` | string | ID del dispositivo (es. `ip-192-168-1-42`) |

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

**Codici di stato**: `200` OK, `404` Device not found, `503` Service not ready

---

### Statistiche match: `GET /api/devices/{device_id}/stats`

Statistiche in tempo reale per un dispositivo (alias di `/api/devices/{device_id}`).

**Response** `{"stats": { ... }}` — stessi campi di `/api/devices/{device_id}`.

---

### Tasso di match: `GET /api/devices/{device_id}/match-rate`

Percentuale di match locale, utile per monitorare l'apprendimento.

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

### Modalità dispositivo: `POST /api/devices/{device_id}/mode`

Cambia la modalità operativa di un dispositivo.

**Body JSON**

```json
{
  "mode": "production"
}
```

| Campo | Tipo | Valori | Descrizione |
|-------|------|--------|-------------|
| `mode` | string | `learning`, `production`, `hybrid` | Modalità operativa |

**Response `200 OK`** `{"device_id": "...", "mode": "production"}`

**Codici di stato**: `200` OK, `400` Invalid mode, `404` Device not found, `503` Service not ready

---

### Auto-switch: `GET /api/devices/{device_id}/auto-switch`

Legge lo stato dell'auto-switch per un dispositivo.

**Response `200 OK`**

```json
{
  "device_id": "ip-192-168-1-42",
  "auto_switch_enabled": false
}
```

### Auto-switch: `PUT /api/devices/{device_id}/auto-switch`

Abilita o disabilita il passaggio automatico da learning a production.

**Body JSON**

```json
{
  "enabled": true
}
```

**Response `200 OK`** `{"device_id": "...", "auto_switch_enabled": true}`

**Note**: Quando abilitato, il `AutoSwitchScheduler` passa automaticamente il
dispositivo a `production` quando il tasso di match raggiunge il 99% con almeno
10 pattern appresi e 50 richieste totali. Se il tasso scende sotto il 90%, torna
a `learning`.

---

### Configurazione LLM: `PUT /api/devices/{device_id}/llm`

Configura il profilo LLM per un dispositivo specifico (sovrascrive il profilo di default).

**Body JSON**

```json
{
  "base_url": "http://localhost:11434/v1",
  "model_id": "llama3.1:8b",
  "profile_name": "local_ollama"
}
```

| Campo | Tipo | Descrizione |
|-------|------|-------------|
| `base_url` | string | URL base API OpenAI-compatible |
| `model_id` | string | Identificatore del modello |
| `profile_name` | string | Nome del profilo (opzionale) |

**Response `200 OK`** `{"device_id": "...", "status": "updated"}`

---

### Configurazione TLS: `PUT /api/devices/{device_id}/tls-config`

Aggiorna nome, vendor, passthrough e bypass pinning per un dispositivo.

**Body JSON**

```json
{
  "name": "Termostato Cucina",
  "vendor": "example",
  "passthrough": true,
  "pinning_bypass": "mitm_proxy"
}
```

---

### Context notes: `GET /api/devices/{device_id}/context`

Legge le note contestuali per un dispositivo (utili come hint per l'LLM).

**Response** `{"device_id": "...", "context_notes": "Termostato modello XYZ"}`

### Context notes: `PUT /api/devices/{device_id}/context`

Aggiorna le note contestuali.

**Body JSON** `{"context_notes": "Termostato modello ABC-123, firmware 2.4"}`

---

### Assegnazione database: `POST /api/devices/{device_id}/database`

Assegna un database specifico a un dispositivo (SQLite o PostgreSQL).

**Body JSON**

```json
{
  "database_name": "my_ac_protocol"
}
```

OPPURE

```json
{
  "database_url": "postgresql://localhost/my_ac_protocol"
}
```

---

### Risoluzione IP: `GET /api/devices/by-ip/{ip_address}`

Cerca un dispositivo tramite indirizzo IP.

**Response `200 OK`**

```json
{
  "device_id": "ip-192-168-1-42",
  "ip_address": "192.168.1.42"
}
```

---

### Registrazione IP: `POST /api/devices/{device_id}/ip`

Associa un indirizzo IP a un dispositivo.

**Body JSON** `{"ip_address": "192.168.1.100"}`

---

## Buffer

### Lista buffer: `GET /api/devices/{device_id}/buffer`

Elenca le entry del buffer non ancora processate dall'LLM.

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

### Elimina entry buffer: `DELETE /api/devices/{device_id}/buffer/{entry_id}`

Rimuove una singola entry dal buffer.

| Parametro | Tipo | Descrizione |
|-----------|------|-------------|
| `entry_id` | int | ID numerico della entry |

**Response `200 OK** `{"device_id": "...", "entry_id": 1, "status": "deleted"}`

---

### Flush su LLM: `POST /api/devices/{device_id}/llm/flush`

Invia le entry del buffer selezionate all'LLM per l'analisi e l'apprendimento.

**Body JSON**

```json
{
  "pair_ids": [1, 2, 3, 4, 5],
  "context_notes": "Il dispositivo invia heartbeat ogni 60 secondi"
}
```

| Campo | Tipo | Descrizione |
|-------|------|-------------|
| `pair_ids` | array[int] | (Opzionale) ID specifici da flusciare. Se omesso, flush completo |
| `context_notes` | string | (Opzionale) Note contestuali per guidare l'LLM |

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

### Preview LLM: `POST /api/devices/{device_id}/llm/preview`

Analizza le entry senza salvare i pattern. Usato per validare prima dell'import.

**Body JSON**: Stessa struttura di `/llm/flush`.

**Response**: Analisi LLM senza persistenza.

---

## Pattern

### Lista pattern: `GET /api/devices/{device_id}/patterns`

Restituisce tutti i pattern appresi per un dispositivo, ordinati per confidence
decrescente.

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

### Dettaglio pattern: `GET /api/devices/{device_id}/patterns/{pattern_id}`

Dettaglio completo di un pattern, incluse le field mappings.

**Response `200 OK`**: Include `pattern`, `response_template` e `field_mappings`.

---

### Aggiorna pattern: `PUT /api/devices/{device_id}/patterns/{pattern_id}`

Aggiornamento completo (upsert) di un pattern, template risposta e field mappings.

**Body JSON**: Stesso formato della risposta di `GET patterns/{pattern_id}`, con
aggiunta di `field_mappings` array.

Se il `pattern_id` non esiste, viene creato (upsert).

---

### Modifica parziale pattern: `PATCH /api/devices/{device_id}/patterns/{pattern_id}`

Aggiornamento parziale — solo i campi presenti nel body vengono modificati.

---

### Elimina pattern: `DELETE /api/devices/{device_id}/patterns/{pattern_id}`

Elimina il pattern, il template risposta associato e tutte le field mappings
per quell'intent.

**Response `200 OK`** `{"status": "deleted", "pattern_id": "abc123"}`

---

### Esporta pattern: `GET /api/devices/{device_id}/patterns/export`

Esporta tutti i pattern di un dispositivo nel formato portabile `.ride-pattern.json`.

**Response `200 OK`**: Documento JSON conforme allo schema `PatternDB` contenente:

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

### Importa pattern: `POST /api/devices/{device_id}/patterns/import`

Importa pattern da un file `.ride-pattern.json`. I pattern vengono validati contro
lo schema JSON prima dell'import.

**Body JSON**: Documento `PatternDB` (stesso formato dell'export).

**Response `200 OK`**

```json
{
  "imported": 12,
  "device_id": "ip-192-168-1-42",
  "warnings": []
}
```

**Codici di stato**: `200` OK, `422` Validation error (con dettagli), `400` Bad request

**Nota**: Il formato `.ride-pattern.json` è progettato per la condivisione tra
installazioni e il backup. I pattern importati vengono applicati allo stato del
dispositivo e alle variabili virtuali/sensori configurati.

---

## Capture

Le capture contengono coppie richiesta/risposta **raw** (non ancora analizzate
dall'LLM), nel formato `.ride-capture.json`.

### Esporta capture: `GET /api/devices/{device_id}/capture/export`

Esporta il buffer grezzo in formato portabile.

**Response `200 OK`**: Documento JSON conforme allo schema `CaptureDB`:

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

### Importa capture: `POST /api/devices/{device_id}/capture/import`

Importa coppie raw nel buffer di un dispositivo. Validazione JSON Schema prima
dell'import.

**Body JSON**: Documento `CaptureDB` (stesso formato dell'export).

**Response `200 OK`**

```json
{
  "imported": 25,
  "device_id": "ip-192-168-1-42",
  "warnings": []
}
```

**Codici di stato**: `200` OK, `422` Validation error (con dettagli), `400` Bad request

**Nota**: Le capture importate si aggiungono al buffer esistente. Quando il buffer
raggiunge la capacità massima (default 512 KB), viene segnalato e può essere
flusciato manualmente all'LLM tramite `POST /api/devices/{device_id}/llm/flush`.

---

## TLS / MITM

### Scarica CA: `GET /api/tls/ca-cert`

Scarica il certificato CA in formato PEM per installazione sui dispositivi.

**Response**: `application/x-pem-file` con `Content-Disposition: attachment; filename=ride-the-api-ca.pem`

**Codici di stato**: `200` OK, `503` Cert manager not ready

---

### Statistiche TLS: `GET /api/tls/stats`

Stato del server MITM e statistiche dei certificati.

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

### Dispositivi TLS: `GET /api/tls/device-ports`

Mapping IP → dispositivo per tutte le connessioni TLS attive.

---

### Non identificati: `GET /api/tls/unidentified`

Lista dei dispositivi con ID che iniziano per `ip-` (creati automaticamente
dal TLS handler, non ancora rinominati).

---

### Lista porte TLS: `GET /api/tls/ports`

Porte di ascolto TLS attualmente attive.

**Response** `{"ports": [443, 8883, 8443], "enabled": true}`

---

### Aggiungi porta TLS: `POST /api/tls/ports`

Aggiunge dinamicamente una porta di ascolto TLS (persiste in config.yaml).

**Body JSON** `{"port": 8443}`

**Response** `{"status": "ok", "port": 8443, "listen_ports": [443, 8883, 8443]}`

---

### Rimuovi porta TLS: `DELETE /api/tls/ports/{port}`

Rimuove una porta di ascolto TLS.

**Response** `{"status": "ok", "port": 8443, "listen_ports": [443, 8883]}`

---

### Lista certificati: `GET /api/tls/certs`

Lista di tutti i certificati importati e generati automaticamente.

---

### Info certificato: `GET /api/tls/certs/{hostname}`

Dettagli di un certificato per hostname specifico.

---

### Carica certificato: `POST /api/tls/certs/upload`

Carica certificato + chiave privata in formato PEM (multipart form).

| Campo | Tipo | Descrizione |
|-------|------|-------------|
| `hostname` | string | Nome host (form) |
| `cert` | file | File certificato PEM |
| `key` | file | File chiave privata PEM |

---

### Carica certificato JSON: `POST /api/tls/certs/upload-json`

Stessa operazione ma in JSON con PEM in base64.

**Body JSON**

```json
{
  "hostname": "api.example.com",
  "cert_base64": "LS0tLS1CRUdJTiBDRV...",
  "key_base64": "LS0tLS1CRUdJTiBSU0..."
}
```

---

### Elimina certificato: `DELETE /api/tls/certs/{hostname}`

Rimuove un certificato importato (torna al certificato auto-generato).

---

### Ruota certificato: `POST /api/tls/certs/{hostname}/rotate`

Sostituisce un certificato senza downtime. Stesso formato di `upload-json`.

---

### Scarica root CA: `POST /api/tls/root-ca/download`

Alias di `GET /api/tls/ca-cert`.

---

### Script Frida: `GET /api/tls/frida/script.js`

Restituisce uno script Frida JavaScript per bypassare il certificate pinning
su dispositivi Android. Utilizzo:

```bash
frida -U -l script.js <nome-app>
```

**Response**: `application/javascript`

---

## Profili LLM

Endpoint per la gestione dei profili LLM salvati dall'utente.

### `GET /api/llm/profiles`

Elenco dei profili LLM di sistema (da `config.yaml`).

### `GET /api/llm/user-profiles`

Elenco dei profili utente salvati.

### `POST /api/llm/user-profiles`

Crea un nuovo profilo utente.

**Body JSON**

```json
{
  "name": "my_analysis_profile",
  "prompt_template": "Analizza i seguenti messaggi...",
  "model_id": "gpt-4o-mini",
  "base_url": "https://api.openai.com/v1"
}
```

### `GET /api/llm/user-profiles/{name}`

Dettaglio di un profilo utente.

### `PUT /api/llm/user-profiles/{name}`

Aggiorna un profilo utente.

### `DELETE /api/llm/user-profiles/{name}`

Elimina un profilo utente.

---

## Server di Protocollo

### `GET /api/protocol-servers`

Stato di tutti i server di protocollo (MQTT, CoAP, Modbus, WebSocket, ecc.).

```json
{
  "servers": [
    {"name": "mqtt", "running": true, "port": 1883},
    {"name": "coap", "running": false, "port": 5683}
  ]
}
```

### `POST /api/protocol-servers/{name}/start`

Avvia un server di protocollo specifico.

### `POST /api/protocol-servers/{name}/stop`

Ferma un server di protocollo.

### `GET /api/protocol-servers/{name}/config`

Configurazione di un server di protocollo specifico.

---

## Indipendenza dal Cloud

Endpoint per verificare e gestire l'indipendenza dei dispositivi dal vendor cloud.

| Metodo | Path | Descrizione |
|--------|------|-------------|
| `GET` | `/api/independence/{device_id}` | Verifica se un dispositivo può funzionare senza cloud |
| `GET` | `/api/independence/` | Verifica per tutti i dispositivi |
| `POST` | `/api/independence/{device_id}/auto-switch` | Forza auto-switch a production |
| `GET` | `/api/independence/{device_id}/export` | Esporta pattern per backup |
| `POST` | `/api/independence/{device_id}/import` | Importa pattern da backup |

---

## Riferimenti

- [Deployment](deployment.md) — esecuzione diretta, Docker, Docker Compose
- [Configurazione](configurazione.md) — riferimento completo `config.yaml`
- [nginx Architecture](nginx-architecture.md) — reverse proxy e prevenzione loop DNS
- [Portable Pattern Database](portable-pattern-database.md) — formato `.ride-pattern.json` / `.ride-capture.json`