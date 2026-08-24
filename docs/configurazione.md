# Riferimento Completo Configurazione — ride-the-api

> File di configurazione YAML (default: `config/config.yaml`).  
> Tutti i valori mostrati sono i **default** applicati dal modello Pydantic `Config`.

---

## Indice

1. [core](#core) — Database e contesto
2. [proxy](#proxy) — Proxy HTTP, TLS e fallback
3. [vendors](#vendors) — Configurazione per vendor IoT
4. [models](#models) — Modelli ML e inferenza
5. [control](#control) — Controllo, policy e apprendimento online
6. [observability](#observability) — Logging, metriche, tracing e health check
7. [dns](#dns) — Integrazione DNS (Pi-hole, AdGuard)
8. [traffic_selection](#traffic_selection) — Regole di selezione del traffico
9. [llm_decipher](#llm_decipher) — Decifratura tramite LLM
10. [modification](#modification) — Regole di modifica delle richieste/risposte
11. [correlation](#correlation) — Correlazione richiesta-risposta
12. [learning](#learning) — Modalità di apprendimento e produzione
13. [tls_decrypt](#tls_decrypt) — Decrittazione TLS/MITM
14. [protocol_servers](#protocol_servers) — Server multi-protocollo

---

## core

Configurazione del database e del contesto per dispositivo.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `database_url` | `string` | `"sqlite+aiosqlite:///./data/core.db"` | URL di connessione al database core (SQLite con aiosqlite) |
| `device_db_dir` | `string` | `"./data/devices"` | Directory per i database specifici di ogni dispositivo |
| `device_databases` | `dict[string, string]` | `{}` | Mappa nome-dispositivo → percorso database, per sovrascrivere il percorso predefinito |
| `default_context_buffer_size` | `integer` | `524288` | Dimensione predefinita del buffer di contesto in byte (default 512 KB). Valori possibili da enum `ContextBufferSizes`: `131072` (128 KB), `262144` (256 KB), `524288` (512 KB), `1048576` (1 MB), `2097152` (2 MB), `5242880` (5 MB), `10485760` (10 MB) |

Esempio:

```yaml
core:
  database_url: "sqlite+aiosqlite:///./data/core.db"
  device_db_dir: "./data/devices"
  device_databases:
    termostato_soggiorno: "./data/custom/termostato.db"
  default_context_buffer_size: 1048576
```

---

## proxy

Configurazione del proxy HTTP principale, TLS di ascolto e comportamento di fallback.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `host` | `string` | `"0.0.0.0"` | Indirizzo su cui il proxy è in ascolto |
| `port` | `integer` | `8911` | Porta del proxy principale |
| `tls` | `TLSConfig` | — | Configurazione TLS del proxy |
| `request_timeout` | `integer` | `30` | Timeout in secondi per la gestione di una richiesta |
| `max_request_size` | `integer` | `1048576` | Dimensione massima della richiesta in byte (1 MB) |
| `fallback` | `FallbackConfig` | — | Configurazione del fallback verso il cloud del vendor |

### proxy.tls

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita TLS sul proxy in ingresso |
| `cert_file` | `string` | `"./certs/ride-api.pem"` | Percorso del certificato TLS del proxy |
| `key_file` | `string` | `"./certs/ride-api.key"` | Percorso della chiave privata TLS del proxy |

### proxy.fallback

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita il fallback automatico al cloud del vendor |
| `timeout` | `integer` | `10` | Timeout in secondi per il fallback |
| `retry_count` | `integer` | `2` | Numero di tentativi di fallback |
| `confidence_threshold` | `float` | `0.7` | Soglia di confidenza sotto cui attivare il fallback (0.0 – 1.0) |

Esempio:

```yaml
proxy:
  host: "0.0.0.0"
  port: 8911
  tls:
    enabled: true
    cert_file: "./certs/ride-api.pem"
    key_file: "./certs/ride-api.key"
  request_timeout: 30
  max_request_size: 1048576
  fallback:
    enabled: true
    timeout: 10
    retry_count: 2
    confidence_threshold: 0.7
```

---

## vendors

Configurazione per ogni vendor IoT supportato. È un dizionario chiave-valore dove la chiave è il nome del vendor (es. `"shelly"`, `"mqtt"`, `"coap"`).

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita il vendor |
| `cloud` | `CloudConfig` | — | Endpoint cloud del vendor |
| `adapter` | `AdapterConfig` | — | Adapter Python da caricare per il vendor |

### vendors.\<vendor\>.cloud

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `api_endpoint` | `string` | `""` | Endpoint API REST del cloud del vendor |
| `mqtt_endpoint` | `string` | `""` | Endpoint MQTT del cloud del vendor |
| `mqtt_port` | `integer` | `8883` | Porta MQTT del cloud del vendor |

### vendors.\<vendor\>.adapter

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `class` | `string` | `""` | Nome completo della classe adapter (es. `"adapters.shelly.ShellyAdapter"`) |
| `extra` | `dict` | `{}` | Configurazione extra specifica del vendor (passata all'adapter) |

Esempio:

```yaml
vendors:
  shelly:
    enabled: true
    cloud:
      api_endpoint: "https://api.shelly.cloud"
      mqtt_endpoint: "mqtt.shelly.cloud"
      mqtt_port: 8883
    adapter:
      class: "adapters.shelly.ShellyAdapter"
      extra:
        auth_token: "..."
  mqtt:
    enabled: true
    adapter:
      class: "adapters.mqtt.MQTTAdapter"
```

---

## models

Configurazione dei modelli ML (ONNX) per inferenza locale.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `registry_path` | `string` | `"models"` | Directory contenente i modelli ONNX registrati |
| `defaults` | `ModelDefaults` | — | Modelli predefiniti per ogni tipologia di dispositivo |
| `inference` | `InferenceConfig` | — | Configurazione del runtime di inferenza ONNX |
| `hot_reload` | `HotReloadConfig` | — | Ricarica a caldo dei modelli |

### models.defaults

Dizionario nome-dispositivo → nome file modello ONNX.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `ac` | `string` | `"example_ac_v1.onnx"` | Modello predefinito per condizionatori |
| `heat_pump` | `string` | `"example_hp_v1.onnx"` | Modello predefinito per pompe di calore |

### models.inference

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `batch_size` | `integer` | `1` | Dimensione del batch per inferenza |
| `intra_op_threads` | `integer` | `2` | Thread intra-operazione ONNX Runtime |
| `inter_op_threads` | `integer` | `2` | Thread inter-operazione ONNX Runtime |
| `execution_providers` | `array[string]` | `["CPUExecutionProvider"]` | Provider di esecuzione ONNX (es. `CPUExecutionProvider`, `CUDAExecutionProvider`) |

### models.hot_reload

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita la ricarica a caldo dei modelli |
| `check_interval` | `integer` | `30` | Intervallo in secondi tra i controlli di aggiornamento |

Esempio:

```yaml
models:
  registry_path: "models"
  defaults:
    ac: "example_ac_v1.onnx"
    heat_pump: "example_hp_v1.onnx"
  inference:
    batch_size: 1
    intra_op_threads: 2
    inter_op_threads: 2
    execution_providers:
      - "CPUExecutionProvider"
  hot_reload:
    enabled: true
    check_interval: 30
```

---

## control

Configurazione del sistema di controllo: policy attiva e apprendimento online.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `policy` | `PolicyConfig` | — | Configurazione della policy di controllo |
| `online_learning` | `OnlineLearningConfig` | — | Apprendimento online dei pattern |

### control.policy

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `evaluation_interval` | `integer` | `60` | Intervallo in secondi tra valutazioni della policy |
| `default_policy` | `string` | `"pid_thermal"` | Nome della policy predefinita (es. `pid_thermal`, `rule_based`) |

### control.online_learning

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita l'apprendimento online |
| `buffer_size` | `integer` | `1000` | Dimensione massima del buffer di campioni prima dell'aggiornamento |
| `update_interval` | `integer` | `3600` | Intervallo in secondi tra aggiornamenti del modello (1 ora) |
| `min_samples_for_update` | `integer` | `100` | Numero minimo di campioni richiesti per avviare un aggiornamento |

Esempio:

```yaml
control:
  policy:
    evaluation_interval: 60
    default_policy: "pid_thermal"
  online_learning:
    enabled: true
    buffer_size: 1000
    update_interval: 3600
    min_samples_for_update: 100
```

---

## observability

Osservabilità del sistema: logging strutturato, metriche Prometheus, tracing distribuito e health check.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `logging` | `LoggingConfig` | — | Configurazione del logging |
| `metrics` | `MetricsConfig` | — | Esposizione metriche Prometheus |
| `tracing` | `TracingConfig` | — | Tracing distribuito (OpenTelemetry) |
| `health_check` | `HealthCheckConfig` | — | Endpoint di health check HTTP |

### observability.logging

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `level` | `string` | `"INFO"` | Livello di log (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |
| `format` | `string` | `"json"` | Formato del log (`json`, `text`) |
| `output` | `string` | `"stdout"` | Destinazione del log (`stdout`, `stderr`, percorso file) |

### observability.metrics

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita l'esposizione delle metriche Prometheus |
| `port` | `integer` | `9090` | Porta del server metriche |
| `path` | `string` | `"/metrics"` | Path dell' endpoint metriche |

### observability.tracing

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita il tracing distribuito |
| `exporter` | `string` | `"console"` | Esportatore tracing (`console`, `otlp`) |
| `otlp_endpoint` | `string` | `"http://localhost:4317"` | Endpoint OTLP gRPC per l'esportazione dei trace |
| `sample_rate` | `float` | `0.1` | Frequenza di campionamento dei trace (0.0 – 1.0) |

### observability.health_check

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita l'endpoint di health check |
| `port` | `integer` | `8080` | Porta del server health check |
| `path` | `string` | `"/health"` | Path dell' endpoint health check |

Esempio:

```yaml
observability:
  logging:
    level: "INFO"
    format: "json"
    output: "stdout"
  metrics:
    enabled: true
    port: 9090
    path: "/metrics"
  tracing:
    enabled: true
    exporter: "console"
    otlp_endpoint: "http://localhost:4317"
    sample_rate: 0.1
  health_check:
    enabled: true
    port: 8080
    path: "/health"
```

---

## dns

Integrazione DNS per risoluzione e rewrite locale.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `pihole_custom_dns` | `string` | `""` | URL dell' API Pi-hole per DNS personalizzati (vuoto = disabilitato) |
| `adguard_rewrites` | `string` | `""` | URL dell' API AdGuard Home per rewrite DNS (vuoto = disabilitato) |

Esempio:

```yaml
dns:
  pihole_custom_dns: "http://192.168.1.100/admin/api.php"
  adguard_rewrites: "http://192.168.1.101:80/control/rewrite"
```

---

## traffic_selection

Regole per selezionare quale traffico intercettare, lasciar passare o bloccare.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `default_action` | `string` | `"intercept"` | Azione predefinita per il traffico non coperto da regole (`intercept`, `forward`, `block`) |
| `rules` | `array[TrafficRule]` | `[]` | Elenco di regole di selezione del traffico |

### traffic_selection.rules[]

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `name` | `string` | `""` | Nome descrittivo della regola |
| `scope` | `string` | `"local"` | Scope della regola (`local`, `cloud`) |
| `match_type` | `string` | `"cidr"` | Tipo di corrispondenza (`cidr`, `hostname`, `port`) |
| `match_value` | `string` | `""` | Valore da confrontare (es. CIDR, hostname, porta) |
| `action` | `string` | `"intercept"` | Azione da applicare (`intercept`, `forward`, `block`) |
| `priority` | `integer` | `0` | Priorità della regola (valori più alti hanno precedenza) |
| `enabled` | `boolean` | `true` | Abilita/disabilita la regola |

Esempio:

```yaml
traffic_selection:
  default_action: "intercept"
  rules:
    - name: "Traffico cloud Shelly"
      scope: "cloud"
      match_type: "hostname"
      match_value: "*.shelly.cloud"
      action: "intercept"
      priority: 10
      enabled: true
    - name: "Traffico locale fidato"
      scope: "local"
      match_type: "cidr"
      match_value: "10.0.0.0/8"
      action: "forward"
      priority: 5
      enabled: true
```

---

## llm_decipher

Decifratura di protocolli sconosciuti o payload crittati tramite Large Language Model.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita la decifratura tramite LLM |
| `default_profile` | `string` | `"default"` | Nome del profilo LLM predefinito |
| `profiles` | `dict[string, LLMDecipherProfile]` | `{}` | Profili LLM configurabili (chiave = nome profilo) |

### llm_decipher.profiles.\<nome\>

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `base_url` | `string` | `""` | URL base dell' API LLM (es. `https://api.openai.com/v1`) |
| `api_key` | `string` | `""` | Chiave API per l'accesso all'LLM |
| `model_id` | `string` | `""` | Identificativo del modello LLM (es. `gpt-4o`, `claude-3`) |
| `prompt_template` | `string` | `""` | Template del prompt per guidare la decifratura |

Esempio:

```yaml
llm_decipher:
  enabled: true
  default_profile: "default"
  profiles:
    default:
      base_url: "https://api.openai.com/v1"
      api_key: "${OPENAI_API_KEY}"
      model_id: "gpt-4o"
      prompt_template: "Decodifica il seguente payload IoT in formato JSON:\n{payload}"
```

---

## modification

Regole per modificare, bloccare, iniettare, sostituire, reindirizzare o ritardare le richieste/risposte.

Azioni disponibili (enum `ModificationAction`):

| Azione | Descrizione |
|--------|-------------|
| `modify` | Modifica il valore del campo target |
| `block` | Blocca la richiesta/risposta |
| `inject` | Inietta un nuovo campo o payload |
| `replace` | Sostituisce completamente il payload |
| `redirect` | Reindirizza la richiesta a un altro endpoint |
| `delay` | Ritarda l'elaborazione della richiesta |

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita il sistema di modifica |
| `rules` | `array[ModificationRule]` | `[]` | Elenco di regole di modifica |

### modification.rules[]

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `name` | `string` | `""` | Nome descrittivo della regola |
| `scope` | `string` | `"local"` | Scope della regola (`local`, `cloud`) |
| `match_type` | `string` | `"hostname"` | Tipo di corrispondenza (`hostname`, `path`, `header`, `payload`) |
| `match_value` | `string` | `""` | Valore da confrontare |
| `action` | `string` | `"modify"` | Azione da eseguire (`modify`, `block`, `inject`, `replace`, `redirect`, `delay`) |
| `target_field` | `string` | `""` | Campo del payload su cui applicare l'azione |
| `target_value` | `string` | `""` | Valore da impostare per il campo target |
| `priority` | `integer` | `0` | Priorità della regola (valori più alti hanno precedenza) |
| `enabled` | `boolean` | `true` | Abilita/disabilita la regola |

Esempio:

```yaml
modification:
  enabled: true
  rules:
    - name: "Override temperatura"
      scope: "local"
      match_type: "hostname"
      match_value: "termostato-soggiorno"
      action: "modify"
      target_field: "target_temp"
      target_value: "22.0"
      priority: 10
      enabled: true
    - name: "Blocca comando pericoloso"
      scope: "local"
      match_type: "path"
      match_value: "/cmd/reboot"
      action: "block"
      priority: 100
      enabled: true
```

---

## correlation

Correlazione tra richieste e risposte per ricostruire il dialogo dispositivo-cloud.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita la correlazione richiesta-risposta |
| `http` | `CorrelationHTTPConfig` | — | Configurazione correlazione per HTTP |
| `mqtt` | `CorrelationMQTTConfig` | — | Configurazione correlazione per MQTT |
| `coap` | `CorrelationCoAPConfig` | — | Configurazione correlazione per CoAP |
| `store_pairs` | `boolean` | `true` | Abilita la persistenza delle coppie correlate su database |
| `max_pairs_per_device` | `integer` | `10000` | Numero massimo di coppie mantenute per dispositivo |
| `pair_ttl_hours` | `integer` | `168` | Durata di vita delle coppie in ore (default 7 giorni) |

### correlation.http

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `method` | `string` | `"connection"` | Metodo di correlazione (`connection`, `header`) |
| `correlation_header` | `string` | `"X-Request-ID"` | Nome dell'header usato per la correlazione |
| `keep_alive_timeout` | `integer` | `30` | Timeout keep-alive in secondi per correlazione via connessione |

### correlation.mqtt

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `method` | `string` | `"topic_sequence"` | Metodo di correlazione (`topic_sequence`, `packet_id`) |
| `qos_tracking` | `boolean` | `true` | Traccia il QoS dei pacchetti MQTT per correlazione |
| `retain_handling` | `string` | `"include"` | Gestione dei messaggi retained (`include`, `exclude`) |

### correlation.coap

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `method` | `string` | `"message_id"` | Metodo di correlazione (`message_id`, `token`) |
| `confirmable_timeout` | `integer` | `5` | Timeout in secondi per messaggi CoAP confirmable |

Esempio:

```yaml
correlation:
  enabled: true
  http:
    method: "connection"
    correlation_header: "X-Request-ID"
    keep_alive_timeout: 30
  mqtt:
    method: "topic_sequence"
    qos_tracking: true
    retain_handling: "include"
  coap:
    method: "message_id"
    confirmable_timeout: 5
  store_pairs: true
  max_pairs_per_device: 10000
  pair_ttl_hours: 168
```

---

## learning

Modalità di apprendimento e soglie per il passaggio automatico da apprendimento a produzione.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Abilita il sistema di apprendimento |
| `default_mode` | `string` | `"learning"` | Modalità operativa predefinita: `learning` (apprende), `production` (serve risposte locali), `hybrid` (mista) |
| `default_match_threshold` | `float` | `0.85` | Soglia di similarità predefinita per considerare un pattern corrispondente (0.0 – 1.0) |
| `auto_switch_to_production` | `boolean` | `false` | Passa automaticamente a modalità produzione quando si raggiungono i requisiti |
| `min_patterns_for_production` | `integer` | `10` | Numero minimo di pattern richiesti per entrare in modalità produzione |
| `min_match_rate_for_production` | `float` | `80.0` | Percentuale minima di match rate richiesta per entrare in produzione (0.0 – 100.0) |
| `production_no_fallback` | `boolean` | `false` | Se `true`, in modalità produzione le richieste senza risposta locale restituiscono un errore invece di fare fallback al cloud |
| `signal_forward_to_cloud` | `boolean` | `false` | Se `true`, le richieste senza risposta in produzione/hybrid restituiscono un header `X-Action: forward` invece di chiamare `adapter.forward_to_cloud()` internamente. Inteso per deployment dietro reverse proxy |

Esempio:

```yaml
learning:
  enabled: true
  default_mode: "learning"
  default_match_threshold: 0.85
  auto_switch_to_production: false
  min_patterns_for_production: 10
  min_match_rate_for_production: 80.0
  production_no_fallback: false
  signal_forward_to_cloud: false
```

---

## tls_decrypt

Decrittazione TLS (MITM) per intercettare il traffico crittografato.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Abilita la decrittazione TLS (disabilitata per default) |
| `listen_ports` | `array[integer]` | `[443, 8883, 5684, 8443]` | Porte su cui intercettare il traffico TLS |
| `ca_cert_path` | `string` | `"./certs/ca.pem"` | Percorso del certificato della CA usata per firmare i certificati MITM |
| `ca_key_path` | `string` | `"./certs/ca.key"` | Percorso della chiave privata della CA |
| `device_certs_dir` | `string` | `"./data/device_certs"` | Directory per i certificati generati per ogni dispositivo |
| `external_certs_dir` | `string` | `"./data/external_certs"` | Directory per certificati esterni importati |
| `pinning_bypass` | `dict[string, PinningBypassConfig]` | `{}` | Strategie di bypass certificate pinning per vendor (chiave = nome vendor) |
| `min_tls_version` | `string` | `"TLSv1.2"` | Versione TLS minima accettata |
| `max_tls_version` | `string` | `"TLSv1.3"` | Versione TLS massima accettata |

### tls_decrypt.pinning_bypass.\<vendor\>

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `strategy` | `string` | `"mitm_proxy"` | Strategia di bypass: `mitm_proxy` (proxy MITM standard), `frida` (hook Frida), `disable_pin_check` (disabilita verifica) |

Esempio:

```yaml
tls_decrypt:
  enabled: false
  listen_ports:
    - 443
    - 8883
    - 5684
    - 8443
  ca_cert_path: "./certs/ca.pem"
  ca_key_path: "./certs/ca.key"
  device_certs_dir: "./data/device_certs"
  external_certs_dir: "./data/external_certs"
  pinning_bypass:
    shelly:
      strategy: "mitm_proxy"
    nest:
      strategy: "frida"
  min_tls_version: "TLSv1.2"
  max_tls_version: "TLSv1.3"
```

---

## protocol_servers

Server per protocolli aggiuntivi oltre HTTP. Ogni server è disabilitato per default.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `mqtt` | `MQTTServerConfig` | — | Server/broker MQTT |
| `coap` | `CoAPServerConfig` | — | Server CoAP |
| `modbus` | `ModbusServerConfig` | — | Server Modbus TCP |
| `websocket` | `WebSocketServerConfig` | — | Server WebSocket |
| `raw_tcp` | `RawTCPServerConfig` | — | Server TCP raw |
| `http2` | `HTTP2ServerConfig` | — | Server HTTP/2 (h2/h2c) |
| `zigbee_bridge` | `ZigbeeBridgeConfig` | — | Bridge Zigbee (Zigbee2MQTT) |
| `zwave_bridge` | `ZWaveBridgeConfig` | — | Bridge Z-Wave (Z-Wave JS UI) |
| `matter_bridge` | `MatterBridgeConfig` | — | Bridge Matter (Matter.js) |

---

### protocol_servers.mqtt

Server broker MQTT incorporato.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Abilita il server MQTT |
| `host` | `string` | `"0.0.0.0"` | Indirizzo di ascolto |
| `port` | `integer` | `1883` | Porta MQTT non crittografata |
| `port_tls` | `integer` | `8883` | Porta MQTT con TLS |
| `tls_enabled` | `boolean` | `false` | Abilita TLS per MQTT |
| `max_packet_size` | `integer` | `268435` | Dimensione massima pacchetto MQTT in byte (256 KB) |
| `topic_filters` | `array[string]` | `["#"]` | Filtri topic MQTT da intercettare (tutti per default) |

---

### protocol_servers.coap

Server CoAP (Constrained Application Protocol).

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Abilita il server CoAP |
| `host` | `string` | `"0.0.0.0"` | Indirizzo di ascolto |
| `port` | `integer` | `5683` | Porta CoAP non crittografata |
| `dtls_enabled` | `boolean` | `false` | Abilita DTLS per CoAP |
| `dtls_port` | `integer` | `5684` | Porta DTLS |
| `max_pdu_size` | `integer` | `1024` | Dimensione massima PDU CoAP in byte |

---

### protocol_servers.modbus

Server Modbus TCP.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Abilita il server Modbus |
| `host` | `string` | `"0.0.0.0"` | Indirizzo di ascolto |
| `port` | `integer` | `502` | Porta Modbus TCP |
| `unit_id` | `integer` | `1` | ID unità Modbus predefinito |
| `tls_enabled` | `boolean` | `false` | Abilita Modbus su TLS (Modbus Security) |
| `tls_port` | `integer` | `802` | Porta Modbus Security |
| `holding_registers` | `dict[string, integer]` | `{}` | Mappa nome → indirizzo per holding register predefiniti |
| `coil_registers` | `dict[string, integer]` | `{}` | Mappa nome → indirizzo per coil register predefiniti |

---

### protocol_servers.websocket

Server WebSocket.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Abilita il server WebSocket |
| `host` | `string` | `"0.0.0.0"` | Indirizzo di ascolto |
| `port` | `integer` | `9000` | Porta WebSocket |
| `path` | `string` | `"/ws"` | Path dell' endpoint WebSocket |
| `max_message_size` | `integer` | `1048576` | Dimensione massima messaggio WebSocket in byte (1 MB) |

---

### protocol_servers.raw_tcp

Server TCP raw per protocolli non standard.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Abilita il server TCP raw |
| `host` | `string` | `"0.0.0.0"` | Indirizzo di ascolto |
| `port` | `integer` | `9100` | Porta TCP raw |
| `buffer_size` | `integer` | `4096` | Dimensione del buffer di lettura in byte |
| `idle_timeout` | `integer` | `300` | Timeout di inattività in secondi (5 minuti) |
| `protocol_detect` | `boolean` | `true` | Tenta il rilevamento automatico del protocollo |

---

### protocol_servers.http2

Server HTTP/2 (supporta sia h2 che h2c).

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Abilita il server HTTP/2 |
| `host` | `string` | `"0.0.0.0"` | Indirizzo di ascolto |
| `port` | `integer` | `443` | Porta HTTP/2 con TLS (h2) |
| `cleartext_port` | `integer` | `8080` | Porta HTTP/2 in chiaro (h2c) |
| `tls_enabled` | `boolean` | `true` | Abilita TLS per HTTP/2 |

---

### protocol_servers.zigbee_bridge

Bridge per dispositivi Zigbee tramite Zigbee2MQTT.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Abilita il bridge Zigbee |
| `mqtt_host` | `string` | `"localhost"` | Host MQTT di Zigbee2MQTT |
| `mqtt_port` | `integer` | `1883` | Porta MQTT di Zigbee2MQTT |
| `mqtt_user` | `string` | `""` | Utente MQTT (vuoto = nessuna autenticazione) |
| `mqtt_pass` | `string` | `""` | Password MQTT |
| `topic_prefix` | `string` | `"zigbee2mqtt"` | Prefisso topic MQTT di Zigbee2MQTT |
| `reconnect_interval` | `integer` | `10` | Intervallo di riconnessione in secondi |

---

### protocol_servers.zwave_bridge

Bridge per dispositivi Z-Wave tramite Z-Wave JS UI.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Abilita il bridge Z-Wave |
| `connection_type` | `string` | `"mqtt"` | Tipo di connessione: `mqtt` o `ws` (WebSocket) |
| `host` | `string` | `"localhost"` | Host del server Z-Wave JS UI |
| `port` | `integer` | `1883` | Porta MQTT (usata se `connection_type: mqtt`) |
| `ws_port` | `integer` | `3000` | Porta WebSocket (usata se `connection_type: ws`) |
| `mqtt_user` | `string` | `""` | Utente MQTT (vuoto = nessuna autenticazione) |
| `mqtt_pass` | `string` | `""` | Password MQTT |

---

### protocol_servers.matter_bridge

Bridge per dispositivi Matter tramite Matter.js.

| Campo | Tipo | Default | Descrizione |
|-------|------|---------|-------------|
| `enabled` | `boolean` | `false` | Abilita il bridge Matter |
| `controller_port` | `integer` | `5540` | Porta del controller Matter.js |
| `fabric_id` | `integer` | `1` | ID del fabric Matter |
| `vendor_id` | `integer` | `65521` | ID vendor Matter (0xFFF1 = test/development) |

---

Esempio completo `protocol_servers`:

```yaml
protocol_servers:
  mqtt:
    enabled: false
    host: "0.0.0.0"
    port: 1883
    port_tls: 8883
    tls_enabled: false
    max_packet_size: 268435
    topic_filters:
      - "#"
  coap:
    enabled: false
    host: "0.0.0.0"
    port: 5683
    dtls_enabled: false
    dtls_port: 5684
    max_pdu_size: 1024
  modbus:
    enabled: false
    host: "0.0.0.0"
    port: 502
    unit_id: 1
    tls_enabled: false
    tls_port: 802
    holding_registers: {}
    coil_registers: {}
  websocket:
    enabled: false
    host: "0.0.0.0"
    port: 9000
    path: "/ws"
    max_message_size: 1048576
  raw_tcp:
    enabled: false
    host: "0.0.0.0"
    port: 9100
    buffer_size: 4096
    idle_timeout: 300
    protocol_detect: true
  http2:
    enabled: false
    host: "0.0.0.0"
    port: 443
    cleartext_port: 8080
    tls_enabled: true
  zigbee_bridge:
    enabled: false
    mqtt_host: "localhost"
    mqtt_port: 1883
    mqtt_user: ""
    mqtt_pass: ""
    topic_prefix: "zigbee2mqtt"
    reconnect_interval: 10
  zwave_bridge:
    enabled: false
    connection_type: "mqtt"
    host: "localhost"
    port: 1883
    ws_port: 3000
    mqtt_user: ""
    mqtt_pass: ""
  matter_bridge:
    enabled: false
    controller_port: 5540
    fabric_id: 1
    vendor_id: 65521
```

---

## Esempio completo

```yaml
# config/config.yaml — ride-the-api
core:
  database_url: "sqlite+aiosqlite:///./data/core.db"
  device_db_dir: "./data/devices"
  device_databases: {}
  default_context_buffer_size: 524288

proxy:
  host: "0.0.0.0"
  port: 8911
  tls:
    enabled: true
    cert_file: "./certs/ride-api.pem"
    key_file: "./certs/ride-api.key"
  request_timeout: 30
  max_request_size: 1048576
  fallback:
    enabled: true
    timeout: 10
    retry_count: 2
    confidence_threshold: 0.7

vendors: {}

models:
  registry_path: "models"
  defaults:
    ac: "example_ac_v1.onnx"
    heat_pump: "example_hp_v1.onnx"
  inference:
    batch_size: 1
    intra_op_threads: 2
    inter_op_threads: 2
    execution_providers:
      - "CPUExecutionProvider"
  hot_reload:
    enabled: true
    check_interval: 30

control:
  policy:
    evaluation_interval: 60
    default_policy: "pid_thermal"
  online_learning:
    enabled: true
    buffer_size: 1000
    update_interval: 3600
    min_samples_for_update: 100

observability:
  logging:
    level: "INFO"
    format: "json"
    output: "stdout"
  metrics:
    enabled: true
    port: 9090
    path: "/metrics"
  tracing:
    enabled: true
    exporter: "console"
    otlp_endpoint: "http://localhost:4317"
    sample_rate: 0.1
  health_check:
    enabled: true
    port: 8080
    path: "/health"

dns:
  pihole_custom_dns: ""
  adguard_rewrites: ""

traffic_selection:
  default_action: "intercept"
  rules: []

llm_decipher:
  enabled: true
  default_profile: "default"
  profiles: {}

modification:
  enabled: true
  rules: []

correlation:
  enabled: true
  http:
    method: "connection"
    correlation_header: "X-Request-ID"
    keep_alive_timeout: 30
  mqtt:
    method: "topic_sequence"
    qos_tracking: true
    retain_handling: "include"
  coap:
    method: "message_id"
    confirmable_timeout: 5
  store_pairs: true
  max_pairs_per_device: 10000
  pair_ttl_hours: 168

learning:
  enabled: true
  default_mode: "learning"
  default_match_threshold: 0.85
  auto_switch_to_production: false
  min_patterns_for_production: 10
  min_match_rate_for_production: 80.0
  production_no_fallback: false
  signal_forward_to_cloud: false

tls_decrypt:
  enabled: false
  listen_ports:
    - 443
    - 8883
    - 5684
    - 8443
  ca_cert_path: "./certs/ca.pem"
  ca_key_path: "./certs/ca.key"
  device_certs_dir: "./data/device_certs"
  external_certs_dir: "./data/external_certs"
  pinning_bypass: {}
  min_tls_version: "TLSv1.2"
  max_tls_version: "TLSv1.3"

protocol_servers:
  mqtt:
    enabled: false
    host: "0.0.0.0"
    port: 1883
    port_tls: 8883
    tls_enabled: false
    max_packet_size: 268435
    topic_filters: ["#"]
  coap:
    enabled: false
    host: "0.0.0.0"
    port: 5683
    dtls_enabled: false
    dtls_port: 5684
    max_pdu_size: 1024
  modbus:
    enabled: false
    host: "0.0.0.0"
    port: 502
    unit_id: 1
    tls_enabled: false
    tls_port: 802
    holding_registers: {}
    coil_registers: {}
  websocket:
    enabled: false
    host: "0.0.0.0"
    port: 9000
    path: "/ws"
    max_message_size: 1048576
  raw_tcp:
    enabled: false
    host: "0.0.0.0"
    port: 9100
    buffer_size: 4096
    idle_timeout: 300
    protocol_detect: true
  http2:
    enabled: false
    host: "0.0.0.0"
    port: 443
    cleartext_port: 8080
    tls_enabled: true
  zigbee_bridge:
    enabled: false
    mqtt_host: "localhost"
    mqtt_port: 1883
    mqtt_user: ""
    mqtt_pass: ""
    topic_prefix: "zigbee2mqtt"
    reconnect_interval: 10
  zwave_bridge:
    enabled: false
    connection_type: "mqtt"
    host: "localhost"
    port: 1883
    ws_port: 3000
    mqtt_user: ""
    mqtt_pass: ""
  matter_bridge:
    enabled: false
    controller_port: 5540
    fabric_id: 1
    vendor_id: 65521
```

---

> Documento generato automaticamente dal modello `Config` in `/config/ride-the-api/core/config.py`.
> Ultimo aggiornamento: basato su `core/config.py` — tutte le classi Pydantic e i relativi default.