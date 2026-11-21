/**
 * Ride the API — Configuration Editor Orchestrator
 * Main entrypoint for config.html: binds global keyboard navigation, unload protection, and initializes config loading.
 */

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ── Global event handlers ───────────────────────────────────────────────────

// Warn before navigating away if there are unsaved edits
window.addEventListener('beforeunload', (event) => {
  if (window.dirty && window.dirty.size > 0) {
    event.preventDefault();
    event.returnValue = '';
  }
});

// Global keyboard shortcuts (Ctrl+S / Cmd+S save, Escape hide tooltip, / or Ctrl+K search)
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    if (typeof hideTooltip === 'function') hideTooltip();
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
    event.preventDefault();
    if (window.dirty && window.dirty.size > 0) {
      if (typeof saveConfig === 'function') saveConfig();
    } else {
      if (typeof showToast === 'function') showToast('No unsaved changes', 'info');
    }
  } else if ((!event.ctrlKey && !event.metaKey && !event.altKey && event.key === '/') || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k')) {
    const a = document.activeElement;
    if (!a || (!['INPUT', 'TEXTAREA', 'SELECT'].includes(a.tagName) && !a.isContentEditable)) {
      const s = document.getElementById('sidebarSearch');
      if (s) {
        event.preventDefault();
        s.focus();
        s.select();
      }
    }
  }
});

// Global exports
window.escapeHtml = escapeHtml;

// Initial application load
if (typeof loadConfig === 'function') {
  loadConfig();
}
