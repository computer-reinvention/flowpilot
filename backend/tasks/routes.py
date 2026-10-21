"""Versioned owner HTTP API. Prefix is provided by the frozen bootstrap mount."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from ..task_contracts import (
    AssistanceRequest, Identifier, InteractionResponse, PollRequest, Principal,
    ProfileCreate, TaskStart,
)
from .identity import principal


def create_router(bindings):
    router = APIRouter()
    owner = Annotated[Principal, Depends(principal)]

    @router.post('/', status_code=202)
    async def start(body: TaskStart, account: owner):
        return await bindings.control.start(account, body)

    # Literal routes precede the task identifier route.
    @router.post('/profiles', status_code=201)
    async def create_profile(body: ProfileCreate, account: owner):
        return await bindings.control.create_profile(account, body)

    @router.get('/profiles')
    async def profiles(account: owner, cursor: Identifier | None = None):
        return await bindings.control.list_profiles(account, cursor)

    @router.delete('/profiles/{profile_id}', status_code=204)
    async def delete_profile(profile_id: Identifier, account: owner):
        await bindings.control.delete_profile(account, profile_id)
        return Response(status_code=204)

    @router.get('/{task_id}')
    async def get(task_id: Identifier, account: owner, request: Request,
                  after_revision: int | None = Query(default=None, ge=0),
                  wait_seconds: float = Query(default=0, ge=0, le=25)):
        # Race a bounded DB-backed poll against disconnect; never cancel execution.
        import asyncio
        from contextlib import suppress
        from pydantic import ValidationError
        from ..task_contracts import ErrorCode
        from .repository import fail
        try:
            poll = PollRequest(after_revision=after_revision, wait_seconds=wait_seconds)
        except ValidationError:
            fail(ErrorCode.INVALID_INPUT, 'Invalid polling parameters')
        async def disconnected():
            while not await request.is_disconnected():
                await asyncio.sleep(.05)
        read = asyncio.create_task(bindings.control.get(account, task_id, poll))
        watch = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((read, watch), return_when=asyncio.FIRST_COMPLETED)
            if read in done:
                return await read
            return Response(status_code=204)
        finally:
            for job in (read, watch):
                if not job.done():
                    job.cancel()
            await asyncio.gather(read, watch, return_exceptions=True)

    @router.post('/{task_id}/cancel', status_code=202)
    async def cancel(task_id: Identifier, account: owner):
        return await bindings.control.cancel(account, task_id)

    @router.post('/{task_id}/interactions/{interaction_id}/respond', status_code=202)
    async def respond(task_id: Identifier, interaction_id: Identifier, body: InteractionResponse, account: owner):
        return await bindings.control.respond(account, task_id, interaction_id, body)

    @router.post('/{task_id}/assistance', status_code=201)
    async def assistance(task_id: Identifier, body: AssistanceRequest, account: owner):
        # Authorization and pause checks are repeated by the assistance authority.
        await bindings.control.get(account, task_id, PollRequest())
        return await bindings.assistance.mint(account, task_id, body)

    return router
