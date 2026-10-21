"""Bounded, ephemeral DOM observations. No browser-use Agent replay machinery.

Readiness means convergence during a finite sampling window, not a promise that
the site will never change again. Cross-origin frames and truncated captures are
unknown (fail closed). Network-idle is deliberately not a readiness requirement.
Snapshots may be sent to the model but must not be persisted or traced.
"""

import asyncio
from dataclasses import dataclass, field
from contextvars import ContextVar
import hashlib
import hmac
import json
import secrets
import time

from .task_contracts import ErrorCode, TaskFault, task_error
from .task_ports import ElementReference, Observation
from .task_settings import TaskSettings
from .trace import trace

# Carries Pi's originating snapshot through legacy reflex/exploration callbacks
# without changing their public signatures or copying runtime implementation.
# Scoped/reset around one serialized dispatch; never a process-global browser.
action_observation = ContextVar('flowpilot_action_observation', default=None)


# One synchronous evaluation builds URL, document identity, controls, relevant
# state and node-instance identities together. A WeakMap detects replacement even
# when structural markup is identical. Never assign numeric DOM indices as actions.
CAPTURE_JS = r"""() => {
  const key = Symbol.for('flowpilot.observation.v1');
  const state = window[key] || (window[key] = {nodes: new WeakMap(), next: 0});
  const token = node => {
    if (!state.nodes.has(node)) state.nodes.set(node, crypto.randomUUID());
    return state.nodes.get(node);
  };
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
  const controls = [], content = [], nodes = [], docs = [];
  let unknown = false, busy = false, size = 0;
  const visible = el => {
    const style = el.ownerDocument.defaultView.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      el.getClientRects().length > 0;
  };
  const walk = (doc, frame) => {
    docs.push([frame, token(doc.documentElement)]);
    busy ||= doc.readyState === 'loading' || !!doc.querySelector('[aria-busy="true"]');
    const tree = doc.createTreeWalker(doc.body || doc.documentElement, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let el = tree.currentNode, count = 0;
    do {
      if (++count > 12000 || size > 96000 || controls.length >= 2000) { unknown = true; break; }
      if (el.nodeType === Node.TEXT_NODE) {
        const p = el.parentElement;
        // Only an explicit <time> or data-observation-volatile="timestamp"
        // timestamp is ignored, and never inside a control/result/form field.
        if (p && visible(p) && !p.closest('script,style,noscript') &&
            !(p.closest('time,[data-observation-volatile="timestamp"]') &&
              !p.closest('button,a,input,select,textarea,[role],[data-result]'))) {
          const text = clean(el.textContent);
          if (text) { content.push([frame, text]); size += text.length; }
        }
      } else if (el.nodeType === Node.ELEMENT_NODE && visible(el)) {
        if (el.shadowRoot) unknown = true; // no silently partial shadow tree
        if (el.matches('iframe,frame')) {
          try {
            if (!el.contentDocument) unknown = true;
            else walk(el.contentDocument, frame + '/' + token(el));
          } catch (_) { unknown = true; }
        }
        if (el.matches('a,button,input,select,textarea,[role],[contenteditable="true"],[onclick],[tabindex]')) {
          const identity = [frame, el.tagName, el.id, el.getAttribute('name'),
            el.getAttribute('type'), el.getAttribute('role'), el.getAttribute('aria-label'),
            el.matches('textarea,[contenteditable="true"]') ? '' : clean(el.textContent), el.getAttribute('href')];
          const value = 'value' in el ? String(el.value) : null;
          controls.push({identity, status: [!!el.disabled, el.getAttribute('aria-disabled'),
            el.getAttribute('aria-expanded'), el.getAttribute('aria-checked'),
            !!el.checked, value], label: identity.slice(1, 7).filter(Boolean).join(' ') +
            ' ' + identity[7].slice(0, 160)});
          nodes.push(token(el));
        }
      }
    } while ((el = tree.nextNode()));
  };
  walk(document, 'top');
  return {url: location.href, docs, controls, content, nodes, busy, unknown};
}"""


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _fault(code, message):
    return TaskFault(task_error(code, message))


@dataclass(frozen=True)
class ObservationPolicy:
    """Immutable configuration; no global browser/task history or credential cache."""

    settings: TaskSettings
    _key: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)

    def _digest(self, value):
        # Keyed digests prevent offline guessing of low-entropy form values.
        return hmac.new(self._key, _encoded(value), hashlib.sha256).hexdigest()

    async def _capture(self, browser):
        target = str(browser.agent_focus_target_id)
        page = await browser.must_get_current_page()
        raw = await page.evaluate(CAPTURE_JS)
        if isinstance(raw, str):  # browser-use Actor serializes evaluate results
            raw = json.loads(raw)
        if not isinstance(raw, dict) or str(browser.agent_focus_target_id) != target:
            raise RuntimeError('Observation target changed or capture unavailable')
        identities = [c['identity'] for c in raw['controls']]
        structural = self._digest(identities)
        progress = self._digest([raw['content'], [(c['identity'], c['status']) for c in raw['controls']]])
        document = self._digest(raw['docs'])
        freshness = self._digest([target, raw['url'], document, structural, progress, raw['nodes'], raw['busy'], raw['unknown']])
        return raw, target, document, structural, progress, freshness

    def _snapshot(self, capture, readiness):
        raw, target, document, structural, progress, freshness = capture
        snapshot_id = secrets.token_hex(16) + '_' + freshness
        refs = tuple(ElementReference(reference=f'r{i}', snapshot_id=snapshot_id,
                                      fingerprint=self._digest(c['identity']))
                     for i, c in enumerate(raw['controls']))
        lines = [f'{ref.reference}: {c["label"]} disabled={c["status"][0]}'
                 for ref, c in zip(refs, raw['controls'])]
        elements = '\n'.join(lines)
        if len(elements) > 24000:
            readiness = 'unknown'
        return Observation(snapshot_id=snapshot_id, url=raw['url'], target_id=target,
                           document_id=document, elements=elements[:24000], references=refs,
                           structural_fingerprint=structural, progress_fingerprint=progress,
                           readiness=readiness)

    async def observe(self, browser):
        start = time.monotonic()
        deadline = start + self.settings.readiness_timeout_seconds
        window = self.settings.readiness_window_seconds
        last = None
        unchanged_since = start
        capture = None
        try:
            async with asyncio.timeout(self.settings.readiness_timeout_seconds):
                while True:
                    capture = await self._capture(browser)
                    now = time.monotonic()
                    if capture[-1] != last:
                        unchanged_since, last = now, capture[-1]
                    raw = capture[0]
                    if raw['unknown']:
                        trace('observation.result', readiness='unknown', seconds=round(now - start, 3))
                        return self._snapshot(capture, 'unknown')
                    if not raw['busy'] and now - start >= 2 * window and now - unchanged_since >= window:
                        trace('observation.result', readiness='ready', seconds=round(now - start, 3))
                        return self._snapshot(capture, 'ready')
                    await asyncio.sleep(min(window / 4, 0.1, max(0, deadline - now)))
        except TimeoutError:
            trace('observation.result', readiness='timeout', seconds=round(time.monotonic() - start, 3))
            if capture is not None:
                return self._snapshot(capture, 'timeout')
            raise _fault(ErrorCode.READINESS_TIMEOUT, 'Observation deadline expired without a snapshot') from None
        except asyncio.CancelledError:
            raise
        except Exception:
            if capture is not None:
                return self._snapshot(capture, 'unknown')
            raise _fault(ErrorCode.UNAVAILABLE, 'Browser observation unavailable; website effects are unknown') from None

    async def require_fresh(self, browser, observation):
        if observation.readiness != 'ready':
            raise _fault(ErrorCode.READINESS_TIMEOUT, 'Observation is not ready; inspect before acting')
        try:
            async with asyncio.timeout(self.settings.readiness_timeout_seconds):
                capture = await self._capture(browser)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _fault(ErrorCode.STALE_GENERATION, 'Cannot establish fresh browser state; observe again') from None
        if observation.snapshot_id.split('_', 1)[-1] != capture[-1]:
            trace('observation.invalidated')
            raise _fault(ErrorCode.STALE_GENERATION, 'Page changed since observation; call observe before acting')

    async def after_action(self, browser, observation, action):
        after = await self.observe(browser)
        if after.readiness != 'ready':
            raise _fault(ErrorCode.READINESS_TIMEOUT, 'Action outcome unknown: browser did not become ready; do not replay')

    async def resolve_reference(self, browser, observation, reference):
        """Validate a reference, never silently remap an index or choose a duplicate.

        No Pi element-action endpoint is offered. This helper returns metadata only;
        it does not close the race between validation and an arbitrary script.
        """
        await self.require_fresh(browser, observation)
        matches = [r for r in observation.references if r.reference == reference]
        if len(matches) != 1:
            raise _fault(ErrorCode.STALE_GENERATION, 'Unknown element reference')
        chosen = matches[0]
        if sum(r.fingerprint == chosen.fingerprint for r in observation.references) != 1:
            trace('observation.ambiguity')
            raise _fault(ErrorCode.STALE_GENERATION, 'Ambiguous element identity; use an explicit checked selector')
        return chosen


@dataclass
class ProgressGuard:
    """Task-local bounded evidence, retained across observations/human waits.

    Repeated observe/wait calls do not count. A signature with no changed progress
    is fenced before another effect once its budget is consumed. Successful action
    results are fenced separately by Pi to avoid resubmitting a completed stage.
    """

    limit: int
    attempts: dict = field(default_factory=dict, repr=False)

    def before(self, signature, observation):
        key = (signature, observation.target_id, observation.document_id,
               observation.progress_fingerprint)
        if self.attempts.get(key, 0) >= self.limit:
            trace('observation.no_progress')
            raise _fault(ErrorCode.NO_PROGRESS, 'Repeated action without progress; inspect, repair or request help')
        self.attempts[key] = self.attempts.get(key, 0) + 1
        # Keep history bounded without making observations reset loop evidence.
        if len(self.attempts) > 128:
            del self.attempts[next(iter(self.attempts))]


def create_observation(settings):
    return ObservationPolicy(settings)
