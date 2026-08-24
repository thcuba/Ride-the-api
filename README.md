# Ride-the-API

Proxy che intercetta il traffico IoT, impara i protocolli tramite LLM, e risponde localmente — sostituendo il cloud vendor.

```
IoT Device ──▶ nginx/TLS MITM ──▶ Ride-the-API ──▶ Vendor Cloud (solo apprendimento)
                                        │
                                        └── Buffer → LLM → Pattern Engine → Risposta locale
```

## Perché

I dispositivi IoT muoiono quando il vendor chiude i server. Ride-the-API intercetta le chiamate al cloud, le analizza con un LLM, e impara a rispondere localmente.

## Quick Start

```bash
git clone https://github.com/thcuba/Ride-the-api.git
cd Ride-the-api
pip install -e .
# Configura LLM in config/config.yaml
python -m core.server
```

Apri `http://localhost:8911/` — dashboard + pattern editor.

### Docker (con nginx sidecar)

```bash
docker compose -f deploy/docker-compose.yml up -d
```

nginx su 443 → loop-free cloud forwarding via DNS 8.8.8.8/1.1.1.1.

## Docs

| Documento | Contenuto |
|---|---|
| [Architettura](docs/architettura.md) | Componenti, flussi, modalità learning/production |
| [Quick Start](docs/quickstart.md) | Guida dettagliata installazione |
| [Configurazione](docs/configurazione.md) | Riferimento completo config.yaml |
| [Deployment](docs/deployment.md) | Docker, systemd, produzione |
| [API Reference](docs/api.md) | Tutti gli endpoint REST |
| [Pattern DB](docs/portable-pattern-database.md) | Formato .ride-pattern.json / .ride-capture.json |
| [Nginx Architecture](docs/nginx-architecture.md) | Reverse proxy + DNS loop prevention |
| [Protocol Servers](docs/protocol-servers.md) | MQTT, CoAP, Modbus, WebSocket, Raw TCP, bridge plugin |

## Funzionalità principali

- **Apprendimento automatico** — cattura traffico, correla richiesta/risposta, analisi LLM, genera pattern
- **Risposta locale** — matcha richieste contro pattern appresi e risponde senza cloud
- **Auto-switch** — passa automaticamente da learning a production quando il match rate ≥ 99%
- **TLS MITM** — intercettazione TLS multi-porta con SNI e certificati dinamici
- **Multi-protocollo** — HTTP, MQTT, CoAP, Modbus, WebSocket, Raw TCP, HTTP/2, Zigbee, Z-Wave, Matter
- **Pattern portabili** — esporta/importa `.ride-pattern.json` e `.ride-capture.json`
- **Sensori virtuali** — simulazione di sensori con drift, periodico, random
- **Stato persistente** — variabili di stato del dispositivo (power, mode, temperatura)
- Dashboard web + editor pattern integrato
- Resilienza e retry automatici

## Configurazione rapida LLM

```yaml
# config/config.yaml
llm_decipher:
  profiles:
    default:
      base_url: "https://api.openai.com/v1"
      api_key: "${OPENAI_API_KEY}"
      model_id: "gpt-4o-mini"
```

## Progetti correlati

- [haos-jellyfin](https://github.com/thcuba/haos-jellyfin) — Jellyfin per HAOS con accelerazione hardware
- [HAOS Hermes Agent](https://github.com/thcuba/HAOS-hermes-agent) — AI agent per Home Assistant OS

## Licenza

MIT