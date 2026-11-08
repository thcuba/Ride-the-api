/**
 * Ride the API — Control-plane authentication & API client
 * Shared across dashboard, config, and patterns pages.
 */

(function () {
  const KEY_STORAGE = 'rideApiKey';
  const SESSION_STORAGE = 'rideSessionKey';
  let apiKey = localStorage.getItem(KEY_STORAGE) || sessionStorage.getItem(SESSION_STORAGE) || '';
  const keyInput = () => document.getElementById('apiKeyInput');

  function getApiKey() {
    return apiKey;
  }
  window.getApiKey = getApiKey;

  function storeKey(key, remember) {
    if (remember) {
      localStorage.setItem(KEY_STORAGE, key);
      sessionStorage.removeItem(SESSION_STORAGE);
    } else {
      sessionStorage.setItem(SESSION_STORAGE, key);
      localStorage.removeItem(KEY_STORAGE);
    }
  }
  window.storeKey = storeKey;

  function setApiKey(key, notify, remember) {
    apiKey = (key || '').trim();
    if (apiKey) {
      storeKey(apiKey, remember !== false);
    } else {
      localStorage.removeItem(KEY_STORAGE);
      sessionStorage.removeItem(SESSION_STORAGE);
    }
    if (keyInput()) {
      keyInput().value = apiKey;
    }
    if (notify && typeof showToast === 'function') {
      showToast(apiKey ? 'Password saved' : 'Password cleared', 'info');
    }
  }
  window.setApiKey = setApiKey;

  // Ask the user whether to remember the key on this machine for automatic
  // entry. Resolves true = save to localStorage, false = current session only.
  function askRememberKey(key) {
    return new Promise(function (resolve) {
      const overlay = document.createElement('div');
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');
      overlay.setAttribute('aria-labelledby', 'remember-key-title');
      overlay.style.cssText =
        'position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:9500;';
      const box = document.createElement('div');
      box.style.cssText =
        'background:#141820;border:1px solid #2d3648;border-radius:12px;padding:24px;max-width:440px;width:90%;color:#e1e6ed;font-family:Inter,system-ui,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,.3);';
      const title = document.createElement('h2');
      title.id = 'remember-key-title';
      title.textContent = 'Save the control-plane password?';
      title.style.cssText = 'margin:0 0 8px;font-size:16px;color:#6c8cff;';
      const para = document.createElement('p');
      para.style.cssText = 'margin:0 0 12px;font-size:13px;color:#9ca8ba;line-height:1.5;';
      para.textContent =
        'The password was accepted. Save it on this machine so you can enter automatically next time?';
      const label = document.createElement('label');
      label.style.cssText =
        'display:flex;align-items:center;gap:8px;font-size:13px;color:#e1e6ed;margin:8px 0 16px;cursor:pointer;';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = true;
      label.appendChild(cb);
      label.appendChild(document.createTextNode('Remember this password on this machine'));
      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';
      const noBtn = document.createElement('button');
      noBtn.textContent = 'This session only';
      noBtn.style.cssText =
        'padding:8px 14px;border-radius:8px;border:1px solid #2d3648;background:#1a1f2b;color:#e1e6ed;font-family:inherit;font-size:13px;cursor:pointer;';
      const yesBtn = document.createElement('button');
      yesBtn.textContent = 'Save & Continue';
      yesBtn.style.cssText =
        'padding:8px 14px;border-radius:8px;border:none;background:linear-gradient(135deg,#6c8cff,#5b7ad0);color:#fff;font-weight:600;font-family:inherit;font-size:13px;cursor:pointer;';

      function done(remember) {
        overlay.remove();
        resolve(remember);
      }
      noBtn.addEventListener('click', function () {
        done(false);
      });
      yesBtn.addEventListener('click', function () {
        done(cb.checked);
      });
      actions.appendChild(noBtn);
      actions.appendChild(yesBtn);
      box.appendChild(title);
      box.appendChild(para);
      box.appendChild(label);
      box.appendChild(actions);
      overlay.appendChild(box);
      document.body.appendChild(overlay);
      yesBtn.focus();
    });
  }
  window.askRememberKey = askRememberKey;

  // First-run setup: shown when the server reports auth is enabled but no
  // password is configured yet (503 + X-Auth-Setup: required).
  function setupFirstPassword() {
    if (sessionStorage.getItem('ride-setup-shown')) return;
    sessionStorage.setItem('ride-setup-shown', '1');
    const overlay = document.createElement('div');
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'setup-pw-title');
    overlay.style.cssText =
      'position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:9500;';
    const box = document.createElement('div');
    box.style.cssText =
      'background:#141820;border:1px solid #2d3648;border-radius:12px;padding:24px;max-width:440px;width:90%;color:#e1e6ed;font-family:Inter,system-ui,sans-serif;box-shadow:0 8px 32px rgba(0,0,0,.3);';
    const title = document.createElement('h2');
    title.id = 'setup-pw-title';
    title.textContent = 'Set Control-Plane Password';
    title.style.cssText = 'margin:0 0 8px;font-size:16px;color:#6c8cff;';
    const para = document.createElement('p');
    para.style.cssText = 'margin:0 0 12px;font-size:13px;color:#9ca8ba;line-height:1.5;';
    para.textContent =
      'No password is configured. Set a control-plane password (minimum 8 characters) to secure administrative access. The one-time setup token guards this first-run window: it is printed in the server logs and stored in ridebase/setup-token.';

    const tokenWrap = document.createElement('div');
    tokenWrap.style.cssText = 'margin:0 0 12px;';
    const tokenLabel = document.createElement('label');
    tokenLabel.textContent = 'Setup token';
    tokenLabel.style.cssText = 'display:block;margin:0 0 4px;font-size:12px;color:#9ca8ba;';
    const tokenInput = document.createElement('input');
    tokenInput.type = 'text';
    tokenInput.autocomplete = 'off';
    tokenInput.placeholder = 'First-run setup token (from the server logs)';
    tokenInput.setAttribute('aria-label', 'First-run setup token');
    tokenInput.style.cssText =
      'width:100%;box-sizing:border-box;padding:8px 10px;border-radius:6px;border:1px solid #2d3648;background:#0f131a;color:#e1e6ed;font-family:ui-monospace,Menlo,Consolas,monospace;';
    tokenWrap.appendChild(tokenLabel);
    tokenWrap.appendChild(tokenInput);

    function createPasswordInput(placeholder, ariaLabel, marginBottom) {
      const wrap = document.createElement('div');
      wrap.style.cssText =
        'position:relative;display:flex;align-items:center;margin:' + (marginBottom || '0') + ';';
      const input = document.createElement('input');
      input.type = 'password';
      input.placeholder = placeholder;
      input.setAttribute('aria-label', ariaLabel);
      input.style.cssText =
        'width:100%;box-sizing:border-box;padding:10px 32px 10px 12px;border-radius:8px;border:1px solid #2d3648;background:#0e1116;color:#e1e6ed;font-family:inherit;font-size:14px;';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'pw-toggle-btn';
      btn.setAttribute('aria-label', 'Show ' + ariaLabel.toLowerCase());
      btn.title = 'Show ' + ariaLabel.toLowerCase();
      btn.style.cssText =
        'position:absolute;right:8px;background:none;border:none;color:#9ca8ba;cursor:pointer;padding:2px;display:flex;align-items:center;justify-content:center;border-radius:4px;';
      btn.innerHTML =
        '<svg class="icon-svg-sm" aria-hidden="true" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
      btn.addEventListener('click', function () {
        const isPass = input.type === 'password';
        input.type = isPass ? 'text' : 'password';
        btn.setAttribute('aria-label', (isPass ? 'Hide ' : 'Show ') + ariaLabel.toLowerCase());
        btn.title = (isPass ? 'Hide ' : 'Show ') + ariaLabel.toLowerCase();
        btn.querySelector('svg').innerHTML = isPass
          ? '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'
          : '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
      });
      wrap.appendChild(input);
      wrap.appendChild(btn);
      return { wrap: wrap, input: input };
    }
    const pwObj = createPasswordInput('New password', 'New password', '8px 0');
    const pw = pwObj.input;
    const pw2Obj = createPasswordInput('Confirm password', 'Confirm password', '0 0 16px');
    const pw2 = pw2Obj.input;
    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;';
    const cancelBtn = document.createElement('button');
    cancelBtn.textContent = 'Cancel';
    cancelBtn.style.cssText =
      'padding:8px 14px;border-radius:8px;border:1px solid #2d3648;background:#1a1f2b;color:#e1e6ed;font-family:inherit;font-size:13px;cursor:pointer;';
    const saveBtn = document.createElement('button');
    saveBtn.textContent = 'Save';
    saveBtn.style.cssText =
      'padding:8px 14px;border-radius:8px;border:none;background:linear-gradient(135deg,#6c8cff,#5b7ad0);color:#fff;font-weight:600;font-family:inherit;font-size:13px;cursor:pointer;';

    function err(msg) {
      if (typeof showToast === 'function') showToast(msg, 'error');
    }

    saveBtn.addEventListener('click', function () {
      if (!pw.value) {
        err('Please enter a password');
        pw.focus();
        return;
      }
      if (pw.value.length < 8) {
        err('Minimum 8 characters required');
        pw.focus();
        return;
      }
      if (pw.value !== pw2.value) {
        err('Passwords do not match');
        pw2.focus();
        return;
      }
      if (!tokenInput.value) {
        err('Paste the setup token from the server logs');
        tokenInput.focus();
        return;
      }
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving…';
      fetch('/api/auth/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Setup-Token': tokenInput.value.trim() },
        body: JSON.stringify({ password: pw.value }),
      })
        .then(function (r) {
          if (!r.ok) {
            return r.json().then(function (j) {
              throw new Error(j && j.error ? j.error : 'Server error');
            });
          }
          const key = pw.value.trim();
          apiKey = key;
          storeKey(key, true);
          overlay.remove();
          location.reload();
        })
        .catch(function (e) {
          saveBtn.disabled = false;
          saveBtn.textContent = 'Save';
          err('Failed to save password: ' + (e && e.message ? e.message : e));
        });
    });

    cancelBtn.addEventListener('click', function () {
      overlay.remove();
    });
    actions.appendChild(cancelBtn);
    actions.appendChild(saveBtn);
    box.appendChild(title);
    box.appendChild(para);
    box.appendChild(pwObj.wrap);
    box.appendChild(pw2Obj.wrap);
    box.appendChild(tokenWrap);
    box.appendChild(actions);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    pw.focus();
  }
  window.setupFirstPassword = setupFirstPassword;

  // Header password visibility toggle
  window.togglePasswordVisibility = function () {
    const input = document.getElementById('apiKeyInput');
    const btn = document.getElementById('togglePasswordBtn');
    const icon = document.getElementById('togglePasswordIcon');
    if (!input || !btn || !icon) return;
    const isPass = input.type === 'password';
    input.type = isPass ? 'text' : 'password';
    btn.setAttribute('aria-label', isPass ? 'Hide password' : 'Show password');
    btn.title = isPass ? 'Hide password' : 'Show password';
    icon.innerHTML = isPass
      ? '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'
      : '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
  };

  // Profile modal password visibility toggle
  window.toggleLpcKeyVisibility = function (suffix) {
    const input = document.getElementById('lpc-api-key-' + suffix);
    const btn = document.getElementById('toggle-lpc-api-key-' + suffix);
    if (!input || !btn) return;
    const isPass = input.type === 'password';
    input.type = isPass ? 'text' : 'password';
    btn.setAttribute('aria-label', isPass ? 'Hide API key' : 'Show API key');
    btn.title = isPass ? 'Hide API key' : 'Show API key';
    const svg = btn.querySelector('svg');
    if (svg) {
      svg.innerHTML = isPass
        ? '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'
        : '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
    }
  };

  window.saveApiKeyFromInput = function () {
    const input = keyInput();
    const key = (input ? input.value : '').trim();
    if (!key) {
      setApiKey('', true);
      return;
    }
    if (localStorage.getItem(KEY_STORAGE) || sessionStorage.getItem(SESSION_STORAGE)) {
      setApiKey(key, true, true);
    } else {
      askRememberKey(key).then(function (remember) {
        setApiKey(key, true, remember);
      });
    }
  };

  // Intercept window.fetch to attach X-API-Key and handle auth challenges
  const origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    let url = input;
    if (typeof input === 'object' && input && typeof input.url === 'string') {
      url = input.url;
    }
    if (typeof url === 'string' && url.startsWith('/api/') && apiKey) {
      const headers = new Headers(init.headers || (input && input.headers) || {});
      if (!headers.has('X-API-Key')) headers.set('X-API-Key', apiKey);
      init.headers = headers;
    }
    return origFetch(input, init).then(function (resp) {
      const needSetup = resp.status === 503 && resp.headers.get('X-Auth-Setup') === 'required';
      if (needSetup && typeof setupFirstPassword === 'function') {
        setupFirstPassword();
        return resp;
      }
      if (resp.status === 401 && !sessionStorage.getItem('ride-401-shown')) {
        sessionStorage.setItem('ride-401-shown', '1');
        const typed = window.prompt('Enter the control-plane password:');
        if (typed) {
          const key = typed.trim();
          apiKey = key;
          askRememberKey(key).then(function (remember) {
            storeKey(key, remember);
            location.reload();
          });
        }
      }
      return resp;
    });
  };

  document.addEventListener('DOMContentLoaded', function () {
    if (keyInput()) keyInput().value = apiKey || '';
  });
})();
