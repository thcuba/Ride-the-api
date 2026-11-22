/**
 * Ride the API — Configuration Form Renderer Module
 * Dynamic form generation, field controls, quick-pick datalists, tooltips, and section quick-actions.
 */

let fieldCounter = 0;

// Instant hover tooltip, one element shared by every explained control.
// The same text is also set as the native title, so keyboard, touch and screen
// readers get it without hover.
function ensureTooltip() {
  let tip = document.getElementById('hoverTip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'hoverTip';
    tip.className = 'hover-tip';
    tip.setAttribute('role', 'tooltip');
    document.body.appendChild(tip);
  }
  return tip;
}

function placeTip(tip, x, y) {
  const pad = 12;
  const rect = tip.getBoundingClientRect();
  let left = x + 14;
  let top = y + 16;
  if (left + rect.width + pad > window.innerWidth) left = Math.max(pad, x - rect.width - 14);
  if (top + rect.height + pad > window.innerHeight) top = Math.max(pad, y - rect.height - 14);
  tip.style.left = left + 'px';
  tip.style.top = top + 'px';
}

function attachTip(el, text) {
  if (!el || !text) return el;
  el.setAttribute('title', text);
  el.addEventListener('mouseenter', (ev) => {
    const tip = ensureTooltip();
    tip.textContent = text;
    tip.classList.add('show');
    placeTip(tip, ev.clientX, ev.clientY);
  });
  el.addEventListener('mousemove', (ev) => {
    const tip = document.getElementById('hoverTip');
    if (tip && tip.classList.contains('show')) placeTip(tip, ev.clientX, ev.clientY);
  });
  const hide = () => {
    const tip = document.getElementById('hoverTip');
    if (tip) tip.classList.remove('show');
  };
  el.addEventListener('mouseleave', hide);
  el.addEventListener('blur', hide);
  return el;
}

function hideTooltip() {
  const tip = document.getElementById('hoverTip');
  if (tip) tip.classList.remove('show');
}

// Quick-pick list for a field: a datalist attached to a text input, so a
// suggested value is one click away while any other value stays possible.
function buildDatalist(path) {
  const options = typeof suggestionsFor === 'function' ? suggestionsFor(path) : null;
  if (!options) return null;
  const listId = 'dl-' + (fieldCounter++);
  const dl = document.createElement('datalist');
  dl.id = listId;
  options.forEach((value) => {
    const opt = document.createElement('option');
    opt.value = value;
    dl.appendChild(opt);
  });
  dl.dataset.for = path;
  return { listId, dl };
}

// One-click copy of a config path, for pasting into config.yaml or a grep.
function copyFieldPath(path, btn) {
  const done = () => {
    const label = btn.textContent;
    btn.textContent = 'copied';
    setTimeout(() => { btn.textContent = label; }, 1200);
    if (typeof showToast === 'function') showToast('Path copied: ' + path, 'success');
  };
  if (typeof copyToClipboard === 'function') {
    copyToClipboard(path, done, () => {
      if (typeof showToast === 'function') showToast('Copy failed', 'error');
    });
  } else if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(path).then(done).catch(() => {
      if (typeof showToast === 'function') showToast('Copy failed', 'error');
    });
  } else if (typeof showToast === 'function') {
    showToast('Clipboard not available', 'error');
  }
}

// Copy configuration file path from header chip
function copyConfigPath() {
  const pathEl = document.getElementById('configPath');
  const path = pathEl ? (pathEl.textContent || 'config/config.yaml') : 'config/config.yaml';
  const done = () => {
    if (typeof showToast === 'function') showToast('Config path copied to clipboard', 'success');
  };
  if (typeof copyToClipboard === 'function') {
    copyToClipboard(path, done, () => {
      if (typeof showToast === 'function') showToast('Copy failed', 'error');
    });
  } else if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(path).then(done).catch(() => {
      if (typeof showToast === 'function') showToast('Copy failed', 'error');
    });
  } else if (typeof showToast === 'function') {
    showToast('Clipboard not available', 'error');
  }
}

function isSimpleType(v) {
  return ['string', 'number', 'boolean'].includes(typeof v) || v === null;
}

function renderObject(container, obj, path) {
  const currentCfg = window.cfg;
  const entries = Object.entries(obj || {});
  if (entries.length === 0) {
    const p = document.createElement('p');
    p.className = 'sec-desc';
    const emptyHint = typeof hintFor === 'function' ? hintFor(path) : '';
    p.textContent = emptyHint ? 'Empty. ' + emptyHint : 'Empty.';
    container.appendChild(p);
    // Allow populating empty dicts (e.g. core.ip_profiles, vendors, profiles)
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn btn-ghost';
    addBtn.textContent = '+ Add key';
    addBtn.style.cssText = 'margin-top:6px;font-size:12px;padding:4px 12px;';
    addBtn.setAttribute('title', 'Add one or more keys to this section. Separate several keys with a comma.');
    addBtn.addEventListener('click', async () => {
      const raw = window.showPrompt
        ? await showPrompt({
            title: 'Add Configuration Key',
            message: 'New key name (separate several keys with a comma):',
            placeholder: 'key1, key2',
          })
        : prompt('New key name (separate several keys with a comma):');
      if (!raw) return;
      const parent = getAt(window.cfg, path);
      if (!parent) return;
      raw.split(',').map((s) => s.trim()).filter((s) => s !== '').forEach((key) => {
        if (parent[key] === undefined) {
          parent[key] = typeof defaultForKey === 'function' ? defaultForKey(path, key) : '';
        }
      });
      setDirty(path);
      rerenderSection();
    });
    container.appendChild(addBtn);
    return;
  }
  entries.forEach(([k, v]) => {
    const childPath = path ? path + '.' + k : k;
    if (isSimpleType(v)) {
      container.appendChild(renderField(childPath, k, v));
    } else if (Array.isArray(v)) {
      container.appendChild(renderArray(childPath, k, v));
    } else {
      // nested object → collapsible group
      const group = document.createElement('div');
      group.className = 'field-group';
      group.dataset.path = childPath;
      const head = document.createElement('div');
      head.className = 'field-group-title';
      head.dataset.path = childPath;
      head.setAttribute('role', 'button');
      head.setAttribute('tabindex', '0');
      head.setAttribute('aria-expanded', 'true');
      const bodyId = 'fg-body-' + (fieldCounter++);
      head.setAttribute('aria-controls', bodyId);

      const leftDiv = document.createElement('div');
      leftDiv.className = 'field-group-title-left';
      const chevronSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      chevronSvg.setAttribute('class', 'chev-svg');
      chevronSvg.setAttribute('aria-hidden', 'true');
      chevronSvg.setAttribute('viewBox', '0 0 24 24');
      chevronSvg.innerHTML = '<polyline points="6 9 12 15 18 9"/>';
      const nameSpan = document.createElement('span');
      nameSpan.textContent = k;
      leftDiv.appendChild(chevronSvg);
      leftDiv.appendChild(nameSpan);
      const groupHint = typeof hintFor === 'function' ? hintFor(childPath) : '';
      if (groupHint) {
        const help = document.createElement('button');
        help.type = 'button';
        help.className = 'f-help';
        help.textContent = '?';
        help.setAttribute('aria-label', 'What does ' + k + ' do');
        help.addEventListener('click', (e) => e.stopPropagation());
        leftDiv.appendChild(help);
        attachTip(help, groupHint);
      }
      head.appendChild(leftDiv);

      const body = document.createElement('div');
      body.id = bodyId;
      renderObject(body, v, childPath);

      const toggleGroup = () => {
        const hidden = body.style.display === 'none';
        body.style.display = hidden ? '' : 'none';
        chevronSvg.classList.toggle('collapsed', hidden);
        head.setAttribute('aria-expanded', hidden ? 'true' : 'false');
      };
      head.addEventListener('click', toggleGroup);
      head.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          toggleGroup();
        }
      });
      group.appendChild(head);
      group.appendChild(body);
      container.appendChild(group);
    }
  });
}

// Re-render the currently active section (used after structural edits like
// adding/removing array items or dict keys).
function rerenderSection() {
  const active = document.querySelector('.section-item.active');
  if (active && typeof selectSection === 'function') {
    selectSection(active.dataset.section);
  }
}

function renderField(path, label, value) {
  const row = document.createElement('div');
  row.className = 'field';
  row.dataset.path = path;
  const id = 'f-' + (fieldCounter++);
  const hint = typeof hintFor === 'function' ? hintFor(path) : '';
  const info = document.createElement('div');
  info.className = 'f-info';
  const nameRow = document.createElement('span');
  nameRow.className = 'f-name-row';
  const nameLabel = document.createElement('label');
  nameLabel.setAttribute('for', id);
  nameLabel.className = 'f-name';
  nameLabel.textContent = label;
  nameRow.appendChild(nameLabel);
  if (hint) {
    const help = document.createElement('button');
    help.type = 'button';
    help.className = 'f-help';
    help.textContent = '?';
    help.setAttribute('aria-label', 'What does ' + label + ' do');
    nameRow.appendChild(help);
    attachTip(help, hint);
  }
  info.appendChild(nameRow);
  const pathLine = document.createElement('div');
  pathLine.className = 'f-path';
  const pathCode = document.createElement('span');
  pathCode.textContent = path;
  pathLine.appendChild(pathCode);
  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'f-copy';
  copyBtn.textContent = 'copy path';
  copyBtn.setAttribute('aria-label', 'Copy config path ' + path);
  copyBtn.addEventListener('click', () => copyFieldPath(path, copyBtn));
  pathLine.appendChild(copyBtn);
  info.appendChild(pathLine);
  row.appendChild(info);
  const ctrl = document.createElement('div');
  ctrl.className = 'f-control';

  if (typeof value === 'boolean') {
    row.classList.add('field-switch');
    const switchWrap = document.createElement('label');
    switchWrap.className = 'switch-wrap';
    switchWrap.htmlFor = id;
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.className = 'switch';
    input.role = 'switch';
    input.id = id;
    input.checked = value;
    input.setAttribute('aria-checked', value ? 'true' : 'false');
    input.setAttribute('aria-label', label);
    input.addEventListener('change', () => {
      input.setAttribute('aria-checked', input.checked ? 'true' : 'false');
      setAt(window.cfg, path, input.checked);
      setDirty(path);
      markFieldDirty(path);
    });
    switchWrap.appendChild(input);
    ctrl.appendChild(switchWrap);
  } else if (typeof value === 'number') {
    const input = document.createElement('input');
    input.type = 'number';
    input.id = id;
    input.value = value;
    input.step = 'any';
    input.addEventListener('change', () => {
      const n = parseFloat(input.value);
      setAt(window.cfg, path, Number.isNaN(n) ? null : n);
      setDirty(path);
      markFieldDirty(path);
    });
    ctrl.appendChild(input);
  } else if (value === null) {
    const input = document.createElement('input');
    input.type = 'text';
    input.id = id;
    input.value = '';
    input.placeholder = 'null';
    input.addEventListener('change', () => {
      setAt(window.cfg, path, input.value === '' ? null : input.value);
      setDirty(path);
      markFieldDirty(path);
    });
    const list = buildDatalist(path);
    if (list) {
      input.setAttribute('list', list.listId);
      ctrl.appendChild(list.dl);
    }
    ctrl.appendChild(input);
  } else if (typeof value === 'string') {
    const enumOpts = typeof enumOptionsFor === 'function' ? enumOptionsFor(path) : null;
    if (enumOpts) {
      const select = document.createElement('select');
      select.id = id;
      enumOpts.forEach(opt => {
        const o = document.createElement('option');
        o.value = opt;
        o.textContent = opt;
        if (opt === value) o.selected = true;
        select.appendChild(o);
      });
      select.addEventListener('change', () => {
        setAt(window.cfg, path, select.value);
        setDirty(path);
        markFieldDirty(path);
      });
      ctrl.appendChild(select);
    } else {
      const isLong = typeof value === 'string' && value.length > 80;
      if (isLong) {
        const ta = document.createElement('textarea');
        ta.id = id;
        ta.value = value;
        ta.rows = 4;
        ta.addEventListener('input', () => {
          setAt(window.cfg, path, ta.value);
          setDirty(path);
          markFieldDirty(path);
        });
        ctrl.appendChild(ta);
      } else {
        // Secret fields arrive masked from the API; render them as password
        // inputs. Leaving the mask unchanged keeps the stored value on save.
        const isPassword = /(^|\.)(api_key|password|mqtt_pass)$/.test(path);
        if (isPassword) {
          const wrap = document.createElement('div');
          wrap.className = 'pw-wrap';
          const input = document.createElement('input');
          input.type = 'password';
          input.autocomplete = 'off';
          input.id = id;
          input.value = value;
          input.style.paddingRight = '42px';
          input.addEventListener('change', () => {
            setAt(window.cfg, path, input.value === '' ? null : input.value);
            setDirty(path);
            markFieldDirty(path);
          });
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'pw-toggle-btn';
          btn.setAttribute('aria-label', 'Show API key');
          btn.title = 'Show API key';
          btn.innerHTML = '<svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24" style="width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
          btn.addEventListener('click', () => {
            const show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            btn.setAttribute('aria-label', show ? 'Hide API key' : 'Show API key');
            btn.title = show ? 'Hide API key' : 'Show API key';
            const svg = btn.querySelector('svg');
            if (svg) {
              svg.innerHTML = show
                ? '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'
                : '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
            }
          });
          wrap.appendChild(input);
          wrap.appendChild(btn);
          ctrl.appendChild(wrap);
        } else {
          const input = document.createElement('input');
          input.type = 'text';
          input.id = id;
          input.value = value;
          input.addEventListener('change', () => {
            setAt(window.cfg, path, input.value === '' ? null : input.value);
            setDirty(path);
            markFieldDirty(path);
          });
          const list = buildDatalist(path);
          if (list) {
            input.setAttribute('list', list.listId);
            ctrl.appendChild(list.dl);
          }
          ctrl.appendChild(input);
        }
      }
    }
  }

  row.appendChild(ctrl);
  return row;
}

function renderArray(path, label, arr) {
  const group = document.createElement('div');
  group.className = 'field-group';
  group.dataset.path = path;
  const head = document.createElement('div');
  head.className = 'field-group-title';
  head.setAttribute('role', 'button');
  head.setAttribute('tabindex', '0');
  head.setAttribute('aria-expanded', 'true');
  const bodyId = 'fg-body-' + (fieldCounter++);
  head.setAttribute('aria-controls', bodyId);

  const leftDiv = document.createElement('div');
  leftDiv.className = 'field-group-title-left';
  const chevronSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  chevronSvg.setAttribute('class', 'chev-svg');
  chevronSvg.setAttribute('aria-hidden', 'true');
  chevronSvg.setAttribute('viewBox', '0 0 24 24');
  chevronSvg.innerHTML = '<polyline points="6 9 12 15 18 9"/>';
  const nameSpan = document.createElement('span');
  nameSpan.textContent = label + ' (' + arr.length + ')';
  leftDiv.appendChild(chevronSvg);
  leftDiv.appendChild(nameSpan);
  const arrHint = typeof hintFor === 'function' ? hintFor(path) : '';
  if (arrHint) {
    const help = document.createElement('button');
    help.type = 'button';
    help.className = 'f-help';
    help.textContent = '?';
    help.setAttribute('aria-label', 'What does ' + label + ' do');
    help.addEventListener('click', (e) => e.stopPropagation());
    leftDiv.appendChild(help);
    attachTip(help, arrHint);
  }
  head.appendChild(leftDiv);

  const body = document.createElement('div');
  body.id = bodyId;
  const toggleArrayGroup = () => {
    const hidden = body.style.display === 'none';
    body.style.display = hidden ? '' : 'none';
    chevronSvg.classList.toggle('collapsed', hidden);
    head.setAttribute('aria-expanded', hidden ? 'true' : 'false');
  };
  head.addEventListener('click', toggleArrayGroup);
  head.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggleArrayGroup();
    }
  });
  group.appendChild(head);
  group.appendChild(body);
  // Primitive arrays → editable comma-separated list
  if (arr.length && arr.every(isSimpleType)) {
    const row = document.createElement('div');
    row.className = 'field';
    const id = 'f-' + (fieldCounter++);
    const itemHint = typeof hintFor === 'function' ? hintFor(path) : '';
    const info = document.createElement('div');
    info.className = 'f-info';
    const nameRow = document.createElement('span');
    nameRow.className = 'f-name-row';
    const nameLabel = document.createElement('label');
    nameLabel.setAttribute('for', id);
    nameLabel.className = 'f-name';
    nameLabel.textContent = label;
    nameRow.appendChild(nameLabel);
    if (itemHint) {
      const help = document.createElement('button');
      help.type = 'button';
      help.className = 'f-help';
      help.textContent = '?';
      help.setAttribute('aria-label', 'What does ' + label + ' do');
      nameRow.appendChild(help);
      attachTip(help, itemHint);
      attachTip(nameRow, itemHint + ' (' + path + ')');
    }
    info.appendChild(nameRow);
    const pathLine = document.createElement('div');
    pathLine.className = 'f-path';
    const pathCode = document.createElement('span');
    pathCode.textContent = path + ' — comma-separated values';
    pathLine.appendChild(pathCode);
    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'f-copy';
    copyBtn.textContent = 'copy path';
    copyBtn.setAttribute('aria-label', 'Copy config path ' + path);
    copyBtn.addEventListener('click', () => copyFieldPath(path, copyBtn));
    pathLine.appendChild(copyBtn);
    info.appendChild(pathLine);
    row.appendChild(info);
    const ctrl = document.createElement('div');
    ctrl.className = 'f-control';
    const input = document.createElement('input');
    input.type = 'text';
    input.id = id;
    input.value = arr.join(', ');
    input.setAttribute('aria-label', label);
    input.addEventListener('change', () => {
      const parts = input.value.split(',').map(s => s.trim()).filter(s => s !== '');
      const isNum = arr.every(v => typeof v === 'number');
      setAt(window.cfg, path, isNum ? parts.map(Number) : parts);
      setDirty(path);
      markFieldDirty(path);
    });
    ctrl.appendChild(input);
    row.appendChild(ctrl);
    body.appendChild(row);
  } else {
    // Object array (or empty array) → render each item with add/remove controls
    if (arr.length === 0) {
      const p = document.createElement('p');
      p.className = 'sec-desc';
      p.textContent = '(empty)';
      body.appendChild(p);
    } else {
      arr.forEach((item, idx) => {
        const itemPath = path + '.' + idx;
        const itemGroup = document.createElement('div');
        itemGroup.className = 'field-group';
        itemGroup.dataset.path = itemPath;
        const itemHead = document.createElement('div');
        itemHead.className = 'field-group-title';
        itemHead.setAttribute('role', 'button');
        itemHead.setAttribute('tabindex', '0');
        itemHead.setAttribute('aria-expanded', 'true');
        const itemBodyId = 'fg-body-' + (fieldCounter++);
        itemHead.setAttribute('aria-controls', itemBodyId);

        const itemLeftDiv = document.createElement('div');
        itemLeftDiv.className = 'field-group-title-left';
        const itemChevronSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        itemChevronSvg.setAttribute('class', 'chev-svg');
        itemChevronSvg.setAttribute('aria-hidden', 'true');
        itemChevronSvg.setAttribute('viewBox', '0 0 24 24');
        itemChevronSvg.innerHTML = '<polyline points="6 9 12 15 18 9"/>';
        const itemName = document.createElement('span');
        itemName.textContent = '#' + idx;
        itemLeftDiv.appendChild(itemChevronSvg);
        itemLeftDiv.appendChild(itemName);
        itemHead.appendChild(itemLeftDiv);

        const itemBody = document.createElement('div');
        itemBody.id = itemBodyId;
        renderObject(itemBody, item, itemPath);
        const toggleItem = () => {
          const hidden = itemBody.style.display === 'none';
          itemBody.style.display = hidden ? '' : 'none';
          itemChevronSvg.classList.toggle('collapsed', hidden);
          itemHead.setAttribute('aria-expanded', hidden ? 'true' : 'false');
        };
        itemHead.addEventListener('click', toggleItem);
        itemHead.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            toggleItem();
          }
        });
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'btn btn-ghost btn-remove';
        removeBtn.textContent = 'Remove';
        removeBtn.setAttribute('aria-label', 'Remove item #' + idx);
        removeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          const parent = getAt(window.cfg, path);
          if (Array.isArray(parent)) {
            parent.splice(idx, 1);
            setDirty(path);
            rerenderSection();
          }
        });
        itemHead.appendChild(removeBtn);
        itemGroup.appendChild(itemHead);
        itemGroup.appendChild(itemBody);
        body.appendChild(itemGroup);
      });
    }
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn btn-ghost';
    addBtn.textContent = '+ Add item';
    addBtn.style.cssText = 'margin-top:10px;font-size:13px;padding:8px 16px;min-height:38px;';
    addBtn.addEventListener('click', () => {
      const parent = getAt(window.cfg, path);
      if (Array.isArray(parent)) {
        parent.push({});
        setDirty(path);
        rerenderSection();
      }
    });
    body.appendChild(addBtn);
  }
  return group;
}

// ── LLM Decipher section (editable; persistence via the LLM settings API) ───
// The LLM page and this section are two entry points onto the SAME data: the
// fields are populated from the effective (runtime) settings and saved through
// /api/llm/settings (see saveConfig), never through PUT /api/config.
async function renderLLMSection(container) {
  const banner = document.createElement('div');
  banner.className = 'field-group llm-banner-card';
  banner.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:16px;padding:14px 18px;background:color-mix(in srgb, var(--surface2) 60%, var(--surface));border:1px solid var(--border);border-radius:10px;';

  const bannerText = document.createElement('div');
  bannerText.style.cssText = 'flex:1;min-width:240px;';
  const bannerTitle = document.createElement('div');
  bannerTitle.style.cssText = 'font-weight:700;font-size:14px;color:var(--text);margin-bottom:4px;display:flex;align-items:center;gap:8px;';
  bannerTitle.innerHTML = '<svg class="sec-icon" aria-hidden="true" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8.01" y2="16"/><line x1="16" y1="16" x2="16.01" y2="16"/></svg><span>Dedicated LLM Service Manager</span>';
  const bannerDesc = document.createElement('div');
  bannerDesc.className = 'sec-desc';
  bannerDesc.style.cssText = 'margin-bottom:0;font-size:12.5px;line-height:1.45;';
  bannerDesc.textContent = 'Profiles configured here are applied through the LLM settings API — the same service store the LLM page uses.';
  bannerText.appendChild(bannerTitle);
  bannerText.appendChild(bannerDesc);
  banner.appendChild(bannerText);

  const openBtn = document.createElement('button');
  openBtn.type = 'button';
  openBtn.className = 'btn btn-primary btn-open-llm';
  openBtn.style.cssText = 'min-height:42px;padding:8px 16px;font-size:13.5px;font-weight:600;display:inline-flex;align-items:center;gap:8px;border-radius:8px;';
  openBtn.innerHTML = '<span>Open LLM Settings</span><span class="btn-glow-pill" aria-hidden="true">↗</span>';
  openBtn.setAttribute('aria-label', 'Open the LLM settings page');
  openBtn.addEventListener('click', () => { window.location.href = '/#llm-settings'; });
  banner.appendChild(openBtn);

  container.appendChild(banner);

  const box = document.createElement('div');
  container.appendChild(box);
  if (!window.llmSectionLoaded) {
    box.textContent = 'Loading…';
    try {
      const res = await fetch('/api/llm/settings');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      window.cfg.llm_decipher = await res.json();
      window.llmSectionLoaded = true;
    } catch (err) {
      box.textContent = 'Could not load LLM settings: ' + err.message;
      return;
    }
  }
  box.replaceChildren();
  renderObject(box, window.cfg.llm_decipher, 'llm_decipher');
}

// ── Quick actions ───────────────────────────────────────────────────────────
async function addIpProfileOverride() {
  const raw = window.showPrompt
    ? await showPrompt({
        title: 'IP Profile Override',
        message: 'Source IP of the device (for example 192.168.1.50):',
        placeholder: '192.168.1.50',
      })
    : prompt('Source IP of the device (for example 192.168.1.50):');
  if (!raw) return;
  const ip = raw.trim();
  if (!/^[0-9a-fA-F:.]+$/.test(ip)) {
    if (typeof showToast === 'function') showToast('Not a valid IPv4 or IPv6 address', 'error');
    return;
  }
  window.cfg.core = window.cfg.core || {};
  window.cfg.core.ip_profiles = window.cfg.core.ip_profiles || {};
  if (window.cfg.core.ip_profiles[ip]) {
    if (typeof showToast === 'function') showToast('That IP already has an override', 'info');
  } else {
    window.cfg.core.ip_profiles[ip] = { connection: 'auto', bypass: false };
    if (typeof showToast === 'function') {
      showToast('Override added for ' + ip + ' — review it below, then Save', 'success');
    }
  }
  setDirty('core.ip_profiles');
  rerenderSection();
}

const SECTION_QUICK_ACTIONS = {
  core: [
    {
      label: '+ Add per-IP override',
      tip: 'Create one core.ip_profiles entry for a single source IP, with connection auto and bypass off, then edit it below and Save.',
      run: addIpProfileOverride,
    },
  ],
  dns: [
    {
      label: 'Intercept every name',
      tip: 'Turn on dns.intercept_all so every device that uses this host as its resolver is captured. Review the exclude list before saving.',
      run: () => {
        if (!window.cfg.dns) return;
        window.cfg.dns.intercept_all = true;
        setDirty('dns.intercept_all');
        rerenderSection();
        if (typeof showToast === 'function') {
          showToast('intercept_all switched on — review exclude, then Save', 'info');
        }
      },
    },
  ],
};

function renderQuickActions(container, key) {
  const actions = SECTION_QUICK_ACTIONS[key];
  if (!actions || !actions.length) return;
  const bar = document.createElement('div');
  bar.className = 'sec-quick';
  actions.forEach((action) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-ghost';
    btn.textContent = action.label;
    attachTip(btn, action.tip);
    btn.addEventListener('click', action.run);
    bar.appendChild(btn);
  });
  container.appendChild(bar);
}

// Global exports
window.ensureTooltip = ensureTooltip;
window.placeTip = placeTip;
window.attachTip = attachTip;
window.hideTooltip = hideTooltip;
window.buildDatalist = buildDatalist;
window.copyFieldPath = copyFieldPath;
window.copyConfigPath = copyConfigPath;
window.isSimpleType = isSimpleType;
window.renderObject = renderObject;
window.renderField = renderField;
window.renderArray = renderArray;
window.rerenderSection = rerenderSection;
window.renderLLMSection = renderLLMSection;
window.addIpProfileOverride = addIpProfileOverride;
window.SECTION_QUICK_ACTIONS = SECTION_QUICK_ACTIONS;
window.renderQuickActions = renderQuickActions;
