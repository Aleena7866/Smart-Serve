/* SmartServe realtime layer
   - Browser GPS → POST /api/location/update (and Socket.IO `share_location` when connected)
   - Socket.IO event wiring (location, chat, negotiation, matching, SOS acknowledgements)
   - SmartTracker: two-way live map for any `.smart-map[data-request-id]` element.
     Uses ONLY real device GPS relayed through the server. When the server reports a
     participant as stale/offline, the marker is greyed out / removed — nothing is invented. */
(function () {
  'use strict';
  var SEND_MS = 4000, POLL_MS = 5000, MIN_MOVE_M = 3;
  var lastSent = 0, lastPos = null, socket = null, watchId = null, geoState = 'idle';
  window.smartSocket = null;

  /* ---------------- GPS sharing ---------------- */
  function setLocationState(text, state) {
    geoState = state || geoState;
    document.querySelectorAll('[data-location-note]').forEach(function (x) { x.textContent = text; x.dataset.state = geoState; });
    document.dispatchEvent(new CustomEvent('smart-geo-state', { detail: { text: text, state: geoState } }));
  }
  function distanceM(a, b) {
    var R = 6371000, p = Math.PI / 180, dLat = (b.latitude - a.latitude) * p, dLon = (b.longitude - a.longitude) * p;
    var x = Math.sin(dLat / 2) * Math.sin(dLat / 2) + Math.cos(a.latitude * p) * Math.cos(b.latitude * p) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return R * 2 * Math.atan2(Math.sqrt(x), Math.sqrt(1 - x));
  }
  function payloadFrom(pos) {
    var c = pos.coords;
    return { latitude: c.latitude, longitude: c.longitude, accuracy: isFinite(c.accuracy) ? c.accuracy : null,
             heading: (c.heading != null && isFinite(c.heading)) ? c.heading : null, speed: (c.speed != null && isFinite(c.speed)) ? c.speed : null };
  }
  async function sendLocation(pos, force) {
    var now = Date.now(), p = payloadFrom(pos);
    if (!force && now - lastSent < SEND_MS - 300) return false;
    if (!force && lastPos && distanceM(lastPos, p) < MIN_MOVE_M && now - lastSent < 15000) return false; // stationary: send at most every 15 s
    lastSent = now;
    try {
      if (socket && socket.connected) { socket.emit('share_location', p); }
      var r = await fetch('/api/location/update', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(p) });
      var d = await r.json().catch(function () { return {}; });
      if (r.ok && d.success) { lastPos = p; if (d.active_requests && d.active_requests.length) setLocationState('GPS active · updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }), 'live'); else if (window.smartservePinActive) setLocationState('PIN service area active · GPS ready for live tracking', 'ready'); else setLocationState('GPS ready · updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }), 'live'); return true; }
      setLocationState(d.message || 'Location could not be saved.', 'error');
      return false;
    } catch (e) { setLocationState('Offline · location not sent. Reconnecting…', 'offline'); return false; }
  }
  function geoError(e) {
    var map = { 1: 'Location permission denied. Allow location access for this site in your browser settings, then reload.', 2: 'Your device could not determine a location. Move outdoors or enable GPS.', 3: 'Location request timed out. Retrying…' };
    setLocationState(map[e && e.code] || 'Unable to get your location.', e && e.code === 1 ? 'denied' : 'error');
  }
  function getPosition(opts) { return new Promise(function (resolve, reject) { if (!navigator.geolocation) return reject({ code: 0, message: 'Geolocation unsupported' }); navigator.geolocation.getCurrentPosition(resolve, reject, opts); }); }
  async function getPositionWithFallback() {
    try { return await getPosition({ enableHighAccuracy: true, maximumAge: 5000, timeout: 12000 }); }
    catch (first) { if (first && first.code === 1) { geoError(first); throw first; } try { return await getPosition({ enableHighAccuracy: false, maximumAge: 30000, timeout: 30000 }); } catch (second) { geoError(second || first); throw second || first; } }
  }
  function startGPS() {
    if (!navigator.geolocation) { setLocationState('GPS is not supported by this browser.', 'unsupported'); return; }
    if (!document.body.dataset.userId || document.body.dataset.userId === '0') return;
    setLocationState('Requesting GPS permission…', 'requesting');
    watchId = navigator.geolocation.watchPosition(function (pos) { sendLocation(pos, false); }, geoError, { enableHighAccuracy: true, maximumAge: 4000, timeout: 20000 });
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') { getPosition({ enableHighAccuracy: true, maximumAge: 0, timeout: 12000 }).then(function (p) { sendLocation(p, true); }).catch(function () {}); } });
  }
  window.requestLocationNow = async function (button) {
    var original = button ? button.textContent : '';
    if (button) { button.disabled = true; button.textContent = 'Locating…'; }
    setLocationState('Requesting GPS location…', 'requesting');
    try {
      var pos = await getPositionWithFallback();
      var ok = await sendLocation(pos, true);
      if (!ok) throw new Error('Location could not be sent to SmartServe.');
      if (button) { button.textContent = '✓ Location shared'; setTimeout(function () { button.textContent = original || '📍 Refresh live GPS'; button.disabled = false; }, 1800); }
      document.querySelectorAll('.smart-map[data-request-id]').forEach(function (el) { el.dispatchEvent(new CustomEvent('smart-location')); });
    } catch (e) {
      if (button) { button.disabled = false; button.textContent = original || '📍 Refresh live GPS'; }
      var msg = (e && e.code === 1) ? 'Location permission is denied. Allow location for this site and try again.' : (e && e.code === 3) ? 'Location timed out. Please try again.' : (e && e.message) || 'Your browser could not determine a location.';
      if (window.showToast) window.showToast(msg, 'error'); else alert(msg);
    }
  };
  window.goOnlineAndShare = async function (button) {
    var original = button ? button.textContent : '';
    if (button) { button.disabled = true; button.textContent = 'Getting GPS…'; }
    setLocationState('Requesting GPS before going online…', 'requesting');
    try {
      var pos = await getPositionWithFallback();
      var ok = await sendLocation(pos, true);
      if (!ok) throw new Error('GPS update failed');
      var r = await fetch('/api/presence', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ online: true }) });
      var d = await r.json().catch(function () { return {}; });
      if (!r.ok || !d.success) throw new Error(d.message || 'Unable to change online status');
      if (button) { button.textContent = '● Online · GPS active'; button.disabled = false; }
      if (window.showToast) window.showToast('You are online. Nearby requests will reach you in real time.', 'success');
    } catch (e) {
      if (button) { button.disabled = false; button.textContent = original || '📍 Go online & share location'; }
      setLocationState('Offline · waiting for a valid GPS location', 'offline');
      var msg = e.message === 'GPS update failed' ? 'SmartServe did not receive a valid GPS position. Please try again.' : (e.message || 'Could not get a valid GPS location, so you remain offline.');
      if (window.showToast) window.showToast(msg, 'error'); else alert(msg);
    }
  };

  function esc(v) { return String(v == null ? '' : v).replace(/[&<>'"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]; }); }
  function notify(msg, type) { if (window.showToast) window.showToast(msg, type); else alert(msg); }

  /* ---------------- Socket.IO ---------------- */
  function socketBoot() {
    if (!window.io || !document.body.dataset.userId || document.body.dataset.userId === '0') return;
    socket = io({ transports: ['websocket', 'polling'], reconnection: true, reconnectionAttempts: Infinity, reconnectionDelay: 1000, reconnectionDelayMax: 8000 });
    window.smartSocket = socket;
    socket.on('connect', function () {
      document.querySelectorAll('.smart-map[data-request-id]').forEach(function (el) { socket.emit('join_request', { request_id: Number(el.dataset.requestId) }); });
      document.dispatchEvent(new CustomEvent('smart-socket', { detail: { connected: true } }));
    });
    socket.on('disconnect', function () { document.dispatchEvent(new CustomEvent('smart-socket', { detail: { connected: false } })); });
    socket.on('location_update', function (data) {
      document.querySelectorAll('.smart-map[data-request-id]').forEach(function (el) { if (!data || !data.request_id || Number(el.dataset.requestId) === Number(data.request_id)) el.dispatchEvent(new CustomEvent('smart-location', { detail: data })); });
    });
    socket.on('request_update', function (data) {
      document.querySelectorAll('[data-request-id="' + data.id + '"]').forEach(function (card) { var pill = card.querySelector('.request-status'); if (pill) pill.textContent = String(data.status || '').replace(/_/g, ' '); });
      document.dispatchEvent(new CustomEvent('smart-request-update', { detail: data }));
      if (!document.body.dataset.noAutoReload && ['ASSIGNED', 'ACCEPTED', 'ARRIVED', 'IN_PROGRESS', 'AWAITING_VERIFICATION', 'AWAITING_PAYMENT', 'COMPLETED', 'REJECTED', 'CANCELLED'].indexOf(data.status) >= 0) setTimeout(function () { location.reload(); }, 350);
    });
    socket.on('chat_message', function (data) { if (window.activeChatId && Number(window.activeChatId) === Number(data.request_id) && typeof window.appendChatMessage === 'function') window.appendChatMessage(data); else if (Number(data.sender_id) !== Number(document.body.dataset.userId)) notify('💬 ' + (data.sender_name || 'New message') + ': ' + String(data.message || '').slice(0, 80), 'info'); });
    socket.on('price_proposal', function (data) { if (window.negotiationRequestId && Number(window.negotiationRequestId) === Number(data.request_id) && typeof window.loadNegotiation === 'function') window.loadNegotiation(); else notify('₹ New price offer on request #' + data.request_id, 'info'); });
    socket.on('price_update', function (data) { if (window.negotiationRequestId && Number(window.negotiationRequestId) === Number(data.request_id) && typeof window.loadNegotiation === 'function') window.loadNegotiation(); });
    socket.on('provider_selected', function (data) { notify('🎉 A customer selected you for request #' + data.request_id + '. Review it and choose Accept or Decline.', 'success'); setTimeout(function () { location.reload(); }, 900); });
    socket.on('provider_response', function (data) { if (data.status === 'ACCEPTED') notify('✓ Your selected provider accepted the service.', 'success'); else if (data.status === 'REJECTED') notify('The selected provider declined. Choose another professional.', 'error'); setTimeout(function () { location.reload(); }, 900); });
    socket.on('sos_acknowledged', function (data) { notify('🛡️ SmartServe operations acknowledged your SOS for service #' + data.request_id + '.', 'success'); });
    socket.on('dispute_update', function (data) { notify('⚖️ Dispute #' + data.dispute_id + ' is now ' + String(data.status || '').toLowerCase() + '.', 'info'); });
    socket.on('sos_alert', function (data) { if (document.body.dataset.role === 'admin') { notify('🆘 SOS on service #' + data.request_id + ' — open Operations.', 'error'); document.dispatchEvent(new CustomEvent('smart-sos', { detail: data })); } });
  }

  /* ---------------- SmartTracker (two-way live map) ---------------- */
  function pinIcon(kind, state, photoUrl, initial) {
    var cls = 'marker-pin ' + kind + (state === 'stale' ? ' stale' : '') + (state === 'approximate' ? ' approximate' : '');
    var inner = photoUrl ? '<img class="marker-photo" src="' + esc(photoUrl) + '" alt="" onerror="this.remove()">' : '<span>' + (kind === 'provider' ? '🛠️' : '🏠') + '</span>';
    return L.divIcon({ className: 'smart-map-marker', html: '<div class="' + cls + '">' + inner + '</div>', iconSize: [42, 42], iconAnchor: [21, 42], popupAnchor: [0, -40] });
  }
  function ageText(sec) { if (sec == null) return ''; if (sec < 60) return sec + ' s ago'; if (sec < 3600) return Math.round(sec / 60) + ' min ago'; return Math.round(sec / 3600) + ' h ago'; }
  function stateLabel(p, who) {
    if (!p) return who + ': unknown';
    switch (p.state) {
      case 'live': return who + ' · live GPS' + (p.accuracy ? ' ±' + Math.round(p.accuracy) + ' m' : '');
      case 'stale': return who + ' · last seen ' + ageText(p.age_seconds);
      case 'approximate': return who + ' · approximate (PIN code area)';
      case 'offline': return who + ' · offline (no GPS in the last 2 min)';
      default: return who + ' · waiting for GPS';
    }
  }

  window.SmartTracker = function (el, options) {
    options = options || {};
    if (!window.L || el.dataset.mapReady === '1') return null;
    el.dataset.mapReady = '1';
    var requestId = Number(el.dataset.requestId || options.requestId);
    var container = options.container || el.parentElement;
    var map = L.map(el, { zoomControl: true, scrollWheelZoom: true, dragging: true, attributionControl: true }).setView([20.5937, 78.9629], 5);
    var tiles = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
    var tileFailed = false;
    tiles.on('tileerror', function () { if (!tileFailed) { tileFailed = true; setBanner('Map tiles could not load (offline?). Live positions and ETA still update below.'); } });
    tiles.on('load', function () { if (tileFailed) { tileFailed = false; setBanner(''); } });

    var overlay = document.createElement('div'); overlay.className = 'map-status'; el.appendChild(overlay);
    var empty = document.createElement('div'); empty.className = 'map-empty-state'; el.appendChild(empty);
    var banner = document.createElement('div'); banner.className = 'map-empty-state'; banner.style.display = 'none'; banner.style.inset = 'auto 18px 60px 18px'; el.appendChild(banner);
    function setBanner(t) { banner.textContent = t; banner.style.display = t ? 'grid' : 'none'; }

    var markers = {}, circles = {}, routeLine = null, fitted = false, lastRouteKey = '', lastData = null, timer = null, backoff = POLL_MS, failures = 0, recentered = false;
    map.on('dragstart zoomstart', function () { recentered = true; });

    function upsert(kind, p, label, photoUrl) {
      var present = p && p.latitude != null && p.longitude != null && p.state !== 'offline' && p.state !== 'unknown';
      if (!present) { if (markers[kind]) { map.removeLayer(markers[kind]); delete markers[kind]; } if (circles[kind]) { map.removeLayer(circles[kind]); delete circles[kind]; } return null; }
      var ll = [Number(p.latitude), Number(p.longitude)];
      var icon = pinIcon(kind, p.state, photoUrl, label);
      var popup = '<strong>' + esc(label || kind) + '</strong><br>' + esc(stateLabel(p, kind === 'provider' ? 'Provider' : 'Customer')) + (p.speed ? '<br>' + (p.speed * 3.6).toFixed(0) + ' km/h' : '');
      if (!markers[kind]) { markers[kind] = L.marker(ll, { icon: icon, zIndexOffset: kind === 'provider' ? 1000 : 500 }).addTo(map).bindPopup(popup); }
      else { markers[kind].setLatLng(ll); markers[kind].setIcon(icon); markers[kind].setPopupContent(popup); }
      var acc = p.state === 'approximate' ? 600 : Math.max(8, Number(p.accuracy || 0));
      var style = p.state === 'approximate' ? { weight: 1, dashArray: '4 4', fillOpacity: .06, color: '#7b5cf5' } : { weight: 1, fillOpacity: .08, color: kind === 'provider' ? '#1a63f0' : '#12a86b' };
      if (circles[kind]) circles[kind].setLatLng(ll).setRadius(Math.min(acc, 800)).setStyle(style);
      else circles[kind] = L.circle(ll, Object.assign({ radius: Math.min(acc, 800) }, style)).addTo(map);
      return ll;
    }
    function setText(sel, text) { container.querySelectorAll(sel).forEach(function (x) { x.textContent = text; }); }
    function render(d) {
      lastData = d;
      var c = upsert('customer', d.customer, d.customer && d.customer.name, d.customer && d.customer.photo_url);
      var p = upsert('provider', d.provider, d.provider && d.provider.name, d.provider && d.provider.photo_url);
      var pts = [c, p].filter(Boolean);
      // Route polyline: real OSRM geometry from the server, or a dashed straight line when routing is unavailable.
      if (pts.length === 2) {
        var coords = (d.route && d.route.length > 1) ? d.route : pts;
        var key = JSON.stringify(coords.length > 2 ? [coords[0], coords[coords.length - 1], coords.length] : coords);
        if (key !== lastRouteKey) {
          lastRouteKey = key;
          var style = d.route ? { weight: 5, opacity: .85, color: '#1a63f0' } : { weight: 4, dashArray: '9 9', opacity: .6, color: '#66758f' };
          if (routeLine) { routeLine.setLatLngs(coords); routeLine.setStyle(style); } else routeLine = L.polyline(coords, style).addTo(map);
        }
      } else if (routeLine) { map.removeLayer(routeLine); routeLine = null; lastRouteKey = ''; }
      if (pts.length && (!fitted || (!recentered && pts.length === 2))) {
        if (pts.length === 2) map.fitBounds(routeLine ? routeLine.getBounds() : L.latLngBounds(pts), { padding: [50, 50], maxZoom: 16 }); else map.setView(pts[0], 15);
        fitted = true;
      }
      // Status pills
      var pills = [];
      [['customer', 'Customer'], ['provider', 'Provider']].forEach(function (pair) {
        var info = d[pair[0]] || {}; var st = info.state || 'unknown';
        pills.push('<span class="pill"><span class="dot ' + st + '"></span>' + esc(stateLabel(info, pair[1])) + '</span>');
      });
      overlay.innerHTML = pills.join('');
      // Metrics
      var distance = d.distance_km != null ? (d.distance_km < 1 ? Math.round(d.distance_km * 1000) + ' m' : d.distance_km.toFixed(1) + ' km') : '—';
      var eta = d.eta_minutes != null ? d.eta_minutes + ' min' : '—';
      var etaNote = d.eta_source === 'road' ? 'road route' : d.eta_source === 'estimate' ? 'estimate (routing offline)' : d.tracking_active ? 'needs both live locations' : 'tracking finished';
      setText('[data-distance]', distance); setText('[data-eta-value]', eta); setText('[data-eta-note]', etaNote);
      setText('[data-eta]', d.eta_minutes != null ? '🚗 ' + eta + ' ETA · ' + distance + (d.eta_source === 'road' ? ' by road' : ' (straight-line estimate)') : (d.status === 'ARRIVED' || d.status === 'IN_PROGRESS' ? 'Provider is on site' : 'ETA unavailable · waiting for both live locations'));
      var live = ['customer', 'provider'].filter(function (k) { return d[k] && d[k].state === 'live'; }).length;
      setText('[data-live-location-status]', live === 2 ? 'Both devices live' : live === 1 ? 'One device live · waiting for the other' : 'Waiting for live GPS');
      if (d.arrived_nearby) setText('[data-arrival-hint]', 'Provider is within 200 m.');
      empty.style.display = pts.length ? 'none' : 'grid';
      if (!pts.length) empty.innerHTML = '<strong>Waiting for live GPS</strong><span>' + (d.tracking_active ? 'Allow location access on both devices. SmartServe never shows a simulated position.' : 'Live tracking is only active during an assigned service.') + '</span>';
      el.dispatchEvent(new CustomEvent('smart-tracking', { detail: d }));
      if (typeof options.onUpdate === 'function') options.onUpdate(d);
    }
    async function poll() {
      try {
        var r = await fetch('/api/request-location/' + encodeURIComponent(requestId), { cache: 'no-store' });
        var d = await r.json().catch(function () { return {}; });
        if (r.status === 401) { setBanner('Session expired. Please sign in again.'); stop(); return; }
        if (r.status === 403) { setBanner('You are not part of this service.'); stop(); return; }
        if (d.success) { failures = 0; backoff = POLL_MS; render(d); setBanner(''); }
        else throw new Error(d.message || 'Tracking unavailable');
      } catch (e) {
        failures += 1; backoff = Math.min(30000, POLL_MS * Math.pow(1.6, failures));
        if (failures >= 2) setBanner('Connection lost · retrying in ' + Math.round(backoff / 1000) + ' s');
      } finally { schedule(); }
    }
    function schedule() { clearTimeout(timer); if (!lastData || lastData.tracking_active !== false || failures) timer = setTimeout(poll, document.visibilityState === 'hidden' ? Math.max(backoff, 15000) : backoff); }
    function stop() { clearTimeout(timer); timer = null; }
    el.addEventListener('smart-location', function (e) {
      var d = e.detail;
      if (d && d.role && lastData && lastData[d.role]) {
        // Instant socket update: patch the last snapshot for a smooth marker move, then refresh distance/ETA from the server.
        lastData[d.role] = Object.assign({}, lastData[d.role], { latitude: d.latitude, longitude: d.longitude, accuracy: d.accuracy, heading: d.heading, speed: d.speed, state: 'live', live: true, stale: false, age_seconds: 0 });
        render(lastData);
      }
      clearTimeout(timer); timer = setTimeout(poll, 600);
    });
    document.addEventListener('smart-socket', function (e) { if (e.detail.connected && socket) socket.emit('join_request', { request_id: requestId }); });
    poll();
    if (socket && socket.connected) socket.emit('join_request', { request_id: requestId });
    setTimeout(function () { map.invalidateSize(); }, 300);
    window.addEventListener('resize', function () { map.invalidateSize(); });
    return { map: map, refresh: poll, stop: stop, getData: function () { return lastData; } };
  };

  function initMap(el) { if (el.dataset.mapReady === '1' || el.dataset.manual === '1') return; if (!el.dataset.requestId) return; window.SmartTracker(el); }

  document.addEventListener('DOMContentLoaded', function () {
    window.smartservePinActive = !!document.querySelector('#providerPincode') && !!document.querySelector('#providerPincode').value;
    socketBoot();
    startGPS();
    var tries = 0;
    var bootMaps = function () { document.querySelectorAll('.smart-map').forEach(initMap); if (!window.L && tries++ < 30) setTimeout(bootMaps, 400); };
    bootMaps();
  });
})();
