/**
 * Ride the API — Configuration State Management Module
 * Working copy state, dirty tracking, path manipulation, and API persistence.
 */

let cfg = null;            // working copy
const dirty = new Set();   // touched section paths
let llmSectionLoaded = false;

// Expose state objects on window
window.cfg = cfg;
window.dirty = dirty;
Object.defineProperty(window, 'cfg', {
  get: () => cfg,
  set: (val) => { cfg = val; },
  configurable: true,
});
Object.defineProperty(window, 'llmSectionLoaded', {
  get: () => llmSectionLoaded,
  set: (val) => { llmSectionLoaded = val; },
  configurable: true,
});

// Path helpers — mirrors how values are read from and written back to config
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
  return cfg ? cfg[key] : null;
}

function setDirty(path) {
  dirty.add(path);
  updateSaveBtn();
}

function markFieldDirty(path) {
  const el = document.querySelector(`[data-path="${path}"]`);
  if (el) el.classList.add('dirty');
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
    window.cfg = cfg;
    const pathEl = document.getElementById('configPath');
    if (pathEl) pathEl.textContent = 'config/config.yaml';
    if (typeof buildSidebar === 'function') buildSidebar();
    if (typeof selectSection === 'function') {
      selectSection(Object.keys(cfg)[0] || 'core');
    }
  } catch (e) {
    const titleEl = document.getElementById('sectionTitle');
    const descEl = document.getElementById('sectionDesc');
    if (titleEl) titleEl.textContent = 'Error loading config';
    if (descEl) descEl.textContent = String(e.message || e);
  }
}

async function saveConfig() {
  const btn = document.getElementById('saveBtn');
  const btn2 = document.getElementById('saveBtn2');
  const status = document.getElementById('saveStatus');
  if (btn) btn.disabled = true;
  if (btn2) btn2.disabled = true;
  if (status) {
    status.textContent = 'Saving…';
    status.className = 'status';
  }

  // First collect current section tree into cfg
  const activeSection = document.querySelector('.section-item.active');
  const key = activeSection ? activeSection.dataset.section : Object.keys(cfg || {})[0];
  if (key === '__raw__') {
    const parsed = collectSection('__raw__');
    if (parsed === null) {
      if (status) {
        status.textContent = 'Invalid JSON — not saved';
        status.className = 'status err';
      }
      updateSaveBtn();
      return;
    }
    cfg = parsed;
    window.cfg = cfg;
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
        if (status) {
          status.textContent = 'LLM profiles not saved: ' + lmsg;
          status.className = 'status err';
        }
        if (typeof showToast === 'function') showToast('LLM profiles not saved', 'error');
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
      if (status) {
        status.textContent = msg;
        status.className = 'status err';
      }
      if (typeof showToast === 'function') showToast('Save failed', 'error');
      updateSaveBtn();
      return;
    }
    // Reload canonical config from server (validated/dumped)
    const savedRes = await fetch('/api/config');
    const savedData = await savedRes.json();
    cfg = savedData.config || cfg;
    window.cfg = cfg;
    dirty.clear();
    llmSectionLoaded = false;
    updateSaveBtn();
    if (status) {
      status.textContent = 'Saved, hot-reload applied.';
      status.className = 'status ok';
    }
    if (typeof showToast === 'function') showToast('Configuration saved', 'success');
    // Rebuild current view from canonical config
    if (typeof selectSection === 'function') selectSection(key);
  } catch (e) {
    if (status) {
      status.textContent = String(e.message || e);
      status.className = 'status err';
    }
    if (typeof showToast === 'function') showToast('Save failed', 'error');
    updateSaveBtn();
  }
}

// Global exports
window.getAt = getAt;
window.setAt = setAt;
window.collectSection = collectSection;
window.setDirty = setDirty;
window.markFieldDirty = markFieldDirty;
window.updateSaveBtn = updateSaveBtn;
window.loadConfig = loadConfig;
window.saveConfig = saveConfig;
