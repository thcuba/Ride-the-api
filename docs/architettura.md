# Architettura di Ride-the-API

> **Panoramica architetturale** — proxy sostitutivo cloud locale che intercetta, impara e serve
> localmente i protocolli dei dispositivi IoT tramite analisi LLM.
>
> Versione documento: 1.0 — basato sul codice di `core/` e `adapters/`.

---

## 1. Flusso architetturale generale

Il sistema è progettato come una pipeline a sei stadi che trasforma il traffico IoT cifrato in
risposte locali autonome. Ogni richiesta attraversa i seguenti stadi:

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
│          │     │                                              │  Risposta       │                │
│          │     │                                              │  Locale  ──► IoT│                │
│          │     │                                              │  Oppure         │                │
│          │     │                                              │  Forward Cloud  │                │
│          │     │                                              │  (via nginx /   │                │
│          │     │                                              │   UpstreamRes.) │                │
│          │     │                                              └─────────────────┘                │
│          │     └──────────────────────────────────────────────────────────────────────────────┘
│          │
│          ▼
│     Vendor Cloud
│     (solo in fase di
│      apprendimento)
```

---

## 2. Stadi nel dettaglio

### 2.1 TLS MITM Server (`core/tls_mitm.py`)

**Ruolo**: terminazione TLS per tutti i dispositivi IoT, indipendentemente dal vendor.

- **Multi-port listening**: si mette in ascolto su porte configurabili (default 8443, 9443, 10443, …),
  in modo che nginx (reverse proxy sidecar) gli inoltri il traffico destinato a diverse porte cloud.
- **SNI Extraction**: analizza il `ClientHello` TLS senza completare l'handshake per estrarre
  l'hostname di destinazione. Questo permette la generazione dinamica del certificato appropriato.
- **CertManager** (`core/cert_manager.py`): genera automaticamente una CA radice (RSA 4096, SHA-256)
  al primo avvio, e per ogni nuovo hostname SNI genera un certificato foglia firmato dalla CA e lo
  cache su disco in `./certs/`.
- **IP-first device routing**: l'identità del dispositivo è determinata dall'**indirizzo IP di
  origine**, non dalla porta o dall'hostname. IP sconosciuti vengono auto-registrati con un database
  dedicato e `passthrough=ON`.
- **REST API**: permette di gestire porte, visualizzare statistiche, elencare dispositivi non
  identificati e scaricare il certificato CA per installazione manuale sui dispositivi.
- **Frida script injection**: fornisce uno script di instrumentazione dinamica per dispositivi che
  effettuano certificate pinning (`GET /api/tls/frida/script.js`).

### 2.2 TrafficSelector (`core/traffic_selector.py`)

**Ruolo**: decide se una richiesta deve essere intercettata (elaborata dalla pipeline locale) o
lasciata passare (inoltrata direttamente al cloud).

- **Regole valutate per priorità**: le regole sono ordinate per priorità (decrescente). La prima
  regola che matcha determina l'azione.
- **Tipi di match**:
  - `CIDR`: match su range IP (per traffico locale).
  - `HOSTNAME`: match su pattern hostname con wildcard (per traffico esterno).
  - `VENDOR`: match sul codice vendor (es. `ty`, `tl`, `zh`, `hr`).
  - `DEVICE_ID`: match su ID dispositivo specifico.
- **Azioni possibili**:
  - `INTERCEPT`: il traffico viene elaborato dalla pipeline edge AI locale.
  - `PASSTHROUGH`: il traffico viene inoltrato direttamente al cloud senza elaborazione.
- **Default action**: configurabile; in assenza di regole matchanti, si applica l'azione di default
  (di solito `INTERCEPT`).
- **Hot-reload**: le regole vengono ricaricate automaticamente quando la configurazione cambia.

### 2.3 LearningOrchestrator (Pipeline) (`core/pipeline.py`)

**Ruolo**: orchestratore centrale che gestisce il ciclo di apprendimento e produzione per ogni
dispositivo. Coordina BufferManager → LLMRouter → DecipherIngest → PatternEngine.

- **Modalità operative**:
  - `LEARNING`: intercetta richieste e risposte, le bufferizza, le invia all'LLM per l'analisi,
    salva i pattern appresi.
  - `PRODUCTION`: matcha le richieste in arrivo contro i pattern appresi; se il match è
    sufficiente, serve una risposta locale; altrimenti forwarda al cloud (con diverse strategie).
  - `HYBRID`: combina apprendimento e produzione contemporaneamente.
- **Auto-switch**: uno scheduler in background controlla ogni 60 secondi il match rate. Quando
  raggiunge ≥ 99% (con almeno 10 pattern e 50 richieste totali), passa automaticamente il
  dispositivo alla modalità produzione.
- **Rollback**: se il match rate scende sotto il 90% in produzione, si torna automaticamente in
  apprendimento.
- **Match rate tracking in tempo reale**: `hits / (hits + misses) × 100%`.

#### 2.3.1 BufferManager (`core/pattern_db/buffer_manager.py`)

**Ruolo**: accumula coppie richiesta/risposta correlate in un buffer a finestra scorrevole fino
al raggiungimento della capacità configurata, poi segnala il flush all'LLM.

- **Accumulo**: riceve coppie già correlate dalla pipeline e le memorizza nel database del
  dispositivo (tabella `LLMContextBuffer`) con una stima della dimensione in byte.
- **Sequenza**: ogni coppia riceve un numero di sequenza progressivo per mantenere l'ordine
  cronologico delle richieste.
- **Capacità configurabile**: ogni dispositivo ha un limite massimo di buffer configurabile
  (default 512 KB) impostabile in `DeviceRegistry.context_buffer_size`.
- **Flush**: quando il buffer supera la capacità massima, `add_pair()` restituisce `True` per
  segnalare che è ora di inviare all'LLM. Il metodo `flush()` marca tutte le entry come
  processate e resetta il contatore.
- **Esportazione/Importazione**: supporta il formato portabile `.ride-capture.json` tramite
  `export_capture()` e `import_capture()`, validato contro uno schema JSON tramite `Validator`.
  Questo permette di condividere tracce di traffico tra utenti senza esporre dati sensibili.
- **Pulizia cache**: dopo il flush, la `SessionCache` del dispositivo viene svuotata per il
  prossimo ciclo di apprendimento.

#### 2.3.2 LLMRouter (LLMDecipherService) (`core/llm_decipher.py`)

**Ruolo**: invia le coppie richiesta/risposta bufferizzate a un LLM configurabile per l'analisi
del protocollo e la decifratura dei campi.

- **Multi-provider**: supporta qualsiasi API compatibile con OpenAI (OpenAI, Ollama locale,
  vLLM, ecc.) tramite profili configurabili.
- **Profili LLM** (`LLMProfile`): ogni profilo specifica `base_url`, `api_key`, `model_id`,
  `prompt_template`, `timeout` e `max_retries`. La `api_key` può essere risolta da variabili
  d'ambiente con sintassi `${VAR_NAME}`.
- **Deciphering singolo e batch**: `decipher_pair()` analizza una singola coppia; `decipher_batch()`
  analizza più coppie in parallelo con `asyncio.gather()`.
- **Costruzione prompt**: il prompt viene costruito a partire da un template che include la
  richiesta, la risposta, lo schema del database vendor, i pattern recenti e le note di contesto
  dell'utente (`llm_context_notes`).
- **Formato di risposta**: l'LLM deve restituire JSON strutturato con `intent`, `fields`,
  `confidence`, `suggested_dp_codes` e `protocol_notes`. Il sistema tenta di estrarre JSON anche
  da risposte markdown (contenenti ```json ... ```).
- **Cache**: i risultati decifrati vengono memorizzati in cache in memoria (TTL: 1 ora) per
  evitare chiamate LLM ridondanti.
- **Retry con backoff**: in caso di timeout o errore HTTP, tenta fino a `max_retries` volte con
  backoff esponenziale (1s, 2s, …).

#### 2.3.3 DecipherIngest (`core/pattern_db/decipher_ingest.py`)

**Ruolo**: prende l'output strutturato dell'LLM e lo trasforma in pattern persistenti nel
database specifico del dispositivo.

- **Input**: dizionario strutturato con chiave `"patterns"` contenente una lista di pattern.
- **Operazione**: per ogni pattern, crea:
  - Un `RequestPattern` con metodo, path pattern, protocollo, header richiesti, schema body,
    chiavi query params, intent e confidence.
  - Un `ResponseTemplate` collegato al pattern, con status code, template header/body,
    field mappings e variabili attese.
  - Uno o più `FieldMapping` che collegano i campi della richiesta (`source`) ai campi della
    risposta (`target`), con tipo di trasformazione (direct, enum, formula) e confidence.
- **Aggiornamento statistiche**: incrementa `patterns_learned` e `templates_created` nelle
  `MatchStats` del dispositivo.
- **Esportazione/Importazione**: `export_patterns()` produce un `PatternDB` portabile (formato
  `.ride-pattern.json`); `import_patterns()` carica pattern da un file portabile, validandoli
  contro lo schema JSON tramite `Validator`.

#### 2.3.4 PatternEngine (`core/pattern_db/pattern_engine.py`)

**Ruolo**: matcha le richieste in arrivo contro i pattern appresi e costruisce risposte locali
con risoluzione di variabili di stato e formule sicure.

- **Pattern matching**: `find_best_match()` calcola un punteggio di similarità (0.0–1.0) tra la
  richiesta in arrivo e ogni pattern noto, basato su:
  - **Method match** (30%): uguaglianza del metodo HTTP.
  - **Path similarity** (30%): corrispondenza del path con supporto per placeholder `{id}`.
  - **Required headers** (15%): quanti degli header richiesti sono presenti.
  - **Query params** (10%): quanti dei parametri attesi sono presenti.
  - **Body schema** (15%): corrispondenza delle chiavi del corpo con lo schema atteso.
- **Costruzione risposta locale**: `build_local_response()` parte da un `ResponseTemplate` e:
  1. Applica le `field_mappings` per trasferire valori dalla richiesta o dallo stato del
     dispositivo alla risposta.
  2. Risolve i template variables `{state.nome_variabile}`, `{request.path.al campo}` e `{uuid}`.
  3. Supporta trasformazioni: `direct` (copia diretta), `enum` (mapping valori), `formula`
     (espressioni aritmetiche sicure valutate via AST walker).
- **State Management**: ogni dispositivo ha un `DeviceStateStore` che mantiene variabili di
  stato persistenti (es. temperatura attuale, modalità operativa) e sensori virtuali.
- **Virtual sensors**: sensori simulati che aggregano dati di stato e applicano formule (es.
  media mobile, conversione unità di misura).
- **Formula safety**: le formule nei pattern vengono valutate tramite un AST interpreter
  ristretto (`_interp()`) che ammette solo operazioni aritmetiche, confronti e funzioni
  matematiche di base (`abs`, `min`, `max`, `round`, `int`, `float`, `str`). Nessun accesso
  ad attributi, import o chiamate arbitrarie — prevenzione dell'iniezione di codice.
- **Caching**: i pattern possono essere caricati da file `.ride-pattern.json` in memoria per
  matching ultra-rapido senza toccare il database.

---

## 3. Correlazione Richiesta/Risposta

La correlazione avviene nella pipeline (`pipeline.py`) e collega una richiesta del dispositivo
alla corrispondente risposta del cloud. Il meccanismo è progettato per funzionare su HTTP/1.1,
HTTP/2, WebSocket, CoAP, MQTT e altri protocolli.

**Strategie di correlazione**:

1. **Connection tracking**: le richieste e risposte che transitano sulla stessa connessione TCP
   vengono associate temporalmente.
2. **Sequence numbers**: per protocolli che li supportano, i numeri di sequenza vengono estratti
   e usati come chiave di correlazione.
3. **Correlation IDs**: header `X-Request-ID`, `X-Correlation-ID` o equivalenti vengono usati
   per abbinare richiesta e risposta.
4. **Timeout di attesa**: se una risposta non arriva entro un timeout configurabile (default 30s),
   la richiesta viene considerata orfana e scartata.

**Struttura dati**: ogni coppia correlata viene memorizzata in `SessionCache` come:

| Campo | Descrizione |
|---|---|
| `correlation_key` | Chiave univoca per il matching (connessione + seq o correlation ID) |
| `method`, `path`, `headers`, `body` | Richiesta originale |
| `response_status`, `response_headers`, `response_body` | Risposta correlata |
| `correlated` | Flag booleano che indica se la coppia è completa |
| `in_buffer` | Flag che indica se è già stata inviata al BufferManager |

Dopo la correlazione, la coppia viene passata al `BufferManager.add_pair()` per l'accumulo.
La `SessionCache` viene svuotata a ogni flush del buffer.

---

## 4. Device Database per Dispositivo

**Principio fondante**: ogni dispositivo IoT ha il proprio database di protocollo dedicato.
Questo isola completamente i pattern appresi, impedendo interferenze tra dispositivi diversi
(anche dello stesso vendor) e garantendo che ogni dispositivo funzioni in modo indipendente.

### 4.1 Architettura

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
│  Device DB #1 (per-device, es. 192.168.1.42)│
│                                               │
│  RequestPattern   ResponseTemplate            │
│  FieldMapping     LLMContextBuffer            │
│  SessionCache     MatchStats                  │
│  InterceptedRequest                           │
│                                               │
├──────────────────────────────────────────────┤
│  Device DB #2 (per-device, es. 192.168.1.77)│
│  ... stesse tabelle ...                       │
└──────────────────────────────────────────────┘
```

### 4.2 Componenti del database per dispositivo

| Tabella | Scopo |
|---|---|
| **DeviceRegistry** | (Core) Anagrafica centrale. Mappa ogni `device_id` al suo vendor, tipo, IP, modalità operativa, soglia di match, configurazione LLM override e dimensione buffer. |
| **RequestPattern** | Pattern di richiesta appresi. Include metodo, path pattern, header richiesti, schema body, intent decifrato e confidence. |
| **ResponseTemplate** | Template di risposta locale. Collega a un pattern, specifica status code, template header/body, field mappings e variabili attese. |
| **FieldMapping** | Mappatura campo-a-campo tra richiesta e risposta. Supporta trasformazioni: `direct`, `enum` (con mappa valori), `formula` (espressioni aritmetiche). |
| **LLMContextBuffer** | Buffer a finestra scorrevole di coppie richiesta/risposta non ancora inviate all'LLM. Ogni entry ha una stima della dimensione in byte e un flag `flushed`. |
| **SessionCache** | Cache temporanea per la correlazione richiesta/risposta. Viene svuotata dopo ogni flush del buffer. |
| **MatchStats** | Statistiche in tempo reale: richieste totali, hit locali, miss cloud, errori, match rate percentuale, pattern appresi, dimensioni attuali del buffer. |
| **InterceptedRequest** | Storico raw delle richieste intercettate. Usato per audit, debug e training di modelli ML on-device. |

### 4.3 Routing per IP

Il `DatabaseManager.resolve_device_id(ip_address)` determina a quale dispositivo appartiene un
dato IP di origine cercando nell'elenco `ip_addresses` di ogni `DeviceRegistry`. Questo è il
meccanismo centrale per il routing IP-first: **l'IP di origine è la chiave d'accesso al database
del dispositivo**.

### 4.4 Database personalizzati

Ogni dispositivo può avere un database completamente separato (URL personalizzato) tramite il
campo `DeviceRegistry.database_url`. Questo permette di isolare fisicamente i dati di dispositivi
diversi (es. su volumi o cluster PostgreSQL separati). Di default, tutti i dispositivi condividono
un database SQLite unico con tabelle separate per dispositivo.

---

## 5. Gestione dei Fallimenti e Resilienza (`core/resilience.py`)

Il modulo di resilienza verifica che i dispositivi possano funzionare indipendentemente dal cloud
del vendor. Funzionalità chiave:

- **Cloud Independence Verifier**: API REST che testa se un dispositivo può operare senza il cloud
  del vendor, analizzando la completezza dei pattern appresi e le statistiche di match.
- **Auto-switch scheduler**: esegue ogni 60 secondi e valuta il match rate di ogni dispositivo.
- **Soglie configurabili**:
  - `AUTO_SWITCH_MATCH_RATE` = 99% (passaggio a produzione).
  - `ROLLBACK_MATCH_RATE` = 90% (ritorno ad apprendimento).
  - `MIN_PATTERNS_FOR_SWITCH` = 10 (pattern minimi per considerare lo switch).
  - `MIN_TOTAL_REQUESTS` = 50 (richieste minime per statistica affidabile).
- **Forwarding loop prevention**: il `UpstreamResolver` risolve i nomi cloud direttamente tramite
  DNS pubblici (8.8.8.8 / 1.1.1.1, dual-stack IPv4+IPv6), bypassando il DNS locale
  (dnsmasq/Pi-hole/AdGuard Home) per evitare che il proxy re-inserisca se stesso.

---

## 6. Modifica On-the-Fly (`core/modification.py`)

Engine di modifica in tempo reale che consente di alterare richieste e risposte al volo secondo
regole configurabili. Supporta:

- Modifica di header, body, parametri query.
- Iniezione di JavaScript/CSS per debugging.
- Logging selettivo del traffico modificato.
- Regole basate su pattern regex, metodi HTTP e path specifici.

---

## 7. Adapter per Protocolli Specializzati (`adapters/`)

Il sistema include adapter per protocolli IoT non-HTTP, ciascuno in una directory dedicata:

| Adapter | Protocollo | Scopo |
|---|---|---|
| `mqtt/` | MQTT | Bridge pub/sub per dispositivi MQTT cloud-based |
| `coap/` | CoAP | Dispositivi Constrained Application Protocol |
| `shelly/` | Shelly API | Adattatore specifico per dispositivi Shelly |
| `zigbee/` | Zigbee | Bridge per coordinator Zigbee |
| `zwave/` | Z-Wave | Bridge per controller Z-Wave |
| `thread_matter/` | Thread / Matter | Bridge per dispositivi Matter over Thread |
| `modbus/` | Modbus | Bridge per dispositivi industriali Modbus TCP |
| `base/` | — | Classe astratta base per tutti gli adapter |
| `example/` | — | Template per creare nuovi adapter |

Ogni adapter implementa l'interfaccia `InterceptedRequest` per normalizzare richieste e risposte
provenienti da protocolli diversi, permettendo al core di elaborarli uniformemente.

---

## 8. Formati Portabili

Il sistema supporta due formati portabili per la condivisione e il riutilizzo dei dati di
apprendimento tra utenti:

### 8.1 `.ride-capture.json` (CaptureDB)

Contiene tracce raw di traffico intercettato (coppie richiesta/risposta) in formato JSON.
Usato per:
- Condividere tracce anonimizzate tra utenti.
- Iniziare l'apprendimento su un dispositivo senza dover intercettare il traffico dal vivo.
- Debug e analisi offline.

### 8.2 `.ride-pattern.json` (PatternDB)

Contiene pattern decifrati, template di risposta, field mappings e configurazione di stato.
Usato per:
- Distribuire pattern già appresi a nuovi dispositivi dello stesso modello.
- Effettuare backup e ripristino dello stato di apprendimento.
- Validazione collaborativa dei pattern tra utenti della community.

Entrambi i formati sono validati tramite schema JSON e supportano l'obfuscatio automatica dei
dati sensibili (device ID, MAC, seriali).

---

## 9. Riepilogo del Ciclo di Vita di una Richiesta

```
1. IoT Device → [TLS MITM Server]                    (terminazione TLS, SNI extraction)
2. [TLS MITM Server] → [TrafficSelector]              (decidere: intercetta o passa?)
3. [TrafficSelector] → PASSTHROUGH → [UpstreamResolver] → Cloud
                   ↘ INTERCEPT → [Pipeline (LearningOrchestrator)]

   Se INTERCEPT e in modalità LEARNING:
4. [Pipeline] → Correlazione richiesta/risposta   (connection tracking / seq # / correlation ID)
5. [Pipeline] → BufferManager.add_pair()          (accumulo in finestra scorrevole)
6. Se buffer pieno → BufferManager.flush()        (segnala pronto per LLM)
7. [Pipeline] → LLMDecipherService.decipher_batch() (analisi LLM)
8. [LLMDecipherService] → DecipherIngest.ingest()   (salva pattern nel device DB)
9. [DecipherIngest] → BufferManager.clear_cache()   (prepara prossimo ciclo)
10. La risposta cloud originale viene inoltrata al dispositivo (trasparente)

   Se INTERCEPT e in modalità PRODUCTION:
4'. [Pipeline] → PatternEngine.find_best_match()   (similarity score contro pattern)
5'. Se score ≥ soglia → PatternEngine.build_local_response() → risposta locale
6'. Se score < soglia e production_no_fallback → 501 (conclusive local-only)
7'. Se score < soglia e signal_forward_to_cloud → nginx forwarda al cloud
8'. Se score < soglia e nessun flag → forward + apprendimento dalla miss

   Auto-switch (scheduler 60s):
     match_rate ≥ 99% → LEARNING → PRODUCTION
     match_rate < 90% → PRODUCTION → LEARNING
```