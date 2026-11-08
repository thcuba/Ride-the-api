/**
 * Ride the API — Common UI utilities
 * Shared across dashboard, config, patterns, and options pages.
 */

// ── Toast notification system ────────────────────────────────────────────────
function showToast(msg, type) {
  let c = document.getElementById('toasts');
  if (!c) {
    c = document.createElement('div');
    c.id = 'toasts';
    c.className = 'toasts-container';
    document.body.appendChild(c);
  }
  const t = document.createElement('div');
  const toastType = type || 'info';
  t.className = 'toast ' + toastType + ' show';
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
  setTimeout(() => {
    t.style.opacity = '0';
    t.style.transition = 'opacity .3s';
    setTimeout(() => t.remove(), 300);
  }, 3500);
}
window.showToast = showToast;

// ── Theme management ─────────────────────────────────────────────────────────
function updateThemeUI(theme) {
  const sel = document.getElementById('themeSelect');
  const icon = document.getElementById('themeIcon');
  const isLight = theme === 'light';
  if (sel) {
    sel.setAttribute('aria-label', 'Colour theme, currently ' + (isLight ? 'Light' : 'Dark'));
    sel.value = isLight ? 'light' : 'dark';
  }
  if (icon) {
    icon.innerHTML = isLight
      ? '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>'
      : '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';
  }
}
window.updateThemeUI = updateThemeUI;

function setTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  try {
    localStorage.setItem('rta-theme', theme);
  } catch (e) {}
  updateThemeUI(theme);
  showToast('Theme updated to ' + (theme.charAt(0).toUpperCase() + theme.slice(1)), 'info');
}
window.setTheme = setTheme;

// ── Copy to clipboard helper ─────────────────────────────────────────────────
function copyToClipboard(text, onSuccess, onError) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(onSuccess).catch(onError || (() => {}));
  } else {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if (ok) {
        if (onSuccess) onSuccess();
      } else if (onError) onError();
    } catch (e) {
      if (onError) onError(e);
    }
  }
}
window.copyToClipboard = copyToClipboard;

// ── Formatting helpers ───────────────────────────────────────────────────────
function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return (bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
}
window.formatBytes = formatBytes;

function escapeHtml(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
window.escapeHtml = escapeHtml;
