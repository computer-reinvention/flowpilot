"""Self-contained, same-origin viewer. Capability stays in the address, not DOM."""

import secrets


def security_headers():
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'",
    }


def render_page():
    nonce = secrets.token_urlsafe(24)
    headers = security_headers()
    headers["Content-Security-Policy"] += f"; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; img-src 'self' blob:; connect-src 'self'; form-action 'none'"
    return PAGE.replace("NONCE", nonce), headers


PAGE = r'''<!doctype html><html><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><title>Flowpilot assistance</title>
<style nonce="NONCE">body{font:16px system-ui;max-width:1100px;margin:24px auto}button,input{padding:10px;margin:4px}img{max-width:100%;touch-action:none;outline:1px solid #aaa}pre{white-space:pre-wrap}</style></head><body>
<h1>Browser assistance</h1><p>This expiring link is a bearer secret. Anyone holding a copy can control this pause until it expires or is revoked. Do not share it publicly.</p>
<pre id="reason"></pre><p id="site"></p><div id="fields"></div>
<p>Control this browser viewport, not the operating-system desktop. Native dialogs are not supported.</p>
<img id="screen" tabindex="0" draggable="false" alt="Browser viewport">
<p><input id="text" type="password" autocomplete="off" placeholder="Text to insert"><button id="type">Insert text</button></p>
<button id="resume" disabled>Resume agent</button><button id="decline" hidden>Decline</button><button id="cancel">Cancel task</button><pre id="status"></pre>
<script nonce="NONCE">
'use strict';
const base=location.pathname.replace(/\/$/,''), image=document.getElementById('screen');
let state,stopped=false,streaming=false,queue=Promise.resolve(),objectURL;
function status(s){document.getElementById('status').textContent=s}
async function post(path,data){const r=await fetch(base+'/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),cache:'no-store',referrerPolicy:'no-referrer'});if(!r.ok)throw Error('Assistance request rejected. Refresh or request a new link.');return r.json()}
function action(data){queue=queue.then(()=>stopped?null:post('input',data)).catch(e=>status(e.message));return queue}
function show(blob){const old=objectURL;objectURL=URL.createObjectURL(blob);image.src=objectURL;if(old)URL.revokeObjectURL(old)}
async function refresh(){if(stopped||streaming)return;try{const r=await fetch(base+'/screen',{cache:'no-store'});if(!r.ok)throw Error();show(await r.blob())}catch{status('Viewport unavailable; the link may have expired.')}}
function point(e){const r=image.getBoundingClientRect();return {x:Math.max(0,Math.min(image.naturalWidth-1,(e.clientX-r.left)*image.naturalWidth/r.width)),y:Math.max(0,Math.min(image.naturalHeight-1,(e.clientY-r.top)*image.naturalHeight/r.height))}}
let movement=null,timer=null;
function flush(){if(timer)clearTimeout(timer);timer=null;if(movement){action({kind:'move',...movement});movement=null}}
image.onpointermove=e=>{movement=point(e);if(!timer)timer=setTimeout(flush,32)};
image.onpointerdown=e=>{e.preventDefault();image.focus();image.setPointerCapture(e.pointerId);flush();action({kind:'down',...point(e),button:['left','middle','right'][e.button]||'left'})};
image.onpointerup=e=>{e.preventDefault();flush();action({kind:'up',...point(e),button:['left','middle','right'][e.button]||'left'});image.releasePointerCapture(e.pointerId)};
image.oncontextmenu=e=>e.preventDefault();
image.addEventListener('wheel',e=>{e.preventDefault();flush();const f=e.deltaMode===1?16:e.deltaMode===2?image.clientHeight:1;action({kind:'scroll',dx:Math.max(-16384,Math.min(16384,e.deltaX*f)),dy:Math.max(-16384,Math.min(16384,e.deltaY*f))})},{passive:false});
for(const kind of ['keydown','keyup'])image.addEventListener(kind,e=>{if(e.isComposing||e.key==='Unidentified')return;e.preventDefault();action({kind,key:e.key===' '?'Space':e.key})});
image.onpaste=e=>{e.preventDefault();action({kind:'type',text:e.clipboardData.getData('text')})};
function release(){flush();action({kind:'release'})}
image.onblur=release;image.onpointercancel=release;window.addEventListener('blur',release);
document.addEventListener('visibilitychange',()=>{if(document.hidden)release()});
document.getElementById('type').onclick=()=>{const field=document.getElementById('text');action({kind:'type',text:field.value});field.value=''};
const socket=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+base+'/stream');socket.binaryType='blob';
socket.onmessage=e=>{streaming=true;show(e.data)};
socket.onclose=()=>{streaming=false;if(!stopped){status('Stream disconnected; using screenshot fallback.');refresh()}};socket.onerror=()=>socket.close();
async function finish(confirmed){try{release();await queue;const interaction=state.interaction;const data={generation:interaction.generation,idempotency_key:crypto.randomUUID(),kind:interaction.kind};if(interaction.kind==='browser')data.browser_resolved=true;else if(interaction.kind==='confirmation')data.confirmed=confirmed;else{data.values={};document.querySelectorAll('[data-field]').forEach(e=>{data.values[e.dataset.field]=e.value;e.value=''})}await post('respond',data);stopped=true;socket.close();status('Decision accepted. You can close this page.')}catch(e){status(e.message)}}
document.getElementById('resume').onclick=()=>finish(true);document.getElementById('decline').onclick=()=>finish(false);
document.getElementById('cancel').onclick=async()=>{try{release();await queue;await post('cancel',{});stopped=true;socket.close();status('Cancellation requested.')}catch(e){status(e.message)}};
(async()=>{try{const r=await fetch(base+'/state',{cache:'no-store'});if(!r.ok)throw Error('Interaction metadata unavailable. Use the authenticated task client to resolve this pause.');state=await r.json();const i=state.interaction;document.getElementById('reason').textContent=i.message;document.getElementById('site').textContent=state.url;for(const f of i.fields||[]){const label=document.createElement('label');label.textContent=f.label;const input=document.createElement(f.kind==='select'?'select':'input');input.dataset.field=f.name;if(f.kind==='secret')input.type='password';for(const value of f.options||[]){const option=document.createElement('option');option.textContent=value;input.append(option)}label.append(input);document.getElementById('fields').append(label)}document.getElementById('resume').disabled=false;document.getElementById('decline').hidden=i.kind!=='confirmation';if(i.kind==='confirmation')document.getElementById('resume').textContent='Approve'}catch(e){status(e.message)}await refresh();setInterval(refresh,1500)})();
</script></body></html>'''
