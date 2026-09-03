## 2026-03-31 - Pre-computed Sets for Validation Hot Paths
**Learning:** Dynamic set comprehensions like `{m.upper() for m in valid}` executed repeatedly during batch capture file validation introduce unnecessary allocations and overhead (~2.2x slower) compared to pre-computed module-level lookup dictionaries.
**Action:** Check validation and routing hot paths for repeated set constructions and pre-compute uppercase/lowercased lookups at module or init level.

## 2026-03-31 - Cached IP Objects for CIDR Matching Hot Paths
**Learning:** Parsing IP strings via `ipaddress.ip_address(client_ip)` inside rule evaluation loops causes repeated parsing overhead on every CIDR rule check (~3.1x slower evaluation). Caching the parsed `IPv4Address`/`IPv6Address` object on the request context (`TrafficRequestInfo`) reduces N string parses to 1 parse per request.
**Action:** When evaluating IP objects against multiple CIDR/subnet rules, parse the IP string once at request creation or lazily cache it on the request object.

## 2026-03-31 - Fast Message Cloning for Interception Hot Paths
**Learning:** Calling `copy.deepcopy` on full dataclass instances (`InterceptedMessage`) inside rule evaluation loops introduces significant object inspection overhead (~2.9x slower) compared to a specialized `.copy()` method that only deep-copies nested mutable payloads (`body`, `modifications`) and shallow-copies dicts.
**Action:** When cloning dataclass objects in hot paths, write a domain-specific `.copy()` method instead of delegating to generic `copy.deepcopy(obj)`.

## 2026-03-31 - Fast Schema Property Lookup in Request Matching
**Learning:** `_body_similarity` created intermediate `set` objects for schema properties and request body keys on every call during request matching, causing unnecessary heap allocations and garbage collection overhead. Iterating directly over schema properties with key-in-dict checks (`sum(1 for k in props if k in body)`) avoids set allocations and speeds up evaluation by ~1.14x per call.
**Action:** When comparing dict structure or key intersections in request matching hot paths, check key membership directly on dicts rather than constructing temporary `set(dict.keys())` objects.

## 2026-03-31 - Fast String-Based IP Family Check in DNS Resolution Hot Paths
**Learning:** `_addr_family` parsed IP strings via `ipaddress.ip_address(address.split('%', maxsplit=1)[0]).version` inside DNS cache hit loops to sort IPv6 before IPv4, causing heavy parsing and allocation overhead (~87x slower) compared to a simple string check `6 if ":" in address else 4`.
**Action:** When checking IP version (v4 vs v6) on formatted address strings in hot paths, use fast string presence checks (`":" in address`) instead of `ipaddress.ip_address` string parsing.

## 2026-03-31 - Pre-computed Trigger-to-Response Map in Pattern Engine
**Learning:** In `PatternEngine.find_best_match`, scanning `cached.server.responses` in an inner loop for every candidate endpoint introduced $O(N)$ response list iteration on every matched request. Pre-indexing triggers to responses into a `_response_trigger_maps` dict during `apply_pattern_db` replaces list iteration with $O(1)$ dict lookup (~21x faster response template resolution).
**Action:** When mapping candidate request entities to server response definitions in hot paths, precompute a trigger-to-response dictionary upon pattern loading instead of linear scanning during request evaluation.

## 2026-03-31 - Pre-indexed Protocol Adapters in Adapter Registry
**Learning:** `ProtocolAdapterRegistry.get_adapter_by_protocol` filtered `self._adapters.values()` with `[a for a in self._adapters.values() if protocol in a.supported_protocols]` on every protocol query, causing $O(N)$ list scanning overhead (~2.4x slower) compared to pre-indexing adapters by `ProtocolType` during registration.
**Action:** When looking up handlers or adapters by protocol/type, build a mapping dictionary during registration instead of dynamically scanning adapter collections at runtime.

## 2026-03-31 - Early Return Fast-Path for Unconfigured Rule Engines
**Learning:** In `ModificationEngine.process_request` and `process_response`, constructing `InterceptedMessage` instances, lowercasing header keys, and converting message records on every request/response when no modification rules are active added ~4.5us of unnecessary overhead per request. An early check `if not self._rules:` returns immediately (~35x faster processing for unconfigured rule engines).
**Action:** When an engine/middleware processes traffic through a dynamic rule pipeline, check if the rule list is empty first to bypass object preparation and loop overhead in the default/unconfigured state.

## 2026-03-31 - Pre-computed Tuples and Regex for Adapter Request Inspection Hot Paths
**Learning:** In `ProtocolAdapter.is_firmware_request`, `is_auth_request`, and HTTP request parsing methods, allocating inline `list` literals (e.g. `["/fota", ...]`) or calling `re.search` with raw regex strings on every incoming request introduced unnecessary heap allocation and regex compilation overhead (~1.47x slower per inspection call).
**Action:** Define immutable module-level tuples (e.g. `_FW_PATHS`) and pre-compiled regex objects (`_RE_DEVICE_PATH = re.compile(...)`) for path/topic keywords and route patterns checked in request-inspection hot paths.
