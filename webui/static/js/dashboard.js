/**
 * Ride the API — Dashboard Orchestrator
 * Core entrypoint, global helpers, theme & buffer toggle, modal management, and initialization.
 */

// Focus target restored when the LLM settings modal closes. Declared at the top of
// the script on purpose: the deep link /#llm-settings below calls
// openLLMSettings() while the document is still parsing, and a let declared
// further down would be in its temporal dead zone -> ReferenceError, the modal
// never opening (the toolbar button and the nav pill never hit this because they
// run on click, after parsing).
let lastModalActiveElement = null;

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

// ── Clipboard & Toast (shared from common.js) ───────────────────────────────
function fallbackCopyText(text, onSuccess, onError) {
  return window.fallbackCopyText ? window.fallbackCopyText(text, onSuccess, onError) : undefined;
}

function copyToClipboard(text, label) {
  return window.copyToClipboard ? window.copyToClipboard(text, label) : undefined;
}

function showToast(msg, type) {
  return window.showToast ? window.showToast(msg, type) : undefined;
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
    const meta = document.querySelector('meta[name="color-scheme"]');
    if (meta) meta.content = next;
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



// Global exports for inline HTML event handlers
window.rateClass = rateClass;
window.rateColor = rateColor;
window.captureNote = captureNote;
window.modeBadge = modeBadge;
window.shortId = shortId;
window.escapeHtml = escapeHtml;
window.escapeJs = escapeJs;
window.deviceIcon = deviceIcon;
window.fallbackCopyText = fallbackCopyText;
window.copyToClipboard = copyToClipboard;
window.showToast = showToast;
window.errorFrom = errorFrom;
window.updateThemeToggleButton = updateThemeToggleButton;
window.toggleTheme = toggleTheme;
window.toggleRamBuffer = toggleRamBuffer;
window.loadRamBufferSetting = loadRamBufferSetting;
window.openLLMSettings = openLLMSettings;
window.closeLLMSettings = closeLLMSettings;
