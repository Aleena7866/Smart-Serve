/* SmartServe UI helpers: toasts, modals, confirm dialogs, reveal/tilt motion, telephony, image fallbacks.
   Loaded before realtime.js on every page that extends base.html. */
(function () {
  'use strict';

  /* ---------- Toasts ---------- */
  window.showToast = function (message, type, timeout) {
    var stack = document.getElementById('toastStack');
    if (!stack) { stack = document.createElement('div'); stack.id = 'toastStack'; stack.className = 'toast-stack'; document.body.appendChild(stack); }
    var el = document.createElement('div');
    el.className = 'toast ' + (type || '');
    el.setAttribute('role', 'status');
    el.textContent = String(message || '');
    stack.appendChild(el);
    setTimeout(function () { el.style.transition = 'opacity .35s'; el.style.opacity = '0'; setTimeout(function () { el.remove(); }, 380); }, timeout || 4200);
    return el;
  };

  /* ---------- Modals (HTML string or node) ---------- */
  var modalRoot = null;
  function ensureModalRoot() {
    if (modalRoot) return modalRoot;
    modalRoot = document.createElement('div');
    modalRoot.className = 'modal-backdrop';
    modalRoot.id = 'uiModal';
    modalRoot.innerHTML = '<div class="modal-card" role="dialog" aria-modal="true"></div>';
    modalRoot.addEventListener('click', function (e) { if (e.target === modalRoot) window.closeModal(); });
    document.body.appendChild(modalRoot);
    return modalRoot;
  }
  window.showModal = function (html, opts) {
    opts = opts || {};
    var root = ensureModalRoot();
    var card = root.querySelector('.modal-card');
    if (typeof html === 'string') card.innerHTML = html; else { card.innerHTML = ''; card.appendChild(html); }
    if (opts.width) card.style.width = 'min(' + opts.width + ', 100%)'; else card.style.width = '';
    root.classList.add('open');
    document.body.style.overflow = 'hidden';
    var first = card.querySelector('input,select,textarea,button');
    if (first) setTimeout(function () { first.focus(); }, 60);
    return card;
  };
  window.closeModal = function () {
    if (!modalRoot) return;
    modalRoot.classList.remove('open');
    document.body.style.overflow = '';
  };
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') window.closeModal(); });

  /* Promise-based confirm dialog replacing window.confirm */
  window.uiConfirm = function (opts) {
    opts = typeof opts === 'string' ? { message: opts } : (opts || {});
    return new Promise(function (resolve) {
      var card = window.showModal(
        '<div class="modal-head"><div><h3>' + escapeHtml(opts.title || 'Please confirm') + '</h3>' +
        '<p class="muted" style="margin:0">' + escapeHtml(opts.message || '') + '</p></div>' +
        '<button type="button" class="modal-close" aria-label="Close">×</button></div>' +
        '<div class="action-row" style="justify-content:flex-end"><button type="button" class="outline-btn" data-act="cancel">' + escapeHtml(opts.cancelText || 'Cancel') + '</button>' +
        '<button type="button" class="' + (opts.danger ? 'danger-btn' : 'primary-btn') + '" data-act="ok">' + escapeHtml(opts.okText || 'Confirm') + '</button></div>'
      );
      card.querySelector('[data-act=ok]').onclick = function () { window.closeModal(); resolve(true); };
      card.querySelector('[data-act=cancel]').onclick = card.querySelector('.modal-close').onclick = function () { window.closeModal(); resolve(false); };
    });
  };

  /* Promise-based prompt dialog (text / number / select / textarea) replacing window.prompt */
  window.uiPrompt = function (opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var fieldHtml;
      if (opts.type === 'select') {
        fieldHtml = '<select class="input" id="uiPromptField">' + (opts.options || []).map(function (o) {
          var v = typeof o === 'string' ? o : o.value, l = typeof o === 'string' ? o : o.label;
          return '<option value="' + escapeHtml(v) + '">' + escapeHtml(l) + '</option>';
        }).join('') + '</select>';
      } else if (opts.type === 'textarea') {
        fieldHtml = '<textarea class="input" id="uiPromptField" placeholder="' + escapeHtml(opts.placeholder || '') + '" maxlength="' + (opts.maxlength || 1000) + '">' + escapeHtml(opts.value || '') + '</textarea>';
      } else {
        fieldHtml = '<input class="input" id="uiPromptField" type="' + escapeHtml(opts.type || 'text') + '" placeholder="' + escapeHtml(opts.placeholder || '') + '" value="' + escapeHtml(opts.value || '') + '"' + (opts.min != null ? ' min="' + opts.min + '"' : '') + (opts.max != null ? ' max="' + opts.max + '"' : '') + (opts.step != null ? ' step="' + opts.step + '"' : '') + '>';
      }
      var card = window.showModal(
        '<div class="modal-head"><div><h3>' + escapeHtml(opts.title || 'Enter a value') + '</h3>' +
        (opts.message ? '<p class="muted" style="margin:0">' + escapeHtml(opts.message) + '</p>' : '') + '</div>' +
        '<button type="button" class="modal-close" aria-label="Close">×</button></div>' +
        '<form id="uiPromptForm">' + (opts.label ? '<label for="uiPromptField" style="font-size:12px;font-weight:800">' + escapeHtml(opts.label) + '</label>' : '') + fieldHtml +
        '<div class="action-row" style="justify-content:flex-end"><button type="button" class="outline-btn" data-act="cancel">Cancel</button><button type="submit" class="primary-btn">' + escapeHtml(opts.okText || 'Continue') + '</button></div></form>'
      );
      var done = function (v) { window.closeModal(); resolve(v); };
      card.querySelector('#uiPromptForm').onsubmit = function (e) { e.preventDefault(); done(card.querySelector('#uiPromptField').value); };
      card.querySelector('[data-act=cancel]').onclick = card.querySelector('.modal-close').onclick = function () { done(null); };
    });
  };

  /* ---------- Fetch helper ---------- */
  window.api = async function (url, options) {
    options = options || {};
    var init = { method: options.method || 'GET', headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin', cache: 'no-store' };
    if (options.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(options.body); }
    var res = await fetch(url, init);
    var data = {};
    try { data = await res.json(); } catch (_) { data = { success: false, message: 'Unexpected response from server.' }; }
    if (!res.ok && data.success === undefined) data.success = false;
    if (res.status === 401) data.message = data.message || 'Please sign in again.';
    return data;
  };

  /* ---------- Escape ---------- */
  function escapeHtml(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  if (!window.escapeHtml) window.escapeHtml = escapeHtml;

  /* ---------- Telephony ---------- */
  window.callSmartServe = async function (phone, name) {
    phone = String(phone || '').trim(); name = String(name || 'the other party');
    if (!phone) { window.showToast(name + ' has not added a mobile number yet.', 'error'); return; }
    var digits = phone.replace(/[^0-9+]/g, '');
    if (!digits) { window.showToast('No valid phone number is available.', 'error'); return; }
    var a = document.createElement('a'); a.href = 'tel:' + digits; a.rel = 'nofollow'; document.body.appendChild(a); a.click(); a.remove();
    try { await navigator.clipboard.writeText(phone); window.showToast('Dialling ' + name + ' · number copied: ' + phone, 'info'); } catch (_) { window.showToast('Call ' + name + ' at ' + phone, 'info'); }
  };

  /* ---------- Avatar/image fallback ---------- */
  window.avatarFallback = function (img) {
    if (!img || img.dataset.fallbackApplied) return;
    img.dataset.fallbackApplied = '1';
    var initial = (img.getAttribute('data-initial') || (img.alt || 'U').trim().charAt(0) || 'U').toUpperCase();
    var div = document.createElement('div');
    div.className = img.className.replace(/\bprovider-photo\b/, 'provider-photo placeholder');
    if (!/placeholder/.test(div.className)) div.className += ' placeholder';
    div.style.cssText = img.getAttribute('style') || '';
    div.textContent = initial;
    img.replaceWith(div);
  };
  document.addEventListener('error', function (e) {
    var t = e.target;
    if (t && t.tagName === 'IMG' && t.matches('.avatar,.avatar-mini img,.provider-photo,.profile-avatar-lg,.profile-photo,.marker-photo')) window.avatarFallback(t);
  }, true);

  /* ---------- Reveal on scroll & subtle tilt ---------- */
  function setupMotion() {
    var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var revealEls = document.querySelectorAll('.reveal');
    if (reduce || !('IntersectionObserver' in window)) { revealEls.forEach(function (el) { el.classList.add('in'); }); }
    else {
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en, i) { if (en.isIntersecting) { en.target.style.transitionDelay = Math.min(i * 40, 240) + 'ms'; en.target.classList.add('in'); io.unobserve(en.target); } });
      }, { threshold: 0.08 });
      revealEls.forEach(function (el) { io.observe(el); });
    }
    if (reduce || !window.matchMedia('(hover:hover)').matches) return;
    document.querySelectorAll('.tilt').forEach(function (el) {
      var max = parseFloat(getComputedStyle(el).getPropertyValue('--tilt')) || 6;
      el.addEventListener('pointermove', function (e) {
        var r = el.getBoundingClientRect();
        var x = (e.clientX - r.left) / r.width - 0.5, y = (e.clientY - r.top) / r.height - 0.5;
        el.style.transform = 'perspective(900px) rotateX(' + (-y * max).toFixed(2) + 'deg) rotateY(' + (x * max).toFixed(2) + 'deg) translateY(-3px)';
      });
      el.addEventListener('pointerleave', function () { el.style.transform = ''; });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setupMotion); else setupMotion();

  /* ---------- Image upload preview + client-side validation ---------- */
  window.bindImagePreview = function (input, preview, opts) {
    opts = opts || {};
    if (!input) return;
    input.addEventListener('change', function () {
      var f = input.files && input.files[0];
      if (!f) { if (preview) { preview.classList.remove('show'); preview.removeAttribute('src'); } return; }
      var ok = /^image\/(jpeg|png|webp)$/i.test(f.type) && /\.(jpe?g|png|webp)$/i.test(f.name);
      if (!ok) { window.showToast('Only JPG, PNG or WEBP images are allowed.', 'error'); input.value = ''; return; }
      if (f.size > (opts.maxBytes || 5 * 1024 * 1024)) { window.showToast('Image must be smaller than 5 MB.', 'error'); input.value = ''; return; }
      if (preview) { preview.src = URL.createObjectURL(f); preview.classList.add('show'); }
      var label = opts.label || input.closest('.dropzone');
      if (label) { var s = label.querySelector('strong'); if (s) s.textContent = f.name; }
    });
    var zone = input.closest('.dropzone');
    if (zone) {
      ['dragenter', 'dragover'].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add('drag'); }); });
      ['dragleave', 'drop'].forEach(function (ev) { zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove('drag'); }); });
      zone.addEventListener('drop', function (e) { if (e.dataTransfer && e.dataTransfer.files.length) { input.files = e.dataTransfer.files; input.dispatchEvent(new Event('change')); } });
    }
  };

  /* ---------- Busy-state for submit buttons ---------- */
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || form.dataset.noBusy !== undefined) return;
    var btn = form.querySelector('button[type=submit],button:not([type])');
    if (btn && !btn.disabled) { btn.dataset.originalText = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> ' + (btn.dataset.busyText || 'Working…'); setTimeout(function () { btn.disabled = false; btn.innerHTML = btn.dataset.originalText || btn.innerHTML; }, 12000); }
  });
})();
