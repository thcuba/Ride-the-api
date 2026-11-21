/**
 * Ride the API — Dashboard LLM Settings Module
 * Per-device LLM section and profile selection, global LLM modal and profile cards.
 */

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
  const name = window.showPrompt
    ? await showPrompt({ title: 'New Profile', message: 'Enter profile name:', placeholder: 'e.g. gpt-4o-mini' })
    : prompt('Profile name:');
  if (!name || !name.trim()) return;
  const description = (window.showPrompt
    ? await showPrompt({ title: 'Profile Description', message: 'Description (optional):', placeholder: 'Optional description' })
    : prompt('Description (optional):')) || '';
  const promptTemplate = window.showPrompt
    ? await showPrompt({
        title: 'Prompt Template',
        message: 'Prompt template (use {vendor}, {device_type}, {pairs}, {device_id}, {context_notes}):',
        defaultValue: '{vendor} {device_type} {pairs}'
      })
    : prompt('Prompt template (use {vendor}, {device_type}, {pairs}, {device_id}, {context_notes}):');
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
    const name = (window.showPrompt
      ? await showPrompt({ title: 'Edit Profile Name', defaultValue: p.name })
      : prompt('Profile name:', p.name)) || p.name;
    const desc = (window.showPrompt
      ? await showPrompt({ title: 'Edit Description', defaultValue: p.description || '' })
      : prompt('Description:', p.description || '')) || '';
    const pt = (window.showPrompt
      ? await showPrompt({ title: 'Edit Prompt Template', defaultValue: p.prompt_template })
      : prompt('Prompt template:', p.prompt_template)) || p.prompt_template;
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
  const ok = window.showConfirm
    ? await showConfirm({
        title: 'Delete Profile',
        message: 'Delete profile "' + sel.value + '"?',
        confirmText: 'Delete',
        danger: true
      })
    : confirm('Delete profile "' + sel.value + '"?');
  if (!ok) return;
  try {
    const res = await fetch('/api/llm/user-profiles/' + encodeURIComponent(sel.value), { method: 'DELETE' });
    if (!res.ok) return showToast('Failed to delete profile', 'error');
    showToast('Profile deleted', 'success');
    loadLLMProfiles(deviceId);
  } catch(e) { showToast('Failed to delete profile', 'error'); }
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
    defaultSel.replaceChildren();
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
    sel.replaceChildren();
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
  list.replaceChildren();
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

async function addLLMProfile() {
  const list = document.getElementById('llm-profiles-list');
  if (!list) return;
  const name = window.showPrompt
    ? await showPrompt({ title: 'New Profile Name', message: 'Enter name for new LLM profile:', placeholder: 'e.g. claude-3-5' })
    : prompt('New profile name:');
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

async function deleteLLMProfile(name) {
  const ok = window.showConfirm
    ? await showConfirm({
        title: 'Delete LLM Profile',
        message: 'Delete LLM profile "' + name + '"?',
        confirmText: 'Delete',
        danger: true
      })
    : confirm('Delete LLM profile "' + name + '"?');
  if (!ok) return;
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


// Global exports for inline HTML event handlers
window.toggleLLMSection = toggleLLMSection;
window.loadLLMProfiles = loadLLMProfiles;
window.onProfileChange = onProfileChange;
window.saveAsNewProfile = saveAsNewProfile;
window.editProfile = editProfile;
window.deleteProfile = deleteProfile;
window.loadLLMSettings = loadLLMSettings;
window.renderLLMSettings = renderLLMSettings;
window.updateLLMCardDirty = updateLLMCardDirty;
window.renderLLMProfileCards = renderLLMProfileCards;
window.updateProfileStatusPill = updateProfileStatusPill;
window.onLLMDefaultChange = onLLMDefaultChange;
window.addLLMProfile = addLLMProfile;
window.deleteLLMProfile = deleteLLMProfile;
window.testLLMProfile = testLLMProfile;
window.collectLLMSettings = collectLLMSettings;
window.saveLLMSettings = saveLLMSettings;
