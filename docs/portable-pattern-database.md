# Portable Pattern Database — Design Document

> Studio del formato portabile per condividere pattern di protocollo tra utenti,
> LLM-agnostic e cross-hardware.

## Indice

1. [Architettura](#architettura)
2. [Buffer DB (da decifrare)](#buffer-db-da-decifrare)
3. [Deciphered DB (decifrato)](#deciphered-db-decifrato)
   - [Client (dispositivo IoT)](#client-dispositivo-iot)
   - [Server (proxy)](#server-proxy)
4. [Esempio completo](#esempio-completo)
5. [Relazioni con il codice esistente](#relazioni-con-il-codice-esistente)
6. [Implementazione futura](#implementazione-futura)

---

## Architettura

Ogni dispositivo ha **due database** portabili, gestiti da un **motore** centrale:

```
┌──────────────┐
│   IoT Device │
└──────┬───────┘
       │ richieste/risposte
       ▼
┌──────────────────────────────────────────────────────────┐
│                    ENGINE (Motore)                        │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  ① Buffer Manager                                 │  │
│  │  - Accumula coppie raw nel Buffer DB              │  │
│  │  - Rispetta capienza configurabile per dispositivo │  │
│  │  - Buffer pieno → flush                          │  │
│  │  - Esporta/Importa .ride-capture.json             │  │
│  └────────┬───────────────────────────────────────────┘  │
│           │ buffer pieno                                 │
│           ▼                                              │
│  ┌────────────────────────────────────────────────────┐  │
│  │  ② LLM Router                                     │  │
│  │  - Prende il buffer pieno                          │  │
│  │  - Lo invia al LLM configurato per dispositivo     │  │
│  │  - Attende risposta strutturata                   │  │
│  └────────┬───────────────────────────────────────────┘  │
│           │ risposta LLM analizzata                      │
│           ▼                                              │
│  ┌────────────────────────────────────────────────────┐  │
│  │  ③ Decipher Ingest                                │  │
│  │  - Riceve l'output strutturato dal LLM             │  │
│  │  - Popola il database decifrato                   │  │
│  │  - Crea pattern, template, field mappings          │  │
│  │  - Pulisce il buffer (dati ora decifrati)          │  │
│  └────────┬───────────────────────────────────────────┘  │
│           │ pattern salvati                              │
│           ▼                                              │
│  ┌────────────────────────────────────────────────────┐  │
│  │  ④ Pattern Engine                                  │  │
│  │  - Matcha richieste contro pattern decifrati       │  │
│  │  - Genera risposte locali dal template             │  │
│  │  - Applica field mappings e trasformazioni         │  │
│  │  - Gestisce stato dispositivo simulato             │  │
│  │  - Gestisce sensori virtuali                       │  │
│  │  - Esporta/Importa .ride-pattern.json              │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
       │
       ▼                               ▼
┌──────────────────────┐   ┌──────────────────────────────┐
│  Buffer DB           │   │  Deciphered DB               │
│  (da decifrare)      │   │  (decifrato)                 │
│  .ride-capture.json  │   │  .ride-pattern.json          │
│  ↓ accumulo raw      │   │  ↑ popolato da output LLM    │
└──────────────────────┘   └──────────────────────────────┘
```

| Componente | Database | Formato file | Descrizione |
|------------|----------|-------------|-------------|
| **Buffer Manager** | Buffer DB | `.ride-capture.json` | Accumula coppie raw fino a capienza, poi flush al LLM |
| **LLM Router** | — | — | Instrada il buffer pieno al LLM configurato per dispositivo |
| **Decipher Ingest** | — | — | Prende output LLM e popola il database decifrato |
| **Pattern Engine** | Deciphered DB | `.ride-pattern.json` | Match, risposte locali, stato dispositivo, sensori virtuali |

### Punti chiave

- **Buffer DB**: ogni dispositivo ha la sua capienza buffer configurabile (es. 512KB). Quando è pieno, viene dato in pasto al LLM scelto per quel dispositivo. Esportabile per condividere dati grezzi.
- **LLM**: scelto per dispositivo nella configurazione del device registry — **slegato** dal pattern database. Nessun riferimento a LLM nei file `.ride-capture.json` o `.ride-pattern.json`.
- **Deciphered DB**: output dell'analisi LLM, popolato dal Decipher Ingest. Pattern pronti per la produzione.

---

## Buffer DB (da decifrare)

Formato per condividere traffico grezzo da far analizzare a un LLM (`.ride-capture.json`):

```json
{
  "$schema": "https://ride-the-api.dev/capture-schema/v1",
  "meta": {
    "version": 1,
    "capture_id": "acme-ac-capture-2024-12",
    "vendor": "acme",
    "device_type": "ac",
    "model": "SmartCool Pro 3000",
    "firmware_version": "3.1.0",
    "capture_date": "2024-12-15T10:00:00Z",
    "description": "Boot sequence + 30 min of normal operation"
  },
  "device_info": {
    "device_id": "obfuscated-001",
    "mac": "obfuscated",
    "serial": "obfuscated"
  },
  "sessions": [
    {
      "session_id": "boot_001",
      "type": "boot",
      "timestamp_start": "2024-12-15T10:00:00Z",
      "pairs": [
        {
          "pair_id": "pair_001",
          "timestamp": "2024-12-15T10:00:01Z",
          "protocol": "http",
          "method": "POST",
          "path": "/v1.0/auth/login",
          "headers": {
            "Content-Type": "application/json",
            "User-Agent": "AC-Client/3.1.0"
          },
          "query_params": {},
          "body": {
            "client_id": "SN-XXXXXXXX",
            "client_secret": "KEY-XXXXXXXX",
            "grant_type": "device_credentials"
          },
          "response": {
            "status_code": 200,
            "headers": {
              "Content-Type": "application/json",
              "Set-Cookie": "session=***"
            },
            "body": {
              "access_token": "eyJ...",
              "expires_in": 86400
            },
            "latency_ms": 234
          }
        },
        {
          "pair_id": "pair_002",
          "timestamp": "2024-12-15T10:00:02Z",
          "protocol": "http",
          "method": "POST",
          "path": "/v1.0/device/SN-XXXXXXXX/commands",
          "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer eyJ..."
          },
          "body": {
            "commands": [
              { "code": "temp_set", "value": 240 },
              { "code": "mode_set", "value": 1 }
            ]
          },
          "response": {
            "status_code": 200,
            "body": {
              "result": { "data": { "1": false, "3": 240, "4": 1 } }
            },
            "latency_ms": 150
          }
        }
      ]
    }
  ]
}
```

---

## Deciphered DB (decifrato)

Il database decifrato (`.ride-pattern.json`), diviso in sezione **client** e **server**.

### Client (dispositivo IoT)

Descrive cosa il dispositivo **invia** al cloud:

- Endpoint chiamati, metodi HTTP, pattern di path
- Schema dei body richiesta (campi, tipi, enum, range)
- Header e query parameter richiesti o opzionali
- Variazioni conosciute (es. firmware diverso -> path diverso)
- Sequenze di startup/handshake
- Info su autenticazione

### Server (proxy)

Descrive cosa Ride-the-API deve **rispondere**:

- Template di risposta per ogni intent/endpoint
- Field mappings: quale campo della richiesta popola quale campo della risposta
- Trasformazioni (direct, enum, scaling, formula)
- Valori costanti e default
- Dipendenze tra endpoint
- Stato persistente del dispositivo simulato
- Sensori virtuali

### Schema JSON completo

```json
{
  "$schema": "https://ride-the-api.dev/pattern-schema/v1",
  "meta": {
    "version": 1,
    "pattern_id": "acme-ac-smartcool-v3",
    "vendor": "acme",
    "device_type": "ac",
    "model": "SmartCool Pro 3000",
    "firmware_versions": ["3.1.0", "3.2.0"],
    "description": "Full pattern for Acme AC unit"
  },
  "client": {
    "protocols": ["http", "mqtt"],
    "base_url": "https://api.acme.com",
    "mqtt_topic_prefix": "thing/",
    "authentication": {
      "type": "bearer",
      "token_endpoint": "/v1.0/auth/token",
      "credentials": "stored_externally"
    },
    "endpoints": [
      {
        "id": "login",
        "intent": "authenticate",
        "method": "POST",
        "path": "/v1.0/auth/login",
        "headers": { "required": ["Content-Type"] },
        "body_schema": {
          "type": "object",
          "properties": {
            "client_id": { "type": "string", "description": "Device serial" },
            "client_secret": { "type": "string", "description": "Device key" },
            "grant_type": { "type": "string", "const": "device_credentials" }
          },
          "required": ["client_id", "client_secret", "grant_type"]
        },
        "response_fields": [
          { "field": "body.access_token", "type": "string", "stores": "auth_token" }
        ]
      },
      {
        "id": "set_temp",
        "intent": "set_temperature",
        "method": "POST",
        "path_pattern": "/v1.0/device/{device_id}/commands",
        "headers": { "required": ["Content-Type", "Authorization"] },
        "query_params": [],
        "body_schema": {
          "type": "object",
          "properties": {
            "commands": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "code": { "type": "string", "enum": ["temp_set"] },
                  "value": { "type": "integer", "min": 100, "max": 400 }
                },
                "required": ["code", "value"]
              },
              "min_items": 1
            }
          },
          "required": ["commands"]
        },
        "variants": [
          {
            "firmware": ">=3.2.0",
            "path": "/v2.0/device/{device_id}/ctrl",
            "body_schema": {
              "type": "object",
              "properties": {
                "cmd": { "type": "string", "const": "temp" },
                "val": { "type": "integer" }
              },
              "required": ["cmd", "val"]
            }
          }
        ]
      }
    ]
  },
  "server": {
    "state_variables": [
      {
        "name": "power",
        "type": "boolean",
        "default": false,
        "persist": true,
        "description": "Device on/off state"
      },
      {
        "name": "temp_target",
        "type": "integer",
        "min": 100,
        "max": 400,
        "default": 240,
        "unit": "decicelsius",
        "persist": true
      },
      {
        "name": "mode",
        "type": "string",
        "enum": ["cool", "heat", "fan", "auto", "dry"],
        "default": "cool",
        "persist": true
      }
    ],
    "responses": [
      {
        "id": "rsp_set_temp",
        "triggers": ["set_temp"],
        "status_code": 200,
        "headers_template": {
          "Content-Type": "application/json",
          "X-Request-Id": "{uuid}"
        },
        "body_template": {
          "result": {
            "data": {
              "1": "{state.power}",
              "3": "{state.temp_target}"
            }
          }
        },
        "field_mappings": [
          {
            "source": "request.body.commands[0].value",
            "target": "state.temp_target",
            "transform": "direct",
            "description": "Set temperature from command"
          }
        ]
      },
      {
        "id": "rsp_get_status",
        "triggers": ["get_status"],
        "status_code": 200,
        "headers_template": { "Content-Type": "application/json" },
        "body_template": {
          "device": {
            "online": true,
            "mode": "{state.mode}",
            "temp_target": "{state.temp_target}",
            "temp_actual": "{state.temp_actual}",
            "power": "{state.power}"
          }
        },
        "field_mappings": [
          {
            "source": "constant.default_temp",
            "target": "state.temp_actual",
            "transform": "formula",
            "formula": "state.temp_target + random(-5, 5)",
            "description": "Simulate actual temp near target"
          }
        ]
      }
    ],
    "virtual_sensors": [
      {
        "name": "temp_actual",
        "type": "integer",
        "behavior": "drift",
        "baseline": "{state.temp_target}",
        "drift_range": [-5, 5],
        "update_interval_s": 60,
        "description": "Simulated room temperature that drifts around target"
      }
    ]
  }
}
```

---

## Relazioni con il codice esistente

| Modello DB attuale | Nuovo formato JSON | Note |
|-------------------|-------------------|------|
| `RequestPattern` | `client.endpoints[]` | Method, path_pattern, headers, body_schema, intent |
| `ResponseTemplate` | `server.responses[].body_template` | Status code, headers, body |
| `FieldMapping` | `server.responses[].field_mappings[]` | Source/target field, transform |
| `DeviceRegistry.mode` | — | Rimane nella configurazione, non nel pattern |
| `LLMContextBuffer` | `sessions[].pairs[]` | Il buffer esportato diventa il raw capture |
| — (nuovo) | `server.state_variables` | Stato persistente del dispositivo simulato |
| — (nuovo) | `server.virtual_sensors` | Sensori simulati |
| — (nuovo) | `client.endpoints[].variants` | Variazioni per firmware |
| — (nuovo) | `client.authentication` | Info su auth del dispositivo |

---

## Implementazione futura

1. **Pattern Engine**: modificare `PatternMatcher` per caricare pattern da file `.ride-pattern.json`
2. **Buffer Manager**: esportare il `ContextBuffer` in formato `.ride-capture.json`
3. **Decipher Ingest**: nuovo componente che prende output LLM e popola il database decifrato
4. **Endpoint API**: `/api/devices/{id}/patterns/import`, `/export`, `/capture/export`
5. **State persistence**: implementare `state_variables` nel device DB per risposte dinamiche
6. **Virtual sensors**: motore di simulazione per sensori (temperatura, umidità, etc.)
7. **Validazione**: JSON Schema validation all'import di entrambi i formati