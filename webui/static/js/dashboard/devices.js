/**
 * Ride the API — Dashboard Devices Module
 * Device listing, filtering, detail panel, device connection settings, auto-switch.
 */
let lastActiveElement = null;

// ── Load device list ─────────────────────────────────────────────────────────
async function loadDevices() {
  const btn = document.getElementById('refreshBtn');
  if (btn) { btn.disabled = true; btn.classList.add('btn-spinning'); }
  try {
    const res = await fetch('/api/devices');
    const data = await res.json();
    const container = document.getElementById('devices'), devices = data.devices || [];

    // Global stats
    const total = devices.length;
    const learn = devices.filter(d => d.mode === 'learning').length;
    const prod = devices.filter(d => d.mode === 'production').length;
    const hybrid = devices.filter(d => d.mode === 'hybrid').length;
    const gs = document.getElementById('globalStats');
    gs.querySelectorAll('.gs-num')[0].textContent = total;
    gs.querySelectorAll('.gs-num')[1].textContent = learn;
    gs.querySelectorAll('.gs-num')[2].textContent = prod;
    gs.querySelectorAll('.gs-num')[3].textContent = hybrid;

    if (devices.length === 0) {
      container.innerHTML = '<div class="empty empty-onboarding">' +
        '<div class="empty-icon">' +
        '<svg class="icon-svg" aria-hidden="true" viewBox="0 0 24 24" style="width:48px;height:48px;"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>' +
        '</div>' +
        '<div class="empty-text">No devices intercepted yet</div>' +
        '<div class="empty-sub">Follow these steps to route IoT device traffic through Ride-the-API and begin learning:</div>' +
        '<div class="onboarding-steps">' +
        '  <div class="onboarding-step">' +
        '    <div class="step-num">1</div>' +
        '    <div class="step-content">' +
        '      <div class="step-title">Configure DNS Interception</div>' +
        '      <div class="step-desc">Set your IoT device or router DNS resolver to point to this host\'s IP address.</div>' +
        '    </div>' +
        '  </div>' +
        '  <div class="onboarding-step">' +
        '    <div class="step-num">2</div>' +
        '    <div class="step-content">' +
        '      <div class="step-title">Verify Captured Queries</div>' +
        '      <div class="step-desc">Open <strong>Network &amp; protocol controls</strong> above to monitor detected queries in <strong>DNS Discovery</strong>.</div>' +
        '    </div>' +
        '  </div>' +
        '  <div class="onboarding-step">' +
        '    <div class="step-num">3</div>' +
        '    <div class="step-content">' +
        '      <div class="step-title">Automatic Learning</div>' +
        '      <div class="step-desc">As the device talks to vendor cloud endpoints, requests are mirrored and analyzed by the LLM pattern engine.</div>' +
        '    </div>' +
        '  </div>' +
        '</div>' +
        '</div>';
      return;
    }
    container.innerHTML = devices.map(d => {
      const rate = d.match_rate_pct !== undefined ? d.match_rate_pct : null;
      const isUnknown = d.device_id && d.device_id.startsWith('ip-');
      const unkClass = isUnknown ? ' unidentified' : '';
      const unkBadge = isUnknown ? '<span class="badge badge-hybrid">⚠ Unidentified</span>' : modeBadge(d.mode);
            const devName = d.name || d.device_id || 'unknown';
            return `<button type="button" class="device-card${unkClass}" data-mode="${escapeHtml(d.mode || 'learning')}" data-device-id="${escapeHtml(d.device_id || '')}" aria-label="View details for ${escapeHtml(devName)}" onclick="loadDeviceStats('${escapeJs(d.device_id)}')">
              <div class="dc-top">
                <div><div class="dc-name">${escapeHtml(d.name || shortId(d.device_id))}</div><div class="dc-id" title="${escapeHtml(d.device_id || '')}">${escapeHtml(shortId(d.device_id))}</div></div>
                ${unkBadge}
              </div>
              <div class="dc-meta">${escapeHtml(d.vendor || 'unknown')} · ${escapeHtml(d.device_type || 'generic')}</div>
        <div class="dc-stats">
          ${deviceIps(d).length ? `<span class="dc-stat"><span class="dc-label">IP:</span><span class="dc-value">${escapeHtml(deviceIps(d).join(', '))}</span></span>` : ''}
          ${d.relayed_connections ? `<span class="dc-stat"><span class="dc-label">Relayed:</span><span class="dc-value" title="Connections forwarded without decryption">${d.relayed_connections}</span></span>` : ''}
          ${rate !== null ? `<span class="dc-stat"><span class="dc-label">Match:</span><span class="dc-value" style="color:${rateColor(rate)}">${rate}%</span></span>` : ''}
          <span class="dc-stat"><span class="dc-label">Last:</span><span class="dc-value">${d.last_seen ? new Date(d.last_seen).toLocaleString() : 'Never'}</span></span>
        </div>
      </button>`;
    }).join('');
    filterDevices();
  } catch(e) {
    document.getElementById('devices').innerHTML = '<div class="empty"><div class="empty-icon">' +
      '<svg class="icon-svg" aria-hidden="true" viewBox="0 0 24 24" style="width:48px;height:48px;"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>' +
      '</div><div class="empty-text">Error loading devices</div><div class="empty-sub">Is the server running?</div></div>';
    showToast('Failed to load devices', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('btn-spinning'); }
  }
}

let activeModeFilter = null;

// Addresses to show for a device: the ones registered on the registry, plus the address
// embedded in an auto-created "ip-<addr>" device id when the device is still unidentified.
// Those ids use dashes as separators ("ip-192-168-1-42"), so they are converted back to dots;
// anything that is not a four-octet IPv4 id is shown as-is.
function deviceIps(d) {
  const ips = Array.isArray(d.ip_addresses) ? d.ip_addresses.filter(Boolean).slice() : [];
  const raw = (d.device_id || '').startsWith('ip-') ? (d.device_id || '').slice(3) : '';
  if (raw) {
    const embedded = /^\d{1,3}(-\d{1,3}){3}$/.test(raw) ? raw.replace(/-/g, '.') : raw;
    if (!ips.includes(embedded)) ips.unshift(embedded);
  }
  return ips;
}

function filterByMode(mode) {
  activeModeFilter = (activeModeFilter === mode) ? null : mode;
  filterDevices();
}

function clearDeviceSearch() {
  activeModeFilter = null;
  const input = document.getElementById('deviceSearchInput');
  if (input) {
    input.value = '';
    input.focus();
  }
  filterDevices();
}

// ── Real-time device filter ──────────────────────────────────────────────────
function filterDevices() {
  const query = (document.getElementById('deviceSearchInput')?.value || '').trim().toLowerCase();
  const clearBtn = document.getElementById('clearSearchBtn');
  if (clearBtn) clearBtn.style.display = (query || activeModeFilter) ? 'block' : 'none';

  // Update active state on global stat cards
  const gsCards = document.querySelectorAll('#globalStats .global-stat');
  const modes = [null, 'learning', 'production', 'hybrid'];
  gsCards.forEach((card, i) => {
    const isSelected = activeModeFilter === modes[i] || (modes[i] === null && activeModeFilter === null);
    card.classList.toggle('active', isSelected);
    card.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
  });

  const cards = document.querySelectorAll('#devices .device-card');
  let visibleCount = 0;
  cards.forEach(card => {
    const text = card.textContent.toLowerCase();
    const deviceId = (card.dataset.deviceId || '').toLowerCase();
    const cardMode = card.dataset.mode;
    const matchesQuery = !query || text.includes(query) || deviceId.includes(query);
    const matchesMode = !activeModeFilter || cardMode === activeModeFilter;
    const matches = matchesQuery && matchesMode;
    card.style.display = matches ? '' : 'none';
    if (matches) visibleCount++;
  });

  const statusEl = document.getElementById('filterStatus');
  if (statusEl && cards.length > 0) {
    if (activeModeFilter && query) {
      statusEl.textContent = `Showing ${visibleCount} of ${cards.length} devices (${activeModeFilter} mode, matching "${query}")`;
    } else if (activeModeFilter) {
      statusEl.textContent = `Showing ${visibleCount} of ${cards.length} devices (${activeModeFilter} mode)`;
    } else if (query) {
      statusEl.textContent = `Showing ${visibleCount} of ${cards.length} devices`;
    } else {
      statusEl.textContent = '';
    }
  }

  let emptyMsg = document.getElementById('noMatchingDevices');
  if (cards.length > 0 && visibleCount === 0) {
    if (!emptyMsg) {
      emptyMsg = document.createElement('div');
      emptyMsg.id = 'noMatchingDevices';
      emptyMsg.className = 'empty';
      emptyMsg.innerHTML = '<div class="empty-icon"><svg class="icon-svg" aria-hidden="true" viewBox="0 0 24 24" style="width:48px;height:48px;"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg></div><div class="empty-text">No matching devices found</div><div class="empty-sub">Try searching with a different name, ID, or vendor.</div><button class="btn btn-ghost" style="margin-top:12px;font-size:12px;" onclick="clearDeviceSearch()" aria-label="Clear device search filter">Clear search filter</button>';
      document.getElementById('devices').appendChild(emptyMsg);
    } else {
      emptyMsg.style.display = '';
    }
  } else if (emptyMsg) {
    emptyMsg.style.display = 'none';
  }
}

// ── Load device detail ───────────────────────────────────────────────────────
async function loadDeviceStats(deviceId) {
  lastActiveElement = document.activeElement;
  try {
    const res = await fetch(`/api/devices/${deviceId}/stats`);
    const stats = await res.json();
    const s = stats.stats, pct = s.match_rate_pct || 0, rc = rateClass(pct);
    const detail = document.getElementById('details');
    detail.classList.remove('invisible');

    // SVG ring
    const circum = 2 * Math.PI * 44;
    const offset = circum - (pct / 100) * circum;
    const ringColor = rateColor(pct);
    const ring = `<div class="ring-wrap" role="img" aria-label="Match rate ${pct} percent"><svg class="ring-svg" viewBox="0 0 100 100" aria-hidden="true">
      <circle class="ring-bg" cx="50" cy="50" r="44"/>
      <circle class="ring-fg" cx="50" cy="50" r="44" stroke="${ringColor}" stroke-dasharray="${circum}" stroke-dashoffset="${offset}"/>
    </svg><div class="ring-label" aria-hidden="true"><span class="pct" style="color:${ringColor}">${pct}%</span><span class="pct-label">Match Rate</span></div></div>`;

    // Sparkline with proportional heights and interactive tooltips
    const recent = (s.recent_results || []).slice(-80);
    let spark = '';
    if (recent.length > 0) {
      const hits = recent.filter(r => r.result === 'local_hit').length;
      const misses = recent.filter(r => r.result === 'cloud_miss').length;
      const errs = recent.filter(r => r.result !== 'local_hit' && r.result !== 'cloud_miss').length;
      const chartAria = `Recent activity chart: ${hits} local hit${hits !== 1 ? 's' : ''}, ${misses} cloud miss${misses !== 1 ? 'es' : ''}, ${errs} error${errs !== 1 ? 's' : ''}`;

      spark = `<div class="sparkline-wrap"><div class="sparkline-label" id="sparkline-label">Recent Activity (${recent.length})</div><div class="chart" role="img" aria-labelledby="sparkline-label" aria-label="${escapeHtml(chartAria)}" aria-description="Bar chart showing local hits in green, cloud misses in blue, and errors in red">`;
      recent.forEach(r => {
        const isHit = r.result === 'local_hit';
        const isMiss = r.result === 'cloud_miss';
        const cls = isHit ? 'hit' : isMiss ? 'miss' : 'error';
        const h = Math.max(6, (r.confidence || 0.5) * 36);
        const typeName = isHit ? 'Local hit' : isMiss ? 'Cloud miss' : 'Error';
        const confText = r.confidence !== undefined && r.confidence !== null ? ` (${Math.round(r.confidence * 100)}% match)` : '';
        const barDesc = `${typeName}${confText}`;
        spark += `<div class="bar ${cls}" style="height:${h}px" role="img" title="${escapeHtml(barDesc)}" aria-label="${escapeHtml(barDesc)}"></div>`;
      });
      spark += '</div><div class="legend">';
      if (hits) spark += `<span class="legend-item"><span class="legend-dot" style="background:var(--green)"></span>${hits} hits</span>`;
      if (misses) spark += `<span class="legend-item"><span class="legend-dot" style="background:var(--blue)"></span>${misses} misses</span>`;
      if (errs) spark += `<span class="legend-item"><span class="legend-dot" style="background:var(--red)"></span>${errs} errors</span>`;
      spark += '</div></div>';
    } else {
      spark = '<div class="sparkline-wrap"><div class="sparkline-label" id="sparkline-label">Recent Activity</div><div class="sparkline-empty" role="status">No recent requests recorded yet — activity will appear here as the device communicates.</div></div>';
    }

    detail.innerHTML = `<div class="detail-card">
      <div class="detail-header">
            <h2 id="details-title" tabindex="-1">${escapeHtml(s.name || deviceId)} — Details</h2>
        <button class="btn btn-danger" style="padding:4px 10px;font-size:12px;margin-right:8px;" onclick="deleteDevice('${escapeJs(deviceId)}', this)" aria-label="Delete device ${escapeHtml(deviceId)}" title="Delete device and remove all learned patterns">Delete</button>
        <button class="close-btn" onclick="closeDetails()" aria-label="Close details panel" title="Close details panel">&times;</button>
      </div>
      ${ring}
      <div class="stats-grid">
        <div class="stat-box"><div class="num green">${s.local_hits}</div><div class="lbl">Local Hits</div></div>
        <div class="stat-box"><div class="num blue">${s.cloud_misses}</div><div class="lbl">Cloud Misses</div></div>
        <div class="stat-box"><div class="num red">${s.errors}</div><div class="lbl">Errors</div></div>
        <div class="stat-box"><div class="num yellow">${s.patterns_learned}</div><div class="lbl">Patterns</div></div>
      </div>
      <div class="detail-rows">
        <div class="detail-row"><span class="dr-label">Total Requests</span><span class="dr-value">${s.total_requests}</span></div>
        <div class="detail-row"><span class="dr-label">Mode</span>${modeBadge(s.mode)}</div>
        <div class="detail-row"><span class="dr-label">Threshold</span><span class="dr-value">${(s.match_threshold * 100).toFixed(0)}%</span></div>
        <div class="detail-row"><span class="dr-label">Buffer</span><span class="dr-value">${(s.current_buffer_size_bytes / 1024).toFixed(1)} KB / ${(s.context_buffer_size / 1024).toFixed(0)} KB</span></div>
        <div class="detail-row"><span class="dr-label">Flushes</span><span class="dr-value">${s.buffer_flushes}</span></div>
        <div class="detail-row"><span class="dr-label">Templates</span><span class="dr-value">${s.templates_created}</span></div>
      </div>
      ${captureNote(s)}
      ${spark}
      <div class="mode-area">
        <label for="mode-select-${escapeHtml(deviceId)}">Mode:</label>
        <select id="mode-select-${escapeHtml(deviceId)}" title="Select traffic routing mode for this device" aria-label="Device mode selector">
          <option value="learning" ${s.mode === 'learning' ? 'selected' : ''} title="Forward all requests to cloud and record response patterns">Cloud — Learn All</option>
          <option value="production" ${s.mode === 'production' ? 'selected' : ''} title="Serve all matched requests locally without cloud forwarding">Local — Serve All</option>
          <option value="hybrid" ${s.mode === 'hybrid' ? 'selected' : ''} title="Serve local matches first, fallback to cloud for misses">Hybrid — Local then Cloud</option>
          <option value="passthrough" ${s.mode === 'passthrough' ? 'selected' : ''} title="Forward all requests to cloud without analysis or buffering">Passthrough — Cloud only</option>
        </select>
        <button class="btn btn-primary" id="apply-mode-btn-${escapeHtml(deviceId)}" onclick="switchMode('${escapeJs(deviceId)}')" title="Apply selected routing mode" aria-label="Apply mode change">Apply</button>
      </div>

              <!-- Auto-Switch to Production toggle -->
              <div class="mode-area" style="margin-top: 8px;">
                <label for="auto-switch-${escapeHtml(deviceId)}" style="font-size:14px;color:var(--text2);cursor:pointer;" title="Automatically switch device mode to Production once match rate reaches 99%">Auto-Switch to Production (99% match rate)</label>
                <label class="tc-toggle" aria-label="Toggle auto-switch to production at 99% match rate" title="Toggle auto-switch to production at 99% match rate">
                  <input type="checkbox" id="auto-switch-${escapeHtml(deviceId)}"
                         role="switch"
                         aria-checked="${!!s.auto_switch_enabled}"
                         ${s.auto_switch_enabled ? 'checked' : ''}
                         aria-label="Auto-switch to production when match rate reaches 99%"
                         onchange="toggleAutoSwitch('${escapeJs(deviceId)}', this.checked)">
                  <span class="tc-slider"></span>
                </label>
                <div style="font-size:12px;color:var(--text2);margin-top:4px;width:100%;">
                  Current match rate: ${s.match_rate_pct != null ? s.match_rate_pct.toFixed(1) + '%' : 'N/A'}
                  &middot; Threshold: 99.0%
                </div>
              </div>

              <!-- TLS Config Panel (for all devices) -->
      <div class="tls-config" role="region" aria-label="TLS interception configuration for ${escapeHtml(s.name || deviceId)}">
        <h3>
          <svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          TLS Interception
        </h3>
        <div class="tc-row"><span class="tc-label">Device ID</span><div style="display:flex;align-items:center;gap:6px;"><span class="tc-value" title="${escapeHtml(deviceId)}">${escapeHtml(shortId(deviceId))}</span><button class="btn btn-ghost" style="padding:2px 6px;font-size:11px;" onclick="copyToClipboard('${escapeJs(deviceId)}', 'Device ID')" aria-label="Copy full device ID ${escapeHtml(deviceId)}" title="Copy full device ID"><svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button></div></div>
        <div class="tc-row">
          <label for="tc-name-${escapeHtml(deviceId)}" class="tc-label">Name</label>
          <input type="text" id="tc-name-${escapeHtml(deviceId)}" value="${escapeHtml(s.name || deviceId)}">
        </div>
        <div class="tc-row">
          <label for="tc-vendor-${escapeHtml(deviceId)}" class="tc-label">Vendor / Adapter</label>
          <select id="tc-vendor-${escapeHtml(deviceId)}" data-current="${escapeHtml(s.vendor || 'unknown')}">
            <option value="unknown" ${(s.vendor || 'unknown') === 'unknown' ? 'selected' : ''}>unknown</option>
          </select>
        </div>
        <div class="tc-row">
          <label for="tc-passthrough-${escapeHtml(deviceId)}" class="tc-label" id="passthrough-label-${escapeHtml(deviceId)}" style="cursor:pointer;">Passthrough to Cloud</label>
          <label class="tc-toggle" aria-label="Toggle passthrough to cloud">
                    <input type="checkbox" id="tc-passthrough-${escapeHtml(deviceId)}" role="switch" aria-checked="${!!s.tls_passthrough}" ${s.tls_passthrough ? 'checked' : ''} aria-labelledby="passthrough-label-${escapeHtml(deviceId)}" onchange="this.setAttribute('aria-checked', this.checked)">
            <span class="tc-slider"></span>
          </label>
        </div>
        <div class="tc-row">
          <label for="tc-bypass-${escapeHtml(deviceId)}" class="tc-label" id="bypass-label-${escapeHtml(deviceId)}" style="cursor:pointer;">Bypass (forward to cloud, no analysis)</label>
          <label class="tc-toggle" aria-label="Toggle per-IP bypass: forward directly to cloud without analysis">
            <input type="checkbox" id="tc-bypass-${escapeHtml(deviceId)}" role="switch" aria-checked="${!!s.bypass}" ${s.bypass ? 'checked' : ''} aria-labelledby="bypass-label-${escapeHtml(deviceId)}" onchange="this.setAttribute('aria-checked', this.checked)">
            <span class="tc-slider"></span>
          </label>
        </div>
        <div class="tc-row">
          <label for="tc-pinning-${escapeHtml(deviceId)}" class="tc-label">Device-side TLS guidance</label>
          <select id="tc-pinning-${escapeHtml(deviceId)}">
            <option value="mitm_proxy" ${(s.tls_pinning_bypass || 'mitm_proxy') === 'mitm_proxy' ? 'selected' : ''}>Trust the Ride CA</option>
            <option value="frida" ${s.tls_pinning_bypass === 'frida' ? 'selected' : ''}>Run Frida manually</option>
            <option value="disable_pin_check" ${s.tls_pinning_bypass === 'disable_pin_check' ? 'selected' : ''}>Disable pinning on device</option>
          </select>
        </div>
        <p class="tc-help" role="note">
          This saved choice is guidance only; the proxy cannot change certificate checks inside the device app.
          If the device rejects the proxy certificate, TLS is relayed encrypted and no HTTP pairs reach the LLM buffer.
          Trust the Ride CA on the device, or download the Frida script and run it manually on a supported app.
          <button class="btn btn-ghost" type="button" onclick="downloadFridaScript()" style="padding:4px 10px;font-size:12px;" aria-label="Download Frida script (JS)" title="Download Frida script (JS)">Download Frida script</button>
        </p>
        <div class="tc-row">
          <span class="tc-label">CA Certificate</span>
          <button class="btn btn-ghost" onclick="downloadCaCert()" style="padding:4px 10px;font-size:12px;" aria-label="Download CA certificate" title="Download CA certificate (PEM)">Download</button>
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-top:8px;gap:8px;">
          <a class="btn btn-ghost" href="/patterns/${escapeHtml(deviceId)}" style="padding:6px 12px;font-size:12px;" aria-label="View learned patterns for ${escapeHtml(s.name || deviceId)}" title="View learned patterns for this device">View Patterns</a>
          <div style="text-align:right;">
            <button class="btn btn-primary" id="save-tls-btn-${escapeHtml(deviceId)}" onclick="saveTlsConfig('${escapeJs(deviceId)}')" style="padding:6px 16px;font-size:12px;" aria-label="Save TLS configuration for ${escapeHtml(s.name || deviceId)}" title="Save TLS configuration">Save TLS Config</button>
          </div>
        </div>

              <!-- Device Connection, LLM & Database Config Panel -->
      <div class="tls-config" role="region" aria-label="Device connection, LLM and database configuration for ${escapeHtml(s.name || deviceId)}">
        <h3>
          <svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24"><rect x="1" y="4" width="22" height="16" rx="2" ry="2"/><line x1="1" y1="10" x2="23" y2="10"/></svg>
          Connection, LLM &amp; Database
        </h3>
        <div class="tc-row">
          <label for="dc-connection-${escapeHtml(deviceId)}" class="tc-label">Connection Type</label>
          <select id="dc-connection-${escapeHtml(deviceId)}">
            <option value="auto" ${s.connection === 'auto' ? 'selected' : ''} title="Detect on first bytes (decrypt TLS or route to protocol handler)">auto (detect)</option>
            <option value="tls" ${s.connection === 'tls' ? 'selected' : ''} title="Always decrypt as HTTPS/MQTT-TLS (MITM)">tls</option>
            <option value="http" ${s.connection === 'http' ? 'selected' : ''} title="Route to the HTTP protocol handler">http</option>
            <option value="mqtt" ${s.connection === 'mqtt' ? 'selected' : ''} title="Route to the MQTT protocol handler">mqtt</option>
            <option value="coap" ${s.connection === 'coap' ? 'selected' : ''} title="Route to the CoAP protocol handler">coap</option>
            <option value="modbus" ${s.connection === 'modbus' ? 'selected' : ''} title="Route to the Modbus protocol handler">modbus</option>
          </select>
        </div>

        <div class="tc-row">
          <label for="dc-threshold-${escapeHtml(deviceId)}" class="tc-label">Match Threshold</label>
          <div style="flex:1;display:flex;gap:6px;align-items:center;">
            <input type="number" id="dc-threshold-${escapeHtml(deviceId)}" value="${(s.match_threshold * 100).toFixed(0)}" min="0" max="100" step="1" style="flex:1;" aria-label="Match threshold percent">
            <span style="font-size:11px;color:var(--text2);">%</span>
            <button type="button" class="btn btn-ghost" onclick="saveDeviceThreshold('${escapeJs(deviceId)}', this)" style="padding:4px 10px;font-size:12px;" aria-label="Save match threshold for ${escapeHtml(s.name || deviceId)}" title="Save match threshold">Save</button>
          </div>
        </div>
        <div class="tc-row">
          <label for="dc-buffer-${escapeHtml(deviceId)}" class="tc-label">Context Buffer</label>
          <div style="flex:1;display:flex;gap:6px;align-items:center;">
            <select id="dc-buffer-${escapeHtml(deviceId)}" aria-label="Context buffer size">
              ${[131072, 262144, 524288, 1048576, 2097152, 5242880, 10485760].map(sz => `<option value="${sz}" ${s.context_buffer_size === sz ? 'selected' : ''}>${(sz/1024).toFixed(0)} KB${sz===1048576?' (1MB)':''}</option>`).join('')}
              <option value="${s.context_buffer_size}" ${![131072, 262144, 524288, 1048576, 2097152, 5242880, 10485760].includes(s.context_buffer_size) ? 'selected' : ''}>Custom (${(s.context_buffer_size/1024).toFixed(0)} KB)</option>
            </select>
            <button type="button" class="btn btn-ghost" onclick="saveDeviceBuffer('${escapeJs(deviceId)}', this)" style="padding:4px 10px;font-size:12px;" aria-label="Save context buffer size for ${escapeHtml(s.name || deviceId)}" title="Save context buffer size">Save</button>
          </div>
        </div>

        <div class="tc-row">
          <label for="dc-profile-${escapeHtml(deviceId)}" class="tc-label">LLM Profile</label>
          <select id="dc-profile-${escapeHtml(deviceId)}" data-current="${escapeHtml(s.llm_profile_name || 'default')}"></select>
        </div>
        <div class="tc-row">
          <label for="dc-base-url-${escapeHtml(deviceId)}" class="tc-label">LLM Base URL</label>
          <input type="text" id="dc-base-url-${escapeHtml(deviceId)}" value="${escapeHtml(s.llm_base_url || '')}" placeholder="override profile base URL">
        </div>
        <div class="tc-row">
          <label for="dc-model-${escapeHtml(deviceId)}" class="tc-label">LLM Model</label>
          <input type="text" id="dc-model-${escapeHtml(deviceId)}" value="${escapeHtml(s.llm_model_id || '')}" placeholder="override">
        </div>

        <div class="tc-row">
          <label for="dc-db-name-${escapeHtml(deviceId)}" class="tc-label">Database Name</label>
          <input type="text" id="dc-db-name-${escapeHtml(deviceId)}" value="${escapeHtml(deviceDatabaseName(deviceId, s))}" list="dc-db-names-${escapeHtml(deviceId)}" placeholder="e.g. my_ac_protocol" onfocus="loadDatabaseOptions('${escapeJs(deviceId)}')">
          <datalist id="dc-db-names-${escapeHtml(deviceId)}"></datalist>
        </div>
        <div class="tc-row">
          <label for="dc-db-url-${escapeHtml(deviceId)}" class="tc-label">Database URL</label>
          <input type="text" id="dc-db-url-${escapeHtml(deviceId)}" value="${escapeHtml(deviceDatabaseUrl(deviceId, s))}" list="dc-db-urls-${escapeHtml(deviceId)}" placeholder="e.g. sqlite:///./my_ac.db" onfocus="loadDatabaseOptions('${escapeJs(deviceId)}')">
          <datalist id="dc-db-urls-${escapeHtml(deviceId)}"></datalist>
        </div>

        <div class="tc-row" style="align-items:flex-start;">
          <span class="tc-label">IP Addresses</span>
          <div style="flex:1;display:flex;flex-direction:column;gap:6px;">
            <div id="dc-ips-${escapeHtml(deviceId)}" style="display:flex;flex-wrap:wrap;gap:6px;">
              ${(s.ip_addresses && s.ip_addresses.length) ? s.ip_addresses.map(ip => `<span class="tb-badge tb-active" style="display:inline-flex;align-items:center;gap:4px;" role="listitem"><span>${escapeHtml(ip)}</span><button class="tb-btn" onclick="copyToClipboard('${escapeJs(ip)}', 'IP address')" aria-label="Copy IP address ${escapeHtml(ip)}" title="Copy IP address"><svg class="icon-svg-sm" style="width:12px;height:12px;" aria-hidden="true" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button></span>`).join('') : '<span style="font-size:12px;color:var(--text2);">none</span>'}
            </div>
            <div style="display:flex;gap:6px;">
              <input type="text" id="dc-ip-new-${escapeHtml(deviceId)}" placeholder="e.g. 192.168.1.50" style="flex:1;" aria-label="New IP address for device">
              <button class="btn btn-ghost" onclick="addDeviceIp('${escapeJs(deviceId)}', this)" style="padding:4px 12px;font-size:12px;" aria-label="Add IP address to device" title="Add IP address to device">+ Add IP</button>
            </div>
          </div>
        </div>

        <div style="display:flex;align-items:center;justify-content:space-between;margin-top:8px;gap:8px;">
          <span style="font-size:11px;color:var(--text2);" id="dc-status-${escapeHtml(deviceId)}"></span>
          <div style="text-align:right;display:flex;gap:8px;">
            <button type="button" class="btn btn-ghost" onclick="saveDeviceConnection('${escapeJs(deviceId)}', this)" style="padding:6px 12px;font-size:12px;" aria-label="Save connection type for ${escapeHtml(s.name || deviceId)}" title="Save connection type">Save Connection</button>
            <button type="button" class="btn btn-ghost" onclick="saveDeviceLLM('${escapeJs(deviceId)}', this)" style="padding:6px 12px;font-size:12px;" aria-label="Save LLM configuration for ${escapeHtml(s.name || deviceId)}" title="Save LLM configuration">Save LLM</button>
            <button type="button" class="btn btn-primary" onclick="assignDeviceDatabase('${escapeJs(deviceId)}', this)" style="padding:6px 16px;font-size:12px;" aria-label="Assign database for ${escapeHtml(s.name || deviceId)}" title="Assign protocol database to device">Assign Database</button>
            <button type="button" class="btn btn-ghost" onclick="renameDeviceDatabase('${escapeJs(deviceId)}', this)" style="padding:6px 12px;font-size:12px;" aria-label="Rename database of ${escapeHtml(s.name || deviceId)}" title="Rename the device database file so it follows the name above">Rename Database</button>
          </div>
        </div>
      </div>

                      <!-- LLM Context & Template Section -->
              <div class="llm-section" role="region" aria-label="LLM decoding context">
                <div class="llm-header" id="llm-header-${escapeHtml(deviceId)}" role="button" tabindex="0" aria-expanded="true" aria-controls="llm-body-${escapeHtml(deviceId)}" onclick="toggleLLMSection('${escapeJs(deviceId)}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleLLMSection('${escapeJs(deviceId)}');}">
                  <svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
                  LLM Context &amp; Template
                  <span style="margin-left:auto;font-size:12px;color:var(--text2);" id="llm-toggle-${escapeHtml(deviceId)}">&#x25BC;</span>
                </div>
                <div class="llm-body" id="llm-body-${escapeHtml(deviceId)}">

                  <!-- Profile selector -->
                  <div class="llm-row">
                    <label for="profile-select-${escapeHtml(deviceId)}">Template</label>
                    <select id="profile-select-${escapeHtml(deviceId)}" style="flex:1;" onchange="onProfileChange('${escapeJs(deviceId)}')">
                      <option value="default">Default</option>
                    </select>
                    <button type="button" class="btn btn-ghost" style="padding:4px 8px;font-size:11px;" onclick="saveAsNewProfile('${escapeJs(deviceId)}')" aria-label="Save template as new profile" title="Save prompt template as new profile">Save As</button>
                  </div>
                  <div class="llm-row" style="justify-content:flex-end;gap:6px;border:none;padding-top:0;">
                    <button type="button" class="btn btn-ghost" style="padding:2px 8px;font-size:11px;" onclick="editProfile('${escapeJs(deviceId)}')" aria-label="Edit template profile" title="Edit selected template profile">Edit</button>
                    <button type="button" class="btn btn-ghost" style="padding:2px 8px;font-size:11px;" onclick="deleteProfile('${escapeJs(deviceId)}')" id="delete-profile-btn-${escapeHtml(deviceId)}" aria-label="Delete template profile" title="Delete selected template profile">Delete</button>
                  </div>

                  <!-- Context notes -->
                  <div class="llm-row" style="flex-direction:column;align-items:stretch;">
                    <label for="context-notes-${escapeHtml(deviceId)}" style="min-width:0;margin-bottom:4px;">Context Notes</label>
                    <textarea id="context-notes-${escapeHtml(deviceId)}" placeholder="Add context for LLM analysis&#10;e.g.: &#34;This endpoint controls device pairing&#34;"></textarea>
                    <div style="display:flex;gap:6px;margin-top:4px;justify-content:flex-end;">
                      <button type="button" class="btn btn-ghost" id="save-notes-btn-${escapeHtml(deviceId)}" style="padding:4px 10px;font-size:11px;" onclick="saveContextNotes('${escapeJs(deviceId)}')" aria-label="Save context notes for ${escapeHtml(s.name || deviceId)}" title="Save context notes for LLM analysis">Save Notes</button>
                    </div>
                  </div>

                  <!-- Buffer entries -->
                  <div class="llm-row" style="flex-direction:column;align-items:stretch;">
                    <div style="display:flex;justify-content:space-between;align-items:center;width:100%;">
                      <label style="min-width:0;">Buffer</label>
                      <div style="display:flex;gap:6px;align-items:center;" id="buffer-actions-${escapeHtml(deviceId)}">
                        <span id="buf-count-${escapeHtml(deviceId)}" style="font-size:11px;color:var(--text2);">0 entries</span>
                        <label for="select-all-buf-${escapeHtml(deviceId)}" style="font-size:11px;color:var(--text2);display:flex;align-items:center;gap:4px;cursor:pointer;">
                          <input type="checkbox" id="select-all-buf-${escapeHtml(deviceId)}" aria-label="Select all buffer entries" onchange="toggleSelectAll('${escapeJs(deviceId)}')"> All
                        </label>
                      </div>
                    </div>
                    <div class="llm-buffer" id="buffer-list-${escapeHtml(deviceId)}">
                      <div style="color:var(--text2);font-size:12px;text-align:center;padding:12px;">Loading buffer...</div>
                    </div>
                  </div>

                  <!-- Actions -->
                  <div class="llm-actions">
                    <button type="button" class="btn btn-ghost" onclick="previewAnalysis('${escapeJs(deviceId)}')" id="preview-btn-${escapeHtml(deviceId)}" aria-label="Preview and edit LLM analysis" title="Preview and edit LLM analysis before committing">&#x1F50D; Preview &amp; Edit</button>
                    <button type="button" class="btn btn-primary" onclick="sendToLLM('${escapeJs(deviceId)}')" id="send-btn-${escapeHtml(deviceId)}" aria-label="Send selected entries to LLM" title="Send selected buffer entries to LLM for protocol learning">&#x27A4; Send to LLM</button>
                  </div>

                  <!-- Analysis preview (hidden) -->
                  <div class="analysis-box" id="analysis-box-${escapeHtml(deviceId)}" style="display:none;">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                      <h4 style="margin-bottom:0;">&#x1F4DD; LLM Analysis Preview</h4>
                      <div style="display:flex;gap:6px;">
                        <button type="button" class="btn btn-ghost" style="padding:2px 8px;font-size:11px;" onclick="formatAnalysisJson('${escapeJs(deviceId)}')" aria-label="Format LLM analysis JSON" title="Format analysis JSON with 2-space indentation">Format JSON</button>
                        <button type="button" class="btn btn-ghost" style="padding:2px 8px;font-size:11px;" onclick="copyAnalysisJson('${escapeJs(deviceId)}')" aria-label="Copy LLM analysis JSON to clipboard" title="Copy analysis JSON to clipboard">Copy JSON</button>
                      </div>
                    </div>
                    <textarea id="analysis-text-${escapeHtml(deviceId)}" style="min-height:120px;" aria-label="LLM analysis JSON editor"></textarea>
                    <div style="font-size:12px;color:var(--text2);margin:4px 0;">Edit the analysis above before committing, then click Commit to save patterns.</div>
                    <div class="llm-actions">
                      <button type="button" class="btn btn-ghost" onclick="discardPreview('${escapeJs(deviceId)}')" aria-label="Discard preview" title="Discard LLM analysis preview">Discard</button>
                      <button type="button" class="btn btn-primary" onclick="commitPatterns('${escapeJs(deviceId)}')" aria-label="Commit patterns to device" title="Commit analyzed patterns to device pattern database">&#x2714; Commit Patterns</button>
                    </div>
                  </div>

                </div>
              </div>

              </div>`;

    // Focus the detail panel for accessibility
    const focusTarget = detail.querySelector('h2');
    if (focusTarget) focusTarget.focus();

        // Load LLM section data
        loadLLMProfiles(deviceId);
        loadDeviceLLMConfig(deviceId);
        loadContextNotes(deviceId);
        loadBufferEntries(deviceId);
      } catch(e) { showToast('Failed to load device details', 'error'); }
    }

function closeDetails() {
  const detail = document.getElementById('details');
  if (detail) detail.classList.add('invisible');
  // Return focus to previously active element or first device card
  if (lastActiveElement && document.body.contains(lastActiveElement) && typeof lastActiveElement.focus === 'function') {
    lastActiveElement.focus();
  } else {
    const firstCard = document.querySelector('.device-card');
    if (firstCard) firstCard.focus();
  }
}

async function switchMode(deviceId) {
  const select = document.getElementById('mode-select-' + deviceId);
  const btn = document.getElementById('apply-mode-btn-' + deviceId);
  const mode = select.value;
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Applying...'; }
  try {
    await fetch(`/api/devices/${deviceId}/mode`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode}),
    });
    showToast('Mode switched to ' + mode, 'success');
    loadDeviceStats(deviceId);
    loadDevices();
  } catch(e) { showToast('Failed to switch mode', 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = origText; } }
}

async function deleteDevice(deviceId, btn) {
  const ok = window.showConfirm
    ? await showConfirm({
        title: 'Delete Device',
        message: 'Delete device "' + deviceId + '" and all its learned data? This cannot be undone.',
        confirmText: 'Delete Device',
        danger: true
      })
    : confirm('Delete device "' + deviceId + '" and all its learned data? This cannot be undone.');
  if (!ok) return;
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const origText = targetBtn ? targetBtn.textContent : '';
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = 'Deleting...'; }
  try {
    const res = await fetch(`/api/devices/${deviceId}`, { method: 'DELETE' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      showToast((data.error || 'Failed to delete device') + ' (HTTP ' + res.status + ')', 'error');
      return;
    }
    showToast('Device deleted', 'success');
    closeDetails();
    loadDevices();
  } catch(e) { showToast('Failed to delete device', 'error'); }
  finally { if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = origText; } }
}

  async function toggleAutoSwitch(deviceId, enabled) {
    const el = document.getElementById('auto-switch-' + deviceId);
    if (el) el.setAttribute('aria-checked', enabled ? 'true' : 'false');
    try {
      const res = await fetch(`/api/devices/${deviceId}/auto-switch`, {
        method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled}),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      showToast('Auto-switch ' + (enabled ? 'enabled' : 'disabled'), 'success');
      loadDevices();
    } catch(e) {
      showToast('Failed to toggle auto-switch', 'error');
      if (el) {
        el.checked = !enabled;
        el.setAttribute('aria-checked', (!enabled) ? 'true' : 'false');
      }
    }
  }


// ── Device Connection, LLM & Database config ─────────────────────────────────

// Populate the LLM profile dropdown (system profiles) and select the current one.
async function loadDeviceLLMConfig(deviceId) {
  const sel = document.getElementById('dc-profile-' + deviceId);
  if (!sel) return;
  try {
    const res = await fetch('/api/llm/profiles');
    if (!res.ok) return;
    const data = await res.json();
    const current = sel.getAttribute('data-current') || 'default';
    sel.innerHTML = (data.profiles || []).map(p => {
      const name = typeof p === 'string' ? p : (p.name || '');
      const isSel = (name === current);
      return `<option value="${escapeHtml(name)}"${isSel ? ' selected' : ''}>${escapeHtml(name)}</option>`;
    }).join('');
  } catch(_e) { /* ignore */ }
}

async function saveDeviceConnection(deviceId, btn) {
  const sel = document.getElementById('dc-connection-' + deviceId);
  const connection = sel ? sel.value : 'auto';
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const origText = targetBtn ? targetBtn.textContent : '';
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = 'Saving...'; }
  try {
    const res = await fetch(`/api/devices/${deviceId}/connection`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({connection}),
    });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to save connection'), 'error'); return; }
    showToast('Connection set to ' + connection, 'success');
    loadDeviceStats(deviceId);
  } catch(e) { showToast(await errorFrom(e, 'Failed to save connection'), 'error'); }
  finally { if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = origText; } }
}

async function saveDeviceThreshold(deviceId, btn) {
  const el = document.getElementById('dc-threshold-' + deviceId);
  const pct = parseFloat(el ? el.value : '');
  if (isNaN(pct) || pct < 0 || pct > 100) { showToast('Threshold must be 0–100%', 'warning'); return; }
  const threshold = pct / 100;
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const origText = targetBtn ? targetBtn.textContent : '';
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = 'Saving...'; }
  try {
    const res = await fetch(`/api/devices/${deviceId}/threshold`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({match_threshold: threshold}),
    });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to save threshold'), 'error'); return; }
    showToast('Match threshold set to ' + pct + '%', 'success');
    loadDeviceStats(deviceId);
  } catch(e) { showToast(await errorFrom(e, 'Failed to save threshold'), 'error'); }
  finally { if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = origText; } }
}

async function saveDeviceBuffer(deviceId, btn) {
  const el = document.getElementById('dc-buffer-' + deviceId);
  const size = parseInt(el ? el.value : '', 10);
  if (!size || size <= 0) { showToast('Invalid buffer size', 'warning'); return; }
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const origText = targetBtn ? targetBtn.textContent : '';
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = 'Saving...'; }
  try {
    const res = await fetch(`/api/devices/${deviceId}/context-buffer`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({context_buffer_size: size}),
    });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to save buffer size'), 'error'); return; }
    showToast('Context buffer set to ' + (size / 1024) + ' KB', 'success');
    loadDeviceStats(deviceId);
  } catch(e) { showToast(await errorFrom(e, 'Failed to save buffer size'), 'error'); }
  finally { if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = origText; } }
}

async function saveDeviceLLM(deviceId, btn) {
  const profile = (document.getElementById('dc-profile-' + deviceId) || {}).value || 'default';
  const baseUrl = (document.getElementById('dc-base-url-' + deviceId) || {}).value || '';
  const model = (document.getElementById('dc-model-' + deviceId) || {}).value || '';
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const origText = targetBtn ? targetBtn.textContent : '';
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = 'Saving...'; }
  try {
    const res = await fetch(`/api/devices/${deviceId}/llm`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({profile_name: profile, base_url: baseUrl, model_id: model}),
    });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to save LLM config'), 'error'); return; }
    showToast('LLM config saved', 'success');
    loadDeviceStats(deviceId);
  } catch(e) { showToast(await errorFrom(e, 'Failed to save LLM config'), 'error'); }
  finally { if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = origText; } }
}

async function addDeviceIp(deviceId, btn) {
  const input = document.getElementById('dc-ip-new-' + deviceId);
  if (!input) return;
  const ip = input.value.trim();
  if (!ip) { showToast('IP address required', 'warning'); return; }
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const origText = targetBtn ? targetBtn.textContent : '';
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = 'Adding...'; }
  try {
    const res = await fetch(`/api/devices/${deviceId}/ip`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ip_address: ip}),
    });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to add IP'), 'error'); return; }
    showToast('IP ' + ip + ' added', 'success');
    input.value = '';
    loadDeviceStats(deviceId);
  } catch(e) { showToast(await errorFrom(e, 'Failed to add IP'), 'error'); }
  finally { if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = origText; } }
}



// Global exports for inline HTML event handlers
window.loadDevices = loadDevices;
window.deviceIps = deviceIps;
window.filterByMode = filterByMode;
window.clearDeviceSearch = clearDeviceSearch;
window.filterDevices = filterDevices;
window.loadDeviceStats = loadDeviceStats;
window.closeDetails = closeDetails;
window.switchMode = switchMode;
window.deleteDevice = deleteDevice;
window.toggleAutoSwitch = toggleAutoSwitch;
window.loadDeviceLLMConfig = loadDeviceLLMConfig;
window.saveDeviceConnection = saveDeviceConnection;
window.saveDeviceThreshold = saveDeviceThreshold;
window.saveDeviceBuffer = saveDeviceBuffer;
window.saveDeviceLLM = saveDeviceLLM;
window.addDeviceIp = addDeviceIp;
