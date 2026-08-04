"""API 依赖注入

对齐 01_phase1_design.md §6 / 04_phase4_design.md §2
- SessionDep: PG 异步会话
- verify_token / ReviewerDep: JWT 鉴权 (P4), 返回 user_id; 注入租户上下文
- current_user: CurrentUser (含 role/tenant), 供 require_permission
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.traceback.core.db import get_session
from src.app.traceback.core.security import CurrentUser, current_user_dep

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUserDep = Annotated[CurrentUser, Depends(current_user_dep)]


async def verify_token(authorization: Annotated[str | None, Header()] = None) -> str:
    """JWT 鉴权依赖, 返回 user_id (兼容既有 ReviewerDep 用法)。

    无 jwt_secret 且无 api_token 时 dev 跳过(P4 current_user_dep 处理)。
    """
    user = await current_user_dep(authorization)
    return user.user_id


ReviewerDep = Annotated[str, Depends(verify_token)]
