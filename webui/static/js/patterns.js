/**
 * Ride the API — Patterns Page Logic
 * Handles pattern listing, editing, creation, simulation, and device list.
 */

  function deviceIdFromPath(pathname){
    const parts = (pathname || '').split('/').filter(Boolean);
    return parts.length > 1 && parts[0] === 'patterns' ? decodeURIComponent(parts[1]) : '';
  }

  (function(){
    const deviceId = deviceIdFromPath(window.location.pathname);
  const cardsEl = document.getElementById('cards');
  const toasts = document.getElementById('toasts');

  function showToast(msg, type='info'){
    const t = document.createElement('div');
    const toastType = type || 'info';
    t.className = 'toast ' + toastType;
    const icons = { success: '✓', error: '✕', warning: '⚠', info: 'ℹ' };
    const iconSpan = document.createElement('span');
    iconSpan.className = 'toast-icon';
    iconSpan.setAttribute('aria-hidden', 'true');
    iconSpan.textContent = icons[toastType] || 'ℹ';
    const textSpan = document.createElement('span');
    textSpan.textContent = msg;
    t.appendChild(iconSpan);
    t.appendChild(textSpan);
    toasts.appendChild(t);
    setTimeout(()=> t.remove(), 4000);
  }

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
      if (ok) onSuccess(); else onError();
    } catch (e) {
      onError();
    }
  }

  function copyToClipboard(text, label) {
    if (!text) return;
    const onSuccess = () => showToast((label || 'Text') + ' copied to clipboard', 'success');
    const onError = () => showToast('Failed to copy ' + (label || 'text'), 'error');

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(onSuccess).catch(() => {
        fallbackCopyText(text, onSuccess, onError);
      });
    } else {
      fallbackCopyText(text, onSuccess, onError);
    }
  }

  function el(tag,attrs={},children=[]){ const e=document.createElement(tag); for(const k in attrs){ if(k==='html') e.innerHTML=attrs[k]; else e.setAttribute(k,attrs[k]); } (Array.isArray(children)?children:[children]).forEach(c=>{ if(typeof c==='string') e.appendChild(document.createTextNode(c)); else if(c) e.appendChild(c); }); return e; }

  async function loadPatterns(){
    try{
      const res = await fetch(`/api/devices/${deviceId}/patterns`);
      if(!res.ok) throw new Error('Failed to load');
      const json = await res.json();
      renderPatterns(json.patterns || []);
    }catch(e){ cardsEl.innerHTML = '<div class="empty">Failed to load patterns</div>'; showToast('Failed to load patterns','error'); }
  }

  // No device in the URL: list the devices so the page is a usable entry point, instead of asking
  // the API for the patterns of a device named after the page.
  async function renderDeviceList(){
    const filterBar = document.querySelector('.pattern-filter-bar');
    if (filterBar) filterBar.style.display = 'none';
    const newBtn = document.getElementById('newPatternBtn');
    if (newBtn) newBtn.style.display = 'none';
    const subtitle = document.getElementById('patternsSubtitle');
    if (subtitle) subtitle.textContent = 'Choose a device to see what it has learned';

    try{
      const res = await fetch('/api/devices');
      if(!res.ok) throw new Error('Failed to load');
      const json = await res.json();
      const devices = json.devices || [];
      cardsEl.innerHTML = '';
      if(!devices.length){
        cardsEl.innerHTML = '<div class="empty"><div style="font-size:16px;font-weight:600;margin-bottom:6px;">No devices yet</div><div style="font-size:13px;color:var(--text2);">A device appears here once its traffic goes through the proxy.</div></div>';
        return;
      }
      devices.forEach(function(d){
        const id = d.device_id || '';
        if(!id) return;
        const card = el('div',{class:'pattern-card'});
        const header = el('div',{class:'pc-header'});
        header.appendChild(el('div',{class:'pc-title'}, d.name || id));
        const right = el('div',{},[]);
        right.appendChild(el('span',{class:'badge badge-low'}, d.mode || 'unknown'));
        header.appendChild(right);
        card.appendChild(header);

        const body = el('div',{},[]);
        const meta = t => el('div',{style:'font-size:13px;color:var(--text2);margin-top:4px;'}, t);
        body.appendChild(meta(`device id: ${id}`));
        const ips = (d.ip_addresses || []).join(', ');
        if(ips) body.appendChild(meta(`ip: ${ips}`));
        if(d.status) body.appendChild(meta(`status: ${d.status}`));
        body.appendChild(el('a',{class:'btn btn-primary',style:'margin-top:10px;display:inline-block;',href:`/patterns/${encodeURIComponent(id)}`},'View Patterns'));
        card.appendChild(body);
        cardsEl.appendChild(card);
      });
    }catch(e){ cardsEl.innerHTML = '<div class="empty">Failed to load devices</div>'; showToast('Failed to load devices','error'); }
  }

  function clearPatternSearch() {
    const input = document.getElementById('patternSearchInput');
    if (input) {
      input.value = '';
      input.focus();
      filterPatterns();
    }
  }
  window.clearPatternSearch = clearPatternSearch;

  function filterPatterns() {
    const query = (document.getElementById('patternSearchInput')?.value || '').trim().toLowerCase();
    const clearBtn = document.getElementById('clearPatternSearchBtn');
    if (clearBtn) clearBtn.style.display = query ? 'block' : 'none';

    const cards = document.querySelectorAll('#cards .pattern-card');
    let visibleCount = 0;
    cards.forEach(card => {
      const text = card.textContent.toLowerCase();
      const matches = !query || text.includes(query);
      card.style.display = matches ? '' : 'none';
      if (matches) visibleCount++;
    });

    const statusEl = document.getElementById('patternFilterStatus');
    if (statusEl && cards.length > 0) {
      statusEl.textContent = query ? `Showing ${visibleCount} of ${cards.length} patterns` : '';
    }

    let emptyMsg = document.getElementById('noMatchingPatterns');
    if (cards.length > 0 && visibleCount === 0) {
      if (!emptyMsg) {
        emptyMsg = document.createElement('div');
        emptyMsg.id = 'noMatchingPatterns';
        emptyMsg.className = 'empty';
        emptyMsg.innerHTML = '<div style="font-size:16px;font-weight:500;margin-bottom:4px;">No matching patterns found</div><div style="font-size:13px;color:var(--text2);">Try filtering with a different method, path, or intent.</div><button class="btn btn-ghost" style="margin-top:12px;font-size:12px;" onclick="clearPatternSearch()" aria-label="Clear pattern search filter">Clear search filter</button>';
        cardsEl.appendChild(emptyMsg);
      } else {
        emptyMsg.style.display = '';
      }
    } else if (emptyMsg) {
      emptyMsg.style.display = 'none';
    }
  }
  window.filterPatterns = filterPatterns;

  function renderPatterns(list){
    cardsEl.innerHTML='';
    if(!list.length){
      cardsEl.innerHTML = '<div class="empty"><div style="font-size:16px;font-weight:600;margin-bottom:6px;">No patterns created yet</div><div style="font-size:13px;color:var(--text2);margin-bottom:14px;">Patterns are automatically learned when analyzing device traffic, or you can create one manually.</div><button class="btn btn-primary" onclick="renderNewPatternCard()" aria-label="Create a new pattern">+ Create Pattern</button></div>';
      return;
    }
    list.forEach(p=>{
      const card = el('div',{class:'pattern-card'});
      const bodyId = `pc-body-${p.pattern_id}`;
      const header = el('div',{class:'pc-header',tabindex:0,role:'button','aria-expanded':'false','aria-controls':bodyId,'aria-label':`Toggle pattern details for ${p.method} ${p.path}`});
      const title = el('div',{class:'pc-title'},`${p.method} ${p.path} — ${p.intent || ''}`);
      const right = el('div',{},[]);
      const badge = el('span',{class:'badge badge-low'}, (p.confidence||0).toFixed(2));
      const hits = el('span',{style:'margin-left:8px;color:var(--text2);font-size:13px;'},`hits: ${p.hit_count||0}`);
      right.appendChild(badge); right.appendChild(hits);
      header.appendChild(title); header.appendChild(right);
      card.appendChild(header);

      const body = el('div',{class:'pc-body',id:bodyId});
      // Build form fields
      const form = el('div',{});
      form.appendChild(fieldRow('Method', methodInput(p.method)));
      form.appendChild(fieldRow('Path pattern', inputText('path_pattern', p.path||'')));
            form.appendChild(fieldRow('Protocol', protocolInput(p.protocol||'http')));
            form.appendChild(fieldRow('Intent', inputText('intent', p.intent||'')));
            form.appendChild(fieldRow('Confidence', inputNumber('confidence', p.confidence||0.5)));
            form.appendChild(fieldRow('Required headers', tagInput('required_headers', p.required_headers||[])));
            form.appendChild(fieldRow('Query params', tagInput('query_param_keys', p.query_param_keys||[])));
            form.appendChild(fieldRow('Body schema (JSON)', textarea('body_schema', JSON.stringify(p.body_schema||{},null,2))));

            // Response template
            const tpl = p.response_template||{};
            form.appendChild(el('h3',{},'Response'));
            form.appendChild(fieldRow('Status code', inputNumber('status_code', tpl.status_code||200)));
            form.appendChild(fieldRow('Headers template (JSON)', textarea('headers_template', JSON.stringify(tpl.headers_template||{},null,2))));
            form.appendChild(fieldRow('Body template (JSON)', textarea('body_template', JSON.stringify(tpl.body_template||{},null,2))));
            form.appendChild(fieldRow('Expected variables', tagInput('expected_variables', tpl.expected_variables||[])));

      // Field mappings
      const fmContainer = el('div',{class:'fm-list'});
      const fmHeader = el('div',{},[]);
      fmHeader.appendChild(el('strong',{},'Field mappings'));
      const addMappingBtn = el('button',{class:'btn btn-ghost',type:'button',style:'margin-left:8px;','aria-label':'Add field mapping'},'Add mapping');
      fmHeader.appendChild(addMappingBtn);
      fmContainer.appendChild(fmHeader);
      const fms = (tpl.field_mappings && Array.isArray(tpl.field_mappings) ? tpl.field_mappings : []);
      const fmItems = el('div',{});
      fms.forEach(m=> fmItems.appendChild(mappingItem(m)));
      addMappingBtn.addEventListener('click', ()=> fmItems.appendChild(mappingItem({})));
      fmContainer.appendChild(fmItems);
      form.appendChild(fmContainer);

      body.appendChild(form);

      const actions = el('div',{class:'actions'});
      const saveBtn = el('button',{class:'btn btn-primary','aria-label':`Save pattern ${p.method} ${p.path}`,title:`Save pattern ${p.method} ${p.path}`},'Save');
      const delBtn = el('button',{class:'btn btn-danger','aria-label':`Delete pattern ${p.method} ${p.path}`,title:`Delete pattern ${p.method} ${p.path}`},'Delete');
      actions.appendChild(delBtn); actions.appendChild(saveBtn);
      body.appendChild(actions);

      card.appendChild(body);

      // Toggle expand
      const toggleExpand = ()=>{
        const isHidden = getComputedStyle(body).display === 'none';
        body.style.display = isHidden ? 'block' : 'none';
        header.setAttribute('aria-expanded', isHidden ? 'true' : 'false');
      };
      header.addEventListener('click', toggleExpand);
      header.addEventListener('keydown', (e)=>{ if(e.key==='Enter' || e.key===' ') { e.preventDefault(); toggleExpand(); } });

      // Save handler
      saveBtn.addEventListener('click', async ()=>{
        if(saveBtn.disabled) return;
        saveBtn.disabled = true;
        const origText = saveBtn.textContent;
        saveBtn.textContent = 'Saving…';
        try{
          const payload = collectPayload(form);
          const resp = await fetch(`/api/devices/${deviceId}/patterns/${p.pattern_id}`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
          if(!resp.ok) throw new Error('save failed');
          showToast('Pattern saved','success');
          loadPatterns();
        }catch(err){
          showToast(err.message || 'Save failed','error');
          saveBtn.disabled = false;
          saveBtn.textContent = origText;
        }
      });

      delBtn.addEventListener('click', async ()=>{
        if(delBtn.disabled) return;
        if(!confirm('Delete this pattern?')) return;
        delBtn.disabled = true;
        const origText = delBtn.textContent;
        delBtn.textContent = 'Deleting…';
        try{
          const resp = await fetch(`/api/devices/${deviceId}/patterns/${p.pattern_id}`, { method: 'DELETE' });
          if(!resp.ok) throw new Error('delete failed');
          showToast('Pattern deleted','success');
          loadPatterns();
        }catch(e){
          showToast('Delete failed','error');
          delBtn.disabled = false;
          delBtn.textContent = origText;
        }
      });

      cardsEl.appendChild(card);
    });
    filterPatterns();
  }

  function fieldRow(labelText, inputEl){
    const row = el('div',{class:'row'});
    const col = el('div',{class:'col'});
    let inputId = inputEl ? inputEl.id : '';
    if (inputEl && !inputId) {
      const target = (inputEl.tagName === 'INPUT' || inputEl.tagName === 'SELECT' || inputEl.tagName === 'TEXTAREA')
        ? inputEl
        : inputEl.querySelector('input, select, textarea');
      if (target) {
        if (!target.id) {
          const name = inputEl.name || (inputEl.dataset && inputEl.dataset.name) || target.name || 'field';
          target.id = 'field-' + name + '-' + Math.random().toString(36).slice(2, 7);
        }
        inputId = target.id;
      }
    }
    const labelAttrs = { style: 'margin-bottom:0;cursor:pointer;' };
    if (inputId) labelAttrs['for'] = inputId;
    const labelHeader = el('div',{style:'display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;'},[
      el('label', labelAttrs, labelText)
    ]);
    if (inputEl && inputEl.tagName === 'TEXTAREA') {
      const actionsGroup = el('div', {style:'display:flex;gap:6px;'});
      const formatBtn = el('button',{
        type:'button',
        class:'btn btn-ghost',
        style:'padding:2px 8px;font-size:11px;line-height:1.2;',
        'aria-label': `Format ${labelText} JSON`
      }, 'Format JSON');
      formatBtn.addEventListener('click', ()=>{
        if (!inputEl.value.trim()) return;
        try {
          const parsed = JSON.parse(inputEl.value);
          inputEl.value = JSON.stringify(parsed, null, 2);
          showToast(`${labelText} formatted`, 'success');
        } catch (err) {
          showToast(`Invalid JSON in ${labelText}: ${err.message}`, 'error');
        }
      });
      const copyBtn = el('button',{
        type:'button',
        class:'btn btn-ghost',
        style:'padding:2px 8px;font-size:11px;line-height:1.2;',
        'aria-label': `Copy ${labelText} to clipboard`
      }, 'Copy JSON');
      copyBtn.addEventListener('click', ()=>{
        copyToClipboard(inputEl.value, labelText);
      });
      actionsGroup.appendChild(formatBtn);
      actionsGroup.appendChild(copyBtn);
      labelHeader.appendChild(actionsGroup);
    }
    col.appendChild(labelHeader);
    col.appendChild(inputEl);
    row.appendChild(col);
    return row;
  }

  function inputText(name, val){ const i=document.createElement('input'); i.type='text'; i.name=name; i.value = val||''; return i; }
  function inputNumber(name, val){ const i=document.createElement('input'); i.type='number'; i.name=name; i.value = val||0; return i; }
  function textarea(name, val){ const t=document.createElement('textarea'); t.name=name; t.value = val||''; return t; }
  function methodInput(val){ const s=document.createElement('select'); ['GET','POST','PUT','PATCH','DELETE'].forEach(m=>{ const o=document.createElement('option'); o.value=m; o.textContent=m; if(m===val) o.selected=true; s.appendChild(o); }); s.name='method'; return s; }

  function protocolInput(val){ const s=document.createElement('select'); ['http','mqtt','coap','modbus','websocket','raw_tcp','http2'].forEach(m=>{ const o=document.createElement('option'); o.value=m; o.textContent=m; if(m===val) o.selected=true; s.appendChild(o); }); s.name='protocol'; return s; }

  function tagInput(name, items){ const wrap = document.createElement('div'); wrap.className='tags'; wrap.dataset.name = name; items = Array.isArray(items)?items.slice():[];
      const input = document.createElement('input'); input.type='text'; input.placeholder='Add...'; input.setAttribute('aria-label', `Add ${name.replace(/_/g, ' ')}`);
      function render(focusInput = false){ wrap.innerHTML=''; items.forEach((it,idx)=>{ const removeBtn = el('button',{type:'button','aria-label':`Remove ${it}`},'×'); removeBtn.addEventListener('click', ()=>{ items.splice(idx,1); render(true); }); const t = el('span',{class:'tag'}, [it, removeBtn]); wrap.appendChild(t); }); wrap.appendChild(input); if(focusInput) input.focus(); }
      function addTag(val, focusInput = true){ const v = val.trim().replace(/^,|,$/g, ''); if(v){ items.push(v); input.value=''; render(focusInput); } }
      input.addEventListener('keydown', (e)=>{ if(e.key==='Enter' || e.key===','){ e.preventDefault(); addTag(input.value, true); } else if(e.key==='Backspace' && input.value==='' && items.length > 0){ e.preventDefault(); items.pop(); render(true); } });
      input.addEventListener('blur', ()=>{ if(input.value.trim()) addTag(input.value, false); });
      wrap.getValue = () => items.slice();
      render(false); return wrap; }

  function mappingItem(m){ const div = el('div',{class:'fm-item'});
    const src = inputText('request_field', m.request_field||''); src.placeholder = 'Request field (e.g. body.state)'; src.setAttribute('aria-label', 'Request field');
      const srcType = inputText('request_type', m.request_type||''); srcType.placeholder = 'Req type'; srcType.setAttribute('aria-label', 'Request field type');
      const dst = inputText('response_field', m.response_field||''); dst.placeholder = 'Response field (e.g. state.on)'; dst.setAttribute('aria-label', 'Response field');
      const dstType = inputText('response_type', m.response_type||''); dstType.placeholder = 'Resp type'; dstType.setAttribute('aria-label', 'Response field type');
      const tr = el('select',{'aria-label':'Transform mode'},[]); ['none','copy','transform','enum'].forEach(opt=>{ const o=document.createElement('option'); o.value=opt; o.textContent=opt; if(opt=== (m.transform||'none')) o.selected=true; tr.appendChild(o); });
      const enumVals = inputText('enum_values', Array.isArray(m.enum_values) ? m.enum_values.join(', ') : (m.enum_values||'')); enumVals.placeholder = 'Enum values (comma-sep)'; enumVals.setAttribute('aria-label', 'Enum values');
      const rem = el('button',{class:'btn btn-ghost',type:'button','aria-label':'Remove field mapping'},'Remove'); rem.addEventListener('click', ()=> div.remove()); div.appendChild(src); div.appendChild(srcType); div.appendChild(dst); div.appendChild(dstType); div.appendChild(tr); div.appendChild(enumVals); div.appendChild(rem); return div; }

  function parseJsonField(form, name, label) {
    const el = form.querySelector(`textarea[name=${name}]`);
    if (!el || !el.value.trim()) return {};
    try {
      return JSON.parse(el.value);
    } catch (e) {
      throw new Error(`Invalid JSON in ${label}: ${e.message}`);
    }
  }

  function collectPayload(form){ const payload = {};
      const methodEl = form.querySelector('select[name=method]'); if(methodEl) payload.method = methodEl.value;
      const pathEl = form.querySelector('input[name=path_pattern]'); if(pathEl) payload.path_pattern = pathEl.value;
        const protocolEl = form.querySelector('select[name=protocol]'); if(protocolEl) payload.protocol = protocolEl.value;
        const intentEl = form.querySelector('input[name=intent]'); if(intentEl) payload.intent = intentEl.value;
        const confEl = form.querySelector('input[name=confidence]'); if(confEl) payload.confidence = Number(confEl.value||0);
        const tagWraps = form.querySelectorAll('.tags');
        tagWraps.forEach(w => { if(w.dataset.name === 'required_headers' && typeof w.getValue === 'function') payload.required_headers = w.getValue(); });
        tagWraps.forEach(w => { if(w.dataset.name === 'query_param_keys' && typeof w.getValue === 'function') payload.query_param_keys = w.getValue(); });
        payload.body_schema = parseJsonField(form, 'body_schema', 'Body schema');
        // response
        payload.response_template = {};
        const statusEl = form.querySelector('input[name=status_code]'); if(statusEl) payload.response_template.status_code = Number(statusEl.value||200);
        payload.response_template.headers_template = parseJsonField(form, 'headers_template', 'Headers template');
        payload.response_template.body_template = parseJsonField(form, 'body_template', 'Body template');
        tagWraps.forEach(w => { if(w.dataset.name === 'expected_variables' && typeof w.getValue === 'function') payload.response_template.expected_variables = w.getValue(); });
        // field mappings
        const fmDiv = form.querySelector('.fm-list'); const mappings = [];
        if(fmDiv){ const items = fmDiv.querySelectorAll('.fm-item'); items.forEach(it=>{
          const inputs = it.querySelectorAll('input, select'); if(inputs.length>=3){ mappings.push({ request_field: inputs[0].value, request_type: inputs[1].value, response_field: inputs[2].value, response_type: inputs[3].value, transform: inputs[4].value, enum_values: inputs[5].value ? inputs[5].value.split(',').map(s=>s.trim()).filter(Boolean) : undefined }); }
        }); }
        payload.field_mappings = mappings;
                payload.response_template.field_mappings = mappings;
                return payload;
      }

  function renderNewPatternCard(){
      const p = {
        pattern_id: 'new_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
        method: 'GET', path: '/', protocol: 'http', intent: '', confidence: 0.5,
        required_headers: [], query_param_keys: [], body_schema: {},
        response_template: { status_code: 200, headers_template: {}, body_template: {}, expected_variables: [] },
        field_mappings: [],
      };
      const card = el('div',{class:'pattern-card'});
      const bodyId = `pc-body-${p.pattern_id}`;
      const header = el('div',{class:'pc-header',tabindex:0,role:'button','aria-expanded':'true','aria-controls':bodyId,'aria-label':'New pattern'});
      const title = el('div',{class:'pc-title'},'New pattern');
      const right = el('div',{},[]);
      const badge = el('span',{class:'badge badge-low'},'new');
      right.appendChild(badge);
      header.appendChild(title); header.appendChild(right);
      card.appendChild(header);

      const body = el('div',{class:'pc-body',id:bodyId});
      body.style.display = 'block';
      const form = el('div',{});
      form.appendChild(fieldRow('Method', methodInput(p.method)));
      form.appendChild(fieldRow('Path pattern', inputText('path_pattern', p.path)));
      form.appendChild(fieldRow('Protocol', protocolInput(p.protocol)));
      form.appendChild(fieldRow('Intent', inputText('intent', p.intent)));
      form.appendChild(fieldRow('Confidence', inputNumber('confidence', p.confidence)));
      form.appendChild(fieldRow('Required headers', tagInput('required_headers', [])));
      form.appendChild(fieldRow('Query params', tagInput('query_param_keys', [])));
      form.appendChild(fieldRow('Body schema (JSON)', textarea('body_schema', '{}')));
      form.appendChild(el('h3',{},'Response'));
      form.appendChild(fieldRow('Status code', inputNumber('status_code', 200)));
      form.appendChild(fieldRow('Headers template (JSON)', textarea('headers_template', '{}')));
      form.appendChild(fieldRow('Body template (JSON)', textarea('body_template', '{}')));
      form.appendChild(fieldRow('Expected variables', tagInput('expected_variables', [])));
      const fmContainer = el('div',{class:'fm-list'});
      const fmHeader = el('div',{},[]);
      fmHeader.appendChild(el('strong',{},'Field mappings'));
      const addMappingBtn = el('button',{class:'btn btn-ghost',type:'button',style:'margin-left:8px;','aria-label':'Add field mapping'},'Add mapping');
      fmHeader.appendChild(addMappingBtn);
      fmContainer.appendChild(fmHeader);
      const fmItems = el('div',{});
      addMappingBtn.addEventListener('click', ()=> fmItems.appendChild(mappingItem({})));
      fmContainer.appendChild(fmItems);
      form.appendChild(fmContainer);
      body.appendChild(form);

      const actions = el('div',{class:'actions'});
      const saveBtn = el('button',{class:'btn btn-primary','aria-label':'Save new pattern',title:'Save new pattern'},'Save');
      const cancelBtn = el('button',{class:'btn btn-ghost','aria-label':'Cancel new pattern',title:'Cancel new pattern'},'Cancel');
      actions.appendChild(cancelBtn); actions.appendChild(saveBtn);
      body.appendChild(actions);
      card.appendChild(body);

      cancelBtn.addEventListener('click', ()=> { card.remove(); });
      saveBtn.addEventListener('click', async ()=>{
        if(saveBtn.disabled) return;
        saveBtn.disabled = true;
        const origText = saveBtn.textContent;
        saveBtn.textContent = 'Saving…';
        try{
          const payload = collectPayload(form);
          const resp = await fetch(`/api/devices/${deviceId}/patterns/${p.pattern_id}`, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(payload) });
          if(!resp.ok) throw new Error('save failed');
          showToast('Pattern created','success');
          loadPatterns();
        }catch(err){
          showToast(err.message || 'Save failed','error');
          saveBtn.disabled = false;
          saveBtn.textContent = origText;
        }
      });

      cardsEl.prepend(card);
      card.scrollIntoView({behavior:'smooth', block:'start'});
    }

    // Without a device there is nothing per-device to wire up: list the devices and stop.
    if (!deviceId) {
      renderDeviceList();
      return;
    }

    document.getElementById('newPatternBtn').addEventListener('click', renderNewPatternCard);

    document.addEventListener('keydown', function(event) {
      if (event.key === '/' || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k')) {
        const active = document.activeElement;
        const isEditing = active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.tagName === 'SELECT' || active.isContentEditable);
        if (!isEditing) {
          const searchInput = document.getElementById('patternSearchInput');
          if (searchInput) {
            event.preventDefault();
            searchInput.focus();
            searchInput.select();
          }
        }
      }
    });

    loadPatterns();
  })();
