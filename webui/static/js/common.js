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
    c.className = 'toast-container';
    c.style.zIndex = '10050';
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
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'close-btn toast-close';
  closeBtn.setAttribute('aria-label', 'Dismiss notification');
  closeBtn.innerHTML = '&times;';
  let timer;
  const dismiss = () => {
    clearTimeout(timer);
    t.style.opacity = '0';
    t.style.transition = 'opacity .25s ease';
    setTimeout(() => t.remove(), 250);
  };
  closeBtn.addEventListener('click', dismiss);
  t.appendChild(iconSpan);
  t.appendChild(textSpan);
  t.appendChild(closeBtn);
  c.appendChild(t);
  const startTimer = () => { timer = setTimeout(dismiss, 4000); };
  t.addEventListener('mouseenter', () => clearTimeout(timer));
  t.addEventListener('mouseleave', startTimer);
  t.addEventListener('focusin', () => clearTimeout(timer));
  t.addEventListener('focusout', startTimer);
  startTimer();
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
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) meta.content = theme;
  try {
    localStorage.setItem('rta-theme', theme);
  } catch (e) {}
  updateThemeUI(theme);
  showToast('Theme updated to ' + (theme.charAt(0).toUpperCase() + theme.slice(1)), 'info');
}
window.setTheme = setTheme;

// ── Copy to clipboard helper ─────────────────────────────────────────────────
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
    if (ok) {
      if (onSuccess) onSuccess();
    } else if (onError) onError();
  } catch (e) {
    if (onError) onError(e);
  }
}
window.fallbackCopyText = fallbackCopyText;

function copyToClipboard(text, onSuccessOrLabel, onError) {
  if (!text) return;
  const isLabel = typeof onSuccessOrLabel === 'string';
  const label = isLabel ? onSuccessOrLabel : 'Text';
  const onSuccess = typeof onSuccessOrLabel === 'function'
    ? onSuccessOrLabel
    : () => showToast((label || 'Text') + ' copied to clipboard', 'success');
  const onErr = typeof onError === 'function'
    ? onError
    : () => showToast('Failed to copy ' + (label || 'text').toLowerCase(), 'error');

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(onSuccess).catch(() => {
      fallbackCopyText(text, onSuccess, onErr);
    });
  } else {
    fallbackCopyText(text, onSuccess, onErr);
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

function escapeJs(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/"/g, '&quot;')
    .replace(/</g, '\\x3C')
    .replace(/>/g, '\\x3E');
}
window.escapeJs = escapeJs;

// ── Modern Dialogs (showConfirm & showPrompt) ────────────────────────────────
function showConfirm(optionsOrMessage) {
  return new Promise((resolve) => {
    if (typeof document === 'undefined' || typeof HTMLDialogElement !== 'function') {
      const msg = typeof optionsOrMessage === 'string' ? optionsOrMessage : (optionsOrMessage?.message || '');
      resolve(window.confirm(msg));
      return;
    }

    const opts = typeof optionsOrMessage === 'string'
      ? { message: optionsOrMessage }
      : (optionsOrMessage || {});

    const titleText = opts.title || 'Confirm Action';
    const messageText = opts.message || 'Are you sure you want to proceed?';
    const confirmText = opts.confirmText || 'Confirm';
    const cancelText = opts.cancelText || 'Cancel';
    const isDanger = Boolean(opts.danger);

    const dialog = document.createElement('dialog');
    dialog.className = 'modern-dialog';
    dialog.setAttribute('closedby', 'any');
    dialog.setAttribute('aria-labelledby', 'modern-dialog-title');

    // Fallback for browsers without native closedby support (e.g. Safari)
    if (!('closedBy' in HTMLDialogElement.prototype)) {
      dialog.addEventListener('click', (event) => {
        if (event.target !== dialog) return;
        const rect = dialog.getBoundingClientRect();
        const isInside =
          rect.top <= event.clientY &&
          event.clientY <= rect.bottom &&
          rect.left <= event.clientX &&
          event.clientX <= rect.right;
        if (!isInside) dialog.close('cancel');
      });
    }

    const form = document.createElement('form');
    form.method = 'dialog';

    const h2 = document.createElement('h2');
    h2.id = 'modern-dialog-title';
    h2.textContent = titleText;

    const p = document.createElement('p');
    p.textContent = messageText;

    const actions = document.createElement('div');
    actions.className = 'dialog-actions';

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'submit';
    cancelBtn.value = 'cancel';
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = cancelText;
    cancelBtn.autofocus = true;

    const confirmBtn = document.createElement('button');
    confirmBtn.type = 'submit';
    confirmBtn.value = 'confirm';
    confirmBtn.className = 'btn ' + (isDanger ? 'btn-danger' : 'btn-primary');
    confirmBtn.textContent = confirmText;

    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);

    form.appendChild(h2);
    form.appendChild(p);
    form.appendChild(actions);
    dialog.appendChild(form);

    dialog.addEventListener('close', () => {
      const confirmed = dialog.returnValue === 'confirm';
      dialog.returnValue = '';
      dialog.remove();
      resolve(confirmed);
    });

    document.body.appendChild(dialog);
    dialog.showModal();
  });
}
window.showConfirm = showConfirm;

function showPrompt(optionsOrMessage) {
  return new Promise((resolve) => {
    if (typeof document === 'undefined' || typeof HTMLDialogElement !== 'function') {
      const msg = typeof optionsOrMessage === 'string' ? optionsOrMessage : (optionsOrMessage?.message || '');
      const def = typeof optionsOrMessage === 'object' ? optionsOrMessage?.defaultValue : '';
      resolve(window.prompt(msg, def));
      return;
    }

    const opts = typeof optionsOrMessage === 'string'
      ? { message: optionsOrMessage }
      : (optionsOrMessage || {});

    const titleText = opts.title || 'Enter Value';
    const messageText = opts.message || '';
    const placeholder = opts.placeholder || '';
    const defaultValue = opts.defaultValue || '';
    const inputType = opts.inputType || 'text';
    const confirmText = opts.confirmText || 'OK';
    const cancelText = opts.cancelText || 'Cancel';

    const dialog = document.createElement('dialog');
    dialog.className = 'modern-dialog';
    dialog.setAttribute('closedby', 'any');
    dialog.setAttribute('aria-labelledby', 'modern-prompt-title');

    // Fallback for browsers without native closedby support
    if (!('closedBy' in HTMLDialogElement.prototype)) {
      dialog.addEventListener('click', (event) => {
        if (event.target !== dialog) return;
        const rect = dialog.getBoundingClientRect();
        const isInside =
          rect.top <= event.clientY &&
          event.clientY <= rect.bottom &&
          rect.left <= event.clientX &&
          event.clientX <= rect.right;
        if (!isInside) dialog.close('cancel');
      });
    }

    const form = document.createElement('form');
    form.method = 'dialog';

    const h2 = document.createElement('h2');
    h2.id = 'modern-prompt-title';
    h2.textContent = titleText;
    form.appendChild(h2);

    if (messageText) {
      const p = document.createElement('p');
      p.textContent = messageText;
      form.appendChild(p);
    }

    const input = document.createElement('input');
    input.type = inputType;
    input.className = 'prompt-input';
    input.value = defaultValue;
    if (placeholder) input.placeholder = placeholder;
    input.autofocus = true;
    form.appendChild(input);

    const actions = document.createElement('div');
    actions.className = 'dialog-actions';

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn btn-ghost';
    cancelBtn.textContent = cancelText;
    cancelBtn.addEventListener('click', () => dialog.close('cancel'));

    const confirmBtn = document.createElement('button');
    confirmBtn.type = 'submit';
    confirmBtn.value = 'confirm';
    confirmBtn.className = 'btn btn-primary';
    confirmBtn.textContent = confirmText;

    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    form.appendChild(actions);
    dialog.appendChild(form);

    dialog.addEventListener('close', () => {
      const isConfirmed = dialog.returnValue === 'confirm';
      const val = isConfirmed ? input.value : null;
      dialog.returnValue = '';
      dialog.remove();
      resolve(val);
    });

    document.body.appendChild(dialog);
    dialog.showModal();
    input.select();
  });
}
window.showPrompt = showPrompt;

