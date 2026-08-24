# Server Multi-Protocollo

Ride-the-API supporta l'intercettazione diretta di molteplici protocolli IoT e industriali tramite server plugin nativi. Ogni server è opt-in (disabilitato per default).

## Architettura

```
ProtocolServerManager — lifecycle unificato
├── MQTT Server (1883 / 8883 TLS)
├── CoAP Server (5683 / 5684 DTLS)
├── Modbus TCP Server (502 / 802 TLS)
├── WebSocket Server (9000)
├── Raw TCP Server (9100)
├── HTTP/2 Server (443 / 8080 h2c)
├── Zigbee Bridge (via Zigbee2MQTT)
├── Z-Wave Bridge (via Z-Wave JS UI)
└── Matter Bridge (via Matter.js)
```

## Configurazione

```yaml
# config/config.yaml
protocol_servers:
  mqtt:
    enabled: true
    host: "0.0.0.0"
    port: 1883
    tls_enabled: false
    port_tls: 8883
    topic_filters: ["#"]
```

## Server nativi

### MQTT
- Broker MQTT integrato, supporta QoS 0/1/2
- Topic filter per intercettare solo sottoscrizioni specifiche
- Correlazione richiesta/risposta via topic_sequence
- TLS opzionale su porta 8883

### CoAP
- Server CoAP RFC 7252
- Supporto DTLS opzionale su porta 5684
- Correlazione via message_id
- Max PDU configurabile

### Modbus TCP
- Server Modbus TCP con unità slave configurabile
- Supporto TLS (Modbus Security) su porta 802
- Registri holding e coil pre-configurabili

### WebSocket
- Server WebSocket su porta 9000 (configurabile)
- Path personalizzabile
- Max message size: 1MB

### Raw TCP
- Server TCP generico per protocolli non standard
- Rilevamento automatico del protocollo (HTTP, MQTT, etc.)
- Timeout idle configurabile
- Buffer 4KB

### HTTP/2
- Server HTTP/2 con supporto h2c (cleartext) su porta 8080
- TLS su porta 443

## Bridge plugin

### Zigbee (Zigbee2MQTT)
- Si connette a un'istanza Zigbee2MQTT esistente via MQTT
- Topic prefix configurabile (default: `zigbee2mqtt`)
- Reconnect automatico ogni 10 secondi

### Z-Wave (Z-Wave JS UI)
- Si connette a Z-Wave JS UI via MQTT o WebSocket
- Supporta entrambe le modalità di connessione

### Matter (Matter.js)
- Si connette a un controller Matter.js
- Fabric ID e Vendor ID configurabili

## API di controllo

```http
GET  /api/protocol-servers           # Lista server con stato
POST /api/protocol-servers/{name}/start   # Avvia server
POST /api/protocol-servers/{name}/stop    # Ferma server
GET  /api/protocol-servers/{name}/config  # Configurazione attuale
```

## Ciclo di vita

1. All'avvio, `ProtocolServerManager` legge la configurazione
2. Per ogni server con `enabled: true`, crea il plugin corrispondente
3. Avvia ogni server in un asyncio.Task separato
4. Ogni server intercetta i pacchetti e li inoltra al `LearningOrchestrator`
5. L'orchestrator correla e bufferizza come per il traffico HTTP