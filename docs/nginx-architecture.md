# nginx Reverse Proxy Architecture

> Design document for the nginx sidecar that provides stable, loop-free cloud forwarding
> for Ride-the-API.

## Problem

When Ride-the-API needs to forward an unrecognized request to the real vendor cloud
(e.g. during learning or when a production miss occurs), it resolves the cloud hostname
through the system DNS. In a typical setup, the system DNS points to a local DNS server
(dnsmasq / Pi-hole / AdGuard Home) which returns the proxy's own IP address for those
hostnames. This creates an infinite forwarding loop:

```
AdGuard Home → Ride-the-API → forward_to_cloud() → system DNS → "that IP is yours" → Ride-the-API → ...
```

## Solution: nginx sidecar with dedicated DNS resolver

A nginx reverse proxy sits in front of Ride-the-API as a Docker sidecar. It:

1. **Terminates TLS** for device cloud hostnames (port 443)
2. **Routes requests** to Ride-the-API (internal port 8911) for local pattern matching
3. **Catches forward signals** — when Ride-the-API returns HTTP 502 + `X-Action: forward`,
   nginx proxies the original request to the real vendor cloud via a **dedicated dual-stack
   resolver** that bypasses the local DNS entirely

```
IoT Device ──▶ nginx (443) ──▶ Ride-the-API (8911)
                  │                    │
                  │                    └── 502 + X-Action: forward ──▶ nginx ──▶ Vendor Cloud
                  │                                                              (via 8.8.8.8/1.1.1.1)
                  └── MQTT/raw TCP (8883) ──▶ Vendor Cloud
                                              (via 8.8.8.8/1.1.1.1)
```

## DNS resolver configuration

nginx uses the `resolver` directive to resolve cloud upstream hostnames:

```nginx
resolver 8.8.8.8 1.1.1.1 valid=300s ipv6=on;
```

- **8.8.8.8** (Google, primary) — first resolver tried
- **1.1.1.1** (Cloudflare, fallback) — used if 8.8.8.8 is unreachable
- **valid=300s** — results cached for 5 minutes
- **ipv6=on** — IPv6 lookups enabled (AAA records)

### IPv6 equivalents

| Provider | IPv4 | IPv6 |
|----------|------|------|
| Google   | 8.8.8.8 | 2001:4860:4860::8888 |
| Cloudflare | 1.1.1.1 | 2606:4700:4700::1111 |

## Forwarding mechanism

### HTTP forward signal

1. Ride-the-API detects an unmatched request with `signal_forward_to_cloud` enabled
2. Returns `HTTP 502 Bad Gateway` with header `X-Action: forward`
3. nginx' `error_page 502 = @cloud_redirect` directive catches this
4. The `@cloud_redirect` location re-issues the **original request** to the cloud upstream
5. The cloud upstream is resolved via the dedicated resolver (never through local DNS)

```nginx
location / {
    proxy_pass http://ride_api;
    error_page 502 = @cloud_redirect;
}

location @cloud_redirect {
    proxy_pass https://cloud_upstream;
    # resolver: 8.8.8.8 1.1.1.1 ipv6=on ← set in the upstream block
}
```

### Non-HTTP protocols (MQTT, raw TCP)

For non-HTTP protocols that nginx cannot re-issue, the `stream` module proxies
connections directly to the cloud:

```nginx
stream {
    upstream mqtt_cloud {
        resolver 8.8.8.8 1.1.1.1 valid=300s ipv6=on;
        server mqtt.example.com:8883;
    }
    server {
        listen 8883 ssl;
        proxy_pass mqtt_cloud;
        proxy_ssl_server_name on;
    }
}
```

## Software resolver fallback

For protocols not proxied through nginx (CoAP, Modbus, custom protocols), the
Python module `core/upstream_resolver.py` provides the same dual-stack resolution
via the `dnspython` library:

```python
async def resolve_upstream(hostname: str, prefer_ipv6: bool = False) -> list[str]:
    """Resolve via 8.8.8.8 → 1.1.1.1, with system-resolver fallback."""
```

- Resolves A (IPv4) and AAAA (IPv6) records
- Returns IPv6 first when `prefer_ipv6=True`
- Falls back to the system resolver (with a warning) when all upstream DNS servers are unreachable
- 300s in-memory cache shared across all callers
- Batch resolution with `batch_resolve_upstream()` for parallel lookups

## Configuration flags

Two flags in `config.yaml` control production-mode behavior:

```yaml
learning:
  signal_forward_to_cloud: false   # Enable 502+X-Action signaling for nginx
  production_no_fallback: false    # Return 501 on miss (no cloud fallback at all)
```

- With **both false** (default): falls through to `adapter.forward_to_cloud()` (legacy path)
- **signal_forward_to_cloud=true**: nginx handles cloud forwarding loop-free
- **production_no_fallback=true**: unmatched requests return 501 — fully local operation

## Deployment

The nginx sidecar runs alongside Ride-the-API in Docker Compose:

```yaml
services:
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
      - "8883:8883"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./data/certs:/etc/nginx/certs:ro

  ride-the-api:
    build: .
    # port 8911 NOT exposed to the host — only accessible via nginx
    expose:
      - "8911"
```

## See also

- `deploy/nginx.conf` — full nginx configuration
- `deploy/docker-compose.yml` — Docker Compose with nginx sidecar
- `core/upstream_resolver.py` — Python upstream DNS resolver for non-HTTP protocols
- `config/config.yaml` — `signal_forward_to_cloud` and `production_no_fallback` flags
