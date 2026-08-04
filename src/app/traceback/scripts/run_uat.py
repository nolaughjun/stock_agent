"""UAT-001 端到端验收脚本

对齐 traceback_system_prd.md §8.4 UAT-001 / 01_phase1_design.md §12 M4。
前置: Docker Compose 栈已起 (PG/Mongo/MinIO/Redis) + API 服务已启动。
  docker compose -f src/app/traceback/docker-compose.yml up -d
  .venv/Scripts/python.exe -m src.app.traceback.main
用法:
  .venv/Scripts/python.exe -m src.app.traceback.scripts.run_uat [--base http://localhost:8000]

跑通: 创建勒索病毒任务 → 自动分析(证据收集/时间线/攻击链) → 审核①② approve → 生成报告,
断言报告包含关键章节与 TTP, 验证端到端闭环 < 30 分钟 (NFR-004)。
无真实 SIEM/EDR 时走 mock 证据(evidence_mock_mode), 仍可完整跑通。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx

PREFIX = "/api/v1/traceback"
REVIEW_POLL_TIMEOUT = 600  # 单阶段最长等待(秒)
POLL_INTERVAL = 3


async def wait_for_review(client: httpx.AsyncClient, task_id: str) -> str:
    """轮询进度直到进入审核等待, 返回 review_stage"""
    deadline = time.time() + REVIEW_POLL_TIMEOUT
    while time.time() < deadline:
        r = await client.get(f"{PREFIX}/tasks/{task_id}/progress")
        r.raise_for_status()
        p = r.json()
        print(f"  进度: {p['progress_percent']}% stage={p['current_stage']}", flush=True)
        if p["awaiting_review"]:
            return p["review_stage"]
        if p["status"] == "failed":
            raise RuntimeError(f"任务失败: {p.get('message')}")
        await asyncio.sleep(POLL_INTERVAL)
    raise TimeoutError("等待审核超时")


async def run_uat(base: str) -> None:
    started = time.time()
    async with httpx.AsyncClient(base_url=base, timeout=30) as client:
        # 1. 创建任务
        print("[1/6] 创建勒索病毒溯源任务...")
        r = await client.post(f"{PREFIX}/tasks", json={
            "task_name": "UAT-001 勒索病毒感染事件",
            "description": "财务部主机感染勒索病毒, 疑似钓鱼邮件入口",
            "asset_ips": ["10.0.0.5"], "asset_hosts": ["fin-pc-01"],
        })
        r.raise_for_status()
        task_id = r.json()["task_id"]
        print(f"      task_id={task_id}")

        # 2. 启动分析
        print("[2/6] 启动分析...")
        r = await client.post(f"{PREFIX}/tasks/{task_id}/analyze")
        r.raise_for_status()

        # 3. 审核① (时间线)
        print("[3/6] 等待时间线构建 → 审核①...")
        stage = await wait_for_review(client, task_id)
        assert stage == "timeline", f"期望 timeline 审核, 实际 {stage}"
        r = await client.post(f"{PREFIX}/tasks/{task_id}/review", json={
            "stage": "timeline", "action": "approve", "reviewer": "uat", "comments": "时间线准确",
        })
        r.raise_for_status()
        print("      审核① approve ✓")

        # 4. 审核② (攻击链)
        print("[4/8] 等待攻击链分析 → 审核②...")
        stage = await wait_for_review(client, task_id)
        assert stage == "attack_chain", f"期望 attack_chain 审核, 实际 {stage}"
        r = await client.post(f"{PREFIX}/tasks/{task_id}/review", json={
            "stage": "attack_chain", "action": "approve", "reviewer": "uat", "comments": "攻击链准确",
        })
        r.raise_for_status()
        print("      审核② approve ✓")

        # 5. 审核③ (归因)
        print("[5/8] 等待归因分析 → 审核③...")
        stage = await wait_for_review(client, task_id)
        assert stage == "attribution", f"期望 attribution 审核, 实际 {stage}"
        attr = (await client.get(f"{PREFIX}/tasks/{task_id}/attribution")).json()
        r = await client.post(f"{PREFIX}/tasks/{task_id}/review", json={
            "stage": "attribution", "action": "approve", "reviewer": "uat", "comments": "归因确认",
        })
        r.raise_for_status()
        print(f"      审核③ approve ✓ (归因: {attr.get('attribution', {}).get('threat_actor', '未归因')})")

        # 6. 等待报告生成
        print("[6/8] 等待报告生成...")
        deadline = time.time() + REVIEW_POLL_TIMEOUT
        while time.time() < deadline:
            r = await client.get(f"{PREFIX}/tasks/{task_id}/progress")
            p = r.json()
            if p["current_stage"] in ("reported", "knowledge_review"):
                break
            if p["status"] == "failed":
                raise RuntimeError("报告阶段失败")
            await asyncio.sleep(POLL_INTERVAL)

        # 7. 审核④ (知识沉淀)
        print("[7/8] 等待知识沉淀 → 审核④...")
        stage = await wait_for_review(client, task_id)
        assert stage == "knowledge", f"期望 knowledge 审核, 实际 {stage}"
        r = await client.post(f"{PREFIX}/tasks/{task_id}/review", json={
            "stage": "knowledge", "action": "approve", "reviewer": "uat", "comments": "知识沉淀确认",
        })
        r.raise_for_status()
        # 注: 沉淀的案例为 draft, 可经 /knowledge/cases/{id}/publish 发布参与检索
        print("      审核④ approve ✓")

        # 8. 断言报告完整度 + 知识闭环
        print("[8/8] 校验报告完整度与知识闭环...")
        tl = (await client.get(f"{PREFIX}/tasks/{task_id}/timeline")).json()
        ac = (await client.get(f"{PREFIX}/tasks/{task_id}/attack-chain")).json()
        rpt = (await client.get(f"{PREFIX}/tasks/{task_id}/report?fmt=md")).content.decode("utf-8")
        # 知识闭环: 相似案例可检索
        similar = (await client.post(f"{PREFIX}/knowledge/cases/search-similar", json={
            "attack_type": "ransomware", "ttps": ["T1486"], "top_k": 3,
        })).json()

        checks = {
            "时间线非空": len(tl["events"]) > 0,
            "含关键事件": tl["key_event_count"] > 0,
            "攻击链有入口": ac["chain"] and ac["chain"]["entry_point"] is not None,
            "归因有结果": attr.get("attribution") is not None,
            "报告含事件概述": "事件概述" in rpt,
            "报告含攻击链分析": "攻击链分析" in rpt,
            "报告含处置建议": "处置建议" in rpt,
            "报告含 ATT&CK 技术": "T1566" in rpt or "T1486" in rpt,
            "报告含路径图": "<svg" in rpt,
            "知识闭环-相似案例可检索": len(similar.get("cases", [])) > 0,
        }
        elapsed = time.time() - started
        print(f"\n耗时: {elapsed:.1f}s (NFR-004 要求 < 1800s)")
        all_ok = True
        for name, ok in checks.items():
            print(f"  [{'✓' if ok else '✗'}] {name}")
            all_ok = all_ok and ok

        if not all_ok:
            print("\n❌ UAT-001 失败: 报告完整度不达标")
            sys.exit(1)
        if elapsed > 1800:
            print("\n❌ UAT-001 失败: 超出 30 分钟 SLA")
            sys.exit(1)
        print("\n✅ UAT-001 通过: 端到端溯源闭环验证成功")


def main() -> None:
    parser = argparse.ArgumentParser(description="UAT-001 端到端验收")
    parser.add_argument("--base", default="http://localhost:8000", help="API base URL")
    args = parser.parse_args()
    asyncio.run(run_uat(args.base))


if __name__ == "__main__":
    main()
