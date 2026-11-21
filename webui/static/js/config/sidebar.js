/**
 * Ride the API — Configuration Sidebar & Navigation Module
 * Sidebar building, search filtering, tab accessibility, and section selection view.
 */

function clearSidebarSearch() {
  const input = document.getElementById('sidebarSearch');
  if (input) {
    input.value = '';
    input.focus();
    filterSidebar('');
  }
}

function filterSidebar(query) {
  const q = (query || '').trim().toLowerCase();
  const clearBtn = document.getElementById('clearSidebarSearchBtn');
  if (clearBtn) clearBtn.style.display = q ? 'block' : 'none';
  const allBtns = document.querySelectorAll('#sidebar .sidebar-items .section-item');
  let matches = 0;
  allBtns.forEach(btn => {
    const sec = btn.dataset.section || '';
    const label = (window.SECTION_LABELS && window.SECTION_LABELS[sec]) || sec;
    const detail = (window.SECTION_DETAILS && window.SECTION_DETAILS[sec]) || '';
    const isMatch = !q || sec.toLowerCase().includes(q) || label.toLowerCase().includes(q) || detail.toLowerCase().includes(q);
    btn.style.display = isMatch ? '' : 'none';
    if (isMatch) matches++;
  });
  const emptyEl = document.getElementById('sidebarEmpty');
  if (emptyEl) {
    emptyEl.style.display = (matches === 0 && q) ? 'block' : 'none';
  }
  const statusEl = document.getElementById('sidebarFilterStatus');
  if (statusEl && allBtns.length > 0) {
    statusEl.textContent = q ? `Showing ${matches} of ${allBtns.length} sections` : '';
  }
}

function buildSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  sidebar.replaceChildren();

  const cfgObj = window.cfg || {};
  const sectionOrder = window.SECTION_ORDER || [];
  const sectionLabels = window.SECTION_LABELS || {};
  const sectionDetails = window.SECTION_DETAILS || {};
  const sectionIcons = window.SECTION_ICONS || {};
  const hints = window.HINTS || {};

  const header = document.createElement('div');
  header.className = 'sidebar-header';
  const label = document.createElement('span');
  label.className = 'sidebar-label';
  label.textContent = 'Sections';
  const badge = document.createElement('span');
  badge.className = 'sidebar-badge';
  const keys = sectionOrder.filter(k => k in cfgObj)
    .concat(Object.keys(cfgObj).filter(k => !sectionOrder.includes(k)));
  badge.textContent = String(keys.length);
  badge.title = `${keys.length} configuration sections`;
  header.appendChild(label);
  header.appendChild(badge);
  sidebar.appendChild(header);

  // Search filter
  const searchWrap = document.createElement('div');
  searchWrap.className = 'sidebar-search-wrap';
  searchWrap.innerHTML = `
    <svg class="sidebar-search-icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    <input type="search" class="sidebar-search" id="sidebarSearch" placeholder="Filter sections…" aria-label="Filter configuration sections" />
    <kbd class="search-kbd" aria-hidden="true">/</kbd>
    <button type="button" class="clear-search-btn" id="clearSidebarSearchBtn" aria-label="Clear configuration search filter" title="Clear configuration search filter">&times;</button>
    <span class="sr-only" id="sidebarFilterStatus" aria-live="polite"></span>
  `;
  const searchInput = searchWrap.querySelector('input');
  searchInput.addEventListener('input', (e) => filterSidebar(e.target.value));
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      clearSidebarSearch();
      e.stopPropagation();
    }
  });
  searchWrap.querySelector('#clearSidebarSearchBtn').addEventListener('click', clearSidebarSearch);
  sidebar.appendChild(searchWrap);

  // Scrollable sections list
  const itemsContainer = document.createElement('div');
  itemsContainer.className = 'sidebar-items';
  itemsContainer.setAttribute('role', 'tablist');
  itemsContainer.setAttribute('aria-orientation', 'vertical');

  keys.forEach(key => {
    const btn = document.createElement('button');
    btn.className = 'section-item';
    btn.dataset.section = key;
    btn.setAttribute('role', 'tab');
    btn.id = 'tab-' + key;
    btn.setAttribute('aria-controls', 'main-content');
    btn.tabIndex = -1;
    btn.title = hints[key] || sectionDetails[key] || 'Configuration section';
    if (typeof attachTip === 'function') attachTip(btn, btn.title);
    const iconSvg = sectionIcons[key] || (sectionIcons._default || '');
    const escapeFn = typeof escapeHtml === 'function' ? escapeHtml : (s) => s;
    btn.innerHTML = `
      <div class="section-item-main">
        <span class="section-item-icon" aria-hidden="true">${iconSvg}</span>
        <span class="section-item-text">
          <span class="section-item-name">${escapeFn(sectionLabels[key] || key)}</span>
          <span class="section-item-key">${escapeFn(key)}</span>
        </span>
      </div>
      <span class="count"></span>`;
    btn.addEventListener('click', () => selectSection(key));
    itemsContainer.appendChild(btn);
  });

  const emptyMsg = document.createElement('div');
  emptyMsg.id = 'sidebarEmpty';
  emptyMsg.className = 'sidebar-empty';
  emptyMsg.textContent = 'No matching sections';
  emptyMsg.style.display = 'none';
  itemsContainer.appendChild(emptyMsg);

  sidebar.appendChild(itemsContainer);

  // Advanced / Developer Tools
  const divider = document.createElement('div');
  divider.className = 'sidebar-divider';
  sidebar.appendChild(divider);

  const footerLabel = document.createElement('div');
  footerLabel.className = 'sidebar-label sidebar-footer-label';
  footerLabel.textContent = 'Advanced';
  sidebar.appendChild(footerLabel);

  const raw = document.createElement('button');
  raw.className = 'section-item';
  raw.dataset.section = '__raw__';
  raw.setAttribute('role', 'tab');
  raw.id = 'tab-__raw__';
  raw.setAttribute('aria-controls', 'main-content');
  raw.tabIndex = -1;
  raw.title = 'Full configuration as raw JSON';
  raw.innerHTML = `
    <div class="section-item-main">
      <span class="section-item-icon" aria-hidden="true">${sectionIcons.__raw__ || ''}</span>
      <span class="section-item-text">
        <span class="section-item-name">Raw</span>
        <span class="section-item-key">JSON / YAML</span>
      </span>
    </div>
    <span class="count"></span>`;
  raw.addEventListener('click', () => selectSection('__raw__'));
  sidebar.appendChild(raw);

  if (!sidebar.dataset.tablistBound) {
    sidebar.dataset.tablistBound = '1';
    sidebar.addEventListener('keydown', (e) => {
      if (e.target && e.target.classList.contains('section-item')) {
        const tabs = Array.from(sidebar.querySelectorAll('.section-item')).filter(t => t.offsetParent !== null);
        if (!tabs.length) return;
        const curIdx = tabs.indexOf(e.target);
        let nextIdx = -1;
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          nextIdx = (curIdx + 1) % tabs.length;
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          nextIdx = (curIdx - 1 + tabs.length) % tabs.length;
        } else if (e.key === 'Home') {
          e.preventDefault();
          nextIdx = 0;
        } else if (e.key === 'End') {
          e.preventDefault();
          nextIdx = tabs.length - 1;
        }
        if (nextIdx >= 0) {
          selectSection(tabs[nextIdx].dataset.section, true);
        }
      }
    });
  }
}

function selectSection(key, shouldFocus = false) {
  document.querySelectorAll('.section-item').forEach(b => {
    const isActive = b.dataset.section === key;
    b.classList.toggle('active', isActive);
    b.tabIndex = isActive ? 0 : -1;
    if (isActive) {
      b.setAttribute('aria-current', 'true');
      b.setAttribute('aria-selected', 'true');
      if (shouldFocus) b.focus();
    } else {
      b.removeAttribute('aria-current');
      b.setAttribute('aria-selected', 'false');
    }
  });

  const title = document.getElementById('sectionTitle');
  const desc = document.getElementById('sectionDesc');
  const fields = document.getElementById('fields');
  const sectionLabels = window.SECTION_LABELS || {};
  const hints = window.HINTS || {};

  if (title) {
    title.setAttribute('tabindex', '-1');
  }

  if (key === '__raw__') {
    if (title) title.textContent = 'Raw Configuration';
    if (desc) {
      desc.innerHTML = '<span>Full config as JSON — edit carefully, validated on save.</span>';
      const fmtBtn = document.createElement('button');
      fmtBtn.type = 'button';
      fmtBtn.className = 'btn btn-ghost';
      fmtBtn.style.cssText = 'margin-left:12px;padding:3px 10px;font-size:12px;';
      fmtBtn.textContent = 'Format JSON';
      fmtBtn.setAttribute('aria-label', 'Format Raw Configuration JSON');

      const ta = document.createElement('textarea');
      ta.id = 'raw-editor';
      ta.setAttribute('aria-label', 'Raw configuration JSON');
      ta.style.cssText = 'width:100%;min-height:60vh;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:12px;font-family:ui-monospace,monospace;font-size:12px;resize:vertical;';
      ta.value = JSON.stringify(window.cfg, null, 2);
      ta.addEventListener('input', () => {
        fields.classList.add('dirty');
        setDirty('__raw__');
      });

      fmtBtn.addEventListener('click', () => {
        try {
          const parsed = JSON.parse(ta.value);
          ta.value = JSON.stringify(parsed, null, 2);
          fields.classList.add('dirty');
          setDirty('__raw__');
          if (typeof showToast === 'function') showToast('JSON formatted', 'success');
        } catch (err) {
          if (typeof showToast === 'function') {
            showToast('Invalid JSON in Raw Configuration: ' + err.message, 'error');
          }
        }
      });

      desc.appendChild(fmtBtn);
      fields.replaceChildren();
      fields.appendChild(ta);
    }
    setDirty('__raw__');
    if (title && shouldFocus) title.focus();
    return;
  }

  if (key === 'llm_decipher') {
    // Editable here too, but persistence belongs to the LLM settings API:
    // values are loaded from the effective (runtime) state and saved through
    // /api/llm/settings, never written to config.yaml by this page.
    if (title) title.textContent = sectionLabels[key] || 'LLM Decipher';
    if (desc) desc.textContent = 'LLM profiles in use — saved through the LLM settings API';
    fields.replaceChildren();
    fields.classList.remove('dirty');
    if (typeof renderLLMSection === 'function') renderLLMSection(fields);
    if (title && shouldFocus) title.focus();
    return;
  }

  if (title) title.textContent = sectionLabels[key] || key;
  if (desc) desc.textContent = hints[key] || 'Configuration section';
  fields.replaceChildren();
  if (typeof hideTooltip === 'function') hideTooltip();
  if (typeof renderQuickActions === 'function') renderQuickActions(fields, key);
  if (typeof renderObject === 'function' && window.cfg) {
    renderObject(fields, window.cfg[key], key);
  }
  fields.classList.remove('dirty');

  // re-mark dirty fields visually
  if (window.dirty) {
    window.dirty.forEach(p => {
      document.querySelectorAll(`[data-path="${p}"]`).forEach(el => el.classList.add('dirty'));
    });
  }
  if (title && shouldFocus) title.focus();
}

// Global exports
window.clearSidebarSearch = clearSidebarSearch;
window.filterSidebar = filterSidebar;
window.buildSidebar = buildSidebar;
window.selectSection = selectSection;
