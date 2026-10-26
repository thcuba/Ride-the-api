# Verifying the request/response path

Any claim about this project must be backed by one of the two things below. This file records
what has actually been run, on what, and what remains open.

## 1. The executable specification

`tests/test_complete_path.py` **is** the specification of a truthful path. It walks the whole
route in order, against a **real** HTTP vendor on a localhost socket (not a mock), and asserts
each step:

| # | What it proves | How |
|---|----------------|-----|
| 1 | A fresh install comes up and serves | empty database directory, stack built as the lifespan does |
| 2 | Forwarding preserves bytes, status and headers **in both directions** | vendor answers `429` + `retry-after` + `application/xml` + a non-JSON body; the device must see them unchanged, and the vendor must receive the device's byte-identical payload |
| 3 | Learning captures the **real** response | the stored pair must carry the `429` and its headers, not a fabricated `200` |
| 4 | Local serving survives losing the vendor and a restart | vendor shut down → learned pattern serves locally with the sent value; then the database manager is rebuilt on the same directory (restart / container recreation with a persistent volume) and it must still serve locally |

Stated limits of that test: the pattern *extraction* step is fed a canned analysis instead of
calling a real model, so model quality is not measured. Capture, correlation, persistence,
matching and serving are production code, and the network exchange is real.

## 2. Live verification on hardware

Run on a a proxy host 4 (`pi4ride`), deployed from source, service `ride-the-api` under a
systemd user unit, proxy listening on `:8911`, with a **real public vendor**
(`httpbingo.org`) reached over the internet. Commit `a5e6b51`.

| Check | Command (from the proxy) | Observed |
|-------|----------------------|----------|
| Status is preserved | `curl -H "Host: httpbingo.org" http://127.0.0.1:8911/example/status/429` | `HTTP 429` |
| Non-JSON body and content-type are preserved | `curl -D - -H "Host: httpbingo.org" http://127.0.0.1:8911/example/xml` | `HTTP 200`, `content-type: application/xml`, XML body unchanged |
| The vendor receives the device's own bytes | `curl -X POST -H "Host: httpbingo.org" -H "content-type: text/plain" --data-binary 'probe=<not-json>&x=1' http://127.0.0.1:8911/example/anything` | `data: 'probe=<not-json>&x=1'`, `content-type: text/plain` — no re-encoding |
| Control-plane errors leak no internals | `curl -H "X-API-Key: …" "http://127.0.0.1:8911/api/tls/certs/invalid%20hostname"` | `400 {"error":"Invalid certificate lookup request"}` |
| An unreachable vendor fails **loudly** | passthrough to a name that does not resolve | `502 {"error":"Cloud passthrough failed", …}` — never a fabricated `200 "forwarded"` |
| The integration specification passes on the device | `.venv/bin/python -m pytest tests/test_complete_path.py` | `1 passed` |

## Defects this found

Live verification is not decoration: it found defects the suite could not.

- **Nothing was ever learned on the HTTPS path** (`core/server.py`,
  `handle_tls_decrypted_request`). The decrypted body was handed to the pipeline as raw `bytes`,
  while `InterceptedRequest.body` is the *parsed* body and `session_cache.body` is a JSON column.
  The INSERT raised `TypeError: Object of type bytes is not JSON serializable`, so on the HTTPS
  path — the one every real device speaks — each request was dropped and the device received
  `502 {"error": "local_response_unavailable"}` instead of the upstream response. The body is now
  normalised at every ingress (`normalize_request_body`: JSON → dict, text → str, binary → a
  lossless base64 marker) while the raw bytes stay in `body_bytes` for byte-faithful forwarding.
  `tests/test_tls_learning_path.py` fails without the fix, with exactly that INSERT error.

- **The forward ignored the request's `Host` header** (`adapters/example`, `adapters/shelly`).
  The adapters looked up `headers["Host"]` case-sensitively, so with the lower-cased headers
  the proxy hands over they never found it and fell back to the **configured** hostname. A
  request for `httpbingo.org` was forwarded to `api.example.com`. Fixed in #408; the suite had
  missed it because the integration test substitutes the resolver, so the hostname chosen for
  the forward was never asserted — live traffic was.

## Still not verified

- A run against **one named physical device and firmware version**: DNS interception of a real
  device, its own certificate handling and retry logic, and its behaviour across a real reboot.
- Pattern extraction by a **real LLM** from real vendor traffic, end to end.
- A container recreation with the shipped `deploy/` compose file (the restart case is covered
  by rebuilding the database manager on the same directory, not by Docker).

Until those exist, the project stays **experimental protocol emulation**, and a device is only
labelled cloud independent when it is in production mode *and* its measured match rate reaches
`AUTO_SWITCH_MATCH_RATE` (`core/resilience.py`).
