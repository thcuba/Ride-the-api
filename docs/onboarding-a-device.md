# Onboarding a real device

How to take one named device, point it at the proxy, and see the first translated
request/response pair. Every step below has a check you can run, because the failures in this
chain are quiet: a listener that never came up, a device that never asked, a model that answers
with prose instead of JSON.

Worked example: a **a consumer IoT device** (`192.168.1.77`) that talks to
`ats.iot.vendor-cloud.example` (a vendor cloud IoT broker, MQTT over WSS on **443**). The measured results of
that run are at the bottom.

## 0. What has to be true before starting

| Piece | Why it is needed |
|-------|------------------|
| The proxy listens where the device looks (**443**) | The device dials 443 itself; a DNS answer pointing at a port nobody listens on produces a refused connection, not an interception |
| Something answers DNS for that device | Either this proxy (see [deployment](deployment.md#dns-interception)) or the network's resolver with a per-client upstream / rewrite |
| A capable model for the decipher | A model too small produces prose, not the pattern JSON the pipeline parses |

## 1. Make the proxy listen on the privileged ports

```bash
sudo setcap cap_net_bind_service=+ep "$(readlink -f .venv/bin/python)"
systemctl --user restart ride-the-api
```

The capability goes on the **resolved** interpreter: `bin/python` in a virtualenv is a symlink
and `setcap` refuses symlinks ("Invalid file ... for capability operation") while changing
nothing. One capability covers 53 and 443.

**Check** — `GET /health` reports what is bound, so nothing has to be guessed:

```bash
curl -s http://127.0.0.1:8911/health | jq .ports
# "tls_mitm": {"requested": [443, 8883, 5684, 8443], "bound": [...], "missing": []}
```

`missing` must be empty. The log says the same: `TLS MITM: successfully bound 4/4 ports`.

## 2. Send the device's names to the proxy

Two distinct roles, deliberately:

- the **network's resolver** (an external DNS filter Home, a DNS filter, a local DNS server) decides *which device* is sent
  here — a per-client upstream DNS, reversible by deleting that entry;
- **this proxy** intercepts, for that client, the names in `dns.intercept` **plus** the hostnames
  every registered protocol adapter declares, and forwards everything else untouched — unless
  `dns.intercept_all` is deliberately switched on, in which case every name is answered by the proxy
  and only `dns.exclude` (clock, OCSP/CRL, local names) keeps a name out.

The adapters are a source of intercepted names on purpose: a service is covered by shipping the
adapter that implements it, not by an operator remembering to append its domains one at a time.
`dns.intercept` stays for names that have no adapter behind them.

```yaml
dns:
  enabled: true
  listen_host: 0.0.0.0
  listen_port: 53
  answer_ip: 192.168.1.200          # what intercepted names resolve to
  intercept:
    - ats.iot.vendor-cloud.example  # the vendor cloud, by name
```

Leave time sources (`ntp.*`) out: the device needs its real clock.

**Check** — ask the proxy from a *different* host, so the network path is exercised and not just
loopback:

```bash
dig +short @192.168.1.200 ats.iot.vendor-cloud.example   # -> 192.168.1.200 (the proxy)
dig +short @192.168.1.200 example.org                      # -> the real address (forwarded)
```

## 3. Make the device talk

The device only produces traffic when it retries its cloud connection: **power-cycle it**. Its
DNS then flows resolver -> proxy, the name resolves to the proxy, and it dials 443 here.

**Checks**, in order:

1. the resolver's query log shows the device's name answered by the proxy address;
2. the proxy logs the TLS session on 443;
3. the pair appears in the learning store. The device entry is created from the traffic itself
   (`ip-192-168-1-77`) — there is nothing to pre-register, `/api/devices/{id}/...` answers
   *404* until the first packet arrives.

Enrich it as soon as it exists, so the decipher prompt carries the right context instead of
`vendor: unknown`:

```bash
curl -X PUT http://127.0.0.1:8911/api/devices/ip-192-168-1-77/tls-config \
  -H "X-API-Key: $PASSWORD" -H 'Content-Type: application/json' \
  -d '{"name": "Living room air sensor", "vendor": "example-vendor", "passthrough": true, "pinning_bypass": "mitm_proxy"}'
```

## 4. Turn the traffic into a translation

The decipher sends the learned pairs to the configured LLM profile and stores the patterns it
returns (`GET /api/devices/{id}/patterns`). Two things decide whether that works:

- **the model.** Check it on the real task, not with the provider's "hello": local models on a
  small board answered in prose after ~56 s (and never finished without a ceiling), while a
  cloud model returned valid patterns in ~12 s;
- **`max_tokens`** (default 2048, `0` = do not send): a model that ignores
  `response_format: json_object` otherwise generates until the client `timeout` cuts it off, and
  the retry pays that timeout again.

Then switch the device to `production` (`POST /api/devices/{id}/mode`) once its match rate is
good, and it is served locally.

## What goes wrong, and what it looks like

**Diagnose in this order.** A healthy proxy that captures nothing is almost always one of the
three switches above, and they are checked in the order the runtime hits them:

1. `GET /api/devices/<id>` → `capture.state`. `capturing` means nothing is in the way.
2. Is the device actually resolving to the proxy? `dig @<proxy> <its hostname>` must answer the
   proxy address, and the resolver in front of it must forward here (a client-specific upstream
   entry in a DNS filter only applies to clients it matched).
3. Is the proxy decrypting at all? `echo | openssl s_client -connect <proxy>:443 -servername
   <hostname> | openssl x509 -noout -issuer` — `Ride the API Local CA` means it will decrypt; the
   vendor's real issuer (e.g. `Amazon RSA 2048 M01`) means the connection is only relayed and no
   amount of pipeline debugging will produce a pair.

| Symptom | Cause | Fix |
|---------|-------|-----|
| Device resolves to the proxy but nothing is intercepted | 443 not listening (no `CAP_NET_BIND_SERVICE`) | step 1; `ports.tls_mitm.missing` shows it |
| Device works normally, nothing is seen | its names are in neither `dns.intercept` nor any adapter's declared hostnames | open the dashboard's *DNS Discovery* panel and promote the name it asked for, add the name here, or add the adapter for that service so every name it uses is covered — an unclaimed name is forwarded, never guessed |
| Device has no DNS at all | it was pointed at a resolver that does not answer | point it at a resolver that forwards here, or at this proxy with `dns.enabled` |
| Device talks to the proxy but its buffer stays empty | one of the three capture switches is off: per-IP `bypass`, TLS passthrough, or device mode `passthrough` | read `capture.state` in `GET /api/devices/{id}`: it names the switch that is off and why. `relayed` means TLS passthrough (the device refused our certificate), not a broken pipeline. `tls_refusals` (and `capture.refusals`) says how many handshakes it aborted: `1` is a device that refused once and is forwarded ever since, a growing number is one still trying to negotiate |
| `last_seen` is null / 0 requests on a device whose buffer has bytes | the match counters only record requests the pipeline *analysed*; a device parked in bypass or passthrough is never analysed | look at `capture.state`; a device that is `capturing` and still shows numbers here is simply not reaching the proxy |
| TLS session fails after the proxy certificate | the app does not trust the proxy CA or pins the vendor certificate | Install/trust the Ride CA, or download the Frida script with an authenticated `GET /api/tls/frida/script.js` request (send the control-plane password in `X-API-Key`) and run it manually. The saved `pinning_bypass` value is guidance only; until the device accepts the proxy certificate, traffic is relayed encrypted and cannot be buffered for the LLM. |
| Decipher fails with "LLM call failed" | local model produced nothing usable within the timeout | give the profile a capable model (and a `max_tokens` ceiling) |
| `/health` reports `version: 0.2.0` on a prebuilt | the bundle had no `VERSION` file | fixed in the build workflow; a source deploy writes it in `bootstrap.sh` |

## Measured run — a consumer IoT device

```
proxy          192.168.1.200 (a proxy host), release 1.2.15
device         192.168.1.77, cloud ats.iot.vendor-cloud.example (a vendor cloud IoT broker, 443)
DNS            ats... -> 192.168.1.200 | example.org -> real address | ntp... -> forwarded
listeners      TLS MITM bound 4/4 ports [443, 8883, 5684, 8443]; DNS on 0.0.0.0:53 (udp+tcp, +ipv6)
resolver       an external DNS filter Home, client "Living room air sensor" upstream 192.168.1.200
translation    same prompt: local model 56.4s -> prose, invalid JSON; cloud model 11.9s -> 2 patterns
open           the device has to be power-cycled to retry its cloud connection; when it did, it
               REFUSED the local CA (tls_untrusted_reason=client_refused_certificate, a vendor cloud IoT
               broker pinning its trust anchor) and the proxy relayed it encrypted from then on, so no
               pairs reached the buffer -- the working capture for that device is DNS-level (the names
               it asks for) plus the relayed contact: recorded when the session ENDS, so hours of
               real activity show up as nothing until it closes (the open ones are listed by
               GET /api/tls/relayed in in_progress), not the LLM buffer
```

## References

- [Deployment](deployment.md) — capability, DNS interception, both deployment shapes
- [Architecture](architecture.md) — DNS interception, request lifecycle, learning/production
- [Verification](verification.md) — the request/response path proved end to end
- [API](api.md) — device, pattern and health endpoints used above
