"""Atomic SQLite authorization/race evidence for the approved assistance port."""

import asyncio
from datetime import timedelta

import pytest

from backend.task_contracts import (
    AssistanceRequest, Interaction, InteractionResponse, Principal, TaskFault,
    TaskStart, TaskState,
)
from backend.task_ports import AssistanceBinding
from backend.task_settings import TaskSettings
from backend.tasks.repository import Repository, now


async def paused(root):
    repository = Repository(TaskSettings(state_dir=root), b'x' * 32)
    await repository.initialize()
    principal = Principal(account_id='owner')
    admission = await repository.admit(principal, TaskStart(task='Synthetic assistance', idempotency_key='assistance-admission-001'))
    execution = await repository.claim(principal, admission.task_id)
    interaction = Interaction(interaction_id='browser_pause', generation=7, kind='browser', message='Private synthetic assistance', expires_at=now() + timedelta(seconds=60))
    await repository.transition(execution.lease, execution.revision, TaskState.WAITING_FOR_BROWSER, interaction=interaction)
    request = AssistanceRequest(interaction_id=interaction.interaction_id, generation=interaction.generation)
    return repository, principal, execution.lease, interaction, request


@pytest.mark.asyncio
async def test_lookup_uses_execution_generation_and_checks_owner_pause_and_expiry(tmp_path):
    repository, principal, lease, interaction, request = await paused(tmp_path)
    resolved = await repository.assistance_lease(principal, lease.task_id, request)
    assert resolved == lease
    assert resolved.generation == 1 and request.generation == 7
    assert 'generation' not in (await repository.get(principal, lease.task_id)).model_dump(exclude={'interaction'})
    with pytest.raises(TaskFault) as foreign:
        await repository.assistance_lease(Principal(account_id='foreign'), lease.task_id, request)
    assert foreign.value.error.code == 'not_found'
    for wrong in (request.model_copy(update={'generation': 1}), request.model_copy(update={'interaction_id': 'other_pause'})):
        with pytest.raises(TaskFault) as stale:
            await repository.assistance_lease(principal, lease.task_id, wrong)
        assert stale.value.error.code == 'stale_generation'
    # Expiry is checked from authoritative DB readback, not a cached lookup.
    import backend.tasks.repository as module
    original = module.now
    module.now = lambda: interaction.expires_at + timedelta(seconds=1)
    try:
        with pytest.raises(TaskFault):
            await repository.assistance_lease(principal, lease.task_id, request)
    finally:
        module.now = original


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['cancel', 'resolve', 'restart'])
async def test_lookup_cannot_authorize_late_save_after_pause_mutation(tmp_path, mutation):
    repository, principal, lease, interaction, request = await paused(tmp_path)
    resolved = await repository.assistance_lease(principal, lease.task_id, request)
    binding = AssistanceBinding(lease=resolved, interaction_id=interaction.interaction_id, generation=7, digest='a' * 64, expires_at=interaction.expires_at)
    if mutation == 'cancel':
        await repository.request_cancel(principal, lease.task_id)
    elif mutation == 'resolve':
        await repository.resolve(principal, lease.task_id, interaction.interaction_id, InteractionResponse(generation=7, idempotency_key='browser-response-001', kind='browser', browser_resolved=True))
    else:
        await repository.interrupt_active()
    with pytest.raises(TaskFault):
        await repository.save_capability(binding)
    with pytest.raises(TaskFault):
        await repository.capability(binding.digest, now())


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['cancel', 'resolve'])
async def test_concurrent_capability_commit_and_mutation_never_leaves_authority(tmp_path, mutation):
    repository, principal, lease, interaction, request = await paused(tmp_path)
    binding = AssistanceBinding(lease=lease, interaction_id=interaction.interaction_id, generation=7, digest='b' * 64, expires_at=interaction.expires_at)
    if mutation == 'cancel':
        change = repository.request_cancel(principal, lease.task_id)
    else:
        change = repository.resolve(principal, lease.task_id, interaction.interaction_id, InteractionResponse(generation=7, idempotency_key='racing-response-001', kind='browser', browser_resolved=True))
    outcomes = await asyncio.gather(repository.save_capability(binding), change, return_exceptions=True)
    assert not isinstance(outcomes[1], Exception)
    assert outcomes[0] is None or isinstance(outcomes[0], TaskFault)
    with pytest.raises(TaskFault):
        await repository.capability(binding.digest, now())
    with pytest.raises(TaskFault):
        await repository.assistance_lease(principal, lease.task_id, request)


@pytest.mark.asyncio
async def test_capability_save_rechecks_execution_generation_and_rotates_digest(tmp_path):
    repository, principal, lease, interaction, request = await paused(tmp_path)
    binding = AssistanceBinding(lease=lease, interaction_id=interaction.interaction_id, generation=7, digest='c' * 64, expires_at=interaction.expires_at)
    with pytest.raises(TaskFault):
        await repository.save_capability(binding.model_copy(update={'lease': lease.model_copy(update={'generation': 7})}))
    await repository.save_capability(binding)
    assert await repository.capability(binding.digest, now()) == binding
    rotated = binding.model_copy(update={'digest': 'd' * 64})
    await repository.save_capability(rotated)
    with pytest.raises(TaskFault):
        await repository.capability(binding.digest, now())
    assert await repository.capability(rotated.digest, now()) == rotated
