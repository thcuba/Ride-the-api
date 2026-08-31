# Multi-Protocol Server

Ride-the-API supports direct interception of multiple IoT and industrial protocols via native plugin servers. Each server is opt-in (disabled by default).

## Architecture

```
ProtocolServerManager — unified lifecycle
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

## Configuration

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

## Native Servers

### MQTT
- Built-in MQTT broker, supports QoS 0/1/2
- Topic filter to intercept only specific subscriptions
- Request/response correlation via topic_sequence
- Optional TLS on port 8883

### CoAP
- CoAP server (RFC 7252)
- Optional DTLS support on port 5684
- Correlation via message_id
- Configurable max PDU

### Modbus TCP
- Modbus TCP server with configurable slave unit
- TLS support (Modbus Security) on port 802
- Pre-configurable holding registers and coils

### WebSocket
- WebSocket server on port 9000 (configurable)
- Customizable path
- Max message size: 1MB

### Raw TCP
- Generic TCP server for non-standard protocols
- Automatic protocol detection (HTTP, MQTT, etc.)
- Configurable idle timeout
- 4KB buffer

### HTTP/2
- HTTP/2 server with h2c (cleartext) support on port 8080
- TLS on port 443

## Bridge Plugins

### Zigbee (Zigbee2MQTT)
- Connects to an existing Zigbee2MQTT instance via MQTT
- Configurable topic prefix (default: `zigbee2mqtt`)
- Auto-reconnect every 10 seconds

### Z-Wave (Z-Wave JS UI)
- Connects to Z-Wave JS UI via MQTT or WebSocket
- Supports both connection modes

### Matter (Matter.js)
- Connects to a Matter.js controller
- Configurable Fabric ID and Vendor ID

## Control API

```http
GET  /api/protocol-servers           # List servers with status
POST /api/protocol-servers/{name}/start   # Start a server
POST /api/protocol-servers/{name}/stop    # Stop a server
GET  /api/protocol-servers/{name}/config  # Current configuration
```

## Lifecycle

1. On startup, `ProtocolServerManager` reads the configuration
2. For each server with `enabled: true`, it creates the corresponding plugin, **wiring it with a
   common request handler** (`handle_protocol_request` in `core/server.py`)
3. Starts each server in a separate asyncio.Task
4. Each server intercepts packets and forwards them to `LearningOrchestrator`
5. The orchestrator correlates and buffers as with HTTP traffic

The common handler (`handle_protocol_request`) translates each plugin's `InterceptedRequest` into an
`orchestrator.handle_request(...)` call, mapping the protocol-aware method and path (e.g. an MQTT
topic becomes the path with method `publish`). A `local_response` result tells the plugin to send the
response back to the device.