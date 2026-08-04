"""认证 API (P4, SEC-001)

对齐 04_phase4_design.md §2.3
- /auth/login: 本地账户(bcrypt)/LDAP 校验 → 颁发 access+refresh
- /auth/refresh: refresh 换 access
- /auth/me: 当前用户信息
本地账户为兜底; LDAP/OAuth2 为外部集成(配置后启用)。
"""
from __future__ import annotations

from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.app.traceback.api.deps import CurrentUserDep
from src.app.traceback.core.config import settings
from src.app.traceback.core.security import (
    create_token_pair,
    decode_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["认证"])


class LoginRequest(BaseModel):
    username: str
    password: str
    tenant_id: str = "default"


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


# ---- 本地账户存储 (dev 兜底; 生产换 LDAP/DB) ----
# {tenant_id: {username: {password_hash, role}}}
_LOCAL_USERS: dict[str, dict[str, dict]] = {}


def register_local_user(*, tenant_id: str, username: str, password: str, role: str) -> None:
    """注册本地账户 (dev/测试用; 生产由管理员接口或 LDAP)"""
    _LOCAL_USERS.setdefault(tenant_id, {})[username] = {
        "password_hash": hash_password(password), "role": role,
    }


def _init_dev_users() -> None:
    """dev 预置账户 (各角色)"""
    if not _LOCAL_USERS:
        for role in ("soc_engineer", "analyst", "responder", "researcher", "manager", "admin"):
            register_local_user(
                tenant_id="default", username=role, password=f"{role}123", role=role,
            )


async def _authenticate_local(tenant_id: str, username: str, password: str) -> Optional[dict]:
    _init_dev_users()
    user = _LOCAL_USERS.get(tenant_id, {}).get(username)
    if user and verify_password(password, user["password_hash"]):
        return {"username": username, "role": user["role"]}
    return None


async def _authenticate_ldap(username: str, password: str) -> Optional[dict]:
    """LDAP 校验 (配置 ldap_url 时启用)。成功返回 {username, role}。"""
    if not settings.ldap_url:
        return None
    try:
        from ldap3 import (  # type: ignore
            ALL,
            Connection,
            Server,
        )

        server = Server(settings.ldap_url, get_info=ALL)
        conn = Connection(server, user=username, password=password, auto_bind=True)
        conn.unbind()
        # LDAP 不返回角色 → 默认 analyst, 由管理员后续分配
        return {"username": username, "role": "analyst"}
    except Exception:  # noqa: BLE001
        return None


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest):
    """登录 → 颁发 JWT (本地/LDAP)"""
    user = await _authenticate_local(payload.tenant_id, payload.username, payload.password)
    if user is None:
        user = await _authenticate_ldap(payload.username, payload.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    tokens = create_token_pair(
        user_id=user["username"], tenant_id=payload.tenant_id, role=user["role"]
    )
    return TokenResponse(**tokens)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest):
    """refresh token 换新 access+refresh"""
    try:
        claims = decode_token(payload.refresh_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="refresh token 已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="refresh token 无效")
    if claims.get("type") != "refresh":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="需 refresh token")
    tokens = create_token_pair(
        user_id=claims["sub"], tenant_id=claims["tenant_id"], role=claims["role"]
    )
    return TokenResponse(**tokens)


@router.get("/me")
async def me(user: CurrentUserDep):
    """当前用户信息 (tenant/role)"""
    return {
        "user_id": user.user_id, "tenant_id": user.tenant_id, "role": user.role,
        "is_admin": user.is_admin,
    }
