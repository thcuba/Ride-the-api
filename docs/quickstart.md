# Guida Rapida — Ride-the-API

Proxy di sostituzione cloud locale che intercetta il traffico IoT, impara i protocolli tramite LLM e serve risposte localmente.

---

## 1. Prerequisiti

- **Python ≥ 3.11**
- **Git**
- **DNS server** (dnsmasq, Pi-hole o AdGuard Home) per il routing dei dispositivi
- **API LLM** (OpenAI, Ollama locale, vLLM, ecc.)
- **Opzionale — Docker**: per lo stack completo (nginx + ride-the-api)

---

## 2. Clona il repository

```bash
git clone https://github.com/thcuba/Ride-the-api.git
cd Ride-the-api
```

---

## 3. Installa le dipendenze

### Con pip (consigliato)

```bash
pip install -e .
```

### Con uv (alternativa più veloce)

```bash
uv pip install -e .
```

### Dipendenze opzionali

- **Dev tools** (ruff, mypy, pytest): `pip install -e ".[dev]"`
- **GPU ONNX**: `pip install -e ".[gpu]"`
- **TFLite**: `pip install -e ".[tflite]"`

---

## 4. Configura config.yaml

Copia e personalizza il file di configurazione:

```bash
cp config/config.yaml config/config.local.yaml
# oppure modifica direttamente config/config.yaml
```

### 4.1 — LLM (obbligatorio)

Imposta il profilo LLM nella sezione `llm_decipher`:

```yaml
llm_decipher:
  enabled: true
  default_profile: "default"

  profiles:
    default:
      base_url: "https://api.openai.com/v1"
      api_key: "${OPENAI_API_KEY}"            # o metti la chiave in chiaro
      model_id: "gpt-4o-mini"
```

> **Ollama locale:** cambia `base_url` in `http://localhost:11434/v1`, `api_key` in `"ollama"`, e `model_id` in `"llama3.1:8b"` (o il modello che preferisci).

Esporta la variabile d'ambiente:

```bash
export OPENAI_API_KEY="sk-..."
```

### 4.2 — Database (facoltativo)

Per sviluppo con SQLite (default), non serve modificare nulla:

```yaml
core:
  database_url: "sqlite+aiosqlite:///./data/core.db"
  device_db_dir: "./data/devices"
```

Per produzione, imposta un URL PostgreSQL:

```yaml
core:
  database_url: "postgresql+asyncpg://user:pass@localhost/ride_api"
```

### 4.3 — Modalità apprendimento/produzione

```yaml
learning:
  enabled: true
  default_mode: "learning"        # learning | production | hybrid
  default_match_threshold: 0.85
  auto_switch_to_production: false  # true per switch automatico al 99% match rate
```

### 4.4 — Routing DNS (esempio dnsmasq)

Crea `/etc/dnsmasq.d/ride-api.conf`:

```
# Sostituisci 192.168.1.100 con l'IP del server ride-the-api
address=/mqtt.example.com/192.168.1.100
address=/api.example.com/192.168.1.100
address=/openapi.example.com/192.168.1.100
```

Riavvia dnsmasq:

```bash
sudo systemctl restart dnsmasq
```

---

## 5. Avvia il server

### Direttamente con Python

```bash
python -m core.server
```

Il server si avvia su `http://0.0.0.0:8911` (di default).

### Con Docker Compose (consigliato per produzione)

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Lo stack avvia:
- **nginx** sulle porte 80/443 (TLS) e 8883 (MQTT over TLS)
- **Ride-the-API** sulla porta interna 8911

### Come servizio systemd (Linux)

```bash
sudo cp deploy/ride-the-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ride-the-api
sudo systemctl start ride-the-api
```

---

## 6. Connetti un dispositivo IoT

### 6.1 — Configura il DNS del dispositivo

- Sul tuo router/dhcp, imposta il DNS primario sull'IP del server ride-the-api
- Oppure configura staticamente il DNS sul dispositivo IoT
- Oppure, per un test rapido, aggiungi una riga in `/etc/hosts` del dispositivo:

```
192.168.1.100   mqtt.example.com
192.168.1.100   api.example.com
```

### 6.2 — (Opzionale) Intercetta TLS

Se il dispositivo usa HTTPS, il proxy MITM deve essere attivo:

```yaml
tls_decrypt:
  enabled: true
  listen_ports:
    - 443
    - 8883
```

Scarica il certificato CA da `http://<ip-server>:8911/api/tls/ca.pem` e installalo sul dispositivo come autorità di fiducia.

### 6.3 — Verifica la connessione

Accendi il dispositivo IoT. Il dispositivo inizierà a parlare col cloud — ride-the-api intercetta il traffico e inizia l'apprendimento.

---

## 7. Verifica

### Dashboard web

Apri nel browser:

```
http://<ip-server>:8911/
```

Vedrai:
- Elenco dei dispositivi rilevati
- Match rate e numero di pattern appresi per ogni dispositivo
- Pulsanti per switch tra modalità learning/production
- Buffer fill level

### API health check

```bash
curl http://localhost:8911/health
```

Risposta attesa: `{"status": "ok"}` (o simile).

### Stato TLS

```bash
curl http://localhost:8911/api/tls/ports
```

Mostra le porte di ascolto TLS e i certificati attivi.

### Log in tempo reale

```bash
# Se avviato manualmente — i log vanno su stdout
tail -f data/core.log
```

---

## 8. Prossimi passi

| Cosa fare | Documentazione |
|---|---|
| Capire l'architettura completa | `docs/nginx-architecture.md` |
| Formato database portatile | `docs/portable-pattern-database.md` |
| Editor pattern via web UI | `http://localhost:8911/patterns/{device_id}` |
| Esportare/importare pattern | API REST: `GET/POST /api/patterns/export` |
| Configurare server protocolli diretti (MQTT, CoAP, Modbus…) | Sezione `protocol_servers` in `config.yaml` |

---

## 9. Risoluzione problemi rapidi

| Problema | Causa probabile | Soluzione |
|---|---|---|
| Il dispositivo non viene rilevato | DNS non punta al proxy | Verifica `nslookup <hostname-cloud>` dal dispositivo |
| `Connection refused` su :8911 | Server non avviato | Controlla `python -m core.server` e i log |
| TLS handshake fallisce | Certificato CA non installato sul device | Scarica e installa CA da `/api/tls/ca.pem` |
| Match rate al 0% | Nessun pattern ancora appreso | Aspetta qualche richiesta in learning mode |
| Forwarding loop | DNS risolve al proxy stesso | Usa `signal_forward_to_cloud: true` con nginx |
| `OPENAI_API_KEY` non trovata | Variabile d'ambiente non impostata | `export OPENAI_API_KEY="sk-..."` |