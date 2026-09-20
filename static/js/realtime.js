(function(){
"use strict";
const SEND_MS=4000,POLL_MS=4000,STALE_MS=20000;let lastSent=0,socket=null,locationOk=false,watchId=null;window.smartSocket=null;
function setLocationState(text){document.querySelectorAll('[data-location-note]').forEach(x=>x.textContent=text);}
async function sendLocation(pos){if(window.smartservePinActive)return false;const now=Date.now();if(now-lastSent<SEND_MS-300)return false;lastSent=now;try{const r=await fetch('/api/location/update',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({latitude:pos.coords.latitude,longitude:pos.coords.longitude,accuracy:pos.coords.accuracy})});const d=await r.json().catch(()=>({}));if(r.ok&&d.success){locationOk=true;setLocationState('GPS active · location updated');return true;}return false;}catch(e){return false;}}
function geoError(e){const map={1:'Location permission denied. Allow location for this site.',2:'Your device/browser could not determine a location. Try again or use a phone with GPS.',3:'Location request timed out. Try again.'};setLocationState(map[e&&e.code]||'Unable to get your location.');}
function getPosition(){return new Promise((resolve,reject)=>{if(!navigator.geolocation)return reject({code:0,message:'Geolocation unsupported'});navigator.geolocation.getCurrentPosition(resolve,reject,{enableHighAccuracy:true,maximumAge:5000,timeout:12000});});}
async function getPositionWithFallback(){try{return await getPosition();}catch(first){try{return await new Promise((resolve,reject)=>navigator.geolocation.getCurrentPosition(resolve,reject,{enableHighAccuracy:false,maximumAge:30000,timeout:30000}));}catch(second){geoError(second||first);throw second||first;}}}
function startGPS(){if(window.smartservePinActive || document.querySelector('#providerPincode')){return;}if(!navigator.geolocation){setLocationState('GPS is not supported by this browser.');return;}watchId=navigator.geolocation.watchPosition(async function(pos){await sendLocation(pos);},geoError,{enableHighAccuracy:true,maximumAge:5000,timeout:20000});}
window.requestLocationNow=async function(button){if(button)button.disabled=true;setLocationState('Requesting GPS location…');try{const pos=await getPositionWithFallback();const ok=await sendLocation(pos);if(!ok)throw new Error('Location could not be sent to SmartServe.');if(button){button.textContent='✓ Location shared';setTimeout(function(){button.textContent='📍 Refresh live GPS';button.disabled=false;},1800);}}catch(e){if(button){button.disabled=false;button.textContent='📍 Refresh live GPS';}if(e&&e.code===1)alert('Location permission is denied. Allow location for 127.0.0.1:5000 and try again.');else if(e&&e.code===3)alert('Location timed out. Try again.');else alert('Your browser could not determine a location. For local desktop testing, use DevTools Sensors or test from a phone.');}};
window.goOnlineAndShare=async function(button){if(button)button.disabled=true;setLocationState('Requesting GPS before going online…');try{const pos=await getPositionWithFallback();const ok=await sendLocation(pos);if(!ok)throw new Error('GPS update failed');const r=await fetch('/api/presence',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({online:true})});const d=await r.json().catch(()=>({}));if(!r.ok||!d.success)throw new Error(d.message||'Unable to change online status');if(button){button.textContent='● Online · GPS active';button.disabled=false;}}catch(e){if(button){button.disabled=false;button.textContent='📍 Go online & share location';}setLocationState('Offline · waiting for a valid GPS location');alert(e.message==='GPS update failed'?'SmartServe did not receive a valid GPS position. Please try again.':'Could not get a valid GPS location, so you remain offline.');}};
function esc(v){return String(v||'').replace(/[&<>\'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function socketBoot(){if(!window.io)return;socket=io({transports:['websocket','polling'],reconnection:true,reconnectionAttempts:20,reconnectionDelay:1000});window.smartSocket=socket;socket.on('connect',function(){document.querySelectorAll('.smart-map[data-request-id]').forEach(el=>socket.emit('join_request',{request_id:Number(el.dataset.requestId)}));});socket.on('location_update',function(){document.querySelectorAll('.smart-map[data-request-id]').forEach(el=>el.dispatchEvent(new CustomEvent('smart-location')));});socket.on('request_update',function(data){document.querySelectorAll('[data-request-id="'+data.id+'"]').forEach(card=>{const pill=card.querySelector('.request-status');if(pill)pill.textContent=String(data.status||'').replaceAll('_',' ');});if(['ASSIGNED','ACCEPTED','ARRIVED','IN_PROGRESS','AWAITING_VERIFICATION','AWAITING_PAYMENT','COMPLETED','REJECTED'].includes(data.status)){setTimeout(()=>location.reload(),250);}});socket.on('chat_message',function(data){if(window.activeChatId&&Number(window.activeChatId)===Number(data.request_id)&&typeof window.appendChatMessage==='function')window.appendChatMessage(data);});socket.on('price_proposal',function(data){if(window.negotiationRequestId&&Number(window.negotiationRequestId)===Number(data.request_id)&&typeof window.loadNegotiation==='function')window.loadNegotiation();});socket.on('price_update',function(data){if(window.negotiationRequestId&&Number(window.negotiationRequestId)===Number(data.request_id)&&typeof window.loadNegotiation==='function')window.loadNegotiation();});socket.on('provider_selected',function(data){if(document.body.dataset.userId){alert('🎉 A customer selected you for request #'+data.request_id+'. Review the request and choose Accept or Decline.');setTimeout(()=>location.reload(),300);}});socket.on('provider_response',function(data){if(document.body.dataset.userId){if(data.status==='ACCEPTED')alert('✓ Your selected provider accepted the service.');else if(data.status==='REJECTED')alert('The selected provider declined. Choose another professional.');setTimeout(()=>location.reload(),300);}});}
function haversine(a,b){const R=6371,p=Math.PI/180,dLat=(b[0]-a[0])*p,dLon=(b[1]-a[1])*p,x=Math.sin(dLat/2)**2+Math.cos(a[0]*p)*Math.cos(b[0]*p)*Math.sin(dLon/2)**2;return R*2*Math.atan2(Math.sqrt(x),Math.sqrt(1-x));}
function initMap(el){
  if(!window.L||el.dataset.mapReady==='1')return;
  el.dataset.mapReady='1';
  const map=L.map(el,{zoomControl:true,scrollWheelZoom:true,dragging:true}).setView([0,0],2);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
  const empty=document.createElement('div'); empty.className='map-empty-state'; empty.innerHTML='<strong>Waiting for live GPS</strong><span>Allow location access on both devices. No demo location is used.</span>'; el.appendChild(empty);
  const markers={},circles={},lines={}; let fitted=false,lastRouteKey='';
  function isLive(d){return !!(d&&d.live&&d.latitude!=null&&d.longitude!=null);}
  function marker(key,d,label){
    if(!isLive(d)){
      if(markers[key]){map.removeLayer(markers[key]);delete markers[key];}
      if(circles[key]){map.removeLayer(circles[key]);delete circles[key];}
      return null;
    }
    const ll=[Number(d.latitude),Number(d.longitude)];
    const icon=L.divIcon({className:'smart-map-marker '+key,html:key==='customer'?'👤':'🛠️',iconSize:[42,42],iconAnchor:[21,21]});
    if(!markers[key]){markers[key]=L.marker(ll,{icon}).addTo(map);markers[key].bindPopup('<strong>'+esc(label||key)+'</strong><br>Live GPS location');}
    else markers[key].setLatLng(ll);
    const accuracy=Math.max(5,Number(d.accuracy||0));
    if(circles[key])circles[key].setLatLng(ll).setRadius(Math.min(accuracy,150));
    else circles[key]=L.circle(ll,{radius:Math.min(accuracy,150),weight:1,fillOpacity:.08}).addTo(map);
    return ll;
  }
  async function routeETA(a,b){
    const eta=el.parentElement.querySelector('[data-eta]');
    if(!a||!b){if(eta)eta.textContent='ETA unavailable · waiting for both live locations';return;}
    const key=a.join(',')+'|'+b.join(','); if(key===lastRouteKey)return; lastRouteKey=key;
    try{
      const url='https://router.project-osrm.org/route/v1/driving/'+a[1]+','+a[0]+';'+b[1]+','+b[0]+'?overview=false&steps=false';
      const d=await fetch(url).then(r=>r.json());
      if(d.code==='Ok'&&d.routes&&d.routes[0]){const mins=Math.max(1,Math.round(d.routes[0].duration/60)),km=(d.routes[0].distance/1000).toFixed(1);if(eta)eta.textContent='🚗 '+mins+' min ETA · '+km+' km driving route';return;}
    }catch(e){}
    if(eta)eta.textContent='ETA temporarily unavailable';
  }
  function render(d){
    const a=marker('customer',d.customer,d.customer&&d.customer.name), b=marker('provider',d.provider,d.provider&&d.provider.name), pts=[a,b].filter(Boolean);
    if(lines.main)map.removeLayer(lines.main);
    if(pts.length===2)lines.main=L.polyline(pts,{weight:5,dashArray:'9 9',opacity:.75}).addTo(map);
    if(pts.length===2){if(!fitted){map.fitBounds(pts,{padding:[55,55],maxZoom:16});fitted=true;}} else if(pts.length===1&&!fitted){map.setView(pts[0],16);fitted=true;}
    const status=pts.length===2?'Live: customer + provider':pts.length===1?(isLive(d.customer)?'Live: customer GPS':'Live: provider GPS'):'Waiting for live GPS';
    el.parentElement.querySelectorAll('[data-live-location-status]').forEach(x=>x.textContent=status);
    if(pts.length===0)empty.style.display='grid'; else empty.style.display='none';
    routeETA(a,b);
  }
  async function poll(){try{const d=await fetch('/api/request-location/'+encodeURIComponent(el.dataset.requestId),{cache:'no-store'}).then(r=>r.json());if(d.success)render(d);}catch(e){}}
  el.addEventListener('smart-location',poll); poll(); setInterval(poll,POLL_MS);
  if(socket)socket.emit('join_request',{request_id:Number(el.dataset.requestId)});
}
window.callSmartServe=function(phone,name){const value=String(phone||'').trim();if(!value){alert((name||'This provider')+' has not added a mobile number yet. Ask them to add one in Profile.');return;}window.location.href='tel:'+value;};
window.appendChatMessage=function(m){const box=document.getElementById('chatMessages');if(!box)return;if(box.querySelector('.muted'))box.innerHTML='';const mine=Number(m.sender_id)===Number(document.body.dataset.userId||-1),div=document.createElement('div');div.style.cssText='max-width:78%;align-self:'+(mine?'flex-end':'flex-start')+';background:'+(mine?'linear-gradient(135deg,var(--blue),var(--blue2))':'var(--card2)')+';color:'+(mine?'#fff':'var(--text)')+';padding:9px 11px;border-radius:13px;font-size:11px';div.textContent=m.message;box.appendChild(div);box.scrollTop=box.scrollHeight;};
document.addEventListener('DOMContentLoaded',function(){window.smartservePinActive=!!document.querySelector('#providerPincode') && !!document.querySelector('#providerPincode').value;startGPS();socketBoot();let tries=0;const bootMaps=()=>{document.querySelectorAll('.smart-map').forEach(initMap);if(!window.L&&tries++<30)setTimeout(bootMaps,400);};bootMaps();});
})();
