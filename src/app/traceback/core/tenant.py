"""租户上下文 (多租户行级隔离, SEC-005)

对齐 04_phase4_design.md §2.1
请求级 contextvar, 由 TenantMiddleware 从 JWT 注入; 仓库层查询时强制带 tenant_id。
"""
from __future__ import annotations

from contextvars import ContextVar

# 当前请求的租户ID (None=未鉴权/系统调用)
_tenant_id: ContextVar[str | None] = ContextVar("tenant_id", default=None)
# 当前请求的用户ID
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)


def get_tenant_id() -> str | None:
    return _tenant_id.get()


def get_user_id() -> str | None:
    return _user_id.get()


def set_context(*, tenant_id: str | None, user_id: str | None) -> None:
    _tenant_id.set(tenant_id)
    _user_id.set(user_id)


def clear_context() -> None:
    _tenant_id.set(None)
    _user_id.set(None)
