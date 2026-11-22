/**
 * Ride the API — Dashboard Buffer & Analysis Module
 * Context notes, buffer entries, pattern preview and commit, LLM dispatch.
 */


async function loadContextNotes(deviceId) {
  try {
    const res = await fetch(`/api/devices/${deviceId}/context`);
    if (!res.ok) return;
    const data = await res.json();
    const ta = document.getElementById('context-notes-' + deviceId);
    if (ta) ta.value = data.context_notes || '';
  } catch(e) { /* ignore */ }
}

async function saveContextNotes(deviceId) {
  const ta = document.getElementById('context-notes-' + deviceId);
  if (!ta) return;
  const btn = document.getElementById('save-notes-btn-' + deviceId);
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
  try {
    const res = await fetch(`/api/devices/${deviceId}/context`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({context_notes: ta.value}),
    });
    if (!res.ok) return showToast('Failed to save notes', 'error');
    showToast('Context notes saved', 'success');
  } catch(e) { showToast('Failed to save notes', 'error'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = origText; } }
}

async function loadBufferEntries(deviceId) {
  const container = document.getElementById('buffer-list-' + deviceId);
  const countSpan = document.getElementById('buf-count-' + deviceId);
  if (!container) return;
  try {
    const res = await fetch(`/api/devices/${deviceId}/buffer`);
    if (!res.ok) {
      container.innerHTML = '<div style="color:var(--text2);font-size:12px;text-align:center;padding:12px;">Failed to load buffer</div>';
      return;
    }
    const data = await res.json();
    const entries = data.entries || [];
    if (countSpan) countSpan.textContent = entries.length + ' entries (' + (data.total_size_bytes / 1024).toFixed(1) + ' KB)';
    if (entries.length === 0) {
      container.innerHTML = '<div style="color:var(--text2);font-size:12px;text-align:center;padding:12px;">No buffer entries</div>';
      return;
    }
    container.innerHTML = entries.map(e => {
          const method = e.pair?.method || '';
          const path = e.pair?.path || '';
          const status = e.pair?.response_status || '';
          const reqPreview = (e.pair?.request_body || '').slice(0, 80);
          const resPreview = (e.pair?.response_body || '').slice(0, 80);
          const reqFull = JSON.stringify(e.pair?.request_body || '', null, 2).slice(0, 300);
          const resFull = JSON.stringify(e.pair?.response_body || '', null, 2).slice(0, 300);
    const ts = e.timestamp ? new Date(e.timestamp).toLocaleString() : '';
          return `<div class="buffer-entry" id="buf-entry-${escapeJs(deviceId)}-${e.id}" tabindex="0" role="button" aria-expanded="false" aria-controls="buf-detail-${escapeJs(deviceId)}-${e.id}" onclick="toggleBufDetail('${escapeJs(deviceId)}', ${e.id})" onkeydown="if(event.key==='Enter'||event.key===' '){if(event.target===this||event.target.classList.contains('be-info')||event.target.classList.contains('be-pair')||event.target.classList.contains('be-summary')){event.preventDefault();toggleBufDetail('${escapeJs(deviceId)}', ${e.id});}}">
                  <input type="checkbox" id="buf-chk-${escapeJs(deviceId)}-${e.id}" aria-label="Select buffer entry ${escapeHtml(method)} ${escapeHtml(path)}" onclick="event.stopPropagation()" onchange="updateSelectedCount('${escapeJs(deviceId)}')">
            <div class="be-info">
                    <div class="be-pair"><span style="color:var(--accent)">${escapeHtml(method)}</span> ${escapeHtml(path)}</div>
                    <div class="be-summary">&#x2192; ${escapeHtml(status)} &middot; ${(e.size || 0) / 1024} KB${ts ? ' &middot; ' + ts : ''}</div>
            </div>
            <button class="be-delete" onclick="event.stopPropagation();deleteBufferEntry('${escapeJs(deviceId)}', ${e.id})" title="Delete entry" aria-label="Delete entry ${escapeHtml(method)} ${escapeHtml(path)}">&times;</button>
          </div>
          <div class="buffer-entry-detail" id="buf-detail-${escapeJs(deviceId)}-${e.id}">
    <b>Request:</b> ${escapeHtml(reqFull)}
    <b>Response:</b> ${escapeHtml(resFull)}
          </div>`;
        }).join('');
  } catch(e) {
    container.innerHTML = '<div style="color:var(--red);font-size:12px;text-align:center;padding:12px;">Error loading buffer</div>';
  }
}

function toggleBufDetail(deviceId, entryId) {
  const detail = document.getElementById('buf-detail-' + deviceId + '-' + entryId);
  const entry = document.getElementById('buf-entry-' + deviceId + '-' + entryId);
  if (detail) {
    const isOpen = detail.classList.toggle('open');
    if (entry) entry.setAttribute('aria-expanded', isOpen);
  }
}

function toggleSelectAll(deviceId) {
  const checked = document.getElementById('select-all-buf-' + deviceId).checked;
  const container = document.getElementById('buffer-list-' + deviceId);
  if (!container) return;
  container.querySelectorAll('.buffer-entry input[type="checkbox"]').forEach(cb => cb.checked = checked);
  updateSelectedCount(deviceId);
}

function getSelectedPairIds(deviceId) {
  const ids = [];
  const container = document.getElementById('buffer-list-' + deviceId);
  if (!container) return ids;
  container.querySelectorAll('.buffer-entry input[type="checkbox"]').forEach(cb => {
    if (cb.checked) {
      const id = parseInt(cb.id.split('-').pop());
      if (!isNaN(id)) ids.push(id);
    }
  });
  return ids;
}

function updateSelectedCount(deviceId) {
  const container = document.getElementById('buffer-list-' + deviceId);
  if (container) {
    container.querySelectorAll('.buffer-entry').forEach(card => {
      const cb = card.querySelector('input[type="checkbox"]');
      if (cb) {
        card.classList.toggle('be-checked', cb.checked);
      }
    });
  }
  const ids = getSelectedPairIds(deviceId);
  const previewBtn = document.getElementById('preview-btn-' + deviceId);
  const sendBtn = document.getElementById('send-btn-' + deviceId);
  if (previewBtn) previewBtn.textContent = ids.length ? '\u{1F50D} Preview (' + ids.length + ')' : '\u{1F50D} Preview & Edit';
  if (sendBtn) sendBtn.textContent = ids.length ? '\u{27A4} Send (' + ids.length + ')' : '\u{27A4} Send to LLM';
}

async function deleteBufferEntry(deviceId, entryId) {
  try {
    const res = await fetch(`/api/devices/${deviceId}/buffer/${entryId}`, { method: 'DELETE' });
    if (!res.ok) return showToast('Failed to delete entry', 'error');
    showToast('Entry deleted', 'success');
    loadBufferEntries(deviceId);
    updateSelectedCount(deviceId);
  } catch(e) { showToast('Failed to delete entry', 'error'); }
}

async function previewAnalysis(deviceId) {
  const pairIds = getSelectedPairIds(deviceId);
  if (pairIds.length === 0) {
    showToast('Select at least one buffer entry', 'warning');
    return;
  }
  const notes = (document.getElementById('context-notes-' + deviceId) || {}).value || '';
  const btn = document.getElementById('preview-btn-' + deviceId);
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Analyzing...'; }
  showToast('Running LLM analysis...', 'info');
  try {
    const res = await fetch(`/api/devices/${deviceId}/llm/preview`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pair_ids: pairIds, context_notes: notes}),
    });
    if (!res.ok) {
      const err = await res.json();
      return showToast(err.error || 'Analysis failed', 'error');
    }
    const data = await res.json();
    const box = document.getElementById('analysis-box-' + deviceId);
    const ta = document.getElementById('analysis-text-' + deviceId);
    if (box) box.style.display = '';
    if (ta) ta.value = JSON.stringify(data.analysis || data, null, 2);
    showToast('Preview ready — review and commit or discard', 'success');
  } catch(e) { showToast('Analysis failed: ' + e.message, 'error'); }
  finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

function formatAnalysisJson(deviceId) {
  const ta = document.getElementById('analysis-text-' + deviceId);
  if (!ta || !ta.value.trim()) return;
  try {
    const parsed = JSON.parse(ta.value);
    ta.value = JSON.stringify(parsed, null, 2);
    showToast('Analysis JSON formatted', 'success');
  } catch (err) {
    showToast('Invalid JSON in LLM Analysis Preview: ' + err.message, 'error');
  }
}

function copyAnalysisJson(deviceId) {
  const ta = document.getElementById('analysis-text-' + deviceId);
  if (!ta || !ta.value.trim()) return;
  copyToClipboard(ta.value, 'LLM Analysis Preview');
}

function discardPreview(deviceId) {
  const box = document.getElementById('analysis-box-' + deviceId);
  if (box) box.style.display = 'none';
}

async function commitPatterns(deviceId) {
  const ta = document.getElementById('analysis-text-' + deviceId);
  if (ta && ta.value.trim()) {
    try {
      JSON.parse(ta.value);
    } catch (err) {
      showToast('Invalid JSON in LLM Analysis Preview: ' + err.message, 'error');
      return;
    }
  }
  const pairIds = getSelectedPairIds(deviceId);
  const notes = (document.getElementById('context-notes-' + deviceId) || {}).value || '';
  const box = document.getElementById('analysis-box-' + deviceId);
  const btn = box ? box.querySelector('button.btn-primary') : null;
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Committing...'; }
  showToast('Saving patterns...', 'info');
  try {
    const res = await fetch(`/api/devices/${deviceId}/llm/flush`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pair_ids: pairIds, context_notes: notes}),
    });
    if (!res.ok) {
      const err = await res.json();
      return showToast(err.error || 'Failed to commit', 'error');
    }
    const data = await res.json();
    discardPreview(deviceId);
    showToast(data.patterns_count + ' patterns saved', 'success');
    loadBufferEntries(deviceId);
    updateSelectedCount(deviceId);
    loadDevices();
  } catch(e) { showToast('Failed to commit: ' + e.message, 'error'); }
  finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function sendToLLM(deviceId) {
  const pairIds = getSelectedPairIds(deviceId);
  const notes = (document.getElementById('context-notes-' + deviceId) || {}).value || '';
  if (pairIds.length === 0) {
    showToast('Select at least one buffer entry', 'warning');
    return;
  }
  if (window.showConfirm) {
    const ok = await showConfirm({
      title: 'Send Entries to LLM',
      message: 'Send ' + pairIds.length + ' selected entries to LLM for decoding?',
      confirmText: 'Send to LLM'
    });
    if (!ok) return;
  } else if (!confirm('Send ' + pairIds.length + ' selected entries to LLM for decoding?')) {
    return;
  }
  const btn = document.getElementById('send-btn-' + deviceId);
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Sending...'; }
  showToast('Sending to LLM...', 'info');
  try {
    const res = await fetch(`/api/devices/${deviceId}/llm/flush`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pair_ids: pairIds, context_notes: notes}),
    });
    if (!res.ok) {
      const err = await res.json();
      return showToast(err.error || 'LLM processing failed', 'error');
    }
    const data = await res.json();
    showToast(data.patterns_count + ' patterns learned from ' + data.pairs_count + ' pairs', 'success');
    loadBufferEntries(deviceId);
    updateSelectedCount(deviceId);
    loadDevices();
  } catch(e) { showToast('LLM processing failed: ' + e.message, 'error'); }
  finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function uploadSnifferCapture(deviceId, input) {
  if (!input || !input.files || input.files.length === 0) return;
  const file = input.files[0];
  const formData = new FormData();
  formData.append('file', file);

  showToast('Uploading and parsing ' + file.name + '...', 'info');
  try {
    const res = await fetch('/api/devices/' + encodeURIComponent(deviceId) + '/sniffer/import', {
      method: 'POST',
      body: formData,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.error || 'Failed to import sniffer capture', 'error');
      return;
    }
    const data = await res.json();
    showToast('Imported ' + data.imported + ' pairs from ' + String(data.format || 'capture').toUpperCase() + ' file!', 'success');
    input.value = '';
    loadBufferEntries(deviceId);
    if (typeof loadDevices === 'function') loadDevices();
  } catch(e) {
    showToast('Failed to upload capture: ' + e.message, 'error');
  }
}


// Global exports for inline HTML event handlers
window.loadContextNotes = loadContextNotes;
window.saveContextNotes = saveContextNotes;
window.loadBufferEntries = loadBufferEntries;
window.toggleBufDetail = toggleBufDetail;
window.toggleSelectAll = toggleSelectAll;
window.getSelectedPairIds = getSelectedPairIds;
window.updateSelectedCount = updateSelectedCount;
window.deleteBufferEntry = deleteBufferEntry;
window.previewAnalysis = previewAnalysis;
window.formatAnalysisJson = formatAnalysisJson;
window.copyAnalysisJson = copyAnalysisJson;
window.discardPreview = discardPreview;
window.commitPatterns = commitPatterns;
window.sendToLLM = sendToLLM;
window.uploadSnifferCapture = uploadSnifferCapture;
