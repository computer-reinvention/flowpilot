"""Run one hosted-style agent task locally and print every tool call.

Same code path as a hosted browser task (BrowserSession hosted mode, native
observation policy, question/confirmation ports, saved-script memory) but on
a local Chromium, so prompt and loop changes can be iterated quickly.

    uv run python -m scripts.agent_lab "Find me a flight ..." --user lab-flights
    uv run python -m scripts.agent_lab "..." --user lab-flights   # warm: same user reuses scripts

Confirmations are declined. Input questions are answered from --answer
name=value pairs, otherwise the run is stopped. Nothing is booked.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)  # the dedicated FlowPilot key wins over shell variables

from backend import pi_browser  # noqa: E402
from backend.repl import BrowserSession  # noqa: E402
from backend.task_settings import TaskSettings  # noqa: E402
from backend.observation import create_observation  # noqa: E402

LOG = []
START = time.monotonic()


def emit(line):
    stamp = f'[{time.monotonic() - START:7.1f}s] '
    print(stamp + line, flush=True)
    LOG.append(stamp + line)


def short(value, limit):
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + f'… (+{len(text) - limit})'


def instrument(show_prompt):
    original = pi_browser.run_pi

    async def run_pi(task, system, tools, dispatch, **kwargs):
        if show_prompt:
            emit('SYSTEM PROMPT:\n' + system)
            emit('TASK PROMPT:\n' + short(task, 4000))
            emit('TOOLS: ' + ', '.join(t['name'] for t in tools))

        async def traced(name, args):
            began = time.monotonic()
            emit(f'→ {name} {short(args, 600)}')
            try:
                result = await dispatch(name, args)
            except BaseException as exc:
                emit(f'  ✗ {name} {type(exc).__name__}: {short(str(exc), 600)} ({time.monotonic() - began:.1f}s)')
                raise
            emit(f'  ← {name} {short(result, 900)} ({time.monotonic() - began:.1f}s)')
            return result

        return await original(task, system, tools, traced, **kwargs)

    pi_browser.run_pi = run_pi


class LabChannel:
    """Browser handoffs cannot be served in the lab; they end the run."""

    async def start(self):
        pass

    async def close(self):
        pass

    async def request(self, reason, category='other', **_):
        emit(f'HANDOFF requested ({category}): {reason}')
        raise RuntimeError('Browser handoff requested; lab run stops here')


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('task')
    parser.add_argument('--user', default='lab')
    parser.add_argument('--answer', action='append', default=[], help='name=value for input questions')
    parser.add_argument('--headful', action='store_true')
    parser.add_argument('--prompt', action='store_true', help='print system and task prompts')
    parser.add_argument('--out', default='reports/agent-lab')
    args = parser.parse_args()
    answers = dict(a.split('=', 1) for a in args.answer)
    instrument(args.prompt)

    async def on_question(question):
        emit(f'QUESTION {question["kind"]}: {question["message"]} fields={[f.get("name") for f in question.get("fields") or []]}')
        if question['kind'] == 'confirmation':
            emit('  declining confirmation (lab never books)')
            return {'confirmed': False}
        names = [f['name'] for f in question.get('fields') or []]
        if names and all(n in answers for n in names):
            return {'values': {n: answers[n] for n in names}}
        raise RuntimeError('Unanswerable question; lab run stops here')

    policy = create_observation(TaskSettings())
    began = time.monotonic()
    outcome = {}
    try:
        async with BrowserSession(args.user, state_dir=Path('.lab-state'), headless=not args.headful,
                                  browser_mode='native', verbose=False, hosted=True,
                                  handoff_factory=lambda *a: LabChannel(), on_question=on_question,
                                  observation_policy=policy) as session:
            emit(f'browser ready in {time.monotonic() - began:.1f}s')
            asked = time.monotonic()
            result = await session.ask(args.task, {})
            outcome = {'state': 'completed', 'seconds': round(time.monotonic() - asked, 2), **result}
            emit(f'COMPLETED in {outcome["seconds"]}s metrics={json.dumps(result["metrics"])}')
            emit('ANSWER:\n' + result['answer'])
    except BaseException as exc:
        outcome = {'state': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
        emit('FAILED ' + outcome['error'])
        if not isinstance(exc, Exception):
            raise
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f'{time.strftime("%Y%m%dT%H%M%S")}-{args.user}.json'
    path.write_text(json.dumps({'task': args.task, 'user': args.user, 'outcome': outcome, 'log': LOG}, indent=2, default=str))
    print(f'wrote {path}')


if __name__ == '__main__':
    asyncio.run(main())
