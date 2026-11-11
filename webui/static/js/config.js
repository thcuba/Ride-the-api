/**
 * Ride the API — Configuration Editor Logic
 * Handles schema parsing, dynamic form generation, validation, hot reload, and YAML persistence.
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

// Instant hover tooltip, one element shared by every explained control.
// The same text is also set as the native title, so keyboard, touch and screen
// readers get it without hover.
function ensureTooltip() {
  let tip = document.getElementById('hoverTip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'hoverTip';
    tip.className = 'hover-tip';
    tip.setAttribute('role', 'tooltip');
    document.body.appendChild(tip);
  }
  return tip;
}

function placeTip(tip, x, y) {
  const pad = 12;
  const rect = tip.getBoundingClientRect();
  let left = x + 14;
  let top = y + 16;
  if (left + rect.width + pad > window.innerWidth) left = Math.max(pad, x - rect.width - 14);
  if (top + rect.height + pad > window.innerHeight) top = Math.max(pad, y - rect.height - 14);
  tip.style.left = left + 'px';
  tip.style.top = top + 'px';
}

function attachTip(el, text) {
  if (!el || !text) return el;
  el.setAttribute('title', text);
  el.addEventListener('mouseenter', (ev) => {
    const tip = ensureTooltip();
    tip.textContent = text;
    tip.classList.add('show');
    placeTip(tip, ev.clientX, ev.clientY);
  });
  el.addEventListener('mousemove', (ev) => {
    const tip = document.getElementById('hoverTip');
    if (tip && tip.classList.contains('show')) placeTip(tip, ev.clientX, ev.clientY);
  });
  const hide = () => {
    const tip = document.getElementById('hoverTip');
    if (tip) tip.classList.remove('show');
  };
  el.addEventListener('mouseleave', hide);
  el.addEventListener('blur', hide);
  return el;
}

function hideTooltip() {
  const tip = document.getElementById('hoverTip');
  if (tip) tip.classList.remove('show');
}

// Quick-pick list for a field: a datalist attached to a text input, so a
// suggested value is one click away while any other value stays possible.
function buildDatalist(path) {
  const options = suggestionsFor(path);
  if (!options) return null;
  const listId = 'dl-' + (fieldCounter++);
  const dl = document.createElement('datalist');
  dl.id = listId;
  options.forEach((value) => {
    const opt = document.createElement('option');
    opt.value = value;
    dl.appendChild(opt);
  });
  dl.dataset.for = path;
  return { listId, dl };
}

// One-click copy of a config path, for pasting into config.yaml or a grep.
function copyFieldPath(path, btn) {
  const done = () => {
    const label = btn.textContent;
    btn.textContent = 'copied';
    setTimeout(() => { btn.textContent = label; }, 1200);
    showToast('Path copied: ' + path, 'success');
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(path).then(done).catch(() => showToast('Copy failed', 'error'));
  } else {
    showToast('Clipboard not available', 'error');
  }
}

let cfg = null;            // working copy
const dirty = new Set();   // touched section paths

function showToast(msg, type) {
  const c = document.getElementById('toasts');
  const t = document.createElement('div');
  t.className = 'toast ' + (type || 'info') + ' show';
  const icons = { success: '✓', error: '✕', info: 'ℹ' };
  t.innerHTML = `<span class="toast-icon">${icons[type] || 'ℹ'}</span><span></span>`;
  t.querySelector('span:last-child').textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 3500);
}

// ── Sidebar ─────────────────────────────────────────────────────────────────
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

function clearSidebarSearch() {
  const input = document.getElementById('sidebarSearch');
  if (input) {
    input.value = '';
    input.focus();
    filterSidebar('');
  }
}
window.clearSidebarSearch = clearSidebarSearch;

function filterSidebar(query) {
  const q = (query || '').trim().toLowerCase();
  const clearBtn = document.getElementById('clearSidebarSearchBtn');
  if (clearBtn) clearBtn.style.display = q ? 'block' : 'none';
  const allBtns = document.querySelectorAll('#sidebar .sidebar-items .section-item');
  let matches = 0;
  allBtns.forEach(btn => {
    const sec = btn.dataset.section || '';
    const label = SECTION_LABELS[sec] || sec;
    const detail = SECTION_DETAILS[sec] || '';
    const isMatch = !q || sec.toLowerCase().includes(q) || label.toLowerCase().includes(q) || detail.toLowerCase().includes(q);
    btn.style.display = isMatch ? '' : 'none';
    if (isMatch) matches++;
  });
  const emptyEl = document.getElementById('sidebarEmpty');
  if (emptyEl) {
    emptyEl.style.display = (matches === 0 && q) ? 'block' : 'none';
  }
  const statusEl = document.getElementById('sidebarFilterStatus');
  if (statusEl && allBtns.length > 0) {
    statusEl.textContent = q ? `Showing ${matches} of ${allBtns.length} sections` : '';
  }
}

function buildSidebar() {
  const sidebar = document.getElementById('sidebar');
  sidebar.innerHTML = '';

  const header = document.createElement('div');
  header.className = 'sidebar-header';
  const label = document.createElement('span');
  label.className = 'sidebar-label';
  label.textContent = 'Sections';
  const badge = document.createElement('span');
  badge.className = 'sidebar-badge';
  const keys = SECTION_ORDER.filter(k => k in cfg)
    .concat(Object.keys(cfg).filter(k => !SECTION_ORDER.includes(k)));
  badge.textContent = String(keys.length);
  badge.title = `${keys.length} configuration sections`;
  header.appendChild(label);
  header.appendChild(badge);
  sidebar.appendChild(header);

  // Search filter
  const searchWrap = document.createElement('div');
  searchWrap.className = 'sidebar-search-wrap';
  searchWrap.innerHTML = `
    <svg class="sidebar-search-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    <input type="search" class="sidebar-search" id="sidebarSearch" placeholder="Filter sections…" aria-label="Filter configuration sections" />
    <kbd class="search-kbd" aria-hidden="true">/</kbd>
    <button type="button" class="clear-search-btn" id="clearSidebarSearchBtn" aria-label="Clear configuration search filter" title="Clear configuration search filter">&times;</button>
    <span class="sr-only" id="sidebarFilterStatus" aria-live="polite"></span>
  `;
  const searchInput = searchWrap.querySelector('input');
  searchInput.addEventListener('input', (e) => filterSidebar(e.target.value));
  searchInput.addEventListener('keydown', (e) => { if (e.key === 'Escape') { clearSidebarSearch(); e.stopPropagation(); } });
  searchWrap.querySelector('#clearSidebarSearchBtn').addEventListener('click', clearSidebarSearch);
  sidebar.appendChild(searchWrap);

  // Scrollable sections list
  const itemsContainer = document.createElement('div');
  itemsContainer.className = 'sidebar-items';
  itemsContainer.setAttribute('role', 'tablist');
  itemsContainer.setAttribute('aria-orientation', 'vertical');

  keys.forEach(key => {
    const btn = document.createElement('button');
    btn.className = 'section-item';
    btn.dataset.section = key;
    btn.setAttribute('role', 'tab');
    btn.title = HINTS[key] || SECTION_DETAILS[key] || 'Configuration section';
    attachTip(btn, btn.title);
    const iconSvg = SECTION_ICONS[key] || SECTION_ICONS._default;
    btn.innerHTML = `
      <div class="section-item-main">
        <span class="section-item-icon" aria-hidden="true">${iconSvg}</span>
        <span class="section-item-text">
          <span class="section-item-name">${escapeHtml(SECTION_LABELS[key] || key)}</span>
          <span class="section-item-key">${escapeHtml(key)}</span>
        </span>
      </div>
      <span class="count"></span>`;
    btn.addEventListener('click', () => selectSection(key));
    itemsContainer.appendChild(btn);
  });

  const emptyMsg = document.createElement('div');
  emptyMsg.id = 'sidebarEmpty';
  emptyMsg.className = 'sidebar-empty';
  emptyMsg.textContent = 'No matching sections';
  emptyMsg.style.display = 'none';
  itemsContainer.appendChild(emptyMsg);

  sidebar.appendChild(itemsContainer);

  // Advanced / Developer Tools
  const divider = document.createElement('div');
  divider.className = 'sidebar-divider';
  sidebar.appendChild(divider);

  const footerLabel = document.createElement('div');
  footerLabel.className = 'sidebar-label sidebar-footer-label';
  footerLabel.textContent = 'Advanced';
  sidebar.appendChild(footerLabel);

  const raw = document.createElement('button');
  raw.className = 'section-item';
  raw.dataset.section = '__raw__';
  raw.setAttribute('role', 'tab');
  raw.title = 'Full configuration as raw JSON';
  raw.innerHTML = `
    <div class="section-item-main">
      <span class="section-item-icon" aria-hidden="true">${SECTION_ICONS.__raw__}</span>
      <span class="section-item-text">
        <span class="section-item-name">Raw</span>
        <span class="section-item-key">JSON / YAML</span>
      </span>
    </div>
    <span class="count"></span>`;
  raw.addEventListener('click', () => selectSection('__raw__'));
  sidebar.appendChild(raw);
}

// ── LLM Decipher section (editable; persistence via the LLM settings API) ───
// The LLM page and this section are two entry points onto the SAME data: the
// fields are populated from the effective (runtime) settings and saved through
// /api/llm/settings (see saveConfig), never through PUT /api/config. The
// working copy lives in cfg.llm_decipher so the generic renderer's edits land
// there. No refetch on re-render, or structural edits would discard unsaved
// changes.
let llmSectionLoaded = false;
async function renderLLMSection(container) {
  const banner = document.createElement('div');
  banner.className = 'field-group llm-banner-card';
  banner.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:16px;padding:14px 18px;background:color-mix(in srgb, var(--surface2) 60%, var(--surface));border:1px solid var(--border);border-radius:10px;';

  const bannerText = document.createElement('div');
  bannerText.style.cssText = 'flex:1;min-width:240px;';
  const bannerTitle = document.createElement('div');
  bannerTitle.style.cssText = 'font-weight:700;font-size:14px;color:var(--text);margin-bottom:4px;display:flex;align-items:center;gap:8px;';
  bannerTitle.innerHTML = '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="16" y1="16" x2="16.01" y2="16"/></svg><span>Dedicated LLM Service Manager</span>';
  const bannerDesc = document.createElement('div');
  bannerDesc.className = 'sec-desc';
  bannerDesc.style.cssText = 'margin-bottom:0;font-size:12.5px;line-height:1.45;';
  bannerDesc.textContent = 'Profiles configured here are applied through the LLM settings API — the same service store the LLM page uses.';
  bannerText.appendChild(bannerTitle);
  bannerText.appendChild(bannerDesc);
  banner.appendChild(bannerText);

  const openBtn = document.createElement('button');
  openBtn.type = 'button';
  openBtn.className = 'btn btn-primary btn-open-llm';
  openBtn.style.cssText = 'min-height:42px;padding:8px 16px;font-size:13.5px;font-weight:600;display:inline-flex;align-items:center;gap:8px;border-radius:8px;';
  openBtn.innerHTML = '<span>Open LLM Settings</span><span class="btn-glow-pill" aria-hidden="true">↗</span>';
  openBtn.setAttribute('aria-label', 'Open the LLM settings page');
  openBtn.addEventListener('click', () => { window.location.href = '/#llm-settings'; });
  banner.appendChild(openBtn);

  container.appendChild(banner);

  const box = document.createElement('div');
  container.appendChild(box);
  if (!llmSectionLoaded) {
    box.textContent = 'Loading…';
    try {
      const res = await fetch('/api/llm/settings');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      // The renderer edits write through cfg, so the working copy must hold the
      // effective settings — not the stale config.yaml section.
      cfg.llm_decipher = await res.json();
      llmSectionLoaded = true;
    } catch (err) {
      box.textContent = 'Could not load LLM settings: ' + err.message;
      return;
    }
  }
  box.innerHTML = '';
  renderObject(box, cfg.llm_decipher, 'llm_decipher');
}

// ── Quick actions ───────────────────────────────────────────────────────────
// One-click entry points for the sections where a single action is the usual
// reason to open them. Everything stays an ordinary edit of the working copy,
// so the existing Save / dirty tracking applies unchanged.
function addIpProfileOverride() {
  const raw = prompt('Source IP of the device (for example 192.168.1.50):');
  if (!raw) return;
  const ip = raw.trim();
  if (!/^[0-9a-fA-F:.]+$/.test(ip)) {
    showToast('Not a valid IPv4 or IPv6 address', 'error');
    return;
  }
  cfg.core = cfg.core || {};
  cfg.core.ip_profiles = cfg.core.ip_profiles || {};
  if (cfg.core.ip_profiles[ip]) {
    showToast('That IP already has an override', 'info');
  } else {
    cfg.core.ip_profiles[ip] = { connection: 'auto', bypass: false };
    showToast('Override added for ' + ip + ' — review it below, then Save', 'success');
  }
  setDirty('core.ip_profiles');
  rerenderSection();
}

const SECTION_QUICK_ACTIONS = {
  core: [
    {
      label: '+ Add per-IP override',
      tip: 'Create one core.ip_profiles entry for a single source IP, with connection auto and bypass off, then edit it below and Save.',
      run: addIpProfileOverride,
    },
  ],
  dns: [
    {
      label: 'Intercept every name',
      tip: 'Turn on dns.intercept_all so every device that uses this host as its resolver is captured. Review the exclude list before saving.',
      run: () => {
        if (!cfg.dns) return;
        cfg.dns.intercept_all = true;
        setDirty('dns.intercept_all');
        rerenderSection();
        showToast('intercept_all switched on — review exclude, then Save', 'info');
      },
    },
  ],
};

function renderQuickActions(container, key) {
  const actions = SECTION_QUICK_ACTIONS[key];
  if (!actions || !actions.length) return;
  const bar = document.createElement('div');
  bar.className = 'sec-quick';
  actions.forEach((action) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-ghost';
    btn.textContent = action.label;
    attachTip(btn, action.tip);
    btn.addEventListener('click', action.run);
    bar.appendChild(btn);
  });
  container.appendChild(bar);
}

function selectSection(key) {
  document.querySelectorAll('.section-item').forEach(b => {
    const isActive = b.dataset.section === key;
    b.classList.toggle('active', isActive);
    if (isActive) {
      b.setAttribute('aria-current', 'true');
      b.setAttribute('aria-selected', 'true');
    } else {
      b.removeAttribute('aria-current');
      b.setAttribute('aria-selected', 'false');
    }
  });
  const title = document.getElementById('sectionTitle');
  const desc = document.getElementById('sectionDesc');
  const fields = document.getElementById('fields');
  if (title) {
    title.setAttribute('tabindex', '-1');
  }
  if (key === '__raw__') {
    title.textContent = 'Raw Configuration';
    desc.innerHTML = '<span>Full config as JSON — edit carefully, validated on save.</span>';
    const fmtBtn = document.createElement('button');
    fmtBtn.type = 'button';
    fmtBtn.className = 'btn btn-ghost';
    fmtBtn.style.cssText = 'margin-left:12px;padding:3px 10px;font-size:12px;';
    fmtBtn.textContent = 'Format JSON';
    fmtBtn.setAttribute('aria-label', 'Format Raw Configuration JSON');

    const ta = document.createElement('textarea');
    ta.id = 'raw-editor';
    ta.setAttribute('aria-label', 'Raw configuration JSON');
    ta.style.cssText = 'width:100%;min-height:60vh;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:12px;font-family:ui-monospace,monospace;font-size:12px;resize:vertical;';
    ta.value = JSON.stringify(cfg, null, 2);
    ta.addEventListener('input', () => { fields.classList.add('dirty'); setDirty('__raw__'); });

    fmtBtn.addEventListener('click', () => {
      try {
        const parsed = JSON.parse(ta.value);
        ta.value = JSON.stringify(parsed, null, 2);
        fields.classList.add('dirty');
        setDirty('__raw__');
        showToast('JSON formatted', 'success');
      } catch (err) {
        showToast('Invalid JSON in Raw Configuration: ' + err.message, 'error');
      }
    });

    desc.appendChild(fmtBtn);
    fields.innerHTML = '';
    fields.appendChild(ta);
    setDirty('__raw__');
    if (title) title.focus();
    return;
  }
  if (key === 'llm_decipher') {
    // Editable here too, but persistence belongs to the LLM settings API:
    // values are loaded from the effective (runtime) state and saved through
    // /api/llm/settings, never written to config.yaml by this page.
    title.textContent = SECTION_LABELS[key] || 'LLM Decipher';
    desc.textContent = 'LLM profiles in use — saved through the LLM settings API';
    fields.innerHTML = '';
    fields.classList.remove('dirty');
    renderLLMSection(fields);
    if (title) title.focus();
    return;
  }
  title.textContent = SECTION_LABELS[key] || key;
  desc.textContent = HINTS[key] || 'Configuration section';
  fields.innerHTML = '';
  hideTooltip();
  renderQuickActions(fields, key);
  renderObject(fields, cfg[key], key);
  fields.classList.remove('dirty');
  // re-mark dirty fields visually
  dirty.forEach(p => {
    document.querySelectorAll(`[data-path="${p}"]`).forEach(el => el.classList.add('dirty'));
  });
  if (title) title.focus();
}

// ── Generic field renderer ──────────────────────────────────────────────────
function isSimpleType(v) {
  return ['string', 'number', 'boolean'].includes(typeof v) || v === null;
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

let fieldCounter = 0;

function renderObject(container, obj, path) {
  const entries = Object.entries(obj || {});
  if (entries.length === 0) {
    const p = document.createElement('p');
    p.className = 'sec-desc';
    const emptyHint = hintFor(path);
    p.textContent = emptyHint ? 'Empty. ' + emptyHint : 'Empty.';
    container.appendChild(p);
    // Allow populating empty dicts (e.g. core.ip_profiles, vendors, profiles)
    const addBtn = document.createElement('button');
    addBtn.className = 'btn btn-ghost';
    addBtn.textContent = '+ Add key';
    addBtn.style.cssText = 'margin-top:6px;font-size:12px;padding:4px 12px;';
    addBtn.setAttribute('title', 'Add one or more keys to this section. Separate several keys with a comma.');
    addBtn.addEventListener('click', () => {
      const raw = prompt('New key name (separate several keys with a comma):');
      if (!raw) return;
      const parent = getAt(cfg, path);
      raw.split(',').map((s) => s.trim()).filter((s) => s !== '').forEach((key) => {
        if (parent[key] === undefined) parent[key] = defaultForKey(path, key);
      });
      setDirty(path);
      rerenderSection();
    });
    container.appendChild(addBtn);
    return;
  }
  entries.forEach(([k, v]) => {
    const childPath = path ? path + '.' + k : k;
    if (isSimpleType(v)) {
      container.appendChild(renderField(childPath, k, v));
    } else if (Array.isArray(v)) {
      container.appendChild(renderArray(childPath, k, v));
    } else {
      // nested object → collapsible group
      const group = document.createElement('div');
      group.className = 'field-group';
      group.dataset.path = childPath;
      const head = document.createElement('div');
      head.className = 'field-group-title';
      head.dataset.path = childPath;
      head.setAttribute('role', 'button');
      head.setAttribute('tabindex', '0');
      head.setAttribute('aria-expanded', 'true');
      const bodyId = 'fg-body-' + (fieldCounter++);
      head.setAttribute('aria-controls', bodyId);
      
      const leftDiv = document.createElement('div');
      leftDiv.className = 'field-group-title-left';
      const chevronSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      chevronSvg.setAttribute('class', 'chev-svg');
      chevronSvg.setAttribute('aria-hidden', 'true');
      chevronSvg.setAttribute('viewBox', '0 0 24 24');
      chevronSvg.innerHTML = '<polyline points="6 9 12 15 18 9"/>';
      const nameSpan = document.createElement('span');
      nameSpan.textContent = k;
      leftDiv.appendChild(chevronSvg);
      leftDiv.appendChild(nameSpan);
      const groupHint = hintFor(childPath);
      if (groupHint) {
        const hintSpan = document.createElement('span');
        hintSpan.className = 'fg-hint';
        hintSpan.textContent = groupHint;
        leftDiv.appendChild(hintSpan);
      }
      head.appendChild(leftDiv);
      attachTip(head, groupHint ? groupHint + ' (' + childPath + ')' : childPath);

      const body = document.createElement('div');
      body.id = bodyId;
      renderObject(body, v, childPath);

      const toggleGroup = () => {
        const hidden = body.style.display === 'none';
        body.style.display = hidden ? '' : 'none';
        chevronSvg.classList.toggle('collapsed', hidden);
        head.setAttribute('aria-expanded', hidden ? 'true' : 'false');
      };
      head.addEventListener('click', toggleGroup);
      head.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          toggleGroup();
        }
      });
      group.appendChild(head);
      group.appendChild(body);
      container.appendChild(group);
    }
  });
}

// Re-render the currently active section (used after structural edits like
// adding/removing array items or dict keys).
function rerenderSection() {
  const active = document.querySelector('.section-item.active');
  if (active) selectSection(active.dataset.section);
}

function renderField(path, label, value) {
  const row = document.createElement('div');
  row.className = 'field';
  row.dataset.path = path;
  const id = 'f-' + (fieldCounter++);
  const hint = hintFor(path);
  const info = document.createElement('div');
  info.className = 'f-info';
  const nameRow = document.createElement('span');
  nameRow.className = 'f-name-row';
  const nameLabel = document.createElement('label');
  nameLabel.setAttribute('for', id);
  nameLabel.className = 'f-name';
  nameLabel.textContent = label;
  nameRow.appendChild(nameLabel);
  if (hint) {
    const help = document.createElement('button');
    help.type = 'button';
    help.className = 'f-help';
    help.textContent = '?';
    help.setAttribute('aria-label', 'What does ' + label + ' do');
    nameRow.appendChild(help);
    attachTip(help, hint);
  }
  info.appendChild(nameRow);
  const pathLine = document.createElement('div');
  pathLine.className = 'f-path';
  const pathCode = document.createElement('span');
  pathCode.textContent = path;
  pathLine.appendChild(pathCode);
  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'f-copy';
  copyBtn.textContent = 'copy path';
  copyBtn.setAttribute('aria-label', 'Copy config path ' + path);
  copyBtn.addEventListener('click', () => copyFieldPath(path, copyBtn));
  pathLine.appendChild(copyBtn);
  info.appendChild(pathLine);
  attachTip(nameRow, hint ? hint + ' (' + path + ')' : path);
  row.appendChild(info);
  const ctrl = document.createElement('div');
  ctrl.className = 'f-control';

  if (typeof value === 'boolean') {
    row.classList.add('field-switch');
    const switchWrap = document.createElement('label');
    switchWrap.className = 'switch-wrap';
    switchWrap.htmlFor = id;
    const input = document.createElement('input');
    input.type = 'checkbox'; input.className = 'switch';
    input.role = 'switch';
    input.id = id; input.checked = value;
    input.setAttribute('aria-checked', value ? 'true' : 'false');
    input.setAttribute('aria-label', label);
    input.addEventListener('change', () => {
      input.setAttribute('aria-checked', input.checked ? 'true' : 'false');
      setAt(cfg, path, input.checked);
      setDirty(path);
      markFieldDirty(path);
    });
    switchWrap.appendChild(input);
    ctrl.appendChild(switchWrap);
  } else if (typeof value === 'number') {
    const input = document.createElement('input');
    input.type = 'number'; input.id = id; input.value = value;
    input.step = 'any';
    input.addEventListener('change', () => {
      const n = parseFloat(input.value);
      setAt(cfg, path, Number.isNaN(n) ? null : n);
      setDirty(path); markFieldDirty(path);
    });
    ctrl.appendChild(input);
  } else if (value === null) {
    const input = document.createElement('input');
    input.type = 'text'; input.id = id; input.value = '';
    input.placeholder = 'null';
    input.addEventListener('change', () => {
      setAt(cfg, path, input.value === '' ? null : input.value);
      setDirty(path); markFieldDirty(path);
    });
    const list = buildDatalist(path);
    if (list) { input.setAttribute('list', list.listId); ctrl.appendChild(list.dl); }
    ctrl.appendChild(input);
  } else if (typeof value === 'string') {
    const enumOpts = enumOptionsFor(path);
    if (enumOpts) {
      const select = document.createElement('select');
      select.id = id;
      enumOpts.forEach(opt => {
        const o = document.createElement('option');
        o.value = opt; o.textContent = opt;
        if (opt === value) o.selected = true;
        select.appendChild(o);
      });
      select.addEventListener('change', () => {
        setAt(cfg, path, select.value);
        setDirty(path); markFieldDirty(path);
      });
      ctrl.appendChild(select);
    } else {
      const isLong = typeof value === 'string' && value.length > 80;
      if (isLong) {
        const ta = document.createElement('textarea');
        ta.id = id; ta.value = value; ta.rows = 4;
        ta.addEventListener('input', () => {
          setAt(cfg, path, ta.value);
          setDirty(path); markFieldDirty(path);
        });
        ctrl.appendChild(ta);
      } else {
        // Secret fields arrive masked from the API; render them as password
        // inputs. Leaving the mask unchanged keeps the stored value on save.
        const isPassword = /(^|\.)(api_key|password|mqtt_pass)$/.test(path);
        if (isPassword) {
          const wrap = document.createElement('div');
          wrap.className = 'pw-wrap';
          const input = document.createElement('input');
          input.type = 'password';
          input.autocomplete = 'off';
          input.id = id;
          input.value = value;
          input.style.paddingRight = '42px';
          input.addEventListener('change', () => {
            setAt(cfg, path, input.value === '' ? null : input.value);
            setDirty(path); markFieldDirty(path);
          });
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'pw-toggle-btn';
          btn.setAttribute('aria-label', 'Show API key');
          btn.title = 'Show API key';
          btn.innerHTML = '<svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24" style="width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
          btn.addEventListener('click', () => {
            const show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            btn.setAttribute('aria-label', show ? 'Hide API key' : 'Show API key');
            btn.title = show ? 'Hide API key' : 'Show API key';
            const svg = btn.querySelector('svg');
            if (svg) {
              svg.innerHTML = show
                ? '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'
                : '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
            }
          });
          wrap.appendChild(input);
          wrap.appendChild(btn);
          ctrl.appendChild(wrap);
        } else {
          const input = document.createElement('input');
          input.type = 'text';
          input.id = id; input.value = value;
          input.addEventListener('change', () => {
            setAt(cfg, path, input.value === '' ? null : input.value);
            setDirty(path); markFieldDirty(path);
          });
          const list = buildDatalist(path);
          if (list) { input.setAttribute('list', list.listId); ctrl.appendChild(list.dl); }
          ctrl.appendChild(input);
        }
      }
    }
  }

  row.appendChild(ctrl);
  return row;
}

function renderArray(path, label, arr) {
  const group = document.createElement('div');
  group.className = 'field-group';
  group.dataset.path = path;
  const head = document.createElement('div');
  head.className = 'field-group-title';
  head.setAttribute('role', 'button');
  head.setAttribute('tabindex', '0');
  head.setAttribute('aria-expanded', 'true');
  const bodyId = 'fg-body-' + (fieldCounter++);
  head.setAttribute('aria-controls', bodyId);

  const leftDiv = document.createElement('div');
  leftDiv.className = 'field-group-title-left';
  const chevronSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  chevronSvg.setAttribute('class', 'chev-svg');
  chevronSvg.setAttribute('aria-hidden', 'true');
  chevronSvg.setAttribute('viewBox', '0 0 24 24');
  chevronSvg.innerHTML = '<polyline points="6 9 12 15 18 9"/>';
  const nameSpan = document.createElement('span');
  nameSpan.textContent = label + ' (' + arr.length + ')';
  leftDiv.appendChild(chevronSvg);
  leftDiv.appendChild(nameSpan);
  const arrHint = hintFor(path);
  if (arrHint) {
    const hintSpan = document.createElement('span');
    hintSpan.className = 'fg-hint';
    hintSpan.textContent = arrHint;
    leftDiv.appendChild(hintSpan);
  }
  head.appendChild(leftDiv);
  attachTip(head, arrHint ? arrHint + ' (' + path + ')' : path);

  const body = document.createElement('div');
  body.id = bodyId;
  const toggleArrayGroup = () => {
    const hidden = body.style.display === 'none';
    body.style.display = hidden ? '' : 'none';
    chevronSvg.classList.toggle('collapsed', hidden);
    head.setAttribute('aria-expanded', hidden ? 'true' : 'false');
  };
  head.addEventListener('click', toggleArrayGroup);
  head.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggleArrayGroup();
    }
  });
  group.appendChild(head);
  group.appendChild(body);
  // Primitive arrays → editable comma-separated list
  if (arr.length && arr.every(isSimpleType)) {
    const row = document.createElement('div');
    row.className = 'field';
    const id = 'f-' + (fieldCounter++);
    const itemHint = hintFor(path);
    const info = document.createElement('div');
    info.className = 'f-info';
    const nameRow = document.createElement('span');
    nameRow.className = 'f-name-row';
    const nameLabel = document.createElement('label');
    nameLabel.setAttribute('for', id);
    nameLabel.className = 'f-name';
    nameLabel.textContent = label;
    nameRow.appendChild(nameLabel);
    if (itemHint) {
      const help = document.createElement('button');
      help.type = 'button';
      help.className = 'f-help';
      help.textContent = '?';
      help.setAttribute('aria-label', 'What does ' + label + ' do');
      nameRow.appendChild(help);
      attachTip(help, itemHint);
      attachTip(nameRow, itemHint + ' (' + path + ')');
    }
    info.appendChild(nameRow);
    const pathLine = document.createElement('div');
    pathLine.className = 'f-path';
    const pathCode = document.createElement('span');
    pathCode.textContent = path + ' — comma-separated values';
    pathLine.appendChild(pathCode);
    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'f-copy';
    copyBtn.textContent = 'copy path';
    copyBtn.setAttribute('aria-label', 'Copy config path ' + path);
    copyBtn.addEventListener('click', () => copyFieldPath(path, copyBtn));
    pathLine.appendChild(copyBtn);
    info.appendChild(pathLine);
    row.appendChild(info);
    const ctrl = document.createElement('div');
    ctrl.className = 'f-control';
    const input = document.createElement('input');
    input.type = 'text';
    input.id = id;
    input.value = arr.join(', ');
    input.setAttribute('aria-label', label);
    input.addEventListener('change', () => {
      const parts = input.value.split(',').map(s => s.trim()).filter(s => s !== '');
      const isNum = arr.every(v => typeof v === 'number');
      setAt(cfg, path, isNum ? parts.map(Number) : parts);
      setDirty(path);
      markFieldDirty(path);
    });
    ctrl.appendChild(input);
    row.appendChild(ctrl);
    body.appendChild(row);
  } else {
    // Object array (or empty array) → render each item with add/remove controls
    if (arr.length === 0) {
      const p = document.createElement('p');
      p.className = 'sec-desc';
      p.textContent = '(empty)';
      body.appendChild(p);
    } else {
      arr.forEach((item, idx) => {
        const itemPath = path + '.' + idx;
        const itemGroup = document.createElement('div');
        itemGroup.className = 'field-group';
        itemGroup.dataset.path = itemPath;
        const itemHead = document.createElement('div');
        itemHead.className = 'field-group-title';
        itemHead.setAttribute('role', 'button');
        itemHead.setAttribute('tabindex', '0');
        itemHead.setAttribute('aria-expanded', 'true');
        const itemBodyId = 'fg-body-' + (fieldCounter++);
        itemHead.setAttribute('aria-controls', itemBodyId);

        const itemLeftDiv = document.createElement('div');
        itemLeftDiv.className = 'field-group-title-left';
        const itemChevronSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        itemChevronSvg.setAttribute('class', 'chev-svg');
        itemChevronSvg.setAttribute('aria-hidden', 'true');
        itemChevronSvg.setAttribute('viewBox', '0 0 24 24');
        itemChevronSvg.innerHTML = '<polyline points="6 9 12 15 18 9"/>';
        const itemName = document.createElement('span');
        itemName.textContent = '#' + idx;
        itemLeftDiv.appendChild(itemChevronSvg);
        itemLeftDiv.appendChild(itemName);
        itemHead.appendChild(itemLeftDiv);

        const itemBody = document.createElement('div');
        itemBody.id = itemBodyId;
        renderObject(itemBody, item, itemPath);
        const toggleItem = () => {
          const hidden = itemBody.style.display === 'none';
          itemBody.style.display = hidden ? '' : 'none';
          itemChevronSvg.classList.toggle('collapsed', hidden);
          itemHead.setAttribute('aria-expanded', hidden ? 'true' : 'false');
        };
        itemHead.addEventListener('click', toggleItem);
        itemHead.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            toggleItem();
          }
        });
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn btn-ghost btn-remove';
        removeBtn.textContent = 'Remove';
        removeBtn.setAttribute('aria-label', 'Remove item #' + idx);
        removeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          const parent = getAt(cfg, path);
          parent.splice(idx, 1);
          setDirty(path);
          rerenderSection();
        });
        itemHead.appendChild(removeBtn);
        itemGroup.appendChild(itemHead);
        itemGroup.appendChild(itemBody);
        body.appendChild(itemGroup);
      });
    }
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn btn-ghost';
    addBtn.textContent = '+ Add item';
    addBtn.style.cssText = 'margin-top:10px;font-size:13px;padding:8px 16px;min-height:38px;';
    addBtn.addEventListener('click', () => {
      const parent = getAt(cfg, path);
      parent.push({});
      setDirty(path);
      rerenderSection();
    });
    body.appendChild(addBtn);
  }
  return group;
}

// Path helpers — mirrors how we write values back
function getAt(obj, path) {
  if (!path) return obj;
  return path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
}
function setAt(obj, path, value) {
  const parts = path.split('.');
  const last = parts.pop();
  const parent = parts.reduce((o, k) => (o[k] == null ? (o[k] = {}) : o[k]), obj);
  parent[last] = value;
}

// Collect the edited section tree from the DOM (only the currently rendered section)
function collectSection(key) {
  if (key === '__raw__') {
    const ta = document.getElementById('raw-editor');
    if (!ta) return cfg;
    try { return JSON.parse(ta.value); } catch (e) { return null; }
  }
  return cfg[key];
}

function setDirty(path) { dirty.add(path); updateSaveBtn(); }
function markFieldDirty(path) {
  const el = document.querySelector(`[data-path="${path}"]`);
  if (el) el.classList.add('dirty');
  // also mark parent section title
}
function updateSaveBtn() {
  const has = dirty.size > 0;
  const saveBtn = document.getElementById('saveBtn');
  const saveBtn2 = document.getElementById('saveBtn2');
  if (saveBtn) {
    saveBtn.disabled = !has;
    saveBtn.title = has ? 'Save configuration changes (Ctrl+S)' : 'No unsaved changes (Ctrl+S)';
  }
  if (saveBtn2) {
    saveBtn2.disabled = !has;
    saveBtn2.title = has ? 'Save configuration changes (Ctrl+S)' : 'No unsaved changes (Ctrl+S)';
  }

  document.querySelectorAll('.section-item[data-section]').forEach(btn => {
    const sec = btn.dataset.section;
    let isDirty = false;
    let secDirtyCount = 0;
    if (sec === '__raw__') {
      isDirty = dirty.has('__raw__');
    } else {
      secDirtyCount = [...dirty].filter(p => p === sec || p.startsWith(sec + '.')).length;
      isDirty = secDirtyCount > 0;
    }
    btn.classList.toggle('dirty-section', isDirty);
    const countEl = btn.querySelector('.count');
    if (countEl) {
      countEl.style.display = '';
      if (sec === '__raw__') {
        countEl.textContent = isDirty ? '●' : '';
        countEl.title = isDirty ? 'Has unsaved changes' : '';
      } else {
        countEl.textContent = isDirty ? `● ${secDirtyCount}` : '';
        countEl.title = isDirty ? `${secDirtyCount} unsaved change${secDirtyCount > 1 ? 's' : ''}` : '';
      }
    }
  });
}

// ── Load & save ─────────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const res = await fetch('/api/config');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    cfg = data.config || {};
    document.getElementById('configPath').textContent = 'config/config.yaml';
    buildSidebar();
    selectSection(Object.keys(cfg)[0] || 'core');
  } catch (e) {
    document.getElementById('sectionTitle').textContent = 'Error loading config';
    document.getElementById('sectionDesc').textContent = String(e.message || e);
  }
}

async function saveConfig() {
  const btn = document.getElementById('saveBtn');
  const btn2 = document.getElementById('saveBtn2');
  const status = document.getElementById('saveStatus');
  btn.disabled = true;
  btn2.disabled = true;
  status.textContent = 'Saving…';
  status.className = 'status';
  // First collect current section tree into cfg
  const activeSection = document.querySelector('.section-item.active');
  const key = activeSection ? activeSection.dataset.section : Object.keys(cfg)[0];
  if (key === '__raw__') {
    const parsed = collectSection('__raw__');
    if (parsed === null) {
      status.textContent = 'Invalid JSON — not saved';
      status.className = 'status err';
      updateSaveBtn();
      return;
    }
    cfg = parsed;
    }
    // For normal sections cfg already reflects every edit: field changes write
    // straight into cfg, so edits in any section are preserved across switches.
  try {
    // LLM profiles are persisted by the LLM settings API, not by /api/config:
    // this page is a second entry point onto the same store.
    const llmTouched = [...dirty].some(p => p === 'llm_decipher' || p.startsWith('llm_decipher.'));
    if (llmTouched && cfg.llm_decipher) {
      const lres = await fetch('/api/llm/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(cfg.llm_decipher),
      });
      if (!lres.ok) {
        const lerr = await lres.json().catch(() => null);
        const lmsg = (lerr && lerr.error) ? lerr.error : 'HTTP ' + lres.status;
        status.textContent = 'LLM profiles not saved: ' + lmsg;
        status.className = 'status err';
        showToast('LLM profiles not saved', 'error');
        updateSaveBtn();
        return;
      }
      // Drop the cached working copy so the next visit re-reads the
      // canonical (validated) settings.
      llmSectionLoaded = false;
    }
    const res = await fetch('/api/config', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config: cfg }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => null);
      const msg = (err && err.error) ? err.error : 'HTTP ' + res.status;
      status.textContent = msg;
      status.className = 'status err';
      showToast('Save failed', 'error');
      updateSaveBtn();
      return;
    }
    // Reload canonical config from server (validated/dumped)
    const savedRes = await fetch('/api/config');
    const savedData = await savedRes.json();
    cfg = savedData.config || cfg;
    dirty.clear();
    llmSectionLoaded = false;
    updateSaveBtn();
    status.textContent = 'Saved, hot-reload applied.';
    status.className = 'status ok';
    showToast('Configuration saved', 'success');
    // Rebuild current view from canonical config
    selectSection(key);
  } catch (e) {
    status.textContent = String(e.message || e);
    status.className = 'status err';
    showToast('Save failed', 'error');
    updateSaveBtn();
  }
}

// (Software update moved to the Options page at /options)

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

window.addEventListener('beforeunload', (event) => {
  if (dirty.size > 0) {
    event.preventDefault();
    event.returnValue = '';
  }
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') hideTooltip();
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault();
    if (dirty.size > 0) {
      saveConfig();
    } else {
      showToast('No unsaved changes', 'info');
    }
  } else if ((!event.ctrlKey && !event.metaKey && !event.altKey && event.key === '/') || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k')) {
    const a = document.activeElement;
    if (!a || (!['INPUT', 'TEXTAREA', 'SELECT'].includes(a.tagName) && !a.isContentEditable)) {
      const s = document.getElementById('sidebarSearch');
      if (s) { event.preventDefault(); s.focus(); s.select(); }
    }
  }
});

loadConfig();

