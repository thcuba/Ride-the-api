## 2026-03-31 - Pre-computed Sets for Validation Hot Paths
**Learning:** Dynamic set comprehensions like `{m.upper() for m in valid}` executed repeatedly during batch capture file validation introduce unnecessary allocations and overhead (~2.2x slower) compared to pre-computed module-level lookup dictionaries.
**Action:** Check validation and routing hot paths for repeated set constructions and pre-compute uppercase/lowercased lookups at module or init level.

## 2026-03-31 - Cached IP Objects for CIDR Matching Hot Paths
**Learning:** Parsing IP strings via `ipaddress.ip_address(client_ip)` inside rule evaluation loops causes repeated parsing overhead on every CIDR rule check (~3.1x slower evaluation). Caching the parsed `IPv4Address`/`IPv6Address` object on the request context (`TrafficRequestInfo`) reduces N string parses to 1 parse per request.
**Action:** When evaluating IP objects against multiple CIDR/subnet rules, parse the IP string once at request creation or lazily cache it on the request object.

## 2026-03-31 - Fast Message Cloning for Interception Hot Paths
**Learning:** Calling `copy.deepcopy` on full dataclass instances (`InterceptedMessage`) inside rule evaluation loops introduces significant object inspection overhead (~2.9x slower) compared to a specialized `.copy()` method that only deep-copies nested mutable payloads (`body`, `modifications`) and shallow-copies dicts.
**Action:** When cloning dataclass objects in hot paths, write a domain-specific `.copy()` method instead of delegating to generic `copy.deepcopy(obj)`.
