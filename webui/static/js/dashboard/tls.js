/**
 * Ride the API — Dashboard TLS & Protocol Servers Module
 * TLS MITM ports bar, CA certificates, Frida script, DNS discovery, protocol servers.
 */

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
  const ok = window.showConfirm
    ? await showConfirm({
        title: 'Delete Certificate',
        message: 'Delete imported certificate for "' + hostname + '"? It will revert to the auto-generated cert.',
        confirmText: 'Delete Certificate',
        danger: true
      })
    : confirm('Delete imported certificate for "' + hostname + '"? It will revert to the auto-generated cert.');
  if (!ok) return;
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



// Global exports for inline HTML event handlers
window.loadVendors = loadVendors;
window.loadTlsBar = loadTlsBar;
window.pickCertFiles = pickCertFiles;
window.loadTlsCerts = loadTlsCerts;
window.uploadCert = uploadCert;
window.deleteCert = deleteCert;
window.loadProtoServers = loadProtoServers;
window.toggleProtoServer = toggleProtoServer;
window.loadDnsDiscovery = loadDnsDiscovery;
window.dnsIntercept = dnsIntercept;
window.dnsUnintercept = dnsUnintercept;
window.dnsSetInterceptAll = dnsSetInterceptAll;
window.dnsSaveExclusions = dnsSaveExclusions;
window.dnsClearDiscovery = dnsClearDiscovery;
window.downloadCaCert = downloadCaCert;
window.downloadFridaScript = downloadFridaScript;
window.saveTlsConfig = saveTlsConfig;
