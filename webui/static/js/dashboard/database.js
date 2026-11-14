/**
 * Ride the API — Dashboard Database Module
 * Device database configuration, renaming, and assignment.
 */

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



// Global exports for inline HTML event handlers
window.suggestDatabaseName = suggestDatabaseName;
window.deviceDatabaseName = deviceDatabaseName;
window.deviceDatabaseUrl = deviceDatabaseUrl;
window.loadDatabaseOptions = loadDatabaseOptions;
window.renameDeviceDatabase = renameDeviceDatabase;
window.assignDeviceDatabase = assignDeviceDatabase;
