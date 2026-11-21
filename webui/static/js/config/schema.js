/**
 * Ride the API — Configuration Schema & Metadata Module
 * Section hints, field descriptions, enum options, path suggestions and pattern lookup helpers.
 */

const HINTS = {
  core: 'Core paths and per-IP overrides. What you can do here: point the core database and the per-device database directory somewhere else, size the default capture buffer, and give a single source IP its own database, its own connection handling (auto / tls / http / mqtt / coap / modbus) or a bypass that forwards it untouched to the vendor cloud. Use "Add per-IP override" below to create one in a click.',
  buffer: 'Where the transient capture buffer lives until flushed to the LLM',
  proxy: 'HTTP server bind address and port',
  security: 'Control-plane auth keys and TLS interception settings',
  vendors: 'Per-vendor cloud endpoints and protocol adapters',
  models: 'Local model registry and inference settings',
  control: 'Policy engine and online learning tuning',
  observability: 'Logging, metrics, tracing and health check',
  dns: 'Interception and upstream resolvers. Intercepted names are the ones listed here plus the hostnames every registered protocol adapter declares — one service per adapter, so adding an adapter is enough. `intercept_all` widens that to every name and `exclude` draws the boundary; the dashboard DNS Discovery panel lists what devices actually asked for and promotes a name here in one click.',
  traffic_selection: 'Rules that decide intercept vs passthrough for traffic',
  llm_decipher: 'LLM service profiles used to decode protocols',
  modification: 'On-the-fly request/response transformation rules',
  correlation: 'Request/response matching strategies per protocol',
  learning: 'Default mode, match threshold and auto-switch behaviour',
  tls_decrypt: 'TLS MITM engine — ports, CA and pinning bypass',
  protocol_servers: 'Direct listeners for MQTT, CoAP, Modbus, WS, etc.',
  connection_watchdog: 'Outbound probe that detects the wedged process (every outgoing call fails while local listeners still answer) and restarts the service, or only logs it when the restart command is empty.',
};

// ── Field help (English) ────────────────────────────────────────────────────
// Every field gets a one-line English explanation. It is rendered under the
// field name, exposed as an instant hover tooltip on the name and on the "?"
// button, and mirrored into the native title attribute (keyboard, touch,
// screen readers). Keys are dotted config paths, so they read exactly like the
// path shown under the label; '*' matches one segment (array index, vendor /
// profile / IP / host key) and a trailing '**' matches the rest of the path.
const FIELD_HINTS = {
  // core
  'core.database_url': 'Core database URL (SQLite for development, PostgreSQL for production). Holds devices, learned patterns and the DNS query log.',
  'core.device_db_dir': 'Directory that holds one protocol database per device. It is created on first capture.',
  'core.device_databases': 'Optional per-device database URL overrides. Key = device name, value = SQLAlchemy URL. Leave empty to let each device get its own file.',
  'core.default_context_buffer_size': 'Default capture-buffer size in bytes for new devices. 524288 = 512 KB; common values are 131072, 262144, 524288, 1048576, 2097152, 5242880, 10485760.',
  'core.ip_profiles': 'Per-IP overrides for single devices. Add one key per source IP (for example 192.168.1.50); each key accepts database, connection and bypass.',
  'core.ip_profiles.*.database': 'Database URL used by this IP. Leave empty to give the device its own dedicated database.',
  'core.ip_profiles.*.connection': 'How this IP is handled on ingress: auto detects TLS versus a known protocol; the other values force that handler.',
  'core.ip_profiles.*.bypass': 'When on, this IP is forwarded straight to the vendor cloud with no capture, no LLM analysis and no local match.',

  // buffer
  'buffer.backend': 'Where captured pairs wait before the LLM flush: disk is the durable per-device database, memory is in-process RAM (faster, lost on restart).',

  // proxy
  'proxy.host': 'Interface the proxy listens on. 0.0.0.0 accepts every interface; 127.0.0.1 only local traffic.',
  'proxy.port': 'Port devices and the DNS interception point at (default 8911).',
  'proxy.tls': 'Certificate the proxy presents when it terminates device HTTPS traffic.',
  'proxy.tls.enabled': 'Accept TLS on the proxy port and terminate it with the certificate below.',
  'proxy.tls.cert_file': 'PEM certificate (and chain) used for the proxy TLS endpoint.',
  'proxy.tls.key_file': 'Private key that matches cert_file.',
  'proxy.request_timeout': 'Seconds to wait for an upstream cloud response before the request is abandoned.',
  'proxy.max_request_size': 'Body size cap in bytes for control-plane /api/* requests (1 MB by default).',
  'proxy.max_device_request_size': 'Body size cap in bytes for device (data-plane) traffic read through the proxy. 0 disables the cap.',
  'proxy.fallback': 'What happens when a request cannot be answered locally and has to go to the vendor cloud.',
  'proxy.fallback.enabled': 'Forward unmatched requests to the vendor cloud instead of failing them.',
  'proxy.fallback.timeout': 'Seconds to wait on the cloud fallback before giving up.',
  'proxy.fallback.retry_count': 'How many times a failed cloud fallback is retried.',
  'proxy.fallback.confidence_threshold': 'If the local match confidence is below this value (0 to 1), the request is forwarded to the cloud instead of being served locally.',

  // security
  'security.auth_enabled': 'Require the control-plane password on every /api/* call. Keep it on except for isolated lab setups.',
  'security.password': 'Single control-plane password (8 characters or more), sent as X-API-Key or Bearer token. It grants full read and write access. An empty value with auth on locks the control plane.',

  // vendors
  'vendors.*.enabled': 'Enable this vendor adapter. Disabled adapters are not loaded at startup.',
  'vendors.*.cloud': 'Vendor cloud endpoints used for fallback forwarding and for DNS interception hints.',
  'vendors.*.cloud.api_endpoint': 'Vendor cloud base URL, for example https://openapi.vendor.com.',
  'vendors.*.cloud.mqtt_endpoint': 'Vendor MQTT broker hostname the device normally connects to.',
  'vendors.*.cloud.mqtt_port': 'Vendor MQTT broker port (8883 for TLS, 1883 for plain).',
  'vendors.*.adapter': 'Protocol adapter settings for this vendor.',
  'vendors.*.adapter.class': 'Import path of the adapter class, for example adapters.example.ExampleProtocolAdapter.',
  'vendors.*.adapter.extra': 'Adapter-specific keys (region, api_version, ...). Anything the adapter reads at runtime goes here.',

  // models
  'models.registry_path': 'Directory holding model files, resolved relative to the device database directory.',
  'models.defaults': 'Default model file per vendor and device type. A per-device database entry overrides it.',
  'models.inference': 'ONNX Runtime inference tuning.',
  'models.inference.batch_size': 'How many requests are batched into one inference call. 1 is safest on small hosts.',
  'models.inference.intra_op_threads': 'Threads used inside a single inference operation. Keep it at or below the CPU core count.',
  'models.inference.inter_op_threads': 'Threads used across independent inference operations.',
  'models.inference.execution_providers': 'Execution providers tried in order: CPUExecutionProvider, CUDAExecutionProvider, TensorrtExecutionProvider. Only list what the host really has.',
  'models.hot_reload.enabled': 'Pick up model file changes without restarting the service.',
  'models.hot_reload.check_interval': 'Seconds between model directory scans when hot reload is on.',

  // learning
  'learning.enabled': 'Master switch for the learning pipeline: capture pairs and let the LLM build patterns.',
  'learning.default_mode': 'Mode given to new devices: learning (log and forward everything), production (serve locally only), hybrid (local match first, cloud on a miss).',
  'learning.default_match_threshold': 'Confidence from 0 to 1 that a local match needs to answer in production mode. Below it the request goes to the cloud.',
  'learning.auto_switch_to_production': 'Switch a device to production automatically once it has enough patterns and a high match rate.',
  'learning.min_patterns_for_production': 'Patterns a device needs before the automatic switch to production is allowed.',
  'learning.min_match_rate_for_production': 'Match rate percentage (for example 99.0) required before the automatic switch to production.',
  'learning.production_no_fallback': 'In production mode answer only from the local database: unmatched requests fail instead of reaching the cloud.',
  'learning.signal_forward_to_cloud': 'Return X-Action: forward for unmatched requests so a reverse proxy (nginx) sends them to the real cloud, avoiding a DNS loop.',

  // control
  'control.policy': 'Built-in control policy used to answer set-point and thermal requests.',
  'control.policy.evaluation_interval': 'Seconds between policy evaluations.',
  'control.policy.default_policy': 'Policy to apply: pid_thermal (thermal PID loop) or rule_based (fixed rules).',
  'control.online_learning.enabled': 'Refine the control model while the proxy runs.',
  'control.online_learning.buffer_size': 'Samples kept for online learning.',
  'control.online_learning.update_interval': 'Seconds between online-learning updates.',
  'control.online_learning.min_samples_for_update': 'Samples needed before an online-learning update is applied.',

  // traffic selection
  'traffic_selection.default_action': 'What happens to traffic that matches no rule: intercept (analyse it) or passthrough (relay it untouched).',
  'traffic_selection.rules': 'Rules evaluated by priority, highest first. Each rule matches on cidr, hostname, vendor or device_id and then intercepts or passes through.',
  'traffic_selection.rules.*.name': 'Label for the rule. Only used in the UI and the logs.',
  'traffic_selection.rules.*.scope': 'local matches addresses and names on your network, external matches public cloud destinations.',
  'traffic_selection.rules.*.match_type': 'What match_value is compared against: cidr, hostname, vendor or device_id.',
  'traffic_selection.rules.*.match_value': 'The value to match, for example 192.168.1.0/24 for a cidr rule or *.vendor.com for a hostname rule.',
  'traffic_selection.rules.*.action': 'intercept analyses the traffic, passthrough relays it to the cloud untouched.',
  'traffic_selection.rules.*.priority': 'Higher numbers are evaluated first.',
  'traffic_selection.rules.*.enabled': 'Turn the rule on or off without deleting it.',

  // llm decipher
  'llm_decipher.enabled': 'Use an LLM to decode captured protocols and produce patterns.',
  'llm_decipher.default_profile': 'Profile used when no per-task profile is bound.',
  'llm_decipher.profiles': 'One entry per LLM backend. Any OpenAI-compatible endpoint works (cloud or local Ollama).',
  'llm_decipher.profiles.*.base_url': 'OpenAI-compatible base URL, for example https://api.openai.com/v1 or http://localhost:11434/v1 for Ollama.',
  'llm_decipher.profiles.*.api_key': 'API key for that endpoint. Ollama accepts any placeholder such as ollama.',
  'llm_decipher.profiles.*.model_id': 'Model name as the endpoint knows it. For Ollama it must match an installed model (check with ollama list).',
  'llm_decipher.profiles.*.max_tokens': 'Ceiling on a single completion. Keep it: a local model that ignores the JSON format otherwise generates until the timeout.',
  'llm_decipher.profiles.*.prompt_template': 'Prompt used for the analysis. Placeholders: {device_id}, {vendor}, {device_type}, {pairs}.',
  'llm_decipher.task_profiles': 'Binds a purpose (buffer flush, correlation validation, ...) to a profile. Missing entries fall back to default_profile.',

  // protocol servers
  'protocol_servers': 'Direct listeners for IoT protocols. Enable one only if devices talk to this host on that protocol port.',
  'protocol_servers.*.enabled': 'Start this listener at boot.',
  'protocol_servers.*.host': 'Bind address for this listener. It must be a local address or 0.0.0.0, otherwise the bind fails with cannot assign requested address.',
  'protocol_servers.*.port': 'TCP or UDP port this listener binds.',
  'protocol_servers.mqtt.port_tls': 'TLS port for the MQTT broker (8883 by default).',
  'protocol_servers.mqtt.tls_enabled': 'Serve MQTT over TLS as well as plain MQTT.',
  'protocol_servers.mqtt.max_packet_size': 'Largest MQTT packet accepted, in bytes.',
  'protocol_servers.mqtt.topic_filters': 'Subscriptions the broker records. "#" captures every topic.',
  'protocol_servers.coap.port': 'CoAP UDP port (5683 by default).',
  'protocol_servers.coap.dtls_enabled': 'Accept CoAP over DTLS as well.',
  'protocol_servers.coap.dtls_port': 'DTLS port for CoAP (5684 by default).',
  'protocol_servers.coap.max_pdu_size': 'Largest CoAP PDU accepted, in bytes.',
  'protocol_servers.modbus.unit_id': 'Modbus unit identifier this server answers for.',
  'protocol_servers.modbus.tls_enabled': 'Accept Modbus over TLS as well.',
  'protocol_servers.modbus.tls_port': 'TLS port for Modbus (802 by default).',
  'protocol_servers.websocket.path': 'HTTP path the WebSocket listener answers on.',
  'protocol_servers.websocket.max_message_size': 'Largest WebSocket message accepted, in bytes.',
  'protocol_servers.raw_tcp.buffer_size': 'Read buffer per connection, in bytes.',
  'protocol_servers.raw_tcp.idle_timeout': 'Seconds a raw TCP connection may stay idle before it is closed.',
  'protocol_servers.raw_tcp.protocol_detect': 'Guess the protocol from the first bytes instead of treating the stream as opaque.',
  'protocol_servers.http2.cleartext_port': 'Cleartext HTTP/2 port (8080 by default). Port 443 stays with the TLS MITM listener.',
  'protocol_servers.http2.tls_enabled': 'Serve HTTP/2 over TLS.',
  'protocol_servers.zigbee_bridge.mqtt_host': 'Host of the Zigbee2MQTT broker to subscribe to.',
  'protocol_servers.zigbee_bridge.mqtt_port': 'Port of the Zigbee2MQTT broker.',
  'protocol_servers.zigbee_bridge.topic_prefix': 'Base topic published by Zigbee2MQTT (usually zigbee2mqtt).',
  'protocol_servers.zigbee_bridge.reconnect_interval': 'Seconds between reconnection attempts.',
  'protocol_servers.zwave_bridge.connection_type': 'Talk to Z-Wave JS UI over mqtt or over its ws port.',
  'protocol_servers.zwave_bridge.host': 'Host of the Z-Wave JS UI instance.',
  'protocol_servers.zwave_bridge.port': 'MQTT port used when connection_type is mqtt.',
  'protocol_servers.zwave_bridge.ws_port': 'WebSocket port used when connection_type is ws.',
  'protocol_servers.matter_bridge.controller_port': 'Port of the Matter controller.',
  'protocol_servers.matter_bridge.fabric_id': 'Matter fabric identifier this bridge joins.',
  'protocol_servers.matter_bridge.vendor_id': 'Vendor identifier announced to Matter controllers (65521 is the test vendor).',

  // connection watchdog
  'connection_watchdog.enabled': 'Detect the wedged-process failure where every outgoing call fails while local listeners still work, then restart or just log it.',
  'connection_watchdog.interval': 'Seconds between outbound probes.',
  'connection_watchdog.timeout': 'Seconds to wait for one probe.',
  'connection_watchdog.max_failures': 'Consecutive transport failures before the watchdog acts.',
  'connection_watchdog.probe_url': 'URL probed every interval. Point it at an always-reachable endpoint; a LAN address keeps the check local.',
  'connection_watchdog.restart_command': 'Command run to heal the service. Leave the list empty to only log the wedge.',
  'connection_watchdog.cooldown': 'Seconds to wait after a restart before another one is allowed.',

  // tls decrypt
  'tls_decrypt.enabled': 'Turn on the HTTPS and MQTT-TLS man-in-the-middle listener. Enable it only after DNS sends device traffic here.',
  'tls_decrypt.listen_ports': 'TLS ports to terminate and decrypt, for example 443 and 8443. Device identity comes from the source IP, not from the port.',
  'tls_decrypt.ca_cert_path': 'CA certificate used to sign per-device leaf certificates. It is generated on first run if missing.',
  'tls_decrypt.ca_key_path': 'Private key of that CA. It must stay on this host.',
  'tls_decrypt.device_certs_dir': 'Where per-hostname leaf certificates are cached.',
  'tls_decrypt.pinning_bypass': 'Per-vendor note on how a pinned device is handled. This is metadata only: no bypass is applied automatically.',
  'tls_decrypt.pinning_bypass.*.strategy': 'mitm_proxy (the device must trust this CA), frida (run the script on the device) or disable_pin_check (change validation on the device).',
  'tls_decrypt.min_tls_version': 'Oldest TLS version accepted by the decryptor.',
  'tls_decrypt.max_tls_version': 'Newest TLS version accepted by the decryptor.',

  // modification
  'modification.enabled': 'Master switch for rewriting requests and responses on the fly.',
  'modification.rules': 'Rules that match traffic and then modify, block, inject, replace, redirect or delay it.',
  'modification.rules.*.name': 'Label for the rule, used in the UI and the logs.',
  'modification.rules.*.match_type': 'What the rule compares: hostname, path, header, body or method.',
  'modification.rules.*.match_value': 'Value the match_type is compared against.',
  'modification.rules.*.action': 'What to do on a match: modify a field, block the message, inject a field, replace the body, redirect or delay.',
  'modification.rules.*.direction': 'Apply the rule to the request, the response, or both.',
  'modification.rules.*.target_field': 'Field or header the action writes to.',
  'modification.rules.*.target_value': 'Value written into target_field.',

  // correlation
  'correlation.enabled': 'Match each response to the request that caused it, so pairs can be stored and learned.',
  'correlation.http.method': 'How HTTP pairs are matched: connection, correlation_id (header) or sequence.',
  'correlation.http.correlation_header': 'Header carrying the correlation id when the method is correlation_id.',
  'correlation.http.keep_alive_timeout': 'Seconds a kept-alive HTTP connection stays available for matching.',
  'correlation.mqtt.method': 'How MQTT pairs are matched: topic_sequence or packet_id (correlation data).',
  'correlation.mqtt.qos_tracking': 'Track QoS levels so retransmissions are not stored as new pairs.',
  'correlation.mqtt.retain_handling': 'Whether retained messages are included in matching: include or skip.',
  'correlation.coap.method': 'How CoAP pairs are matched: message_id or token.',
  'correlation.coap.confirmable_timeout': 'Seconds to wait for a CON acknowledgement.',
  'correlation.store_pairs': 'Persist matched request/response pairs for learning.',
  'correlation.max_pairs_per_device': 'Upper bound on stored pairs for one device; oldest pairs are dropped first.',
  'correlation.pair_ttl_hours': 'Age in hours after which stored pairs are deleted (168 = one week).',

  // observability
  'observability.logging.level': 'Minimum level written to the log: DEBUG, INFO, WARNING, ERROR or CRITICAL.',
  'observability.logging.format': 'json for machine parsing, console for human reading.',
  'observability.logging.output': 'stdout, stderr, or a file path such as ./logs/ride-the-api.log.',
  'observability.metrics.enabled': 'Expose Prometheus metrics.',
  'observability.metrics.port': 'Port serving the metrics endpoint.',
  'observability.metrics.path': 'HTTP path of the metrics endpoint.',
  'observability.tracing.enabled': 'Emit OpenTelemetry spans for the request path.',
  'observability.tracing.exporter': 'console, or otlp when the optional opentelemetry-exporter-otlp package is installed.',
  'observability.tracing.otlp_endpoint': 'OTLP collector address, for example http://localhost:4317.',
  'observability.tracing.sample_rate': 'Fraction of requests traced, from 0 to 1 (0.1 = one in ten).',
  'observability.health_check.enabled': 'Serve the health endpoint used by supervisors and containers.',
  'observability.health_check.port': 'Port serving the health endpoint.',
  'observability.health_check.path': 'HTTP path of the health endpoint.',

  // dns
  'dns.enabled': 'Run the built-in DNS server that redirects intercepted device names to this proxy. Port 53 needs CAP_NET_BIND_SERVICE.',
  'dns.listen_host': 'Address the DNS server binds. 0.0.0.0 answers the whole network.',
  'dns.listen_port': 'DNS port, normally 53.',
  'dns.answer_ip': 'Address handed out for intercepted names. Empty means auto-detect the LAN address of this host.',
  'dns.answer_ttl': 'TTL in seconds on the answers, so devices re-resolve quickly after a change.',
  'dns.forward_timeout': 'Seconds to wait for an upstream resolver before the query fails.',
  'dns.intercept': 'Names answered with answer_ip. Exact names or *.domain wildcards. Never list time servers (ntp.*): devices need their real clock.',
  'dns.intercept_all': 'Answer every name with this proxy address, so every device using it as resolver is captured. Off by default: switch on deliberately.',
  'dns.exclude': 'Names always forwarded, whatever intercept and intercept_all say (exclude wins). Keep the clock, revocation and .local names here.',
  'dns.dnsfilter_custom_dns': 'Reference block of custom DNS entries for an external DNS filter. Documentation only.',
  'dns.adguard_rewrites': 'Reference block of DNS rewrites for an external DNS filter. Documentation only.',
  'dns.dns_servers': 'IPv4 upstream resolvers tried in order for names that are not intercepted.',
  'dns.dns_servers_v6': 'IPv6 upstream resolvers tried in order.',
};

// Fallback help for a leaf field whose exact path is not listed above: the
// last path segment decides, so a new or custom key still explains itself.
const FIELD_HINTS_BY_NAME = {
  enabled: 'Turn this feature on or off.',
  host: 'Address this component binds or connects to.',
  port: 'Network port used by this component.',
  path: 'Path, either an HTTP route or a filesystem location, depending on the field.',
  url: 'Endpoint URL, scheme included.',
  name: 'Label used in the UI and the logs.',
  model_id: 'Model name as the endpoint knows it.',
  api_key: 'Credential for this endpoint. Stored in config.yaml; the API returns it masked.',
  password: 'Credential stored in config.yaml. The API returns it masked.',
  timeout: 'Seconds to wait before the operation is abandoned.',
  interval: 'Seconds between two runs of this task.',
  priority: 'Higher numbers are evaluated first.',
  threshold: 'Value a confidence or score must reach before the action is taken.',
  max_tokens: 'Ceiling on the size of a single completion.',
  prompt_template: 'Prompt sent to the LLM. Placeholders in braces are filled at runtime.',
  database: 'Database URL or name (SQLAlchemy URL).',
  connection: 'How this connection is handled on ingress.',
  bypass: 'When on, traffic from this source is forwarded without any analysis.',
  action: 'What to do when this rule matches.',
  strategy: 'Handling strategy for this vendor.',
  tls_enabled: 'Also accept TLS on this listener.',
  provider: 'Backend or provider identifier.',
};

const GROUP_HINTS = {
  core: 'Core service paths and defaults.',
  proxy: 'Proxy listeners and limits.',
  security: 'Control-plane authentication.',
  vendors: 'One entry per vendor: cloud endpoints plus its adapter settings.',
  models: 'Model registry and inference tuning.',
  learning: 'Capture, pattern learning and the switch to production mode.',
  control: 'Control policy and online learning.',
  traffic_selection: 'Intercept versus passthrough rules.',
  llm_decipher: 'LLM backends used to decode protocols.',
  modification: 'On-the-fly request and response rewriting.',
  correlation: 'How request and response pairs are matched.',
  observability: 'Logging, metrics, tracing and health check.',
  dns: 'DNS interception and upstream resolvers.',
  tls_decrypt: 'TLS man-in-the-middle engine.',
  protocol_servers: 'One entry per protocol listener.',
  connection_watchdog: 'Auto-heal for a wedged process.',
};

// Advisory values: rendered as a datalist so a value can be picked in one
// click while a free value stays possible. Unlike ENUM_FIELDS these are not
// enforced, so a valid value the list does not know is still accepted.
const FIELD_SUGGESTIONS = {
  'observability.logging.output': ['stdout', 'stderr', './logs/ride-the-api.log'],
  'observability.tracing.exporter': ['console', 'otlp'],
  'observability.logging.level': ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
  'correlation.http.method': ['connection', 'correlation_id', 'sequence'],
  'correlation.mqtt.method': ['topic_sequence', 'packet_id'],
  'correlation.mqtt.retain_handling': ['include', 'skip'],
  'correlation.coap.method': ['message_id', 'token'],
  'proxy.host': ['0.0.0.0', '127.0.0.1'],
  'dns.listen_host': ['0.0.0.0', '127.0.0.1'],
  'dns.answer_ttl': [],
  'models.inference.execution_providers': ['CPUExecutionProvider', 'CUDAExecutionProvider', 'TensorrtExecutionProvider'],
  'core.default_context_buffer_size': ['131072', '262144', '524288', '1048576', '2097152', '5242880', '10485760'],
  'tls_decrypt.min_tls_version': ['TLSv1.0', 'TLSv1.1', 'TLSv1.2', 'TLSv1.3'],
  'tls_decrypt.max_tls_version': ['TLSv1.0', 'TLSv1.1', 'TLSv1.2', 'TLSv1.3'],
  'vendors.*.cloud.mqtt_port': ['8883', '1883'],
};

// Starting value when a new key is added to a dict. Dicts whose values are
// objects get that shape, so the new entry is immediately editable instead of
// arriving as an empty string.
const DICT_KEY_DEFAULTS = {
  'core.ip_profiles': () => ({ connection: 'auto', bypass: false }),
  'vendors': () => ({ enabled: true, cloud: { api_endpoint: '', mqtt_endpoint: '', mqtt_port: 8883 }, adapter: { 'class': '', extra: {} } }),
  'models.defaults': () => ({}),
  'llm_decipher.profiles': () => ({ base_url: 'https://api.openai.com/v1', api_key: '', model_id: '', max_tokens: 2048, prompt_template: '' }),
  'tls_decrypt.pinning_bypass': () => ({ strategy: 'mitm_proxy' }),
  'protocol_servers': () => ({ enabled: false }),
};

function defaultForKey(path, key) {
  const factory = DICT_KEY_DEFAULTS[path];
  return factory ? factory(key) : '';
}

// ── Field help helpers ──────────────────────────────────────────────────────
// Match a dotted path against a pattern map: '*' consumes exactly one segment,
// a trailing '**' consumes every remaining segment.
function lookupPathPattern(map, path) {
  const seg = String(path || '').split('.');
  for (const [pattern, value] of Object.entries(map)) {
    if (!value) continue;
    const pat = pattern.split('.');
    const tail = pat[pat.length - 1] === '**';
    if (tail) pat.pop();
    if (tail ? seg.length < pat.length : pat.length !== seg.length) continue;
    let match = true;
    for (let i = 0; i < pat.length; i++) {
      if (pat[i] !== '*' && pat[i] !== seg[i]) { match = false; break; }
    }
    if (match) return value;
  }
  return null;
}

// English explanation for one field: exact path, then the last-segment
// fallback, then the enclosing group.
function hintFor(path) {
  const exact = lookupPathPattern(FIELD_HINTS, path);
  if (exact) return exact;
  const seg = String(path || '').split('.');
  const byName = FIELD_HINTS_BY_NAME[seg[seg.length - 1]];
  if (byName) return byName;
  for (let i = seg.length - 1; i > 0; i--) {
    const group = GROUP_HINTS[seg[i - 1]];
    if (group) return group;
  }
  return '';
}

function suggestionsFor(path) {
  const list = lookupPathPattern(FIELD_SUGGESTIONS, path);
  return Array.isArray(list) && list.length ? list : null;
}

// Known enum fields → rendered as dropdowns instead of free text.
// Paths may contain '*' to match any single segment (e.g. array indices).
const ENUM_FIELDS = {
  'learning.default_mode': ['learning', 'production', 'hybrid'],
  'buffer.backend': ['disk', 'memory'],
  'observability.logging.format': ['json', 'console'],
  'observability.logging.level': ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
  'tls_decrypt.min_tls_version': ['TLSv1.0', 'TLSv1.1', 'TLSv1.2', 'TLSv1.3'],
  'tls_decrypt.max_tls_version': ['TLSv1.0', 'TLSv1.1', 'TLSv1.2', 'TLSv1.3'],
  'control.policy.default_policy': ['pid_thermal', 'rule_based'],
  'core.ip_profiles.*.connection': ['auto', 'tls', 'http', 'mqtt', 'coap', 'modbus'],
  'modification.rules.*.action': ['modify', 'block', 'inject', 'replace', 'redirect', 'delay'],
  'modification.rules.*.direction': ['request', 'response', 'both'],
  'modification.rules.*.match_type': ['hostname', 'path', 'header', 'body', 'method'],
  'traffic_selection.default_action': ['intercept', 'passthrough'],
  'traffic_selection.rules.*.action': ['intercept', 'passthrough'],
  'traffic_selection.rules.*.match_type': ['cidr', 'hostname', 'vendor', 'device_id'],
  'traffic_selection.rules.*.scope': ['local', 'external'],
  'tls_decrypt.pinning_bypass.*.strategy': ['mitm_proxy', 'frida', 'disable_pin_check'],
  'protocol_servers.zwave_bridge.connection_type': ['mqtt', 'ws'],
};

function enumOptionsFor(path) {
  for (const [pattern, options] of Object.entries(ENUM_FIELDS)) {
    const pat = pattern.split('.');
    const seg = path.split('.');
    if (pat.length !== seg.length) continue;
    let match = true;
    for (let i = 0; i < pat.length; i++) {
      if (pat[i] !== '*' && pat[i] !== seg[i]) { match = false; break; }
    }
    if (match) return options;
  }
  return null;
}

// ── Sidebar Metadata ────────────────────────────────────────────────────────
// Ordered, human-readable section metadata: friendly label + one-line hint.
const SECTION_ORDER = [
  'core', 'buffer', 'proxy', 'security', 'vendors', 'models', 'control',
  'observability', 'dns', 'traffic_selection', 'llm_decipher', 'modification',
  'correlation', 'learning', 'tls_decrypt', 'protocol_servers',
];

const SECTION_LABELS = {
  core: 'Core', buffer: 'Buffer', proxy: 'Proxy', security: 'Security',
  vendors: 'Vendors', models: 'Models', control: 'Control',
  observability: 'Observability', dns: 'DNS', traffic_selection: 'Traffic Selection',
  llm_decipher: 'LLM Decipher', modification: 'Modification', correlation: 'Correlation',
  learning: 'Learning', tls_decrypt: 'TLS Decrypt', protocol_servers: 'Protocol Servers',
};

const SECTION_DETAILS = {
  core: 'DB paths, per-IP overrides',
  buffer: 'Transient capture buffer',
  proxy: 'HTTP bind & port',
  security: 'Control-plane auth',
  vendors: 'Cloud endpoints & adapters',
  models: 'Local model registry',
  control: 'Policy & online learning',
  observability: 'Logs, metrics, tracing',
  dns: 'DNS interception',
  traffic_selection: 'Intercept vs passthrough',
  llm_decipher: 'LLM service profiles',
  modification: 'Request/response rules',
  correlation: 'Request/response matching',
  learning: 'Mode & match threshold',
  tls_decrypt: 'TLS MITM engine',
  protocol_servers: 'MQTT, CoAP, Modbus, WS…',
  connection_watchdog: 'Auto-heal probe and restart',
};

const SECTION_ICONS = {
  core: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
  buffer: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
  proxy: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>',
  security: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
  vendors: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>',
  models: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>',
  control: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
  observability: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
  dns: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>',
  traffic_selection: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/></svg>',
  llm_decipher: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="16" y1="16" x2="16.01" y2="16"/></svg>',
  modification: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>',
  correlation: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  learning: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
  tls_decrypt: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>',
  protocol_servers: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><path d="M4 11a9 9 0 0 1 9 9"/><path d="M4 4a16 16 0 0 1 16 16"/><circle cx="5" cy="19" r="1"/></svg>',
  connection_watchdog: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>',
  __raw__: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
  _default: '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>',
};

// Global exports
window.HINTS = HINTS;
window.FIELD_HINTS = FIELD_HINTS;
window.FIELD_HINTS_BY_NAME = FIELD_HINTS_BY_NAME;
window.GROUP_HINTS = GROUP_HINTS;
window.FIELD_SUGGESTIONS = FIELD_SUGGESTIONS;
window.DICT_KEY_DEFAULTS = DICT_KEY_DEFAULTS;
window.ENUM_FIELDS = ENUM_FIELDS;
window.SECTION_ORDER = SECTION_ORDER;
window.SECTION_LABELS = SECTION_LABELS;
window.SECTION_DETAILS = SECTION_DETAILS;
window.SECTION_ICONS = SECTION_ICONS;
window.lookupPathPattern = lookupPathPattern;
window.hintFor = hintFor;
window.suggestionsFor = suggestionsFor;
window.defaultForKey = defaultForKey;
window.enumOptionsFor = enumOptionsFor;
