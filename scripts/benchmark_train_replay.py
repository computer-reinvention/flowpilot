"""Live cold/warm train searches through the REPL's BrowserSession.ask API."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from backend.repl import BrowserSession


async def main():
    load_dotenv()
    os.environ.setdefault('LLM_PROVIDER', 'codex')
    os.environ['AGENT_DRIVER'] = 'pi'
    run = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = Path('reports') / f'train-replay-{run}.json'
    report = {'run': run, 'method': 'Sequential fresh identity per route; same inputs across three asks; blank page before each ask; browser startup/reset excluded; 1024 PID limit.', 'cases': []}

    def save():
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    async def question(payload):
        raise RuntimeError('Unexpected question: ' + json.dumps(payload))

    async def human(*args):
        raise RuntimeError('Browser intervention required: ' + str(args))

    for origin, date, adults in [('Berlin', '2026-10-20', 1), ('Paris', '2026-10-21', 2), ('Amsterdam', '2026-10-22', 1)]:
        case = {'origin': origin, 'destination': 'London', 'date': date, 'adults': adults, 'runs': []}
        report['cases'].append(case)
        values = {'origin': origin, 'destination': 'London', 'date': date, 'adults': adults,
                  'current_date': '2026-10-19', 'timezone': 'Europe/Berlin'}
        query = f'Can you find one-way train tickets from {origin} to London on {date} for {adults} adult(s)? Show available departures, durations and prices. Do not book.'
        try:
            async with BrowserSession(f'train-bench-{run}-{origin.lower()}', on_question=question, human_help=human) as session:
                ids = subprocess.check_output(['docker', 'ps', '--filter', f'label=user-id={session.user}', '--format', '{{.ID}}'], text=True).split()
                assert len(ids) == 1
                subprocess.run(['docker', 'update', '--pids-limit', '1024', ids[0]], check=True)
                for label in ['cold', 'warm1', 'warm2']:
                    pages = session.context.pages
                    for page in pages[1:]:
                        await page.close()
                    await pages[0].goto('about:blank')
                    record = {'label': label, 'scripts_before': session.store.scripts()}
                    case['runs'].append(record)
                    started = time.monotonic()
                    try:
                        record.update(await asyncio.wait_for(session.ask(query, dict(values)), timeout=600))
                    except Exception as exc:
                        record['error'] = f'{type(exc).__name__}: {exc}'
                        record['last_turn'] = session.store.history(1)
                    record['elapsed_seconds'] = round(time.monotonic() - started, 2)
                    record['scripts_after'] = session.store.scripts()
                    record['saved_scripts'] = [dict(r) for r in session.store.db.execute('SELECT name,description,script FROM reflexes')]
                    record['pages'] = []
                    for page in session.context.pages:
                        try:
                            record['pages'].append({'url': page.url, 'text': (await page.locator('body').inner_text(timeout=5000))[:50000]})
                        except Exception as exc:
                            record['pages'].append({'url': page.url, 'error': str(exc)})
                    save()
                    print(json.dumps({'origin': origin, 'label': label, 'metrics': record.get('metrics'), 'error': record.get('error'), 'answer': record.get('answer')}, ensure_ascii=False), flush=True)
        except Exception as exc:
            case['error'] = f'{type(exc).__name__}: {exc}'
        save()
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    print(f'REPORT: {output}', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
