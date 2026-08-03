"""API 依赖注入

对齐 01_phase1_design.md §6 / §10 安全(基础 Token)
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.traceback.core.config import settings
from src.app.traceback.core.db import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def verify_token(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """P1 基础 Token 鉴权 (P4 上 RBAC/LDAP)。

    api_token 为空时跳过(仅 dev), 否则校验 Bearer token。
    """
    if not settings.api_token:
        return "anonymous"
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer Token"
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效"
        )
    return "token-user"


ReviewerDep = Annotated[str, Depends(verify_token)]
