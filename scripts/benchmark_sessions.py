"""Capacity test for isolated full-Chromium/Xvfb browser workers.

No third-party websites, account data, or model calls. CPU sets constrain the
browser pool; RAM is split evenly after reserving 25% for non-browser overhead.
The host-side Playwright/browser-use controller is measured but not constrained.
Run: uv run python -m scripts.benchmark_sessions --output reports/session-capacity.json
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import statistics
import time
import uuid

import docker
import httpx
import psutil
from browser_use import Browser
from playwright.async_api import async_playwright


GIB = 1024 ** 3
MIB = 1024 ** 2
FIXTURE = '''<!doctype html><title>Capacity shopping fixture</title>
<style>body{font:16px sans-serif}.grid{display:grid;grid-template-columns:repeat(4,1fr)}article{height:120px;border:1px solid #ccc;margin:4px;padding:8px}canvas{width:80px;height:60px}</style>
<input id="search" placeholder="Search"><output id="total">0</output><div class="grid" id="products"></div>
<iframe src="https://frame-a.test/" width="300" height="120"></iframe>
<iframe src="https://frame-b.test/" width="300" height="120"></iframe>
<script>
window.cart={};
for(let i=0;i<300;i++){let card=document.createElement('article');card.dataset.id=i;card.innerHTML=`<canvas width="160" height="120"></canvas><span>Sandwich product ${i}</span> <span>₹${20+i%100}</span><button id="add-${i}">Add</button>`;let ctx=card.querySelector('canvas').getContext('2d');ctx.fillStyle=`hsl(${i%360},70%,60%)`;ctx.fillRect(0,0,160,120);ctx.fillStyle='black';ctx.fillText('Product '+i,10,50);card.querySelector('button').onclick=()=>{cart[i]=(cart[i]||0)+1;document.querySelector('#total').textContent=Object.values(cart).reduce((a,b)=>a+b,0)};document.querySelector('#products').append(card)}
document.querySelector('#search').oninput=e=>{for(const card of document.querySelectorAll('article'))card.hidden=!card.innerText.toLowerCase().includes(e.target.value.toLowerCase())};
</script>'''
FRAME = '<html><body><h2>Cross-origin component</h2><canvas width="600" height="300"></canvas></body></html>'


def percentile(values, percentile):
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * percentile / 100) - 1)]


def own_process_usage():
    parent = psutil.Process()
    processes = [parent] + parent.children(recursive=True)
    rss, cpu = 0, 0
    for process in processes:
        try:
            rss += process.memory_info().rss
            times = process.cpu_times()
            cpu += times.user + times.system
        except psutil.Error:
            pass
    return {'rss_mib': rss / MIB, 'cpu_seconds': cpu}


async def scenario(client, playwright, cpus, gib, count, seconds, repeat, run_id, pids_limit):
    allocation = int(gib * GIB * .75 / count)
    case = {'cpus': cpus, 'machine_ram_gib': gib, 'concurrency': count,
            'per_container_memory_mib': round(allocation / MIB), 'repeat': repeat,
            'duration_seconds': seconds, 'pid_limit': pids_limit,
            'sessions': [], 'samples': [], 'status': 'running'}
    containers = []
    resources = []
    start = time.monotonic()
    stop_sampling = asyncio.Event()
    sampler = None
    monitor_error = None

    def create(index):
        container = client.containers.run(
            'flowpilot-sandbox', detach=True,
            name=f'flowpilot-bench-{run_id}-{cpus}-{gib}-{count}-{repeat}-{index}',
            labels={'flowpilot-benchmark': run_id, 'benchmark-user': str(index)},
            environment={'DISPLAY': ':1', 'CHROME_AUTORESTART': 'false'},
            ports={'9223/tcp': ('127.0.0.1', None)},
            cpuset_cpus=f'0-{cpus-1}', mem_limit=allocation, memswap_limit=allocation,
            pids_limit=pids_limit, shm_size='256m', security_opt=['no-new-privileges:true'])
        containers.append(container)
        return container

    async def prepare(index):
        record = {'index': index, 'ok': False, 'latencies': [], 'cart_cycles': 0, 'screenshot_ms': []}
        case['sessions'].append(record)
        launched = time.monotonic()
        try:
            container = await asyncio.to_thread(create, index)
            await asyncio.to_thread(container.reload)
            assert not container.attrs['Mounts'], 'Unexpected shared mount'
            port = container.attrs['NetworkSettings']['Ports']['9223/tcp'][0]['HostPort']
            url = f'http://127.0.0.1:{port}'
            async with httpx.AsyncClient(trust_env=False) as http:
                deadline = time.monotonic() + 60
                while True:
                    try:
                        response = await http.get(url+'/json/version', timeout=1)
                        response.raise_for_status()
                        ws = url.replace('http:', 'ws:') + '/devtools/' + response.json()['webSocketDebuggerUrl'].split('/devtools/', 1)[1]
                        break
                    except (httpx.HTTPError, KeyError):
                        if time.monotonic() > deadline:
                            raise TimeoutError('CDP readiness timeout')
                        await asyncio.sleep(.25)
            remote = await playwright.chromium.connect_over_cdp(ws, timeout=20000)
            context = remote.contexts[0]
            await context.route('**/*', lambda route: route.fulfill(
                content_type='text/html', body=FRAME if 'frame-' in route.request.url else FIXTURE))
            browser = Browser(cdp_url=ws, keep_alive=True)
            resources.append(browser)
            await browser.start()
            page = context.pages[0]
            await page.goto('https://shopping.test/', wait_until='load', timeout=20000)
            before = await page.evaluate("({cookie:document.cookie,local:localStorage.getItem('user')})")
            assert before == {'cookie': '', 'local': None}, before
            await page.evaluate("id=>{localStorage.setItem('user',id);document.cookie='user='+id+'; path=/; SameSite=Lax';}", str(index))
            record['startup_seconds'] = round(time.monotonic() - launched, 3)
            record['isolated_initial_state'] = True
            record['ok'] = True
            return record, page, context
        except Exception as exc:
            record['error'] = str(exc)[:600]
            record['startup_seconds'] = round(time.monotonic() - launched, 3)
            return record, None, None

    async def workload(record, page, context, end):
        if page is None:
            return
        index = record['index']
        try:
            while time.monotonic() < end:
                tick = time.monotonic()
                # Five UI additions with actual click events and DOM rendering.
                await page.locator('#search').fill('Sandwich product')
                for item in range(5):
                    await page.locator(f'#add-{item}').click(timeout=5000)
                result = await page.evaluate("({cart, total:document.querySelector('#total').textContent, local:localStorage.getItem('user'), cookie:document.cookie})")
                assert result['total'] == '5'
                assert result['cart'] == {str(i): 1 for i in range(5)}
                assert result['local'] == str(index) and result['cookie'] == f'user={index}'
                record['cart_cycles'] += 1
                record['latencies'].append(round((time.monotonic() - tick) * 1000, 2))
                # Snapshot cost approximates an occasional human review, not
                # continuously streaming viewers on every session.
                if record['cart_cycles'] % 3 == 0:
                    shot = time.monotonic()
                    await page.screenshot(type='jpeg', quality=75, timeout=10000)
                    record['screenshot_ms'].append(round((time.monotonic()-shot)*1000, 2))
                await page.reload(wait_until='load', timeout=15000)
                await asyncio.sleep(.25)
            record['isolation_verified_after_load'] = True
        except Exception as exc:
            record['ok'] = False
            record['error'] = str(exc)[:600]

    def stats(container):
        sample = container.stats(stream=False, one_shot=True)
        memory = sample.get('memory_stats', {})
        return {'memory_mib': memory.get('usage', 0)/MIB,
                'working_mib': max(0, memory.get('usage', 0)-memory.get('stats', {}).get('inactive_file', 0))/MIB,
                'cpu_seconds': sample.get('cpu_stats', {}).get('cpu_usage', {}).get('total_usage', 0)/1e9,
                'pids': sample.get('pids_stats', {}).get('current', 0)}

    async def sample_loop():
        nonlocal monitor_error
        try:
            while not stop_sampling.is_set():
                snapshot = await asyncio.gather(*(asyncio.to_thread(stats, c) for c in list(containers)), return_exceptions=True)
                valid = [s for s in snapshot if isinstance(s, dict)]
                case['samples'].append({'t': round(time.monotonic()-start, 2),
                    'memory_mib': round(sum(s['memory_mib'] for s in valid), 2),
                    'working_mib': round(sum(s['working_mib'] for s in valid), 2),
                    'cpu_seconds': sum(s['cpu_seconds'] for s in valid),
                    'pids_total': sum(s['pids'] for s in valid),
                    'controller': own_process_usage(), 'sample_errors': len(snapshot)-len(valid)})
                await asyncio.sleep(1)
        except Exception as exc:
            monitor_error = str(exc)

    try:
        sampler = asyncio.create_task(sample_loop())
        prepared = await asyncio.wait_for(asyncio.gather(*(prepare(i) for i in range(count))), timeout=95)
        case['active_started_at'] = round(time.monotonic()-start, 2)
        end = time.monotonic() + seconds
        await asyncio.wait_for(asyncio.gather(*(workload(*entry, end) for entry in prepared)), timeout=seconds+25)
    except Exception as exc:
        case['error'] = str(exc)[:600]
    finally:
        stop_sampling.set()
        if sampler:
            await sampler
        if monitor_error:
            case['monitor_error'] = monitor_error
        for browser in resources:
            try:
                await asyncio.wait_for(browser.stop(), 5)
            except Exception:
                pass
        for container in containers:
            try:
                await asyncio.to_thread(container.reload)
                result = await asyncio.to_thread(container.exec_run, ['sh', '-c',
                    'cat /sys/fs/cgroup/memory.events; cat /sys/fs/cgroup/pids.events'])
                case.setdefault('container_events', []).append(result.output.decode(errors='replace'))
            except Exception as exc:
                case.setdefault('container_events', []).append(str(exc)[:300])
            finally:
                await asyncio.to_thread(container.remove, force=True)
    records = case['sessions']
    latencies = [x for s in records for x in s['latencies']]
    samples = case['samples']
    active = [s for s in samples if s['t'] >= case.get('active_started_at', math.inf)]
    startup = [s['startup_seconds'] for s in records if 'startup_seconds' in s]
    events = case.get('container_events', [])
    case['summary'] = {
        'successes': sum(s['ok'] and s.get('isolation_verified_after_load', False) for s in records),
        'cycles': sum(s['cart_cycles'] for s in records),
        'startup_p95_seconds': percentile(startup, 95),
        'cart_p50_ms': percentile(latencies, 50), 'cart_p95_ms': percentile(latencies, 95),
        'peak_memory_mib': max((s['memory_mib'] for s in samples), default=0),
        'peak_working_mib': max((s['working_mib'] for s in samples), default=0),
        'peak_controller_rss_mib': max((s['controller']['rss_mib'] for s in samples), default=0),
        'peak_pids_total': max((s['pids_total'] for s in samples), default=0),
        'active_cpu_cores': ((active[-1]['cpu_seconds']-active[0]['cpu_seconds'])/(active[-1]['t']-active[0]['t'])) if len(active)>1 else None,
        'resource_limit_hit': any(any(line.split()[-1] != '0' for line in e.splitlines() if line.startswith(('oom ', 'oom_kill ', 'max '))) for e in events),
    }
    summary = case['summary']
    case['status'] = 'pass' if (not case.get('error') and summary['successes']==count
        and not summary['resource_limit_hit'] and summary['cart_p95_ms'] is not None
        and summary['cart_p95_ms']<=5000 and summary['startup_p95_seconds']<=60) else 'fail'
    case['elapsed_seconds'] = round(time.monotonic()-start, 2)
    return case


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profiles', default='2:2,4:4,8:6', help='CPU:GiB pairs')
    parser.add_argument('--counts', default='1,2,4,6,8,12,16')
    parser.add_argument('--seconds', type=int, default=30)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--pids-limit', type=int, default=1024)
    args = parser.parse_args()
    client = docker.from_env()
    info = client.info()
    profiles = [tuple(map(int, s.split(':'))) for s in args.profiles.split(',')]
    if any(cpu>info['NCPU'] or gib*GIB>info['MemTotal']*.85 for cpu,gib in profiles):
        raise ValueError('Requested pool exceeds available Docker VM resources/headroom')
    run_id = uuid.uuid4().hex[:8]
    output = {'started_at': datetime.now(timezone.utc).isoformat(), 'run_id': run_id,
              'host': {'platform':platform.platform(),'ram_gib':psutil.virtual_memory().total/GIB,'logical_cpus':psutil.cpu_count()},
              'docker': {'cpus':info['NCPU'],'ram_gib':info['MemTotal']/GIB,'version':info['ServerVersion'],'architecture':info['Architecture']},
              'image_id':client.images.get('flowpilot-sandbox').id,
              'method': 'Shared CPU sets, 75% RAM evenly split into hard container limits; no swap; host controller outside limits. Fresh isolated profiles and simultaneous startup. Synthetic 300-product shop with 2 cross-origin frames, 5 UI additions per cycle, reload, 250ms think time, screenshot every third cycle. No model inference.',
              'pass_criteria': 'All sessions finish, isolation intact, no memory/PID max or OOM events, startup p95 <=60s and five-item cart p95 <=5s.',
              'cases': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(output, indent=2))
    save()
    try:
        async with async_playwright() as playwright:
            for cpus,gib in profiles:
                for count in map(int,args.counts.split(',')):
                    if gib*GIB*.75/count < 256*MIB:
                        break
                    failed = False
                    for repeat in range(args.repeats):
                        case = await scenario(client, playwright, cpus,gib,count,args.seconds,repeat,run_id,args.pids_limit)
                        output['cases'].append(case)
                        save()
                        print(json.dumps({k:case[k] for k in ('cpus','machine_ram_gib','concurrency','repeat','status','summary')}),flush=True)
                        if case['status'] != 'pass':
                            failed = True
                            break
                    if failed:
                        break
    finally:
        # Exact run label only: never touch application or other users' containers.
        for container in client.containers.list(all=True, filters={'label':f'flowpilot-benchmark={run_id}'}):
            container.remove(force=True)
        output['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
        client.close()


if __name__ == '__main__':
    asyncio.run(main())
