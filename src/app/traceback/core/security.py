"""安全: JWT 认证 + RBAC + 密码哈希 (SEC-001/002/006)

对齐 04_phase4_design.md §2.2~§2.4
- JWT access(30min)/refresh(7d), 携带 tenant_id/role/jti
- jti 防重放: 已用 jti 缓存(Redis-ready, 默认内存 set + TTL)
- RBAC: 6 预置角色, 权限粒度 resource:action, require_permission 依赖
- 密码: bcrypt (本地账户兜底); LDAP/OAuth2 为外部集成(P4 脚手架)
"""
from __future__ import annotations

import time
import uuid
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status

from src.app.traceback.core.config import settings
from src.app.traceback.core.tenant import set_context

# ==================== RBAC (§2.2) ====================

# 6 预置角色 → 权限集合 (对齐 PRD §2.1 用户画像)
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "soc_engineer": {  # 安全运营工程师
        "task:create", "task:read", "task:analyze", "report:export",
    },
    "analyst": {  # 安全分析师 = 运营 + 审核知识编辑
        "task:create", "task:read", "task:analyze", "report:export",
        "timeline:review", "attack_chain:review", "knowledge:edit",
    },
    "responder": {  # 应急响应专家 = 分析师 + 分配协作
        "task:create", "task:read", "task:analyze", "report:export",
        "timeline:review", "attack_chain:review", "attribution:review", "knowledge:edit",
        "task:assign",
    },
    "researcher": {  # 安全研究员 = 分析师 + 归因/IOC/案例
        "task:create", "task:read", "task:analyze", "report:export",
        "timeline:review", "attack_chain:review", "attribution:review",
        "knowledge:edit", "ioc:manage", "case:manage",
    },
    "manager": {  # 安全管理者 = 全租户只读 + 报告统计
        "task:read", "report:export", "knowledge:read", "stats:read",
    },
    "admin": {  # 系统管理员 = 全部 (含租户/用户/角色管理)
        "*",  # 通配, has_permission 特判
    },
}

ALL_ROLES = set(ROLE_PERMISSIONS)


def has_permission(role: str | None, permission: str) -> bool:
    if not role:
        return False
    perms = ROLE_PERMISSIONS.get(role)
    if perms is None:
        return False
    return "*" in perms or permission in perms


# ==================== 密码哈希 (bcrypt) ====================

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


# ==================== JWT (§2.3/§2.4) ====================

ACCESS_TTL = 30 * 60        # 30 分钟
REFRESH_TTL = 7 * 24 * 3600  # 7 天
REPLAY_WINDOW = 5 * 60      # jti 防重放窗口


def create_token_pair(*, user_id: str, tenant_id: str, role: str) -> dict[str, str]:
    """颁发 access + refresh token"""
    access_jti = str(uuid.uuid4())
    now = int(time.time())
    access = _encode({
        "sub": user_id, "tenant_id": tenant_id, "role": role,
        "type": "access", "jti": access_jti, "iat": now, "exp": now + ACCESS_TTL,
    })
    refresh = _encode({
        "sub": user_id, "tenant_id": tenant_id, "role": role,
        "type": "refresh", "jti": str(uuid.uuid4()), "iat": now, "exp": now + REFRESH_TTL,
    })
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer"}


def decode_token(token: str) -> dict[str, Any]:
    """解码并校验 JWT (签名+过期); 不校验 jti 重放(由调用方)"""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


# ---- jti 防重放 (内存 set + 过期清理; Redis-ready) ----

_used_jti: dict[str, float] = {}  # jti -> expire_at


def _is_replayed(jti: str, now: float) -> bool:
    # 惰性清理过期项
    expired = [k for k, exp in _used_jti.items() if exp < now]
    for k in expired:
        _used_jti.pop(k, None)
    if jti in _used_jti:
        return True
    _used_jti[jti] = now + REPLAY_WINDOW
    return False


def _reset_replay_store() -> None:
    """测试用: 清空 jti 存储"""
    _used_jti.clear()


def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


# ==================== FastAPI 依赖 ====================

class CurrentUser:
    """从 JWT 解析的当前用户"""

    def __init__(self, claims: dict[str, Any]):
        self.user_id = claims.get("sub", "")
        self.tenant_id = claims.get("tenant_id", "")
        self.role = claims.get("role", "")
        self.jti = claims.get("jti", "")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def current_user_dep(
    authorization: str | None = None,
) -> CurrentUser:
    """FastAPI 依赖: 解析 JWT → CurrentUser, 注入租户上下文, 校验 jti 重放。

    api_token 为空时跳过鉴权(仅 dev); 否则强制 Bearer JWT。
    """
    if not settings.api_token and not settings.jwt_secret:
        # dev 无鉴权: 默认租户+分析师角色
        set_context(tenant_id="default", user_id="anonymous")
        return CurrentUser({"sub": "anonymous", "tenant_id": "default", "role": "analyst"})

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer Token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        claims = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token 已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token 无效")

    if claims.get("type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="需 access token")

    # jti 防重放
    if _is_replayed(claims.get("jti", ""), time.time()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token 已使用(防重放)")

    user = CurrentUser(claims)
    set_context(tenant_id=user.tenant_id, user_id=user.user_id)
    return user


def require_permission(permission: str):
    """RBAC 权限校验依赖工厂: require_permission("task:create")"""

    async def _dep(user: CurrentUser = Depends(current_user_dep)) -> CurrentUser:
        if not has_permission(user.role, permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={"code": "TB-3001", "message": f"无权限: {permission} (角色 {user.role})"},
            )
        return user

    return _dep
