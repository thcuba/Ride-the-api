## 2026-03-31 - Fast First-Segment Topic Extraction and Pre-compiled Regexes
**Learning:** `MQTTServerPlugin._device_id_from_topic` evaluated `[p for p in topic.split("/") if p]` to extract the first non-empty MQTT topic segment, splitting the entire topic and allocating intermediate lists across all segments (~1.43x slower) on every captured message. In addition, `_safe_db_name` and `_safe_filename` compiled raw regex strings on every call (~1.3x slower).
**Action:** When extracting initial path or topic segments in capture hot paths, iterate over `split()` directly with early return rather than constructing full list comprehensions over trailing segments, and pre-compile sanitization regexes at module level.

## 2026-03-31 - Fast Single-Variable Formula Evaluation in Pattern Engine
**Learning:** `PatternEngine._eval_formula` executed regex substitutions and simpleeval AST parsing for all formula strings, including simple single variable references like `{state.temp}` or `{request.body.target}`. Short-circuiting single variable references with direct `store.get()` or `_resolve_source()` lookups bypasses regex substitution and AST parsing overhead (~9x to 15x speedup per formula evaluation).
**Action:** When evaluating template or formula expressions in response building hot paths, add fast-path direct lookups for isolated variable references before delegating to full expression parsers.

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
**Learning:** `_addr_family` parsed IP strings via `ipaddress.ip_address(address.split('%', maxsplit=1)[0]).version` inside DNS cache hit loops to sort IPv6 before IPv4, causing heavy parsing and allocation overhead (~87x slower) compared to a simple string check `6 if ":" in address` else `4`.
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

## 2026-03-31 - Fast Direct Dict Navigation for JSON Path Hot Paths
**Learning:** Resolving and setting dot-separated JSON paths via generic `dpath.get` and `dpath.new` during local response building and rule evaluation introduced significant path parsing, string splitting, and regex/AST traversal overhead. Adding a fast-path direct dictionary/list traversal for dot-paths without array brackets (`[`) yields ~35.9x faster path resolution and ~8.3x faster overall local response generation, while falling back to `dpath` when array index brackets are present.
**Action:** When evaluating or setting nested properties in JSON payload hot paths, attempt direct dictionary key and integer list index navigation first before calling heavy JSONPath/dpath libraries.

## 2026-03-31 - Fast String Prefix Check for Local IP Classification
**Learning:** Checking whether an IP address is local/private via `ipaddress.ip_address(ip).is_private` on every incoming request introduced significant string parsing and object allocation overhead (~7.0us per check). Implementing a fast-path string prefix check for standard private IPv4 ranges (`192.168.`, `10.`, `127.`, `169.254.`, `172.16-31.`) and exact IPv6 loopback (`::1`) reduces local IP detection overhead to ~735ns per call (~9.5x speedup), falling back to `ipaddress` for uncommon ranges.
**Action:** Use fast string prefix matching for standard IPv4/IPv6 private IP classifications in request evaluation hot paths before falling back to full `ipaddress.ip_address` object parsing.

## 2026-03-31 - Early Exit Fast-Paths for Traffic Selection and Pattern Matching
**Learning:** In `TrafficSelector.evaluate`, checking `if not self.rules:` early bypasses lock acquisition and debug logging overhead when no traffic selection rules exist (~7.2x speedup). In pattern similarity loops (`PatternEngine` / `PatternMatcher`), breaking early on `best_score >= 1.0` and skipping candidates with mismatched HTTP methods when `best_score >= 0.7` (since max score without method match is 0.70) avoids expensive path/score calculation on remaining candidates (~10x speedup).
**Action:** In linear matching loops with bounded scoring, break immediately when max score is reached and fast-skip candidate items whose bounded maximum possible score cannot exceed the current best score.

## 2026-03-31 - Direct ASCII Character Length for Standard JSON Serialization Size
**Learning:** `json.dumps()` in Python defaults to `ensure_ascii=True`, escaping all non-ASCII characters as `\uXXXX` sequences. Calling `len(serialized.encode('utf-8'))` in buffer estimation loops needlessly allocated intermediate `bytes` objects. Using `len(serialized)` directly yields the exact same byte length without allocation (~1.20x faster).
**Action:** When calculating UTF-8 byte size for ASCII-guaranteed JSON strings (from `json.dumps(..., ensure_ascii=True)`), use `len(serialized)` to avoid allocating intermediate `bytes` objects.

## 2026-03-31 - Cached Normalized Field Mappings and Exact Path Fast-Path
**Learning:** `_normalize_field_mappings` created lists of field mapping dicts on every `build_local_response` invocation, and `_path_similarity` performed `lru_cache` lookups and string splitting even for exact path matches. Caching normalized field mappings on template objects (`_get_normalized_field_mappings`) yields a ~1.24x speedup per `build_local_response` call, while an exact string equality check `if pattern == actual: return 1.0` yields a ~1.55x speedup for path similarity evaluation.
**Action:** Cache normalized structures directly on template/schema objects when building responses, and check exact string equality before entering memoized or splitting logic on path comparisons.

## 2026-03-31 - Accumulator Loops over Generator Expressions for Hot-Path Collection Counting
**Learning:** Using `sum(1 for h in required_headers if h in actual_headers)` inside pattern matching loops introduced generator creation and iterator protocol overhead on every request matching call. Standard `for` loops with accumulator variables (`matches = 0; for h in required_headers: if h in actual_headers: matches += 1`) avoid generator allocations, resulting in ~2.5x faster collection counting per call and ~1.45x overall speedup for request similarity evaluation (~0.29s vs ~0.42s per 100,000 iterations).
**Action:** When counting matching keys or elements in request/response hot paths, use explicit accumulator `for` loops instead of generator expressions with `sum(...)`.

## 2026-03-31 - Pre-compiled JSON Schema Validator Instances
**Learning:** Calling `jsonschema.validate(data, schema)` on every validation request dynamically looks up and constructs a validator class instance on every call (~20.8s for 1,000 capture validations). Pre-instantiating schema validators via `validator_for(schema)(schema)` and reusing module-level cached validator instances reduces validation overhead to ~0.084s-0.26s per 1,000 calls (~80x to 290x speedup).
**Action:** When performing schema validation on fixed schemas in request or import hot paths, pre-compile and cache the `jsonschema` validator instances (`validator_for(schema)(schema)`) rather than calling generic `jsonschema.validate(data, schema)`.

## 2026-03-31 - Fast Single-Pass Address Partitioning in Upstream Resolver
**Learning:** `resolve_upstream` ran `is_blocked_ip(ip)` and `_addr_family(ip)` twice for every address via dual-list comprehensions (e.g. `blocked = [ip for ip in addrs if is_blocked_ip(ip)]` and `addresses = [ip for ip in addrs if not is_blocked_ip(ip)]`). Replacing dual list comprehensions with single-pass partition loops reduces predicate calls by 50% and speeds up SSRF filtering and IPv6 sorting in DNS resolution by ~2x (from 9.12s to 4.59s per 10k batch).
**Action:** When partitioning collections by a condition or predicate function (especially heavy functions parsing IP strings), use a single-pass `for` loop to append to `safe` and `blocked` lists instead of calling the predicate twice in separate list comprehensions.

## 2026-03-31 - Fast Set Membership and Primitive Bypasses for Credential Redaction Hot Paths
**Learning:** In `core/redaction.py`, `_is_sensitive_key` called `str(key).strip().lower()` unconditionally on every key in payload dictionaries, allocating new string objects even when keys matched standard lowercase names. `redact_value` evaluated `(dict, list)` before checking primitive strings, executing string lowercasing on all primitive integers/floats/booleans. Adding an exact string set lookup fast-path `if key in SENSITIVE_KEYS: return True` and checking `isinstance(value, str)` first in `redact_value` eliminates string allocations for un-redacted primitive fields and speeds up capture redaction by ~15-20% (~0.83s vs ~0.95s per 100,000 requests).
**Action:** When evaluating sensitive dictionary keys in payload redaction hot paths, perform an exact set membership check on raw string keys first before falling back to lowercasing/stripping, and short-circuit primitive non-string values.

## 2026-03-31 - Bulk SQL UPDATE for Buffer Store Flushing
**Learning:** In `SqlAlchemyBufferStore.flush` and `flush_selected`, fetching ORM model instances via `select(...)` and mutating attributes in a Python `for` loop caused ORM object instantiation overhead and $N$ individual SQL `UPDATE` statements on commit (~42.7ms for 500 items). Using a single bulk SQL `update(...)` query (combined with `func.coalesce(func.sum(...))` for size calculation) executes directly in SQLite/PostgreSQL without loading ORM objects into memory (~4.3ms for 500 items, ~10x speedup).
**Action:** When updating or marking status on batches of database rows in buffer or queue operations, use SQLAlchemy's bulk `update()` query construct instead of ORM instance loop mutation.

## 2026-03-31 - Pre-computed Field Path Segments for Modification Rules
**Learning:** In `ModificationRule`, parsing dot-separated JSON paths via `clean.split(".")` and prefix checks (`startswith("$.")`) on every rule match and modification application introduced repeated string allocation and path parsing overhead. Pre-computing `_field_path_parts` as a tuple during rule initialization (`__post_init__`) avoids re-parsing dot-paths and speeds up getter/setter field navigation in rule evaluation by ~2.1x to 2.6x (~0.42s vs ~1.09s per 1,000,000 evaluations).
**Action:** Pre-compute dot-path tuples on rule/schema initialization whenever static path properties are checked repeatedly across request modification pipelines.

## 2026-03-31 - Fast ASGI Scope Access for Middleware Request Inspection
**Learning:** In Starlette HTTP middleware, accessing `request.url.path` creates a new `URL` object and parses URL components on every request (~4x slower than raw string access). In ASGI body-size middleware, calling `dict(scope.get("headers") or [])` constructs an intermediate dictionary and hashes all header keys on every request (~1.4x slower than iterating header tuples directly).
**Action:** In ASGI/Starlette middleware hot paths, access `scope.get("path", "")` directly for path prefix checks and iterate over `scope.get("headers")` tuples instead of building temporary dictionary objects.

## 2026-03-31 - Bulk Pre-fetching for Pattern DB Exports
**Learning:** Querying `ResponseTemplate` by `pattern_id` and `FieldMapping` by `intent` inside the loop over `RequestPattern` in `DecipherIngest.export_patterns` and `export_device_model` caused $2N + 1$ SQL queries per export call. Pre-fetching all templates and mappings into dictionary lookups (`templates_by_pattern_id` and `mappings_by_intent`) reduces queries to 3 total, resulting in a ~9.5x speedup (~9.92s to ~1.04s per 20 exports of 100 patterns).
**Action:** When exporting or serializing relational entities from database sessions, bulk-query child/related tables and index by key before entering iteration loops.

## 2026-03-31 - Pre-computed Set for Orphaned Response Cross-Checks in Pattern Validation
**Learning:** Checking `trigger not in endpoint_intents.values()` in pattern validation hot paths forced linear $O(N)$ scans over `dict_values` for every trigger ($O(N \times M)$ overall). Pre-computing `endpoint_intent_values = set(endpoint_intents.values())` replaces linear list/dict-values iteration with $O(1)$ set lookups (~107x faster check for pattern validation).
**Action:** Always pre-convert `dict.values()` or `dict.keys()` to a `set` before performing repeated membership checks (`in` / `not in`) inside loops.

## 2026-03-31 - Direct SQL Aggregates for Cloud Independence Verification
**Learning:** `CloudIndependenceVerifier.check_cloud_independence` fetched all `RequestPattern` and `ResponseTemplate` ORM model instances into Python lists via `scalars().all()` to count patterns/templates and evaluate max confidence. Replacing ORM model fetching with SQL aggregate functions (`func.count` and `func.max`) computes totals directly in the database engine, eliminating ORM object instantiation and list allocations (~1.44x speedup, ~18.98ms -> ~13.14ms per check).
**Action:** When evaluating existence, counts, or thresholds across database entities in verification or background polling loops, execute SQL aggregate queries (`func.count`, `func.max`) instead of loading ORM model instances into memory.

## 2026-03-31 - Memoized JSON Path Segment Splitting for Pattern Engine and Response Building
**Learning:** Repeatedly checking `path.startswith("$.")` and calling `clean.split(".")` inside JSON path resolution hot paths (`_get_nested`, `_resolve_json_path`, `_dpath_set`) allocates temporary `list` objects on every call. Caching clean segment tuples via `@functools.lru_cache(maxsize=2048)` (`_split_clean_path`) eliminates list allocations and speeds up JSON path resolution by ~1.65x (from ~1.97s to ~1.20s per 1,500,000 lookups).
**Action:** Memoize string splitting and prefix stripping for dynamic string property paths checked repeatedly in payload navigation hot paths.

## 2026-03-31 - Fast Tuple Iteration for Sensitive Key Presence Checks in Payload Redaction
**Learning:** `_contains_sensitive_key` evaluated `any(key in lower for key in _SENSITIVE_KEY_NAMES)` using a generator expression and a `frozenset`. Generator creation and iterator protocol overhead added ~70% runtime overhead compared to an explicit `for key in _SENSITIVE_KEY_NAMES:` loop over a pre-computed `tuple` (~1.70x speedup, 1.39s vs 2.36s per 500k checks).
**Action:** Replace generator expressions (`any(...)`) in string presence checks on payload/redaction hot paths with explicit `for` loops over pre-computed `tuple` objects.

## 2026-03-31 - Upper-Bound Score Short-Circuiting for Pattern Matching Hot Paths
**Learning:** In `PatternEngine._calculate_similarity`, method and path similarity account for up to 60.0 score points, while headers, query parameters, and body schemas can contribute at most 40.0 additional points. Calculating the upper-bound `(score + 40.0) / total_weight` and short-circuiting when it cannot exceed the current `best_score` eliminates unnecessary header, query, and body schema key matching for sub-optimal candidates (~1.64x speedup per pattern evaluation batch, ~1.01s vs ~1.67s per 100k requests).
**Action:** In iterative matching loops with bounded component scores, pass the current best score to candidate evaluators and compute an upper-bound maximum possible score after initial cheap components (e.g. method + path) to short-circuit detailed field comparisons early.

## 2026-03-31 - Bulk Targeted Pre-fetching for DeviceModel Merge
**Learning:** Querying `RequestPattern`, `ResponseTemplate`, and `FieldMapping` sequentially inside the loop over `model.commands` in `DecipherIngest.merge_device_model` caused $O(N + M)$ individual SQL SELECT queries per model merge. Pre-computing candidate IDs and bulk-querying existing rows into dictionary lookups (`existing_patterns`, `existing_templates`, `existing_mappings`) via `.in_(...)` filters reduces queries to 3 total, resulting in an ~8x speedup (~0.138s vs ~1.096s per 250 merged items).
**Action:** When upserting or merging batches of entities in database sessions, pre-compute candidate ID sets and execute targeted bulk `.in_(...)` queries into dictionaries before entering the iteration loop.

## 2026-03-31 - Cached Initial Headers for Request Modification Engine
**Learning:** `ModificationEngine._apply_to_request` evaluated `{k.lower(): v for k, v in original.headers.items()}` on every request to determine whether `msg.headers` had been modified by a rule. Pre-computing a copy on `InterceptedMessage._initial_headers` in `from_request` and comparing `msg.headers != msg._initial_headers` avoids redundant dictionary comprehension and lowercasing per request (~5x speedup for header modification checks in the request hot path).
**Action:** Store a pre-computed lowercased dictionary snapshot on messages during parsing/instantiation to allow O(1) change detection without re-building dictionary comprehensions on completion.

## 2026-03-31 - Native Dictionary Cloning for Header Redaction
**Learning:** Initializing `result = dict(headers)` constructs the initial copy in native C code (~0.16s for 500,000 calls) and only assigns `REDACTED` for matching sensitive keys, avoiding Python element-by-element assignment loop overhead (`result[name] = value`) for clean keys while strictly preserving function immutability contracts.
**Action:** When redacting or updating dictionary fields where most keys remain unchanged, use `result = dict(input)` native copy first before overwriting sensitive entries.

## 2026-03-31 - Fast Direct Raw Header Appending for Security Headers Middleware
**Learning:** In Starlette `BaseHTTPMiddleware`, calling `response.headers.setdefault(...)` multiple times triggers multiple linear scans and key conversions over `response.raw_headers` via `MutableHeaders`. Direct tuple iteration on `response.raw_headers` and bulk `extend()` with pre-computed `(bytes, bytes)` tuples bypasses `MutableHeaders` wrapper overhead and reduces security header injection time by ~2.3x per response (~0.36s vs ~0.84s per 100,000 HTTP responses).
**Action:** When injecting fixed HTTP response headers in Starlette/FastAPI middleware, perform a single-pass inspection directly on `response.raw_headers` tuples and append pre-encoded `(b"name", b"value")` byte tuples instead of making multiple `response.headers.setdefault()` calls.

## 2026-03-31 - Cached Device Buffer Size in Capture Buffer Manager
**Learning:** `BufferManager._get_max_buffer_size(device_id)` executed an async SQL query `select(DeviceRegistry.context_buffer_size)` on every single captured request/response pair added via `add_pair`. Caching the max buffer size in an instance dictionary (`_buffer_size_cache`) and clearing it during session cache flushes eliminates DB query overhead per pair (~64.5x speedup for buffer size lookups, 568ms -> 8.8ms per 100 pairs).
**Action:** Always cache per-device configuration thresholds on manager/service instances when evaluated inside packet interception or capture storage loops.

## 2026-03-31 - In-Memory Bounded Caching for Ingress Protocol Resolution
**Learning:** `DatabaseManager.resolve_device_protocol` opened two async SQL database sessions (`select(DeviceRegistry)` and `select(DeviceMetaRow)`) on every intercepted request, causing ~10ms of SQL query overhead per request (~100 ops/sec). Caching connection mode and device meta in bounded `TTLCache` instances on `DatabaseManager` eliminates per-request database I/O for static device metadata and boosts protocol resolution throughput by ~10,000x (~1.09 million ops/sec).
**Action:** Cache static or rarely-changing per-device configuration and metadata in bounded TTL caches on `DatabaseManager`, with invalidation hooks on update methods.

## 2026-03-31 - Pre-computed Virtual Sensor Parameters in State Manager
**Learning:** `_SensorInstance.read()` repeatedly parsed `self.config.baseline` string formats (`startswith("{")`, string slicing `key[6:]`, `float(raw)` inside `try...except`), fallback drift lists, and trigonometric period steps on every reading call. Pre-parsing baseline keys, static float baselines, drift bounds, and period multipliers during `_SensorInstance.__init__` eliminates string parsing and float conversion overhead on every read (~2.3x to 3.2x speedup per sensor read).
**Action:** Pre-compute static numbers, wave multipliers, and dynamic key lookups on sensor/state instance initialization instead of parsing configuration strings on every evaluation.

## 2026-03-31 - Identity Enum Checks and Local Variable Binding in Traffic Selector Hot Path
**Learning:** `TrafficRule.matches` checked `self.scope == TrafficScope.LOCAL` and `self.match_type == MatchType.CIDR` using `==` equality and accessed `request_info` attributes repeatedly in every match branch. Using identity comparisons (`self.scope is TrafficScope.LOCAL`) on singleton `Enum` instances and binding local variables (`mtype = self.match_type`, `is_local = request_info.is_local`) reduces `LOAD_ATTR` bytecode overhead and speeds up rule matching evaluation by ~1.5x (~0.95s vs ~1.46s per 1,000,000 evaluations).
**Action:** In rule evaluation loops using Enum constants, use `is` identity checks rather than `==` equality, and bind frequently-accessed object attributes to local variables.

## 2026-03-31 - Fast Tuple Iteration for Request Inspection and Lowercased Header Dictionary Normalization
**Learning:** Replacing generator expressions (`any(p in path_lower for p in _FW_PATHS)`) in base adapter request inspection methods with explicit `for` loops eliminates generator allocation overhead (~2.58x speedup). Lowercasing dictionary keys upfront in HTTP header normalization (`_norm_headers`) turns linear string-lowercasing scans (`if not any(k.lower() == "content-type" for k in headers)`) into fast $O(1)$ dictionary key membership checks (`"content-type" not in headers`).
**Action:** Replace `any(...)` generator expressions with explicit `for` loops in request inspection hot paths, and normalize HTTP header keys to lowercase at entry to enable $O(1)$ dictionary lookups.

## 2026-03-31 - Fast O(1) Bounded Storage for Modification Audit Log
**Learning:** `ModificationEngine._audit_log` stored entries in a standard list, performing `self._audit_log = self._audit_log[-10000:]` on every logged modification once full, creating a new 10,000-element list on every call (~365us per append). Replacing the list with a bounded `collections.deque(maxlen=10000)` automatically evicts the oldest entry in $O(1)$ time without copying (~55x speedup per append).
**Action:** Use bounded `collections.deque(maxlen=N)` for fixed-size sliding history logs instead of list slicing `lst = lst[-N:]`.

## 2026-03-31 - Incremental Parsing for Decrypted TLS HTTP Streams
**Learning:** Re-instantiating `h11.Connection` and converting accumulated `bytearray` buffers to `bytes` on every decrypted stream read iteration causes quadratic $O(N^2)$ re-parsing of previously read chunks (~6.89x slower). Maintaining a persistent `h11.Connection` instance per request and feeding incoming chunks incrementally via `conn.receive_data(chunk)` reduces request parsing to $O(N)$ linear time without intermediate buffer heap allocations.
**Action:** When parsing streaming protocol buffers with state machine parsers like `h11`, instantiate the connection parser once per request/connection and feed incoming chunk bytes incrementally as they arrive.

## 2026-03-31 - Async Thread Offloading for Atomic File I/O
**Learning:** Synchronous file operations with disk syncs (`open()`, `write()`, `flush()`, and `os.fsync()`) executed directly on an `asyncio` event loop block event loop execution and stall concurrent background tasks. Providing thread-offloaded async variants (`append_atomic_async` / `append_jsonl_async`) via `asyncio.to_thread` prevents event loop stalls while maintaining crash-safe file write guarantees.
**Action:** Use `asyncio.to_thread` to offload blocking file operations and disk syncs when appending audit logs or state in async request handling contexts.

## 2025-02-23 - Eliminate redundant DB Query in Device Auto-switch Check
**Learning:** In `CloudIndependenceVerifier.auto_switch_to_production`, querying `DeviceRegistry` directly before calling `check_cloud_independence` resulted in a redundant DB query since `check_cloud_independence` already queries `DeviceRegistry` and returns the `auto_switch_enabled` status and missing device state.
**Action:** Always inspect downstream/helper method queries when adding pre-checks to avoid executing identical ORM queries twice in the same code path.
## 2026-03-31 - In-Memory Sequence Number Caching in Buffer Store
**Learning:** `SqlAlchemyBufferStore._next_sequence` ran an async SQL aggregate query `select(func.max(LLMContextBuffer.sequence))` on every single captured request/response pair insertion. Caching sequence numbers in memory per device (`self._sequence_cache[device_id]`) and incrementing directly in memory (with exception invalidation) eliminates SQL query overhead on subsequent pair insertions (~5.2x speedup).
**Action:** When generating sequential integer keys or sequence numbers for database rows inside serialized insertion loops, cache the sequence counter in memory after initial database lookup.

## 2024-05-24 - MatchStats Memory Caching for High-Traffic Database Reductions
**Learning:** Performing per-request SQLite transactions (including `flush`) on a shared `MatchStats` row generates large IO and locking overhead on high-traffic services. Simulating the problem revealed overhead dropped by 40x when avoiding synchronous flushes per item.
**Action:** When tracking high-volume analytics or rate limits in real-time, implement simple batched flushes by buffering updates in an async lockable memory structure (`dict`) and writing them periodically instead of on every increment. Ensure the getter fetches/flushes these in-flight updates so state remains globally consistent without the write penalty.

## 2026-03-31 - Fast In-Memory Field Mapping Resolution in Pattern Matcher
**Learning:** `PatternMatcher.build_local_response` executed `select(FieldMapping)` on every local response generation, opening a database session and fetching all `FieldMapping` ORM rows across all endpoints in the database (~2.43s per 500 calls). Applying `template.field_mappings` directly in-memory via `if template.field_mappings is not None:` bypasses database sessions and ORM object instantiations completely (~820x speedup, ~0.003s per 500 calls).
**Action:** Always check if response templates or cached schemas already carry inline field mappings before executing database SELECT queries across mapping tables.

## 2026-03-31 - Inlined Protocol Matching in Pattern Engine Match Loop
**Learning:** `PatternEngine.find_best_match` instantiated a nested closure function `_protocol_matches` on every incoming request evaluation call and executed function calls with closure scope lookups inside the candidate endpoint loop. Inlining the boolean protocol check (`if protocol and item.protocol and item.protocol != protocol: continue`) eliminates function object creation and call overhead in pattern matching loops (~2.0x speedup for protocol filtering).
**Action:** Avoid defining inner helper functions inside request-matching hot paths when the logic can be inlined as a short-circuiting boolean condition.

## 2026-03-31 - Bulk Pre-fetching and Pre-indexed Response Lookup in DecipherIngest
**Learning:** `DecipherIngest.ingest` executed sequential `select(RequestPattern)` queries inside a `for` loop over LLM output patterns ($N$ queries per batch). Bulk pre-fetching candidate pattern IDs via `.where(RequestPattern.pattern_id.in_(pattern_ids))` reduced SQL queries from $N$ to 1 (~10x speedup). Additionally, `import_patterns` scanned `pattern_db.server.responses` in a nested loop for every endpoint ($O(N \times M)$ list scans). Pre-indexing responses by trigger intent into a dictionary replaces list scanning with $O(1)$ dict lookups (~2.4x speedup).
**Action:** Always bulk pre-fetch candidate database keys using `.in_(...)` filters before iterating over batch items, and pre-index child entities by lookup keys before entering mapping loops.

## 2026-03-31 - Fast-Path Bypass for Response Template Variable Resolution
**Learning:** `PatternEngine.build_local_response` unconditionally executed recursive dict/list traversal (`_resolve_template_vars`) on every generated response body. For response templates containing static values or field-mapped data without template variable placeholders (`{state.`, `{request.`, `{uuid}`), this introduced unnecessary dictionary/list allocations and recursive string type-checking overhead (~1.41ms per 100 calls). Pre-checking and caching whether `template.body_template` contains template variables (`_template_has_vars`) short-circuits recursive traversal when no placeholders are present, speeding up local response building by ~24x (~0.059ms per 100 calls).
**Action:** When performing recursive variable or placeholder substitution on response payloads, pre-check and cache on template instances whether dynamic placeholders exist before triggering payload traversal.

## 2026-03-31 - Fast Exact Key Match and Unrolled Dict Iteration for Payload Redaction
**Learning:** `redact_headers` and `redact_query` called `.lower()` and `isinstance()` on all header and query keys unconditionally before checking `SENSITIVE_HEADERS` / `SENSITIVE_QUERY_PARAMS`. In addition, `redact_body` called `redact_value` on every dict item, introducing function call overhead per field. Performing an exact string set lookup (`if name in SENSITIVE_HEADERS`) first for lowercased keys and inlining dict value iteration with early collection checks (`if not body: return body`) speeds up payload redaction by ~10-15%.
**Action:** In redaction hot paths, perform exact set membership checks on raw string keys before falling back to lowercasing, and inline dict value processing for primitive string and scalar fields.

## 2026-03-31 - Fast Timestamp Generation in Protocol Server Interception Hot Paths
**Learning:** `InterceptedRequest` construction across all protocol servers (`coap_server`, `mqtt_server`, `http2_server`, `websocket_server`, `raw_tcp_server`, `modbus_server`, `zwave_bridge`, `zigbee_bridge`, `matter_bridge`) called `datetime.now(UTC).timestamp()` on every intercepted packet. Instantiating timezone-aware datetime objects before converting to epoch float seconds added significant Python object allocation overhead (~0.93s vs ~0.19s per 1,000,000 calls). Replacing `datetime.now(UTC).timestamp()` with direct C-level `time.time()` calls speeds up timestamp generation in request ingestion hot paths by ~4.8x to 5.8x.
**Action:** Use `time.time()` directly when generating UNIX POSIX timestamp floats for request observation and event metadata instead of creating temporary `datetime.now(UTC)` objects.

## 2026-03-31 - Fast String Character Check for IP Detection Hot Paths
**Learning:** `DatabaseManager._is_ip` and `CertManager._generate_leaf_cert` invoked `ipaddress.ip_address(value)` on arbitrary string identifiers and hostnames (e.g. `"device-12345"`, `"api.example.com"`). Non-IP strings triggered `ValueError` exceptions inside `ipaddress.ip_address`, incurring expensive exception object creation and stack unwinding overhead. Adding a fast short-circuit character presence check `if not value or ("." not in value and ":" not in value): return False` avoids calling `ipaddress.ip_address` for non-IP identifiers (~3.27x speedup).
**Action:** When validating string formats via functions that raise exceptions (like `ipaddress.ip_address`), short-circuit using fast character or delimiter checks (`"." in value or ":" in value`) to bypass exception overhead for standard string identifiers.

## 2026-03-31 - Fast-Path Check for Non-Equals Scalar String Redaction
**Learning:** `_redact_key_value_pairs` in `core/redaction.py` executed `text.split("&")`, item loop iteration, and `"&".join(...)` on every scalar string body, regardless of whether key-value delimiter `=` existed. Adding a fast-path short-circuit `if "=" not in text: return text` eliminates string splitting, list allocation, and string joining on scalar non-key-value string payloads (~4.3x speedup, ~0.11s vs ~0.47s per 1,000,000 checks).
**Action:** Short-circuit string-parsing helpers early with fast `in` character presence checks (e.g. `if "=" not in text: return text`) before allocating lists via `split()` or rejoining strings.


## 2026-03-31 - Batching DB existence queries in resilience pattern imports
**Learning:** Checking for existing records inside a loop generates N+1 queries, significantly slowing down batch operations (e.g. 200 items took ~1.40s for fresh import and ~0.66s for re-import).
**Action:** Use SQLAlchemy `in_` queries to fetch all matching existing record IDs in a single query prior to the loop, reducing database roundtrips from O(N) to O(1) and accelerating execution speed by 5.6x for fresh imports and 59x for duplicate re-imports.

## 2026-03-31 - Memoizing IP Locality Checks with LRU Cache
**Learning:** `is_local_ip` was called on every intercepted request across protocol servers to determine local vs external traffic routing. Even with fast string prefix checks, evaluating repetitive client IP strings incurred string prefix comparisons, splitting, and `ipaddress.ip_address` fallback parsing overhead. Decorating `is_local_ip` with `@functools.lru_cache(maxsize=2048)` memoizes IP locality results and speeds up repeated IP locality checks by ~8.37x (~0.28s vs ~2.37s per 1,400,000 lookups).
**Action:** Apply `@functools.lru_cache` to pure utility functions called per-request on repetitive string identifiers (such as client IP addresses) to eliminate string parsing overhead.

## 2026-03-31 - Fast Byte-Indexing Short-Circuit for JSON Body Parsing in Proxy Entrypoint
**Learning:** `_get_request_body` in `core/server.py` passed raw byte payloads directly to `json.loads(body)` on every intercepted request. For non-JSON or plain text payloads (e.g. form-data, XML, IoT binary/text streams), `json.loads` raised `JSONDecodeError` / `UnicodeDecodeError`, creating exception instances and unrolling stack frames on every non-JSON request (~0.79s for 100k calls). Short-circuiting via zero-allocation byte indexing (`if body[i] not in (123, 91): return None` where 123 is `{` and 91 is `[`) checks the first non-whitespace byte in C without heap allocations, bypassing `json.loads` calls and exception unwinding for non-JSON requests (~21.7x speedup, ~0.036s vs ~0.79s).
**Action:** When parsing JSON payloads in request ingestion hot paths, perform a zero-allocation byte indexing check for leading `{` or `[` before calling `json.loads()` to avoid exception creation overhead for non-JSON payloads.

## 2026-03-31 - Memoized SSRF Guard Check in Upstream Resolver
**Learning:** `is_blocked_ip` in `core/upstream_resolver.py` parsed IP strings via `ipaddress.ip_address` and evaluated 7 IP address properties on every DNS resolution / SSRF check (~67x slower). Decorating `is_blocked_ip` with `@functools.lru_cache(maxsize=2048)` bypasses parsing and object property inspection for active IP addresses (~0.018s vs ~1.237s per 20,000 checks).
**Action:** Memoize pure IP validation / SSRF guard functions that accept string IP literals to eliminate `ipaddress.ip_address` object parsing and property evaluation overhead.

## 2026-03-31 - Zero-Allocation Byte Check for CoAP Message Payload JSON Parsing
**Learning:** In `CoAPServerPlugin.handle_coap_request`, passing raw byte payloads directly to `json.loads` caused `json.JSONDecodeError` / `UnicodeDecodeError` on every non-JSON or binary CoAP packet (e.g. CBOR, Sensor binary data), creating exception instances and unrolling stack frames (~10x slower). Adding a zero-allocation byte check for leading `{` (123) or `[` (91) characters before calling `json.loads()` avoids exception creation for binary/non-JSON CoAP payloads.
**Action:** In protocol server request handlers processing binary or non-JSON payloads, check the leading payload byte for JSON structural characters (`{`, `[`) prior to attempting `json.loads()`.

## 2026-03-31 - Fast Zero-Allocation Leading Character/Byte Check for WebSocket Payload Parsing
**Learning:** In `WebSocketServerPlugin._handle_ws`, passing raw string or bytes frame messages directly to `json.loads` caused `json.JSONDecodeError` / `TypeError` on every non-JSON WebSocket frame (e.g. text commands, binary frames, or ping messages), allocating exception objects and unwinding stack frames (~7.8x slower). Pre-checking leading characters or bytes against valid JSON start tokens (`_JSON_START_CHARS` / `_JSON_START_BYTES`) before calling `json.loads()` eliminates exception overhead for non-JSON frame payloads.
**Action:** In protocol server message handlers processing string/bytes WebSocket frames, perform a fast $O(1)$ leading character/byte set check (`message[0] in _JSON_START_CHARS` or `_JSON_START_BYTES`) before invoking `json.loads()`.
