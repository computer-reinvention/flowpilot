"""Live task adaptation and serialized, generation-fenced human browser access."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ..repl import BrowserSession
from ..task_contracts import (
    BrowserMetadata, ErrorCode, InputField, Interaction, InteractionKind, PollRequest, TaskFault,
    TaskState, task_error,
)
from .browser_adapter import create_browser, mark_recovery
from .memory import ProfileStore


def stale():
    return TaskFault(task_error(ErrorCode.STALE_GENERATION, 'Browser pause is no longer active'))


class HumanBrowser:
    def __init__(self, runtime, interaction):
        self.runtime, self.interaction = runtime, interaction
        self.released = False

    async def _check(self):
        r = self.runtime
        if self.released or r.fenced or r.human_fenced or r.pause != self.interaction:
            raise stale()
        view = await r.control.get(r.lease.principal, r.lease.task_id, PollRequest())
        if self.released or r.fenced or r.human_fenced or r.pause != self.interaction:
            raise stale()
        if view.state != TaskState.WAITING_FOR_BROWSER or view.interaction != self.interaction:
            raise stale()
        if self.interaction.expires_at <= datetime.now(timezone.utc):
            raise stale()

    async def screenshot(self):
        return await self._capture('png')

    async def _capture(self, image_type):
        async with self.runtime.writer:
            await self._check()
            image = await self.runtime.page().screenshot(type=image_type,
                **({'quality': 70} if image_type == 'jpeg' else {}))
            await self._check()
            return image

    async def metadata(self):
        async with self.runtime.writer:
            await self._check()
            # Read together from the live selected document, not another task's
            # cached state or an invented fallback URL/desktop resolution.
            metadata = await self.runtime.page().evaluate('''() => ({
                url: location.href, viewport_width: window.innerWidth,
                viewport_height: window.innerHeight})''')
            await self._check()
            return BrowserMetadata(**metadata)

    async def frames(self):
        while True:
            yield await self._capture('jpeg')
            await asyncio.sleep(.2)

    async def send_input(self, command):
        async with self.runtime.writer:
            await self._check()
            page = self.runtime.page()
            kind = command.kind
            if kind in ('move', 'down', 'up'):
                await page.mouse.move(command.x, command.y)
            if kind == 'down':
                self.runtime.buttons.add(command.button)
                await page.mouse.down(button=command.button)
            elif kind == 'up':
                await page.mouse.up(button=command.button)
                self.runtime.buttons.discard(command.button)
            elif kind == 'scroll':
                await page.mouse.wheel(command.dx, command.dy)
            elif kind == 'keydown':
                self.runtime.keys.add(command.key)
                await page.keyboard.down(command.key)
            elif kind == 'keyup':
                await page.keyboard.up(command.key)
                self.runtime.keys.discard(command.key)
            elif kind == 'type':
                await page.keyboard.insert_text(command.text)
            elif kind == 'release':
                await self.runtime._release_inputs()

    async def release(self):
        async with self.runtime.writer:
            if not self.released:
                await self.runtime._release_inputs()
                self.released = True


class HostedChannel:
    def __init__(self, runtime):
        self.runtime = runtime

    async def start(self):
        pass

    async def close(self):
        await self.runtime.resume()

    async def request(self, reason, category='other', kind='browser', fields=None):
        return await self.runtime.interact(reason, kind, fields)


class ConversationRuntime:
    def __init__(self, settings, execution, bindings, *, session_factory=BrowserSession):
        self.settings, self.execution, self.bindings = settings, execution, bindings
        self.lease, self.control = execution.lease, bindings.control
        self.session_factory = session_factory
        self.session = None
        self.directory = None
        self.pause = None
        self.pause_generation = 0
        self.human_fenced = False
        self.writer = asyncio.Lock()
        self.stop_lock = asyncio.Lock()
        self.writers_stopped = False
        self.keys, self.buttons = set(), set()
        self.handles = []
        self.fenced = False
        self.declined = False
        self.interruption_error = None
        self.owner = None

    async def open(self):
        try:
            store = ProfileStore(self.settings, self.lease.principal, self.lease.profile_id)
        except ValueError:
            raise TaskFault(task_error(ErrorCode.CONFLICT, 'Profile is already open')) from None
        self.directory = store.path
        if (store.path / 'recovery-required').exists():
            store.close()
            raise TaskFault(task_error(ErrorCode.PROFILE_RECOVERY_REQUIRED, 'Profile requires recovery'))
        try:
            self.session = self.session_factory(
                store.path.name, store=store, hosted=True, verbose=False,
                sandbox_factory=create_browser(self.settings, self.lease),
                handoff_factory=lambda *args: HostedChannel(self),
                on_question=self.question, observation_policy=self.bindings.observation)
        except BaseException:
            store.close()
            raise
        try:
            await self.session.__aenter__()
        except BaseException:
            # BrowserSession already runs failure cleanup. Never close a failed
            # startup twice or turn a quarantined profile into a saved profile.
            self.session = None
            if (store.path / 'recovery-required').exists():
                raise TaskFault(task_error(ErrorCode.PROFILE_RECOVERY_REQUIRED, 'Profile startup requires recovery')) from None
            raise

    def page(self):
        pages = self.session.context.pages
        if not pages:
            raise RuntimeError('No browser page')
        # Match the agent's selected CDP target rather than handing out another tab.
        return self._human_page

    async def _select_page(self):
        target = self.session.browser.agent_focus_target_id
        for page in self.session.context.pages:
            client = await self.session.context.new_cdp_session(page)
            try:
                info = await client.send('Target.getTargetInfo')
                if info['targetInfo']['targetId'] == target:
                    self._human_page = page
                    return
            finally:
                await client.detach()
        raise RuntimeError('Active browser target unavailable')

    async def question(self, question):
        return await self.interact(question['message'], question['kind'], question.get('fields'))

    async def interact(self, reason, kind='browser', fields=None):
        if self.fenced or self.pause:
            raise stale()
        kind = InteractionKind.INPUT if kind == 'inputs' else InteractionKind(kind)
        self.pause_generation += 1
        now = datetime.now(timezone.utc)
        interaction = Interaction(
            interaction_id=uuid4().hex, generation=self.pause_generation, kind=kind,
            message=str(reason)[:2000] or 'Assistance requested',
            action_id=uuid4().hex if kind == InteractionKind.CONFIRMATION else None,
            fields=tuple(InputField(name=f['name'], label=f.get('label') or f['name'],
                                    kind=f.get('kind', f.get('type', 'text')),
                                    options=tuple(f.get('options') or ())) for f in fields or ()),
            expires_at=min(self.execution.deadline, now + timedelta(seconds=self.settings.human_wait_seconds)))
        if kind == InteractionKind.BROWSER:
            await self._select_page()
        self.pause = interaction
        self.human_fenced = False
        try:
            remaining = max(0, (interaction.expires_at - now).total_seconds())
            response = await asyncio.wait_for(self.control.wait_for_interaction(self.lease, interaction), remaining)
            response.validate_for(interaction, datetime.now(timezone.utc))
            if self.fenced:
                raise stale()
            if kind == InteractionKind.CONFIRMATION:
                if not response.confirmed:
                    # Fail closed: a model cannot reinterpret a decline and act.
                    self.declined = True
                    self.fenced = True
                    if self.owner:
                        self.owner.cancel()
                    raise asyncio.CancelledError
                await self.control.require_approval(self.lease, interaction.interaction_id,
                                                    interaction.generation, interaction.action_id)
                return {'confirmed': True}
            if kind == InteractionKind.INPUT:
                return {'values': response.values}
            return {'browser_resolved': True}
        except TimeoutError:
            self.abort(task_error(ErrorCode.INTERRUPTED, 'Human wait expired; inspect website state before a new task'))
            raise TaskFault(self.interruption_error) from None
        except TaskFault as exc:
            self.abort(exc.error)
            raise
        finally:
            try:
                await self.resume()
                if kind == InteractionKind.BROWSER and not self.fenced:
                    # Inspection is delegated to the frozen policy, never a local hash.
                    await self.bindings.observation.observe(self.session.browser)
            except TaskFault as exc:
                self.abort(exc.error)
                raise
            except Exception:
                self.abort(task_error(ErrorCode.INTERRUPTED, 'Browser writer drain or inspection failed'))
                raise TaskFault(self.interruption_error) from None

    def abort(self, error):
        self.interruption_error = error
        self.fenced = True
        if self.owner:
            self.owner.cancel()

    async def _release_inputs(self):
        if not (self.keys or self.buttons):
            return
        page = self.page()
        for key in tuple(self.keys):
            await page.keyboard.up(key)
            self.keys.discard(key)
        for button in tuple(self.buttons):
            await page.mouse.up(button=button)
            self.buttons.discard(button)

    async def resume(self):
        self.human_fenced = True
        async with self.writer:
            self.pause = None
            await self._release_inputs()
            for handle in self.handles:
                handle.released = True
            self.handles.clear()

    async def release_human(self, interaction_id, generation):
        interaction = self.pause
        if self.fenced or not interaction or (interaction.interaction_id, interaction.generation) != (interaction_id, generation):
            raise stale()
        # Fence before the first await: no existing/future handle can start a
        # write while we wait for the current writer. Preserve pause identity so
        # repeated valid drains (including no-viewer cases) remain idempotent.
        self.human_fenced = True
        async with self.writer:
            if self.fenced or self.pause != interaction:
                raise stale()
            await self._release_inputs()
            if self.fenced or self.pause != interaction:
                raise stale()
            for handle in self.handles:
                handle.released = True
            self.handles.clear()

    async def browser(self, interaction_id, generation):
        interaction = self.pause
        if not interaction or (interaction.interaction_id, interaction.generation) != (interaction_id, generation):
            raise stale()
        handle = HumanBrowser(self, interaction)
        await handle._check()
        self.handles.append(handle)
        return handle

    async def run(self):
        self.owner = asyncio.current_task()
        await self.open()
        inputs = dict(self.execution.request.inputs)
        try:
            result = await self.session.ask(self.execution.request.task, inputs)
        except asyncio.CancelledError:
            if self.interruption_error is not None:
                raise TaskFault(self.interruption_error) from None
            raise
        result['answer'] = self.session.store.redact(result['answer'], inputs, limit=32000)
        return result

    async def stop_writers(self):
        self.fenced = True
        async with self.stop_lock:
            if self.writers_stopped:
                return
            # Detach browser-use's watchdogs while CDP is alive. Otherwise its
            # storage watchdog attempts long reconnects after container shutdown.
            async def bounded(operation):
                task = asyncio.create_task(operation)
                done, _ = await asyncio.wait({task}, timeout=2)
                if not done:
                    task.cancel()
                elif not task.cancelled():
                    try:
                        task.result()
                    except Exception:
                        pass
            if self.session and self.session.browser:
                await bounded(self.session.browser.stop())
                self.session.browser = None
            if self.session and self.session.remote_browser and self.session.remote_browser.is_connected():
                async def flush():
                    client = await self.session.remote_browser.new_browser_cdp_session()
                    await client.send('Browser.close')
                await bounded(flush())
            # Docker is the final enforcement boundary if CDP or JS did not stop.
            if self.session and self.session.sandbox:
                await self.session.sandbox.stop_writers()
                async with self.writer:
                    self.keys.clear()
                    self.buttons.clear()
            self.writers_stopped = True

    async def close(self):
        self.fenced = True
        await self.resume()
        if self.session:
            await self.session.__aexit__(None, None, None)

    def quarantine(self):
        if self.directory:
            container_id = None
            if self.session and self.session.sandbox and self.session.sandbox.info:
                container_id = self.session.sandbox.info['container_id']
            mark_recovery(self.directory, self.settings, self.lease, container_id)


def create_runtime(settings, execution, bindings):
    return ConversationRuntime(settings, execution, bindings)
