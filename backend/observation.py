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
from .task_ports import MAX_REFERENCES, MAX_VIEW, ElementReference, Observation
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
    if (!state.nodes.has(node)) state.nodes.set(node,
      Array.from(crypto.getRandomValues(new Uint8Array(16)), b=>b.toString(16).padStart(2,'0')).join(''));
    return state.nodes.get(node);
  };
  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
  const controls = [], content = [], display = [], nodes = [], docs = [], opaque = [];
  let unknown = false, busy = false, truncated = false, size = 0, count = 0;
  const viewport = Math.max(1, innerWidth * innerHeight);
  const visible = el => {
    const style = el.ownerDocument.defaultView.getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      el.getClientRects().length > 0;
  };
  // root is a Document or an open ShadowRoot; doc is always its owning document.
  const walk = (root, frame) => {
    const doc = root.ownerDocument || root;
    if (root === doc) {
      docs.push([frame, token(doc.documentElement)]);
      busy ||= doc.readyState === 'loading';
    }
    busy ||= !!root.querySelector('[aria-busy="true"]');
    const tree = doc.createTreeWalker(root === doc ? (doc.body || doc.documentElement) : root,
      NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let el = tree.currentNode;
    do {
      // Large pages are captured up to a bound and marked truncated: the
      // captured prefix and the counts still fingerprint every change in it.
      if (++count > 100000 || size > 1000000) { truncated = true; break; }
      if (el.nodeType === Node.TEXT_NODE) {
        const p = el.parentElement;
        // Only an explicit data-observation-volatile="timestamp"
        // timestamp is ignored, and never inside a control/result/form field.
        if (p && visible(p) && !p.closest('script,style,noscript') &&
            !(p.closest('[data-observation-volatile="timestamp"]') &&
              !p.closest('button,a,input,select,textarea,[role],[data-result],output,label,form'))) {
          const text = clean(el.textContent);
          if (text) {
            content.push([frame, text]); size += text.length;
            if (!p.closest('textarea,[contenteditable="true"],[data-private]')) display.push([frame, text]);
          }
        }
      } else if (el.nodeType === Node.ELEMENT_NODE && visible(el)) {
        if (el.shadowRoot) walk(el.shadowRoot, frame + '#' + token(el));
        if (el.matches('iframe,frame')) {
          let inner = null;
          try { inner = el.contentDocument; } catch (_) {}
          if (inner) walk(inner, frame + '/' + token(el));
          else {
            // Page scripts cannot act inside a cross-origin frame, so it cannot
            // hold this task's effects; only a frame that is the page's main
            // content leaves the observation genuinely unknown.
            const r = el.getBoundingClientRect();
            const area = Math.max(0, r.width) * Math.max(0, r.height) / viewport;
            let origin = '';
            try { origin = new URL(el.src, location.href).origin; } catch (_) {}
            opaque.push([frame + '/' + token(el), origin, Math.round(area * 100) / 100]);
            if (area >= 0.5) unknown = true;
          }
        }
        const control = el.matches('a,button,input,select,textarea,[role],[contenteditable="true"],[onclick],[tabindex]');
        // Past the reference bound, controls are not addressable; text is still read.
        if (control && controls.length >= __MAX_REFERENCES__) truncated = true;
        else if (control) {
          const labelled = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
            .map(id => clean(doc.getElementById(id)?.textContent)).join(' ');
          const identity = [frame, el.tagName, el.id, el.getAttribute('name'),
            el.getAttribute('type'), el.getAttribute('role'), el.getAttribute('aria-label'),
            el.matches('textarea,[contenteditable="true"]') ? '' : clean(el.textContent), el.getAttribute('href'),
            labelled, Array.from(el.labels || [], label=>clean(label.textContent))];
          const value = 'value' in el ? String(el.value) : null;
          controls.push({identity, status: [el.matches(':disabled'), el.getAttribute('aria-disabled'),
            el.getAttribute('aria-expanded'), el.getAttribute('aria-checked'),
            !!el.checked, value, el.getAttribute('aria-selected'), el.getAttribute('aria-pressed'),
            el.getAttribute('aria-invalid'), !!el.readOnly, !!el.required, el.selectedIndex ?? null],
            label: identity.slice(1, 7).filter(Boolean).join(' ') +
            ' ' + identity[7].slice(0, 160)});
          nodes.push(token(el));
        }
      }
    } while ((el = tree.nextNode()));
  };
  walk(document, 'top');
  return {url: location.href, docs, controls, content, display, nodes, busy, unknown, truncated, opaque, count};
}""".replace('__MAX_REFERENCES__', str(MAX_REFERENCES))


# Characters of controls and of page text shown per observe part (within MAX_VIEW).
CONTROL_PAGE, TEXT_PAGE = 24000, 34000


def _pages(items, size):
    """Pack lines into consecutive pages of at most `size` characters."""
    pages, page, used = [], [], 0
    for item in items:
        while len(item) > size:  # a single huge line is split, never dropped
            if page:
                pages.append(page)
                page, used = [], 0
            pages.append([item[:size]])
            item = item[size:]
        if used + len(item) + 1 > size and page:
            pages.append(page)
            page, used = [], 0
        page.append(item)
        used += len(item) + 1
    return pages + [page] if page or not pages else pages


def _windows(text, needle, radius=500, limit=TEXT_PAGE):
    """Passages around each match of `needle`, merged when they overlap."""
    spans, lower, start = [], text.lower(), 0
    while (start := lower.find(needle, start)) != -1:
        a, b = max(0, start - radius), min(len(text), start + len(needle) + radius)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = b
        else:
            spans.append([a, b])
        start += len(needle)
    passages, used = [], 0
    for a, b in spans:
        passage = ('…' if a else '') + text[a:b] + ('…' if b < len(text) else '')
        if used + len(passage) > limit:
            break
        passages.append(passage)
        used += len(passage) + 1
    return passages


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
        # Freshness fences what an action can target: the document, every control's
        # identity, state and node instance. Text-only churn (tickers, carousels,
        # rotating ads) still moves the progress fingerprint for no-progress
        # detection but does not invalidate actions; decisions that depend on a
        # displayed value are restated in the confirmation the owner approves.
        controls = [(c['identity'], c['status']) for c in raw['controls']]
        freshness = self._digest([target, raw['url'], document, structural, controls, raw['nodes'], raw['busy'], raw['unknown']])
        return raw, target, document, structural, progress, freshness

    def _snapshot(self, capture, readiness, part=0, find=None):
        raw, target, document, structural, progress, freshness = capture
        snapshot_id = secrets.token_hex(16) + '_' + freshness
        # Every captured control keeps a reference in every part, so a control
        # read on a later part stays actionable; only the model's text is paged.
        refs = tuple(ElementReference(reference=f'r{i}', snapshot_id=snapshot_id,
                                      fingerprint=self._digest(c['identity']))
                     for i, c in enumerate(raw['controls']))
        lines = [f'{ref.reference}: {c["label"]} disabled={c["status"][0]}'
                 for ref, c in zip(refs, raw['controls'])]
        # Text is from the same capture, never a later unrelated DOM read.
        # Form values are digest-only. Page text is ephemeral/untrusted and
        # intentionally not included in safe metrics or persistence.
        texts = [text for _, text in raw.get('display', ())]
        notes = []
        if raw.get('truncated'):
            notes.append('(Page capture truncated at its size bound.)')
        if raw.get('opaque'):
            notes.append('Cross-origin frames (not inspectable by scripts): ' + ', '.join(
                f'{origin or "unknown origin"} ({int(area * 100)}% of viewport)' for _, origin, area in raw['opaque'][:10]))
            if any(area >= 0.5 for _, _, area in raw['opaque']):
                notes.append('A cross-origin frame covers most of the page: usually a CAPTCHA, bot check or consent wall. '
                             'Scripts cannot act inside it; request human_help if it blocks the task.')
        if find:
            needle = find.lower()
            lines = [line for line in lines if needle in line.lower()]
            texts = _windows('\n'.join(texts), needle)
            header = f'Matches for "{find}": {len(lines)} controls, {len(texts)} text passages.'
            control_parts, text_parts = [lines], [texts]
        else:
            header = ''
            control_parts, text_parts = _pages(lines, CONTROL_PAGE), _pages(texts, TEXT_PAGE)
        parts = max(len(control_parts), len(text_parts))
        part = min(part, parts - 1)
        if parts > 1:
            header = (f'Part {part + 1} of {parts} (last part).' if part == parts - 1 else
                      f'Part {part + 1} of {parts}; call observe with part={part + 1} for the next part, or find="text" to jump to it.')
        shown_controls = control_parts[part] if part < len(control_parts) else []
        shown_text = text_parts[part] if part < len(text_parts) else []
        elements = '\n'.join(filter(None, [header, *(shown_controls or ['(no controls in this part)']), *notes,
                                           'Page text (bounded; form values omitted):', *shown_text]))
        return Observation(snapshot_id=snapshot_id, url=raw['url'], target_id=target,
                           document_id=document, elements=elements[:MAX_VIEW], references=refs,
                           structural_fingerprint=structural, progress_fingerprint=progress,
                           readiness=readiness, part=part, parts=parts)

    async def observe(self, browser, *, part=0, find=None):
        """part pages through long pages; find shows only what matches."""
        start = time.monotonic()
        deadline = start + self.settings.readiness_timeout_seconds
        window = self.settings.readiness_window_seconds
        last = None
        unchanged_since = start
        capture = None
        failed = False
        try:
            async with asyncio.timeout(self.settings.readiness_timeout_seconds):
                while True:
                    try:
                        capture = await self._capture(browser)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if capture is not None:
                            raise
                        # A document still loading after navigation destroys the
                        # evaluation context; retry until the readiness deadline.
                        failed = True
                        await asyncio.sleep(min(window / 2, 0.2))
                        continue
                    now = time.monotonic()
                    # Convergence waits for text too (results still streaming in),
                    # unlike the action fence, which only tracks action targets.
                    if (capture[4], capture[-1]) != last:
                        unchanged_since, last = now, (capture[4], capture[-1])
                    raw = capture[0]
                    if raw['unknown']:
                        trace('observation.result', readiness='unknown', seconds=round(now - start, 3))
                        return self._snapshot(capture, 'unknown', part, find)
                    if not raw['busy'] and now - start >= 2 * window and now - unchanged_since >= window:
                        trace('observation.result', readiness='ready', seconds=round(now - start, 3))
                        return self._snapshot(capture, 'ready', part, find)
                    await asyncio.sleep(min(window / 4, 0.1, max(0, deadline - now)))
        except TimeoutError:
            trace('observation.result', readiness='timeout', seconds=round(time.monotonic() - start, 3))
            if capture is not None:
                return self._snapshot(capture, 'timeout', part, find)
            if failed:
                raise _fault(ErrorCode.UNAVAILABLE, 'Browser observation unavailable; website effects are unknown') from None
            raise _fault(ErrorCode.READINESS_TIMEOUT, 'Observation deadline expired without a snapshot') from None
        except asyncio.CancelledError:
            raise
        except Exception:
            if capture is not None:
                return self._snapshot(capture, 'unknown', part, find)
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
