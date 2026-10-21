"""Shared handoff event and local transport for conversational browser sessions."""
import asyncio
import secrets
import socket
from contextlib import suppress

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
import uvicorn

from .tasks.interaction_transport import release_controls


class HandoffCancelled(RuntimeError):
    pass


def pause_payload(reason, category, browser_cdp_url, resume_url, **extra):
    return dict(event='run.paused', status='paused_for_hitl', message=reason,
                category=category, browser_cdp_url=browser_cdp_url,
                resume_url=resume_url, **extra)


class HandoffServer:
    """One active capability-scoped handoff. No browser access outside a pause."""
    def __init__(self, context, cdp_url, user, notify=None):
        self.context, self.cdp_url, self.user = context, cdp_url, user
        self.notify = notify
        self.pending = None
        self.token = None
        self.event = None
        self.held_keys = set()
        self.held_buttons = set()
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self._interaction_lock = asyncio.Lock()

        @self.app.middleware('http')
        async def serialize_intervention(request, call_next):
            # Finish in-flight human actions before allowing the agent to resume.
            async with self._interaction_lock:
                return await call_next(request)

        def authorize(token):
            if not self.pending or self.pending.done() or not secrets.compare_digest(token, self.token or ''):
                raise HTTPException(410, 'Assistance request expired or unavailable')

        async def page():
            pages = self.context.pages
            if not pages:
                raise HTTPException(409, 'No browser page available')
            return pages[-1]

        @self.app.get('/help/{token}', response_class=HTMLResponse)
        async def help_page(token: str):
            authorize(token)
            return HTMLResponse(PAGE, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

        @self.app.get('/help/{token}/state')
        async def state(token: str):
            authorize(token)
            p = await page()
            return {**self.event, 'url': p.url}

        @self.app.get('/help/{token}/screen')
        async def screen(token: str):
            authorize(token)
            return Response(await (await page()).screenshot(type='png'), media_type='image/png',
                            headers={'Cache-Control': 'no-store'})

        @self.app.websocket('/help/{token}/stream')
        async def stream(websocket: WebSocket, token: str):
            try:
                authorize(token)
            except HTTPException:
                await websocket.close(code=1008)
                return
            await websocket.accept()
            client = await self.context.new_cdp_session(await page())
            frames = asyncio.Queue(maxsize=1)
            async def frame(event):
                await client.send('Page.screencastFrameAck', {'sessionId': event['sessionId']})
                if frames.full():
                    frames.get_nowait()
                frames.put_nowait(event)
            client.on('Page.screencastFrame', frame)
            disconnected = asyncio.create_task(websocket.receive())
            try:
                await client.send('Page.startScreencast', {'format': 'jpeg', 'quality': 75, 'maxWidth': 1600, 'everyNthFrame': 1})
                while self.pending and not self.pending.done() and self.token == token and not disconnected.done():
                    try:
                        event = await asyncio.wait_for(frames.get(), .5)
                    except TimeoutError:
                        continue
                    await websocket.send_json({'data': event['data'],
                                               'width': event['metadata']['deviceWidth'],
                                               'height': event['metadata']['deviceHeight']})
            except WebSocketDisconnect:
                pass
            finally:
                disconnected.cancel()
                await asyncio.gather(disconnected, return_exceptions=True)
                client.remove_listener('Page.screencastFrame', frame)
                await client.send('Page.stopScreencast')
                await client.detach()
                with suppress(WebSocketDisconnect, RuntimeError):
                    await websocket.close()

        @self.app.post('/help/{token}/action')
        async def action(token: str, request: Request):
            authorize(token)
            data = await request.json()
            p = await page()
            kind = data.get('kind')
            if kind == 'click':
                await p.mouse.click(float(data['x']), float(data['y']))
            elif kind in ('move', 'down', 'up'):
                await p.mouse.move(float(data['x']), float(data['y']))
                if kind != 'move':
                    button = data.get('button', 'left')
                    if button not in ('left', 'middle', 'right'):
                        raise HTTPException(400, 'Unknown mouse button')
                    await getattr(p.mouse, kind)(button=button, click_count=int(data.get('count', 1)))
                    (self.held_buttons.add if kind == 'down' else self.held_buttons.discard)(button)
            elif kind == 'type':
                await p.keyboard.insert_text(str(data['text']))
            elif kind == 'key':
                await p.keyboard.press(str(data['key']))
            elif kind in ('keydown', 'keyup'):
                await getattr(p.keyboard, 'down' if kind == 'keydown' else 'up')(str(data['key']))
                (self.held_keys.add if kind == 'keydown' else self.held_keys.discard)(str(data['key']))
            elif kind == 'release':
                await self.release_inputs()
            elif kind == 'scroll':
                await p.mouse.wheel(float(data.get('dx', 0)), float(data['dy']))
            else:
                raise HTTPException(400, 'Unknown action')
            return {'ok': True}

        @self.app.post('/help/{token}/resume')
        async def resume(token: str, request: Request):
            authorize(token)
            data = await request.json()
            kind = self.event['kind']
            if kind == 'confirmation' and not isinstance(data.get('confirmed'), bool):
                raise HTTPException(422, 'An explicit confirmation decision is required')
            if kind == 'inputs':
                values = data.get('values')
                if not isinstance(values, dict) or any(f['name'] not in values for f in self.event['fields']):
                    raise HTTPException(422, 'All requested values are required')
            # Recheck after reading the body: concurrent submissions cannot resolve twice.
            authorize(token)
            self.pending.set_result(data)
            return {'status': 'resumed'}

        @self.app.post('/help/{token}/cancel')
        async def cancel(token: str):
            authorize(token)
            self.pending.set_exception(HandoffCancelled('Human cancelled the request'))
            return {'status': 'cancelled'}

    async def start(self):
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen(128)
        self.base = f'http://127.0.0.1:{self.socket.getsockname()[1]}'
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level='error', access_log=False))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        while not self.server.started:
            if self.task.done():
                await self.task
                raise RuntimeError('Assistance server failed to start')
            await asyncio.sleep(.01)

    async def release_inputs(self):
        if self.context.pages:
            page = self.context.pages[-1]
            await release_controls(page, self.held_keys, self.held_buttons)
        self.held_keys.clear()
        self.held_buttons.clear()

    async def request(self, reason, category='other', kind='browser', fields=None):
        if self.pending:
            raise RuntimeError('An assistance request is already active')
        self.token = secrets.token_urlsafe(32)
        url = f'{self.base}/help/{self.token}'
        self.pending = asyncio.get_running_loop().create_future()
        self.event = pause_payload(reason, category, self.cdp_url, url + '/resume',
                                   assistance_url=url, user_id=self.user, kind=kind, fields=fields or [])
        try:
            if self.notify:
                await self.notify(dict(self.event))
            else:
                print(f'\nHuman assistance needed: {reason}\n{url}\n'
                      'Resolve and resume from this link; the agent is waiting.', flush=True)
            return await self.pending
        finally:
            if not self.pending.done():
                self.pending.cancel()
            self.pending = self.token = self.event = None
            await self.release_inputs()

    async def close(self):
        if self.pending and not self.pending.done():
            self.pending.cancel()
        self.server.should_exit = True
        await self.task
        self.socket.close()


PAGE = '''<!doctype html><meta name="referrer" content="no-referrer">
<title>FlowPilot assistance</title>
<style>body{font:16px system-ui;max-width:1100px;margin:24px auto}button,input{padding:10px;margin:4px}img{max-width:100%;outline:1px solid #aaa;touch-action:none;user-select:none}img:focus{outline:2px solid #2563eb}pre{white-space:pre-wrap}</style>
<h1>FlowPilot needs your help</h1><pre id="reason"></pre><p id="site"></p>
<div id="fields"></div><div id="browser">
<p>Live browser control: move, click, drag, scroll and type directly on the image. Click outside it to release keyboard control. The text box below also supports pasting.</p>
<input id="text" type="password" placeholder="Text to type (not recorded)">
<button id="type">Type</button><button data-key="Tab">Tab</button>
<button data-key="Enter">Enter</button><button data-key="Backspace">Backspace</button>
<button id="up">Scroll up</button><button id="down">Scroll down</button>
<img id="screen" tabindex="0" draggable="false" alt="Live browser stream"></div>
<p><button id="resume">Resume agent</button><button id="decline" hidden>Decline</button><button id="cancel">Cancel task</button></p>
<pre id="status"></pre>
<script>
const base=location.pathname;let state,stopped=false,viewport=null,streaming=false,queue=Promise.resolve();
async function send(path,data){const r=await fetch(base+'/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});if(!r.ok)throw Error(await r.text());return r.json()}
function action(data){queue=queue.then(()=>stopped?null:send('action',data)).catch(e=>statusText(e.message));return queue}
function statusText(s){document.getElementById('status').textContent=s}
async function refresh(){if(stopped||streaming)return;const r=await fetch(base+'/screen');if(!r.ok){stopped=true;return}const old=screen.src;screen.src=URL.createObjectURL(await r.blob());if(old.startsWith('blob:'))URL.revokeObjectURL(old)}
const screen=document.getElementById('screen');
function point(e){const r=screen.getBoundingClientRect();return {x:(e.clientX-r.left)*(viewport?.width||screen.naturalWidth)/r.width,y:(e.clientY-r.top)*(viewport?.height||screen.naturalHeight)/r.height}}
let movement=null,moveTimer=null;
function flushMove(){if(moveTimer)clearTimeout(moveTimer);moveTimer=null;if(movement){action({kind:'move',...movement});movement=null}}
screen.onpointermove=e=>{movement=point(e);if(!moveTimer)moveTimer=setTimeout(flushMove,16)};
screen.onpointerdown=e=>{e.preventDefault();screen.focus();screen.setPointerCapture(e.pointerId);flushMove();action({kind:'down',...point(e),button:['left','middle','right'][e.button]||'left',count:e.detail||1})};
screen.onpointerup=e=>{e.preventDefault();flushMove();action({kind:'up',...point(e),button:['left','middle','right'][e.button]||'left',count:e.detail||1});screen.releasePointerCapture(e.pointerId)};
screen.oncontextmenu=e=>e.preventDefault();
screen.addEventListener('wheel',e=>{e.preventDefault();flushMove();const factor=e.deltaMode===1?16:e.deltaMode===2?screen.clientHeight:1;action({kind:'scroll',dx:e.deltaX*factor,dy:e.deltaY*factor})},{passive:false});
screen.onkeydown=e=>{if(e.key==='Unidentified'||e.isComposing)return;e.preventDefault();action({kind:'keydown',key:e.key===' '?'Space':e.key})};
screen.onkeyup=e=>{if(e.key==='Unidentified'||e.isComposing)return;e.preventDefault();action({kind:'keyup',key:e.key===' '?'Space':e.key})};
screen.onpaste=e=>{e.preventDefault();action({kind:'type',text:e.clipboardData.getData('text')})};
screen.onblur=()=>{flushMove();action({kind:'release'})};
screen.onpointercancel=()=>action({kind:'release'});
window.addEventListener('blur',()=>{flushMove();action({kind:'release'})});
const socket=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+base+'/stream');
socket.onmessage=e=>{const frame=JSON.parse(e.data);viewport=frame;streaming=true;const old=screen.src;screen.src='data:image/jpeg;base64,'+frame.data;if(old.startsWith('blob:'))URL.revokeObjectURL(old)};
socket.onclose=()=>{streaming=false;if(!stopped)statusText('Stream disconnected; using screenshot fallback.')};
document.getElementById('type').onclick=()=>{const t=document.getElementById('text');action({kind:'type',text:t.value});t.value=''};
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>action({kind:'key',key:b.dataset.key}));
document.getElementById('up').onclick=()=>action({kind:'scroll',dy:-500});document.getElementById('down').onclick=()=>action({kind:'scroll',dy:500});
async function finish(confirmed){try{flushMove();await action({kind:'release'});const values={};document.querySelectorAll('[data-field]').forEach(e=>values[e.dataset.field]=e.value);await send('resume',{confirmed,values});stopped=true;socket.close();statusText('Agent resumed. You can close this page.')}catch(e){statusText(e.message)}}
document.getElementById('resume').onclick=()=>finish(true);document.getElementById('decline').onclick=()=>finish(false);
document.getElementById('cancel').onclick=async()=>{flushMove();await action({kind:'release'});await send('cancel',{});stopped=true;socket.close();statusText('Task cancelled')};
(async()=>{state=await(await fetch(base+'/state')).json();document.getElementById('reason').textContent=state.message;document.getElementById('site').textContent=state.url;
for(const f of state.fields){const label=document.createElement('label');label.textContent=f.label||f.name;const input=document.createElement(f.type==='select'?'select':'input');input.dataset.field=f.name;for(const o of f.options||[]){const option=document.createElement('option');option.textContent=o;input.append(option)}label.append(input);document.getElementById('fields').append(label)}
document.getElementById('decline').hidden=state.kind!=='confirmation';if(state.kind==='confirmation')document.getElementById('resume').textContent='Approve';await refresh();setInterval(refresh,1500)})();
</script>'''
