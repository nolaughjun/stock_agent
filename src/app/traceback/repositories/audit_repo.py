"""操作审计仓库 (SEC-004)

对齐 04_phase4_design.md §2.6 audit_logs 表 (追加写, 不可篡改)
记录所有写操作与敏感读操作; 投递 PG audit_logs (可转 Elasticsearch)。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import desc, select, table, column
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.traceback.core.logging import logger

_audit_tbl = table(
    "audit_logs",
    column("log_id"), column("tenant_id"), column("user_id"), column("action"),
    column("resource"), column("resource_id"), column("detail"),
    column("ip"), column("user_agent"), column("log_time"),
)


async def log_audit(
    session: AsyncSession, *,
    tenant_id: str, user_id: str, action: str, resource: str,
    resource_id: str | None = None, detail: dict[str, Any] | None = None,
    ip: str | None = None, user_agent: str | None = None,
) -> str:
    """追加审计日志 (不可篡改: 仅 insert)"""
    log_id = f"audit-{uuid.uuid4().hex[:12]}"
    try:
        await session.execute(
            _audit_tbl.insert().values(
                log_id=log_id, tenant_id=tenant_id, user_id=user_id, action=action,
                resource=resource, resource_id=resource_id, detail=detail or {},
                ip=ip, user_agent=user_agent,
            )
        )
        await session.commit()
    except Exception as e:  # noqa: BLE001
        # 审计失败不阻塞业务 (降级记录到日志)
        logger.warning("审计日志写入失败(降级日志): {}", str(e)[:120])
        logger.info("[AUDIT] tenant={} user={} action={} resource={}/{}",
                    tenant_id, user_id, action, resource, resource_id)
    return log_id


async def list_audit(
    session: AsyncSession, *, tenant_id: str, limit: int = 100
) -> list[dict[str, Any]]:
    rows = (await session.execute(
        select(_audit_tbl).where(_audit_tbl.c.tenant_id == tenant_id)
        .order_by(desc(_audit_tbl.c.log_time)).limit(limit)
    )).mappings().all()
    return [dict(r) for r in rows]
