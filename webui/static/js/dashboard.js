/**
 * Ride the API — Dashboard UI Logic
 * Handles devices, connections, statistics, Auto-Switch, LLM Settings modal, TLS CA certificates,
 * and live WebSocket log streaming.
 */

// Focus target restored when the LLM settings modal closes. Declared at the top of
// the script on purpose: the deep link `/#llm-settings` below calls
// openLLMSettings() while the document is still parsing, and a `let` declared
// further down would be in its temporal dead zone -> ReferenceError, the modal
// never opening (the toolbar button and the nav pill never hit this because they
// run on click, after parsing).
let lastModalActiveElement = null;

let lastActiveElement = null;

// ── Helpers ──────────────────────────────────────────────────────────────────
function rateClass(pct) { return pct >= 80 ? 'good' : pct >= 50 ? 'warning' : 'danger'; }
function rateColor(pct) { return pct >= 80 ? '#34d399' : pct >= 50 ? '#fbbf24' : '#f87171'; }
function captureNote(s) {
  // The proxy explains itself: bypass / relay / passthrough all mean "no pairs", and
  // reading the wrong switch is how an empty buffer looks like a broken pipeline.
  const c = s && s.capture;
  if (!c || !c.state || c.state === 'capturing') return '';
  const title = c.state === 'relayed' ? 'No pairs: TLS is relayed encrypted'
    : c.state === 'bypassed' ? 'No pairs: this IP is in bypass'
    : 'No pairs: the device mode is passthrough';
  const tone = c.state === 'relayed' ? 'capture-warn' : 'capture-info';
  return `<div class="capture-note ${tone}" role="note"><strong>${title}</strong>${escapeHtml(c.hint || '')}</div>`;
}
function modeBadge(mode) {
  const cls = mode === 'production' ? 'badge-production' : mode === 'hybrid' ? 'badge-hybrid' : mode === 'passthrough' ? 'badge-hybrid' : 'badge-learning';
  return `<span class="badge ${cls}">${mode}</span>`;
}
function shortId(id) { return id && id.length > 30 ? id.slice(0, 12) + '…' : id || 'unknown'; }
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}
function escapeJs(s) {
  // Embed a string inside a single-quoted JS string in a double-quoted HTML
  // attribute. `&` first to prevent entity smuggling, then guard against
  // breaking out of the JS string or the HTML attribute.
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/"/g, '&quot;')
    .replace(/</g, '\\x3C')
    .replace(/>/g, '\\x3E');
}

// ── SVG Icons ────────────────────────────────────────────────────────────────
function deviceIcon() {
  return '<svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>';
}

function fallbackCopyText(text, onSuccess, onError) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    ta.style.top = '-9999px';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    if (ok) onSuccess(); else onError();
  } catch (e) {
    onError();
  }
}

function copyToClipboard(text, label) {
  if (!text) return;
  const onSuccess = () => showToast((label || 'Text') + ' copied to clipboard', 'success');
  const onError = () => showToast('Failed to copy ' + (label || 'text'), 'error');

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(onSuccess).catch(() => {
      fallbackCopyText(text, onSuccess, onError);
    });
  } else {
    fallbackCopyText(text, onSuccess, onError);
  }
}

// ── Toast ────────────────────────────────────────────────────────────────────
function showToast(msg, type) {
  const c = document.getElementById('toasts');
  const t = document.createElement('div');
  const toastType = type || 'info';
  t.className = 'toast ' + toastType;
  const icons = { success: '✓', error: '✕', warning: '⚠', info: 'ℹ' };
  const iconSpan = document.createElement('span');
  iconSpan.className = 'toast-icon';
  iconSpan.setAttribute('aria-hidden', 'true');
  iconSpan.textContent = icons[toastType] || 'ℹ';
  const textSpan = document.createElement('span');
  textSpan.textContent = msg;
  t.appendChild(iconSpan);
  t.appendChild(textSpan);
  c.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .3s'; setTimeout(() => t.remove(), 300); }, 3000);
}

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
            return `<div class="device-card${unkClass}" data-mode="${escapeHtml(d.mode || 'learning')}" data-device-id="${escapeHtml(d.device_id || '')}" tabindex="0" role="button" aria-label="View details for ${escapeHtml(devName)}" onclick="loadDeviceStats('${escapeJs(d.device_id)}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();loadDeviceStats('${escapeJs(d.device_id)}')}">
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
      </div>`;
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
    document.body.style.overflow = 'hidden';

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
  document.body.style.overflow = '';
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
  if (!confirm('Delete device "' + deviceId + '" and all its learned data? This cannot be undone.')) return;
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

  // ── Theme toggle ─────────────────────────────────────────────────────────────
  function updateThemeToggleButton(theme) {
    const btn = document.getElementById('themeToggleBtn');
    const icon = document.getElementById('themeToggleIcon');
    if (!btn || !icon) return;
    const isLight = theme === 'light';
    // Icon-only control: the accessible name and the tooltip are the only text left, and both
    // still say what the next click will do.
    btn.setAttribute('aria-label', isLight ? 'Switch to dark theme' : 'Switch to light theme');
    btn.title = isLight ? 'Switch to dark theme' : 'Switch to light theme';
    // Sun icon for light theme (switch to dark), Moon icon for dark theme (switch to light)
    icon.innerHTML = isLight
      ? '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>'
      : '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    const next = current === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('rta-theme', next); } catch (e) {}
    updateThemeToggleButton(next);
    showToast('Theme set to ' + (next.charAt(0).toUpperCase() + next.slice(1)), 'info');
  }

  // Init theme button state on DOM load
  document.addEventListener('DOMContentLoaded', function () {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    updateThemeToggleButton(current);
  });

  // ── RAM Buffer toggle ────────────────────────────────────────────────────────
  async function toggleRamBuffer(enabled) {
    const el = document.getElementById('ram-buffer-toggle');
    if (el) el.setAttribute('aria-checked', enabled ? 'true' : 'false');
    try {
      const res = await fetch('/api/settings/buffer-backend', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({backend: enabled ? 'memory' : 'disk'}),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      showToast('RAM buffer ' + (enabled ? 'enabled' : 'disabled') + (enabled ? ' (volatile)' : ''), 'success');
    } catch(e) {
      showToast('Failed to toggle RAM buffer', 'error');
      if (el) {
        el.checked = !enabled;
        el.setAttribute('aria-checked', (!enabled) ? 'true' : 'false');
      }
    }
  }

  async function loadRamBufferSetting() {
    try {
      const res = await fetch('/api/settings/buffer-backend');
      if (!res.ok) return;
      const data = await res.json();
      const isMem = (data.backend === 'memory');
      const el = document.getElementById('ram-buffer-toggle');
      if (el) {
        el.checked = isMem;
        el.setAttribute('aria-checked', isMem ? 'true' : 'false');
      }
    } catch(e) { /* settings endpoint unavailable — keep toggle off */ }
  }

    loadDevices();
  loadRamBufferSetting();
loadTlsBar();
loadTlsCerts();
loadProtoServers();
loadDnsDiscovery();
loadVendors();
// Deep link used by the Configuration page: /#llm-settings opens the LLM page,
// which owns the profiles (the config page only mirrors them, read-only).
if (window.location.hash === '#llm-settings') { openLLMSettings(); }
window.addEventListener('hashchange', function() {
  if (window.location.hash === '#llm-settings') { openLLMSettings(); }
});
setInterval(loadDevices, 8000);
setInterval(loadTlsBar, 15000);
setInterval(loadDnsDiscovery, 20000);

document.addEventListener('keydown', function(event) {
  if (event.key === 'Escape') {
    const modal = document.getElementById('llm-settings-modal');
    if (modal && modal.style.display !== 'none') {
      closeLLMSettings();
      return;
    }
    const detail = document.getElementById('details');
    if (detail && !detail.classList.contains('invisible')) {
      closeDetails();
    }
  } else if (event.key === '/' || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k')) {
    const active = document.activeElement;
    const isEditing = active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.tagName === 'SELECT' || active.isContentEditable);
    const modal = document.getElementById('llm-settings-modal');
    const isModalOpen = modal && modal.style.display !== 'none';
    if (!isEditing && !isModalOpen) {
      const searchInput = document.getElementById('deviceSearchInput');
      if (searchInput) {
        event.preventDefault();
        searchInput.focus();
        searchInput.select();
      }
    }
  }
});

// ── Load vendors list for TLS config dropdown ─────────────────────────────
async function loadVendors() {
  try {
    const res = await fetch('/health');
    if (!res.ok) return;
    const h = await res.json();
    const adapters = h.adapters || [];
    if (adapters.length === 0) return;
    document.querySelectorAll('[id^="tc-vendor-"]').forEach(sel => {
          const current = sel.getAttribute('data-current') || 'unknown';
          adapters.forEach(v => {
            if (!sel.querySelector(`option[value="${v}"]`)) {
              const opt = document.createElement('option');
              opt.value = v; opt.textContent = v;
              if (v === current) opt.selected = true;
              sel.appendChild(opt);
            }
          });
        });
  } catch(e) { /* ignore */ }
}

// ── TLS: Load monitored ports bar ──────────────────────────────────────────
async function loadTlsBar() {
  try {
    const res = await fetch('/api/tls/ports');
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      document.getElementById('tlsBadges').innerHTML = '<span class="tb-badge tb-inactive">error</span>';
      document.getElementById('tlsStatus').textContent =
        (data && data.error) ? data.error : 'TLS MITM error';
      return;
    }
    const data = await res.json();
    const container = document.getElementById('tlsBadges');
    const status = document.getElementById('tlsStatus');
    if (!data.enabled) {
      container.innerHTML = '<span class="tb-badge tb-inactive">disabled</span>';
      status.textContent = 'TLS MITM off';
      return;
    }
    const ports = data.ports || [];
    // Ports are read-only: they are configured in config.yaml only.
    container.innerHTML = ports.map(p => `<span class="tb-badge tb-active" role="listitem">${p}</span>`).join('');
    status.textContent = `${ports.length} port${ports.length !== 1 ? 's' : ''}`;
  } catch(e) {
    document.getElementById('tlsBadges').innerHTML = '<span class="tb-badge tb-inactive">error</span>';
  }
}

let certFiles = [];
function pickCertFiles() {
  const input = document.getElementById('certFileInput');
  input.onchange = () => {
    certFiles = Array.from(input.files || []);
    const status = document.getElementById('tlsCertsStatus');
    if (status) {
      if (certFiles.length > 0) {
        status.textContent = 'Selected: ' + certFiles.map(f => f.name).join(', ');
      } else {
        status.textContent = '';
      }
    }
  };
  input.click();
}

async function loadTlsCerts() {
  const container = document.getElementById('tlsCertsList');
  const status = document.getElementById('tlsCertsStatus');
  if (!container) return;
  try {
    const res = await fetch('/api/tls/certs');
    if (!res.ok) {
      // Fail loudly and specifically: a silent "error" badge hides whether the
      // cause is a missing/invalid control-plane password (401) or a server fault.
      if (res.status === 401) {
        container.innerHTML = '<span class="tb-badge tb-inactive">not authenticated</span> <span style="font-size:12px;color:var(--red);">Save the control-plane password (header top-right) first, then reload.</span>';
      } else {
        const msg = await errorFrom(res, `Load failed (HTTP ${res.status})`);
        container.innerHTML = `<span class="tb-badge tb-inactive">error ${res.status}</span> <span style="font-size:12px;color:var(--red);">${escapeHtml(msg)}</span>`;
      }
      if (status) status.textContent = '';
      return;
    }
    const data = await res.json();
    const certs = data.certs || [];
    if (!certs.length) {
      container.innerHTML = '<span style="font-size:12px;color:var(--text2);">No imported certificates. Upload a PEM cert + key for a hostname to serve it via MITM.</span>';
      if (status) status.textContent = '';
      return;
    }
    container.innerHTML = certs.map(c => {
      const notAfter = c.not_after ? new Date(c.not_after) : null;
      const expired = notAfter && notAfter < new Date();
      const host = c.hostname || 'unknown';
      return `<div class="tb-badge tb-active" style="align-items:center;gap:8px;" role="listitem">
        <strong>${escapeHtml(host)}</strong>
        ${c.label ? `<span style="opacity:.8">${escapeHtml(c.label)}</span>` : ''}
        ${notAfter ? `<span style="opacity:.8;${expired ? 'color:var(--red);' : ''}">exp ${expired ? '⚠ ' : ''}${new Date(notAfter).toLocaleDateString()}</span>` : ''}
        ${(c.sans && c.sans.length) ? `<span style="opacity:.6">${escapeHtml(c.sans.slice(0,2).join(', '))}${c.sans.length>2?'…':''}</span>` : ''}
        <button class="tb-btn" onclick="deleteCert('${escapeJs(host)}', this)" aria-label="Delete certificate for ${escapeHtml(host)}" title="Delete imported cert (reverts to auto-generated)">&times;</button>
      </div>`;
    }).join('');
    if (status) status.textContent = `${certs.length} certificate${certs.length!==1?'s':''} imported`;
  } catch(_e) {
    container.innerHTML = '<span class="tb-badge tb-inactive">load error</span> <span style="font-size:12px;color:var(--red);">Could not reach the TLS certs endpoint — is the server up?</span>';
    if (status) status.textContent = '';
  }
}

async function uploadCert(btn) {
  const hostname = (document.getElementById('certHostnameInput') || {}).value || '';
  if (!hostname.trim()) { showToast('Hostname required', 'warning'); return; }
  if (!certFiles.length) { showToast('Choose a certificate (PEM) and key file', 'warning'); return; }
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const origText = targetBtn ? targetBtn.textContent : '';
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = 'Uploading...'; }
  // Reuse the JSON endpoint: read PEM files as text, base64-encode, POST /upload-json.
  const readAsText = (file) => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ''));
    r.onerror = () => reject(new Error('Failed to read ' + file.name));
    r.readAsText(file);
  });
  const b64 = (s) => btoa(unescape(encodeURIComponent(s)));
  try {
    const certFile = certFiles.find(f => /\.(pem|crt|cer)$/i.test(f.name)) || certFiles[0];
    const keyFile = certFiles.find(f => /\.key$/i.test(f.name));
    let certPem, keyPem;
    if (certFile && keyFile) {
      certPem = await readAsText(certFile);
      keyPem = await readAsText(keyFile);
    } else if (certFile) {
      // Allow a single combined .pem that contains BOTH the certificate and
      // its private key — split them so import works the same as two files.
      const combined = await readAsText(certFile);
      const blockRe = /-----BEGIN ([A-Z ]+)-----[\s\S]*?-----END \1-----/g;
      const certs = [], keys = [];
      let m;
      while ((m = blockRe.exec(combined)) !== null) {
        if (m[1] === 'CERTIFICATE') certs.push(m[0]);
        else if (m[1] === 'PRIVATE KEY' || /RSA |EC |DSA /i.test(m[1])) keys.push(m[0]);
      }
      if (certs.length && keys.length) { certPem = certs.join('\n'); keyPem = keys.join('\n'); }
      else { showToast('Select both a cert (.pem/.crt) and its key (.key), or one combined .pem', 'warning'); return; }
    } else {
      showToast('Select both a cert (.pem/.crt) and its key (.key), or one combined .pem', 'warning'); return;
    }
    const res = await fetch('/api/tls/certs/upload-json', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({hostname: hostname.trim(), cert_base64: b64(certPem), key_base64: b64(keyPem)}),
    });
    if (!res.ok) {
      // Reflect the real cause: a 401 means the control-plane password is
      // missing/stale (the request never authenticated), anything else is a
      // validation/server error whose message the backend already returned.
      let msg = await errorFrom(res, 'Failed to upload certificate');
      if (res.status === 401) {
        msg = 'Not authenticated. Save the control-plane password (header top-right), then try again. (' + msg + ')';
      } else if (res.status === 400) {
        msg = 'Certificate rejected by the server: ' + msg;
      }
      showToast(msg, 'error');
      return;
    }
    showToast('Certificate imported for ' + hostname.trim(), 'success');
    certFiles = [];
    document.getElementById('certFileInput').value = '';
    document.getElementById('certHostnameInput').value = '';
    loadTlsCerts();
  } catch(e) { showToast(await errorFrom(e, 'Failed to upload certificate'), 'error'); }
  finally { if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = origText; } }
}

async function deleteCert(hostname, btn) {
  if (!confirm('Delete imported certificate for "' + hostname + '"? It will revert to the auto-generated cert.')) return;
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  if (targetBtn) targetBtn.disabled = true;
  try {
    const res = await fetch('/api/tls/certs/' + encodeURIComponent(hostname), { method: 'DELETE' });
    if (!res.ok) {
      let msg = await errorFrom(res, 'Failed to delete certificate');
      if (res.status === 401) {
        msg = 'Not authenticated. Save the control-plane password (header top-right), then try again. (' + msg + ')';
      }
      showToast(msg, 'error');
      return;
    }
    showToast('Certificate deleted for ' + hostname, 'success');
    loadTlsCerts();
  } catch(e) { showToast(await errorFrom(e, 'Failed to delete certificate'), 'error'); }
  finally { if (targetBtn) targetBtn.disabled = false; }
}

// ── Protocol Servers ──────────────────────────────────────────────────────────

const PROTO_DISPLAY = {
  mqtt: 'MQTT', coap: 'CoAP', modbus: 'Modbus', websocket: 'WebSocket',
  raw_tcp: 'Raw TCP', http2: 'HTTP/2', zigbee_bridge: 'Zigbee Bridge',
  zwave_bridge: 'Z-Wave Bridge', matter_bridge: 'Matter Bridge',
};

// ── DNS discovery ──────────────────────────────────────────────────────────
// Answers "what should we intercept next?" from real traffic: the names devices
// asked the DNS for, with the state the proxy gives each of them right now.
async function loadDnsDiscovery() {
  const list = document.getElementById('dnsDiscoveryList');
  if (!list) return;
  const status = document.getElementById('dnsDiscoveryStatus');
  try {
    const res = await fetch('/api/dns/discovery');
    if (!res.ok) { list.innerHTML = '<span class="tb-badge tb-inactive">error</span>'; return; }
    const data = await res.json();
    const toggle = document.getElementById('dnsInterceptAllToggle');
    if (toggle) {
      toggle.checked = !!data.intercept_all;
      toggle.setAttribute('aria-checked', data.intercept_all ? 'true' : 'false');
    }
    const excludeInput = document.getElementById('dnsExcludeInput');
    // Never overwrite what the operator is typing.
    if (excludeInput && document.activeElement !== excludeInput) {
      excludeInput.value = (data.exclude || []).join(', ');
    }
    const entries = data.entries || [];
    const listen = data.listen || {};
    const listening = (listen.udp || listen.tcp) ? 'listening' : 'not listening';
    const stats = data.stats || {};
    if (status) {
      status.textContent = `${entries.length} name(s) seen · DNS ${listening}`
        + ` · ${stats.intercepted || 0} intercepted · ${stats.excluded || 0} excluded`
        + ` · ${stats.forwarded || 0} forwarded`
        + (data.pending_pairs ? ` · ${data.pending_pairs} pending` : '')
        // A recorder that cannot reach the database is otherwise invisible here:
        // only a log line says so, and the operator is looking at this panel.
        + (data.flush_errors ? ` · ${data.flush_errors} write error(s), the log is not reaching the database` : '')
        + (data.dropped_pairs ? ` · ${data.dropped_pairs} name(s) dropped: the in-memory working set is full` : '');
      status.title = 'An excluded name is still forwarded upstream, so it is counted in both totals.'
        + (data.flushed_pairs ? ` ${data.flushed_pairs} pair(s) written so far.` : '');
    }
    if (!entries.length) {
      list.innerHTML = '<span style="font-size:12px;color:var(--text2);">No query recorded yet. Point a device at this host as its DNS resolver (AdGuard: an upstream or a rewrite) — or switch on "Intercept all" — and its requests appear here.</span>';
      return;
    }
    list.innerHTML = entries.map(e => {
      const host = e.hostname || '';
      const seen = e.last_seen ? e.last_seen.replace('T', ' ').slice(0, 19) : '';
      const state = e.intercepted
        ? '<span class="tb-badge tb-active">intercepted</span>'
        : (e.excluded ? '<span class="tb-badge tb-inactive">excluded</span>' : '<span class="tb-badge" style="opacity:.85;">forwarded</span>');
      // Say *why* a name is not interceptable instead of offering a button that
      // would change nothing: an adapter claim wins over the list, and a name kept
      // out by an exclusion pattern stays out whatever the list says.
      // The exclusion comes first: when one is in force it is the rule that decides
      // (it beats an adapter claim), so naming the claim would explain the loser.
      const reason = e.excluded_by
        ? `<span style="font-size:11px;color:var(--text2);">kept out by the exclusion ${escapeHtml(e.excluded_by)}</span>`
        : (e.claimed_by ? `<span style="font-size:11px;color:var(--text2);">declared by the ${escapeHtml(e.claimed_by)} adapter</span>` : '');
      let action = '';
      if (e.suggested) {
        action = `<button class="tb-btn" onclick="dnsIntercept('${escapeJs(host)}', this)" aria-label="Intercept ${escapeHtml(host)}">+ intercept</button>`;
      } else if (e.intercepted && !e.claimed_by) {
        action = `<button class="tb-btn" onclick="dnsUnintercept('${escapeJs(host)}', this)" aria-label="Stop intercepting ${escapeHtml(host)}">− stop</button>`;
      }
      return `<div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-top:1px solid var(--border);flex-wrap:wrap;">
        <code style="font-size:12px;word-break:break-all;">${escapeHtml(host)}</code>
        <span style="font-size:11px;color:var(--text2);">${escapeHtml(e.client_ip || '')} · ${e.hit_count || 0}× · ${escapeHtml(seen)}</span>
        ${state}${reason}${action}
      </div>`;
    }).join('');
  } catch (_e) {
    list.innerHTML = '<span class="tb-badge tb-inactive">error</span>';
  }
}

async function dnsIntercept(hostname, btn) {
  const target = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  if (target) target.disabled = true;
  try {
    const res = await fetch('/api/dns/intercept', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hostname: hostname }),
    });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to intercept ' + hostname), 'error'); return; }
    showToast('Intercepting ' + hostname + ' — restart the device so it re-resolves it', 'success');
    await loadDnsDiscovery();
  } catch (e) {
    showToast(await errorFrom(e, 'Failed to intercept ' + hostname), 'error');
  } finally {
    if (target) target.disabled = false;
  }
}

async function dnsUnintercept(hostname, btn) {
  const target = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  if (target) target.disabled = true;
  try {
    const res = await fetch(`/api/dns/intercept/${encodeURIComponent(hostname)}`, { method: 'DELETE' });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to stop intercepting ' + hostname), 'error'); return; }
    showToast(hostname + ' is forwarded again', 'success');
    await loadDnsDiscovery();
  } catch (e) {
    showToast(await errorFrom(e, 'Failed to stop intercepting ' + hostname), 'error');
  } finally {
    if (target) target.disabled = false;
  }
}

async function dnsSetInterceptAll(enabled) {
  const toggle = document.getElementById('dnsInterceptAllToggle');
  if (toggle) toggle.setAttribute('aria-checked', enabled ? 'true' : 'false');
  try {
    const res = await fetch('/api/dns/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ intercept_all: !!enabled }),
    });
    if (!res.ok) {
      if (toggle) {
        toggle.checked = !enabled;
        toggle.setAttribute('aria-checked', (!enabled) ? 'true' : 'false');
      }
      showToast(await errorFrom(res, 'Failed to change the interception mode'), 'error');
      return;
    }
    showToast(enabled ? 'Intercepting every name (exclusions still forwarded)' : 'Only the listed names are intercepted', 'success');
    await loadDnsDiscovery();
  } catch (e) {
    if (toggle) {
      toggle.checked = !enabled;
      toggle.setAttribute('aria-checked', (!enabled) ? 'true' : 'false');
    }
    showToast(await errorFrom(e, 'Failed to change the interception mode'), 'error');
  }
}

async function dnsSaveExclusions(btn) {
  const input = document.getElementById('dnsExcludeInput');
  if (!input) return;
  const target = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const names = input.value.split(',').map(s => s.trim()).filter(Boolean);
  if (target) target.disabled = true;
  try {
    const res = await fetch('/api/dns/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ exclude: names }),
    });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to save the exclusions'), 'error'); return; }
    const saved = (await res.json()).exclude || [];
    input.value = saved.join(', ');
    showToast('Exclusions saved (' + saved.length + ')', 'success');
    await loadDnsDiscovery();
  } catch (e) {
    showToast(await errorFrom(e, 'Failed to save the exclusions'), 'error');
  } finally {
    if (target) target.disabled = false;
  }
}

async function dnsClearDiscovery(btn) {
  const target = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  if (target) target.disabled = true;
  try {
    const res = await fetch('/api/dns/discovery', { method: 'DELETE' });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to clear the log'), 'error'); return; }
    showToast('Discovery log cleared', 'success');
    await loadDnsDiscovery();
  } catch (e) {
    showToast(await errorFrom(e, 'Failed to clear the log'), 'error');
  } finally {
    if (target) target.disabled = false;
  }
}

async function loadProtoServers() {
  const list = document.getElementById('protoServersList');
  const hint = document.getElementById('protoServersHint');
  if (!list) return;
  try {
    const res = await fetch('/api/protocol-servers');
    if (!res.ok) { list.innerHTML = '<span class="tb-badge tb-inactive">error</span>'; return; }
    const data = await res.json();
    const servers = data.servers || [];
    if (!servers.length) {
      list.innerHTML = '<span style="font-size:12px;color:var(--text2);">No protocol servers configured. Enable a protocol in config.yaml to manage it here.</span>';
      if (hint) hint.textContent = '';
      return;
    }
    list.innerHTML = servers.map(s => {
      const name = s.name || 'unknown';
      const running = !!s.running;
      const display = PROTO_DISPLAY[name] || name;
      const cls = running ? 'tb-active' : 'tb-inactive';
      return `<span class="tb-badge ${cls}" role="listitem" style="align-items:center;gap:8px;">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${running ? 'var(--green,#34d399)' : 'var(--text2,#9ca3af)'};"></span>
        <strong>${escapeHtml(display)}</strong>
        <span style="opacity:.85;text-transform:lowercase;">${running ? 'running' : 'stopped'}</span>
        ${s.error ? `<span style="color:var(--red);font-size:11px;" title="${escapeHtml(s.error)}">⚠</span>` : ''}
        <button class="tb-btn" onclick="toggleProtoServer('${escapeJs(name)}', ${running}, this)" aria-label="${running ? 'Stop' : 'Start'} ${escapeHtml(display)} server">${running ? '■ Stop' : '▶ Start'}</button>
      </span>`;
    }).join('');
    if (hint) hint.textContent = '';
  } catch(_e) {
    list.innerHTML = '<span class="tb-badge tb-inactive">error</span>';
  }
}

async function toggleProtoServer(name, running, btn) {
  const action = running ? 'stop' : 'start';
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const origText = targetBtn ? targetBtn.textContent : '';
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = running ? 'Stopping...' : 'Starting...'; }
  try {
    const res = await fetch(`/api/protocol-servers/${encodeURIComponent(name)}/${action}`, { method: 'POST' });
    if (!res.ok) { showToast(await errorFrom(res, `Failed to ${action} ${name}`), 'error'); return; }
    showToast(`${(PROTO_DISPLAY[name] || name)} ${running ? 'stopped' : 'started'}`, 'success');
    loadProtoServers();
  } catch(e) { showToast(await errorFrom(e, `Failed to ${action} ${name}`), 'error'); }
  finally { if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = origText; } }
}

// Extract a human-readable message from a failed fetch Response (or an Error).
async function errorFrom(resOrErr, fallback) {
  if (resOrErr instanceof Response) {
    try {
      const data = await resOrErr.clone().json();
      if (data && data.error) return data.error;
    } catch(_) { /* body was not JSON */ }
    return fallback;
  }
  return (resOrErr && resOrErr.message) ? resOrErr.message : fallback;
}

function downloadCaCert() {
  window.open('/api/tls/ca-cert', '_blank', 'noopener');
}

async function downloadFridaScript() {
  try {
    const response = await fetch('/api/tls/frida/script.js');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = 'ride-the-api-frida.js';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('Frida script downloaded', 'success');
  } catch (error) {
    showToast(`Could not download Frida script: ${error.message || error}`, 'error');
  }
}

async function saveTlsConfig(deviceId) {
  const name = document.getElementById('tc-name-' + deviceId).value;
  const vendor = document.getElementById('tc-vendor-' + deviceId).value;
  const passthrough = document.getElementById('tc-passthrough-' + deviceId).checked;
  const pinning = document.getElementById('tc-pinning-' + deviceId).value;
  const bypass = document.getElementById('tc-bypass-' + deviceId).checked;
  const btn = document.getElementById('save-tls-btn-' + deviceId);
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
  try {
    const res = await fetch(`/api/devices/${deviceId}/tls-config`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, vendor, passthrough, pinning_bypass: pinning}),
    });
    if (!res.ok) { showToast('Failed to save config', 'error'); return; }
    // Persist the per-IP bypass to the ip_profiles config, using the device's
    // primary IP when known.
    try {
      const st = await (await fetch(`/api/devices/${deviceId}/stats`)).json();
      const ip = (st.stats && st.stats.primary_ip);
      if (ip) {
        await fetch(`/api/ip-profiles/${encodeURIComponent(ip)}/bypass`, {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({bypass}),
        });
      }
    } catch(_e) { /* IP unknown: bypass stays as configured in config */ }
    showToast('TLS config saved', 'success');
    loadDeviceStats(deviceId);
    loadDevices();
  } catch(e) { showToast('Failed to save config', 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = origText; } }
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

// ── Database fields: standard values, already-used options ───────────────────
// The database fields arrive filled in: the name follows the device name and the
// URL points at the per-device file, so the common case needs no typing. The
// datalists offer what is already in use, so an existing database can be picked
// instead of retyped.

function suggestDatabaseName(deviceId, s) {
  const raw = (s && s.name) ? String(s.name) : '';
  // A device still named after its address ("ip-192-168-1-42") has no human name
  // to follow: fall back to the bare address, which stays unique on disk.
  const base = raw || String(deviceId || 'device').replace(/^ip-/, '');
  return base.replace(/\s+/g, '-').replace(/[^A-Za-z0-9._-]/g, '-').replace(/-{2,}/g, '-').replace(/^[-.]+|[-.]+$/g, '') || 'device';
}

function deviceDatabaseName(deviceId, s) {
  return (s && s.database_name) ? s.database_name : suggestDatabaseName(deviceId, s);
}

function deviceDatabaseUrl(deviceId, s) {
  if (s && s.database_url) return s.database_url;
  return `sqlite+aiosqlite:///./ridebase/devices/${suggestDatabaseName(deviceId, s)}.db`;
}

async function loadDatabaseOptions(deviceId) {
  const names = document.getElementById('dc-db-names-' + deviceId);
  const urls = document.getElementById('dc-db-urls-' + deviceId);
  if (!names && !urls) return;
  try {
    const res = await fetch('/api/databases');
    if (!res.ok) return;
    const data = await res.json();
    const seen = new Set();
    const options = [];
    (data.databases || []).forEach(d => {
      const name = d.database_name || (d.database_url || '').split('/').pop().replace(/\.db$/, '');
      if (name && !seen.has('n:' + name)) {
        seen.add('n:' + name);
        options.push(`<option value="${escapeHtml(name)}"></option>`);
      }
    });
    if (names) names.innerHTML = options.join('');
    if (urls) {
      const urlOptions = (data.databases || [])
        .map(d => d.database_url)
        .filter(Boolean)
        .map(u => `<option value="${escapeHtml(u)}"></option>`);
      urls.innerHTML = urlOptions.join('');
    }
    // Every value above goes through escapeHtml(): the datalist holds text, so
    // markup from a database name cannot become DOM here.
  } catch (e) { /* options are a convenience: the fields already hold valid values */ }
}

async function renameDeviceDatabase(deviceId, btn) {
  const name = (document.getElementById('dc-db-name-' + deviceId) || {}).value || '';
  if (!name) {
    showToast('Provide a database name', 'warning');
    return;
  }
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const origText = targetBtn ? targetBtn.textContent : '';
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = 'Renaming...'; }
  const statusEl = document.getElementById('dc-status-' + deviceId);
  if (statusEl) statusEl.textContent = 'Renaming...';
  try {
    const res = await fetch(`/api/devices/${deviceId}/database/rename`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name}),
    });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to rename database'), 'error'); }
    else {
      const out = await res.json();
      showToast(`Database renamed to ${name}`, 'success');
      // Mirror what the server recorded so the fields keep showing the truth.
      const urlEl = document.getElementById('dc-db-url-' + deviceId);
      if (urlEl && out.database_url) urlEl.value = out.database_url;
    }
    if (statusEl) statusEl.textContent = '';
    loadDeviceStats(deviceId);
  } catch(e) {
    showToast(await errorFrom(e, 'Failed to rename database'), 'error');
    if (statusEl) statusEl.textContent = '';
  } finally {
    if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = origText; }
  }
}

async function assignDeviceDatabase(deviceId, btn) {
  const name = (document.getElementById('dc-db-name-' + deviceId) || {}).value || '';
  const url = (document.getElementById('dc-db-url-' + deviceId) || {}).value || '';
  if (!name && !url) {
    showToast('Provide a database name or URL', 'warning');
    return;
  }
  const targetBtn = btn instanceof HTMLElement ? btn : (btn?.target?.closest('button') || null);
  const origText = targetBtn ? targetBtn.textContent : '';
  if (targetBtn) { targetBtn.disabled = true; targetBtn.textContent = 'Assigning...'; }
  const statusEl = document.getElementById('dc-status-' + deviceId);
  if (statusEl) statusEl.textContent = 'Assigning...';
  try {
    const res = await fetch(`/api/devices/${deviceId}/database`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({database_name: name, database_url: url}),
    });
    if (!res.ok) { showToast(await errorFrom(res, 'Failed to assign database'), 'error'); }
    else { showToast('Database assigned', 'success'); }
    if (statusEl) statusEl.textContent = '';
    loadDeviceStats(deviceId);
  } catch(e) {
    showToast(await errorFrom(e, 'Failed to assign database'), 'error');
    if (statusEl) statusEl.textContent = '';
  } finally {
    if (targetBtn) { targetBtn.disabled = false; targetBtn.textContent = origText; }
  }
}

// ── LLM Context & Template ───────────────────────────────────────────────────

function toggleLLMSection(id) {
  const body = document.getElementById('llm-body-' + id);
  const toggle = document.getElementById('llm-toggle-' + id);
  const header = document.getElementById('llm-header-' + id);
  if (!body) return;
  const collapsed = body.classList.toggle('collapsed');
  toggle.innerHTML = collapsed ? '&#x25B6;' : '&#x25BC;';
  if (header) header.setAttribute('aria-expanded', !collapsed);
}

async function loadLLMProfiles(deviceId) {
  try {
    const res = await fetch('/api/llm/user-profiles');
    if (!res.ok) return;
    const data = await res.json();
    const sel = document.getElementById('profile-select-' + deviceId);
    if (!sel) return;
    const current = sel.value;
    sel.innerHTML = '<option value="default">Default</option>';
    (data.profiles || []).forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.name; opt.textContent = p.name;
      sel.appendChild(opt);
    });
    sel.value = current !== 'default' && [...sel.options].some(o => o.value === current) ? current : 'default';
    onProfileChange(deviceId);
  } catch(e) { /* ignore */ }
}

function onProfileChange(deviceId) {
  const sel = document.getElementById('profile-select-' + deviceId);
  const delBtn = document.getElementById('delete-profile-btn-' + deviceId);
  if (!sel || !delBtn) return;
  delBtn.style.display = sel.value === 'default' ? 'none' : '';
}

async function saveAsNewProfile(deviceId) {
  const name = prompt('Profile name:');
  if (!name || !name.trim()) return;
  const description = prompt('Description (optional):') || '';
  const promptTemplate = prompt('Prompt template (use {vendor}, {device_type}, {pairs}, {device_id}, {context_notes}):');
  if (!promptTemplate) return showToast('Prompt template required', 'error');
  try {
    const res = await fetch('/api/llm/user-profiles', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name.trim(), description, prompt_template: promptTemplate}),
    });
    if (!res.ok) {
      const err = await res.json();
      return showToast(err.error || 'Failed to create profile', 'error');
    }
    showToast('Profile "' + name.trim() + '" created', 'success');
    loadLLMProfiles(deviceId);
  } catch(e) { showToast('Failed to create profile', 'error'); }
}

async function editProfile(deviceId) {
  const sel = document.getElementById('profile-select-' + deviceId);
  if (!sel || sel.value === 'default') return;
  try {
    const res = await fetch('/api/llm/user-profiles/' + encodeURIComponent(sel.value));
    if (!res.ok) return showToast('Failed to load profile', 'error');
    const p = await res.json();
    // Simple inline edit via prompt
    const name = prompt('Profile name:', p.name) || p.name;
    const desc = prompt('Description:', p.description || '') || '';
    const pt = prompt('Prompt template:', p.prompt_template) || p.prompt_template;
    const upRes = await fetch('/api/llm/user-profiles/' + encodeURIComponent(sel.value), {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, description: desc, prompt_template: pt}),
    });
    if (!upRes.ok) return showToast('Failed to update profile', 'error');
    showToast('Profile updated', 'success');
    loadLLMProfiles(deviceId);
  } catch(e) { showToast('Failed to edit profile', 'error'); }
}

async function deleteProfile(deviceId) {
  const sel = document.getElementById('profile-select-' + deviceId);
  if (!sel || sel.value === 'default') return;
  if (!confirm('Delete profile "' + sel.value + '"?')) return;
  try {
    const res = await fetch('/api/llm/user-profiles/' + encodeURIComponent(sel.value), { method: 'DELETE' });
    if (!res.ok) return showToast('Failed to delete profile', 'error');
    showToast('Profile deleted', 'success');
    loadLLMProfiles(deviceId);
  } catch(e) { showToast('Failed to delete profile', 'error'); }
}

// ── LLM Settings (global: which LLM to use + its configuration) ──────────────

function openLLMSettings() {
  lastModalActiveElement = document.activeElement;
  const modal = document.getElementById('llm-settings-modal');
  if (modal) modal.style.display = 'flex';
  const title = document.getElementById('llm-settings-title');
  if (title) title.focus();
  loadLLMSettings();
}

function closeLLMSettings() {
  const modal = document.getElementById('llm-settings-modal');
  if (modal) modal.style.display = 'none';
  if (window.location.hash === '#llm-settings') {
    history.replaceState(null, '', window.location.pathname + window.location.search);
  }
  if (lastModalActiveElement && document.body.contains(lastModalActiveElement) && typeof lastModalActiveElement.focus === 'function') {
    lastModalActiveElement.focus();
  } else {
    const btn = document.getElementById('llmSettingsBtn');
    if (btn) btn.focus();
  }
}

async function loadLLMSettings() {
  try {
    const res = await fetch('/api/llm/settings');
    if (!res.ok) {
      const err = await res.json();
      return showToast(err.error || 'Failed to load LLM settings', 'error');
    }
    const settings = await res.json();
    renderLLMSettings(settings);
  } catch(e) { showToast('Failed to load LLM settings: ' + e.message, 'error'); }
}

function renderLLMSettings(settings) {
  const enabledToggle = document.getElementById('llm-enabled-toggle');
  if (enabledToggle) {
    enabledToggle.checked = !!settings.enabled;
    enabledToggle.setAttribute('aria-checked', !!settings.enabled);
  }
  const defaultSel = document.getElementById('llm-default-profile');
  if (defaultSel) {
    defaultSel.innerHTML = '';
    Object.keys(settings.profiles || {}).forEach(name => {
      const opt = document.createElement('option');
      opt.value = name; opt.textContent = name;
      defaultSel.appendChild(opt);
    });
    defaultSel.value = settings.default_profile || Object.keys(settings.profiles || {})[0] || '';
  }
  // Purpose → profile bindings (empty = fall back to the default profile).
  const taskProfiles = settings.task_profiles || {};
  ['correlation', 'flush', 'learning_context'].forEach(task => {
    const sel = document.getElementById('llm-task-' + task);
    if (!sel) return;
    sel.innerHTML = '';
    const none = document.createElement('option');
    none.value = '';
    none.textContent = '— use default —';
    sel.appendChild(none);
    Object.keys(settings.profiles || {}).forEach(name => {
      const opt = document.createElement('option');
      opt.value = name; opt.textContent = name;
      sel.appendChild(opt);
    });
    sel.value = taskProfiles[task] || '';
  });
  renderLLMProfileCards(settings);
}

let savedLLMSettings = null;

window.updateProfileStatusPill = function (safeKey, enabled) {
  const pill = document.getElementById('lpc-status-pill-' + safeKey);
  if (pill) {
    pill.classList.toggle('active', !!enabled);
    pill.textContent = enabled ? 'Active' : 'Disabled';
  }
};

function updateLLMCardDirty(card) {
  if (!card || !savedLLMSettings) return;
  const saved = savedLLMSettings.profiles && savedLLMSettings.profiles[card.dataset.name];
  const values = {
    base_url: card.querySelector('.lpc-base-url').value,
    model_id: card.querySelector('.lpc-model-id').value,
    api_key: card.querySelector('.lpc-api-key').value,
    timeout: card.querySelector('.lpc-timeout').value,
    max_retries: card.querySelector('.lpc-max-retries').value,
    prompt_template: card.querySelector('.lpc-prompt-template').value,
    enabled: card.querySelector('.lpc-enabled').checked,
  };
  const dirty = !saved || Object.keys(values).some(k => String(values[k]) !== String(saved[k]));
  card.classList.toggle('lpc-dirty', dirty);
  const badge = card.querySelector('.lpc-unsaved-badge');
  if (badge) badge.style.display = dirty ? 'inline-flex' : 'none';
}

function renderLLMProfileCards(settings) {
  const list = document.getElementById('llm-profiles-list');
  if (!list) return;
  list.innerHTML = '';
  // Snapshot of the last-saved state, used to flag cards whose form values
  // differ from what is persisted (the Test button probes UNSAVED values, so
  // a passed test does NOT mean the change is live until Save is pressed).
  savedLLMSettings = settings;
  if (!list.dataset.llmDirtyListener) {
    list.dataset.llmDirtyListener = '1';
    list.addEventListener('input', (ev) => {
      const card = ev.target.closest('.llm-profile-card');
      if (card) updateLLMCardDirty(card);
    });
  }
  const profiles = settings.profiles || {};
  Object.keys(profiles).forEach((name, idx) => {
    const p = profiles[name];
    const card = document.createElement('div');
    card.className = 'llm-profile-card';
    card.dataset.name = name;
    const safeKey = 'p' + idx + '-' + String(name).replace(/[^a-zA-Z0-9_-]/g, '_');
    card.innerHTML = `
      <div class="lpc-head">
        <div class="lpc-title-row">
          <div class="lpc-name-group">
            <svg class="lpc-icon" aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="16" y1="16" x2="16.01" y2="16"/></svg>
            <span class="lpc-name">${escapeHtml(name)}</span>
            <span class="lpc-status-pill ${p.enabled ? 'active' : ''}" id="lpc-status-pill-${safeKey}">${p.enabled ? 'Active' : 'Disabled'}</span>
            <span class="lpc-unsaved-badge" style="display:none;" title="Has unsaved changes">unsaved</span>
          </div>
        </div>
        <div class="lpc-actions">
          <label for="lpc-enabled-${safeKey}" class="lpc-enable" title="Toggle active status for profile ${escapeHtml(name)}">
            <span class="lpc-enable-label">Enabled</span>
            <span class="switch-wrap">
              <input type="checkbox" id="lpc-enabled-${safeKey}" class="switch lpc-enabled" role="switch" aria-checked="${!!p.enabled}" ${p.enabled ? 'checked' : ''} onchange="this.setAttribute('aria-checked', this.checked); updateProfileStatusPill('${safeKey}', this.checked);">
            </span>
          </label>
          <button type="button" class="btn btn-test-profile" onclick="testLLMProfile('${escapeJs(name)}')" aria-label="Test ${escapeHtml(name)} LLM connection">
            <svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            <span class="btn-label">Test</span>
            <span class="btn-glow-pill" aria-hidden="true">⚡</span>
          </button>
          <button type="button" class="btn btn-danger btn-delete-profile" onclick="deleteLLMProfile('${escapeJs(name)}')" aria-label="Delete ${escapeHtml(name)} LLM profile">
            <svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            <span>Delete</span>
            <span class="btn-glow-pill" aria-hidden="true">&times;</span>
          </button>
        </div>
      </div>
      <div class="lpc-fields">
        <div class="lpc-field">
          <label for="lpc-base-url-${safeKey}">Base URL</label>
          <input type="text" id="lpc-base-url-${safeKey}" class="lpc-base-url" value="${escapeHtml(p.base_url || '')}" placeholder="https://api.openai.com/v1">
        </div>
        <div class="lpc-field">
          <label for="lpc-model-id-${safeKey}">Model ID</label>
          <input type="text" id="lpc-model-id-${safeKey}" class="lpc-model-id" value="${escapeHtml(p.model_id || '')}" placeholder="gpt-4o-mini">
        </div>
        <div class="lpc-field full">
          <label for="lpc-api-key-${safeKey}">API Key</label>
          <div class="pw-wrap">
            <input type="password" id="lpc-api-key-${safeKey}" class="lpc-api-key" value="${escapeHtml(p.api_key || '')}" autocomplete="off" placeholder="sk-...">
            <button type="button" class="pw-toggle-btn" id="toggle-lpc-api-key-${safeKey}" onclick="toggleLpcKeyVisibility('${safeKey}')" aria-label="Show API key" title="Show API key">
              <svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
              </svg>
            </button>
          </div>
        </div>
        <div class="lpc-field">
          <label for="lpc-timeout-${safeKey}">Timeout (s)</label>
          <input type="number" id="lpc-timeout-${safeKey}" class="lpc-timeout" value="${escapeHtml(p.timeout ?? 30)}" min="1">
        </div>
        <div class="lpc-field">
          <label for="lpc-max-retries-${safeKey}">Max Retries</label>
          <input type="number" id="lpc-max-retries-${safeKey}" class="lpc-max-retries" value="${escapeHtml(p.max_retries ?? 2)}" min="0">
        </div>
        <div class="lpc-field full">
          <label for="lpc-prompt-template-${safeKey}">Prompt Template</label>
          <textarea id="lpc-prompt-template-${safeKey}" class="lpc-prompt-template" placeholder="Custom system instructions or response schema...">${escapeHtml(p.prompt_template || '')}</textarea>
        </div>
      </div>`;
    list.appendChild(card);
  });
}

function onLLMDefaultChange() {
  // No-op hook; the default profile is read at save time.
}

function addLLMProfile() {
  const list = document.getElementById('llm-profiles-list');
  if (!list) return;
  const name = prompt('New profile name:');
  if (!name || !name.trim()) return;
  const card = document.createElement('div');
  card.className = 'llm-profile-card';
  card.dataset.name = name.trim();
  const safeKey = 'new-' + Date.now() + '-' + String(name.trim()).replace(/[^a-zA-Z0-9_-]/g, '_');
  card.innerHTML = `
    <div class="lpc-head">
      <div class="lpc-title-row">
        <div class="lpc-name-group">
          <svg class="lpc-icon" aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="16" y1="16" x2="16.01" y2="16"/></svg>
          <span class="lpc-name">${escapeHtml(name.trim())}</span>
          <span class="lpc-status-pill active" id="lpc-status-pill-${safeKey}">Active</span>
          <span class="lpc-unsaved-badge" style="display:none;" title="Has unsaved changes">unsaved</span>
        </div>
      </div>
      <div class="lpc-actions">
        <label for="lpc-enabled-${safeKey}" class="lpc-enable" title="Toggle active status for profile ${escapeHtml(name.trim())}">
          <span class="lpc-enable-label">Enabled</span>
          <span class="switch-wrap">
            <input type="checkbox" id="lpc-enabled-${safeKey}" class="switch lpc-enabled" role="switch" aria-checked="true" checked onchange="this.setAttribute('aria-checked', this.checked); updateProfileStatusPill('${safeKey}', this.checked);">
          </span>
        </label>
        <button type="button" class="btn btn-test-profile" onclick="testLLMProfile('${escapeJs(name.trim())}')" aria-label="Test ${escapeHtml(name.trim())} LLM connection">
          <svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
          <span class="btn-label">Test</span>
          <span class="btn-glow-pill" aria-hidden="true">⚡</span>
        </button>
        <button type="button" class="btn btn-danger btn-delete-profile" onclick="deleteLLMProfile('${escapeJs(name.trim())}')" aria-label="Delete ${escapeHtml(name.trim())} LLM profile">
          <svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          <span>Delete</span>
          <span class="btn-glow-pill" aria-hidden="true">&times;</span>
        </button>
      </div>
    </div>
    <div class="lpc-fields">
      <div class="lpc-field">
        <label for="lpc-base-url-${safeKey}">Base URL</label>
        <input type="text" id="lpc-base-url-${safeKey}" class="lpc-base-url" value="https://api.openai.com/v1" placeholder="https://api.openai.com/v1">
      </div>
      <div class="lpc-field">
        <label for="lpc-model-id-${safeKey}">Model ID</label>
        <input type="text" id="lpc-model-id-${safeKey}" class="lpc-model-id" value="gpt-4o-mini" placeholder="gpt-4o-mini">
      </div>
      <div class="lpc-field full">
        <label for="lpc-api-key-${safeKey}">API Key</label>
        <div class="pw-wrap">
          <input type="password" id="lpc-api-key-${safeKey}" class="lpc-api-key" autocomplete="off" placeholder="sk-...">
          <button type="button" class="pw-toggle-btn" id="toggle-lpc-api-key-${safeKey}" onclick="toggleLpcKeyVisibility('${safeKey}')" aria-label="Show API key" title="Show API key">
            <svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
            </svg>
          </button>
        </div>
      </div>
      <div class="lpc-field">
        <label for="lpc-timeout-${safeKey}">Timeout (s)</label>
        <input type="number" id="lpc-timeout-${safeKey}" class="lpc-timeout" value="30" min="1">
      </div>
      <div class="lpc-field">
        <label for="lpc-max-retries-${safeKey}">Max Retries</label>
        <input type="number" id="lpc-max-retries-${safeKey}" class="lpc-max-retries" value="2" min="0">
      </div>
      <div class="lpc-field full">
        <label for="lpc-prompt-template-${safeKey}">Prompt Template</label>
        <textarea id="lpc-prompt-template-${safeKey}" class="lpc-prompt-template" placeholder="Custom system instructions or response schema..."></textarea>
      </div>
    </div>`;
  list.appendChild(card);
  // A brand-new profile is by definition not saved yet.
  updateLLMCardDirty(card);
  // Make the new profile selectable as default.
  const defaultSel = document.getElementById('llm-default-profile');
  if (defaultSel) {
    const opt = document.createElement('option');
    opt.value = name.trim(); opt.textContent = name.trim();
    defaultSel.appendChild(opt);
  }
}

function deleteLLMProfile(name) {
  if (!confirm('Delete LLM profile "' + name + '"?')) return;
  const list = document.getElementById('llm-profiles-list');
  if (!list) return;
  const card = list.querySelector('.llm-profile-card[data-name="' + name + '"]');
  if (card) card.remove();
  const defaultSel = document.getElementById('llm-default-profile');
  if (defaultSel) {
    const opt = [...defaultSel.options].find(o => o.value === name);
    if (opt) opt.remove();
    if (defaultSel.value === name) defaultSel.value = defaultSel.options[0] ? defaultSel.options[0].value : '';
  }
}

async function testLLMProfile(name) {
  // Send a "hello" completion to the provider using this profile's CURRENT
  // form values (which need not be saved yet). A response = test passed.
  const list = document.getElementById('llm-profiles-list');
  if (!list) return;
  const card = list.querySelector('.llm-profile-card[data-name="' + name + '"]');
  if (!card) return;
  const btn = card.querySelector('.btn-test-profile') || card.querySelector('.lpc-actions .btn');
  const span = btn ? (btn.querySelector('.btn-label') || btn.querySelector('span') || btn) : null;
  const origText = span ? span.textContent : '';
  if (btn) { btn.disabled = true; if (span) span.textContent = 'Testing…'; }
  const body = {
    // The profile name lets the server use the stored key when the key field
    // still shows the masked value (stored keys are never sent to the browser).
    name: name,
    base_url: card.querySelector('.lpc-base-url').value,
    model_id: card.querySelector('.lpc-model-id').value,
    api_key: card.querySelector('.lpc-api-key').value,
    timeout: parseInt(card.querySelector('.lpc-timeout').value, 10) || 30,
    max_retries: parseInt(card.querySelector('.lpc-max-retries').value, 10) || 2,
  };
  try {
    const res = await fetch('/api/llm/profiles/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await errorFrom(res, 'Test failed');
      showToast(`${name}: ${err}`, 'error');
      return;
    }
    const data = await res.json();
    const baseMsg = data.message || 'Test passed: got a response';
    const unsaved = card.classList.contains('lpc-dirty');
    showToast(
      `${name}: ${baseMsg}${unsaved ? ' — press Save to apply' : ''}`,
      unsaved ? 'warning' : 'success'
    );
  } catch(e) {
    showToast(`${name}: ${e.message || 'Test failed'}`, 'error');
  } finally {
    if (btn) { btn.disabled = false; if (span) span.textContent = origText; }
  }
}

function collectLLMSettings() {
  const enabled = document.getElementById('llm-enabled-toggle').checked;
  const defaultProfile = document.getElementById('llm-default-profile').value;
  const profiles = {};
  const list = document.getElementById('llm-profiles-list');
  if (list) {
    list.querySelectorAll('.llm-profile-card').forEach(card => {
      const name = card.dataset.name;
      profiles[name] = {
        base_url: card.querySelector('.lpc-base-url').value,
        api_key: card.querySelector('.lpc-api-key').value,
        model_id: card.querySelector('.lpc-model-id').value,
        prompt_template: card.querySelector('.lpc-prompt-template').value,
        enabled: card.querySelector('.lpc-enabled').checked,
        timeout: parseInt(card.querySelector('.lpc-timeout').value, 10) || 30,
        max_retries: parseInt(card.querySelector('.lpc-max-retries').value, 10) || 2,
      };
    });
  }
  // Purpose → profile bindings; an empty selection is omitted so the purpose
  // stays unbound and falls back to the device/default profile.
  const task_profiles = {};
  ['correlation', 'flush', 'learning_context'].forEach(task => {
    const sel = document.getElementById('llm-task-' + task);
    if (sel && sel.value) task_profiles[task] = sel.value;
  });
  return { enabled, default_profile: defaultProfile, profiles, task_profiles };
}

async function saveLLMSettings() {
  const settings = collectLLMSettings();
  if (!settings.default_profile || !settings.profiles[settings.default_profile]) {
    return showToast('A valid default profile is required', 'error');
  }
  try {
    const res = await fetch('/api/llm/settings', {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(settings),
    });
    if (!res.ok) {
      const err = await res.json();
      return showToast(err.error || 'Failed to save LLM settings', 'error');
    }
    showToast('LLM settings saved', 'success');
    // The form values are now the persisted truth: clear the dirty badges.
    savedLLMSettings = settings;
    document.querySelectorAll('#llm-profiles-list .llm-profile-card').forEach(card => {
      card.classList.remove('lpc-dirty');
      const badge = card.querySelector('.lpc-unsaved-badge');
      if (badge) badge.style.display = 'none';
    });
    closeLLMSettings();
    // Refresh the per-device profile dropdowns so they reflect the new set.
    document.querySelectorAll('[id^="profile-select-"]').forEach(sel => {
      const id = sel.id.replace('profile-select-', '');
      loadLLMProfiles(id);
    });
  } catch(e) { showToast('Failed to save LLM settings: ' + e.message, 'error'); }
}

async function loadContextNotes(deviceId) {
  try {
    const res = await fetch(`/api/devices/${deviceId}/context`);
    if (!res.ok) return;
    const data = await res.json();
    const ta = document.getElementById('context-notes-' + deviceId);
    if (ta) ta.value = data.context_notes || '';
  } catch(e) { /* ignore */ }
}

async function saveContextNotes(deviceId) {
  const ta = document.getElementById('context-notes-' + deviceId);
  if (!ta) return;
  const btn = document.getElementById('save-notes-btn-' + deviceId);
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
  try {
    const res = await fetch(`/api/devices/${deviceId}/context`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({context_notes: ta.value}),
    });
    if (!res.ok) return showToast('Failed to save notes', 'error');
    showToast('Context notes saved', 'success');
  } catch(e) { showToast('Failed to save notes', 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = origText; } }
}

async function loadBufferEntries(deviceId) {
  const container = document.getElementById('buffer-list-' + deviceId);
  const countSpan = document.getElementById('buf-count-' + deviceId);
  if (!container) return;
  try {
    const res = await fetch(`/api/devices/${deviceId}/buffer`);
    if (!res.ok) {
      container.innerHTML = '<div style="color:var(--text2);font-size:12px;text-align:center;padding:12px;">Failed to load buffer</div>';
      return;
    }
    const data = await res.json();
    const entries = data.entries || [];
    if (countSpan) countSpan.textContent = entries.length + ' entries (' + (data.total_size_bytes / 1024).toFixed(1) + ' KB)';
    if (entries.length === 0) {
      container.innerHTML = '<div style="color:var(--text2);font-size:12px;text-align:center;padding:12px;">No buffer entries</div>';
      return;
    }
    container.innerHTML = entries.map(e => {
          const method = e.pair?.method || '';
          const path = e.pair?.path || '';
          const status = e.pair?.response_status || '';
          const reqPreview = (e.pair?.request_body || '').slice(0, 80);
          const resPreview = (e.pair?.response_body || '').slice(0, 80);
          const reqFull = JSON.stringify(e.pair?.request_body || '', null, 2).slice(0, 300);
          const resFull = JSON.stringify(e.pair?.response_body || '', null, 2).slice(0, 300);
    const ts = e.timestamp ? new Date(e.timestamp).toLocaleString() : '';
          return `<div class="buffer-entry" id="buf-entry-${escapeJs(deviceId)}-${e.id}" tabindex="0" role="button" aria-expanded="false" aria-controls="buf-detail-${escapeJs(deviceId)}-${e.id}" onclick="toggleBufDetail('${escapeJs(deviceId)}', ${e.id})" onkeydown="if(event.key==='Enter'||event.key===' '){if(event.target===this||event.target.classList.contains('be-info')||event.target.classList.contains('be-pair')||event.target.classList.contains('be-summary')){event.preventDefault();toggleBufDetail('${escapeJs(deviceId)}', ${e.id});}}">
                  <input type="checkbox" id="buf-chk-${escapeJs(deviceId)}-${e.id}" aria-label="Select buffer entry ${escapeHtml(method)} ${escapeHtml(path)}" onclick="event.stopPropagation()" onchange="updateSelectedCount('${escapeJs(deviceId)}')">
            <div class="be-info">
                    <div class="be-pair"><span style="color:var(--accent)">${escapeHtml(method)}</span> ${escapeHtml(path)}</div>
                    <div class="be-summary">&#x2192; ${escapeHtml(status)} &middot; ${(e.size || 0) / 1024} KB${ts ? ' &middot; ' + ts : ''}</div>
            </div>
            <button class="be-delete" onclick="event.stopPropagation();deleteBufferEntry('${escapeJs(deviceId)}', ${e.id})" title="Delete entry" aria-label="Delete entry ${escapeHtml(method)} ${escapeHtml(path)}">&times;</button>
          </div>
          <div class="buffer-entry-detail" id="buf-detail-${escapeJs(deviceId)}-${e.id}">
    <b>Request:</b> ${escapeHtml(reqFull)}
    <b>Response:</b> ${escapeHtml(resFull)}
          </div>`;
        }).join('');
  } catch(e) {
    container.innerHTML = '<div style="color:var(--red);font-size:12px;text-align:center;padding:12px;">Error loading buffer</div>';
  }
}

function toggleBufDetail(deviceId, entryId) {
  const detail = document.getElementById('buf-detail-' + deviceId + '-' + entryId);
  const entry = document.getElementById('buf-entry-' + deviceId + '-' + entryId);
  if (detail) {
    const isOpen = detail.classList.toggle('open');
    if (entry) entry.setAttribute('aria-expanded', isOpen);
  }
}

function toggleSelectAll(deviceId) {
  const checked = document.getElementById('select-all-buf-' + deviceId).checked;
  const container = document.getElementById('buffer-list-' + deviceId);
  if (!container) return;
  container.querySelectorAll('.buffer-entry input[type="checkbox"]').forEach(cb => cb.checked = checked);
  updateSelectedCount(deviceId);
}

function getSelectedPairIds(deviceId) {
  const ids = [];
  const container = document.getElementById('buffer-list-' + deviceId);
  if (!container) return ids;
  container.querySelectorAll('.buffer-entry input[type="checkbox"]').forEach(cb => {
    if (cb.checked) {
      const id = parseInt(cb.id.split('-').pop());
      if (!isNaN(id)) ids.push(id);
    }
  });
  return ids;
}

function updateSelectedCount(deviceId) {
  const container = document.getElementById('buffer-list-' + deviceId);
  if (container) {
    container.querySelectorAll('.buffer-entry').forEach(card => {
      const cb = card.querySelector('input[type="checkbox"]');
      if (cb) {
        card.classList.toggle('be-checked', cb.checked);
      }
    });
  }
  const ids = getSelectedPairIds(deviceId);
  const previewBtn = document.getElementById('preview-btn-' + deviceId);
  const sendBtn = document.getElementById('send-btn-' + deviceId);
  if (previewBtn) previewBtn.textContent = ids.length ? '\u{1F50D} Preview (' + ids.length + ')' : '\u{1F50D} Preview & Edit';
  if (sendBtn) sendBtn.textContent = ids.length ? '\u{27A4} Send (' + ids.length + ')' : '\u{27A4} Send to LLM';
}

async function deleteBufferEntry(deviceId, entryId) {
  try {
    const res = await fetch(`/api/devices/${deviceId}/buffer/${entryId}`, { method: 'DELETE' });
    if (!res.ok) return showToast('Failed to delete entry', 'error');
    showToast('Entry deleted', 'success');
    loadBufferEntries(deviceId);
    updateSelectedCount(deviceId);
  } catch(e) { showToast('Failed to delete entry', 'error'); }
}

async function previewAnalysis(deviceId) {
  const pairIds = getSelectedPairIds(deviceId);
  if (pairIds.length === 0) {
    showToast('Select at least one buffer entry', 'warning');
    return;
  }
  const notes = (document.getElementById('context-notes-' + deviceId) || {}).value || '';
  const btn = document.getElementById('preview-btn-' + deviceId);
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }
  showToast('Running LLM analysis...', 'info');
  try {
    const res = await fetch(`/api/devices/${deviceId}/llm/preview`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pair_ids: pairIds, context_notes: notes}),
    });
    if (!res.ok) {
      const err = await res.json();
      return showToast(err.error || 'Analysis failed', 'error');
    }
    const data = await res.json();
    const box = document.getElementById('analysis-box-' + deviceId);
    const ta = document.getElementById('analysis-text-' + deviceId);
    if (box) box.style.display = '';
    if (ta) ta.value = JSON.stringify(data.analysis || data, null, 2);
    showToast('Preview ready — review and commit or discard', 'success');
  } catch(e) { showToast('Analysis failed: ' + e.message, 'error'); }
  finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

function formatAnalysisJson(deviceId) {
  const ta = document.getElementById('analysis-text-' + deviceId);
  if (!ta || !ta.value.trim()) return;
  try {
    const parsed = JSON.parse(ta.value);
    ta.value = JSON.stringify(parsed, null, 2);
    showToast('Analysis JSON formatted', 'success');
  } catch (err) {
    showToast('Invalid JSON in LLM Analysis Preview: ' + err.message, 'error');
  }
}

function copyAnalysisJson(deviceId) {
  const ta = document.getElementById('analysis-text-' + deviceId);
  if (!ta || !ta.value.trim()) return;
  copyToClipboard(ta.value, 'LLM Analysis Preview');
}

function discardPreview(deviceId) {
  const box = document.getElementById('analysis-box-' + deviceId);
  if (box) box.style.display = 'none';
}

async function commitPatterns(deviceId) {
  const ta = document.getElementById('analysis-text-' + deviceId);
  if (ta && ta.value.trim()) {
    try {
      JSON.parse(ta.value);
    } catch (err) {
      showToast('Invalid JSON in LLM Analysis Preview: ' + err.message, 'error');
      return;
    }
  }
  const pairIds = getSelectedPairIds(deviceId);
  const notes = (document.getElementById('context-notes-' + deviceId) || {}).value || '';
  const box = document.getElementById('analysis-box-' + deviceId);
  const btn = box ? box.querySelector('button.btn-primary') : null;
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Committing...'; }
  showToast('Saving patterns...', 'info');
  try {
    const res = await fetch(`/api/devices/${deviceId}/llm/flush`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pair_ids: pairIds, context_notes: notes}),
    });
    if (!res.ok) {
      const err = await res.json();
      return showToast(err.error || 'Failed to commit', 'error');
    }
    const data = await res.json();
    discardPreview(deviceId);
    showToast(data.patterns_count + ' patterns saved', 'success');
    loadBufferEntries(deviceId);
    updateSelectedCount(deviceId);
    loadDevices();
  } catch(e) { showToast('Failed to commit: ' + e.message, 'error'); }
  finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function sendToLLM(deviceId) {
  const pairIds = getSelectedPairIds(deviceId);
  const notes = (document.getElementById('context-notes-' + deviceId) || {}).value || '';
  if (pairIds.length === 0) {
    showToast('Select at least one buffer entry', 'warning');
    return;
  }
  if (!confirm('Send ' + pairIds.length + ' selected entries to LLM for decoding?')) return;
  const btn = document.getElementById('send-btn-' + deviceId);
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Sending...'; }
  showToast('Sending to LLM...', 'info');
  try {
    const res = await fetch(`/api/devices/${deviceId}/llm/flush`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pair_ids: pairIds, context_notes: notes}),
    });
    if (!res.ok) {
      const err = await res.json();
      return showToast(err.error || 'LLM processing failed', 'error');
    }
    const data = await res.json();
    showToast(data.patterns_count + ' patterns learned from ' + data.pairs_count + ' pairs', 'success');
    loadBufferEntries(deviceId);
    updateSelectedCount(deviceId);
    loadDevices();
  } catch(e) { showToast('LLM processing failed: ' + e.message, 'error'); }
  finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}
