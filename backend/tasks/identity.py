"""Only existing server-resolved accounts authorize tasks; headers are not tenants."""

from fastapi import Depends, HTTPException

from ..auth import get_current_user, rate_limit
from ..task_contracts import ErrorCode, Principal, TaskFault, task_error


async def principal(user: dict = Depends(get_current_user)) -> Principal:
    from ..task_settings import TaskSettings
    try:
        rate_limit('tasks:' + user['id'], TaskSettings.from_env().api_requests_per_minute)
    except HTTPException:
        raise TaskFault(task_error(ErrorCode.CAPACITY_EXHAUSTED, 'Account request limit exceeded', 60)) from None
    return Principal(account_id=user['id'])
