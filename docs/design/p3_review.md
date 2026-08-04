# P3 评审与验证报告

> **日期**: 2026-08-04  
> **范围**: 溯源分析系统 P3（知识沉淀与智能应用）代码评审 + 状态一致性加固  
> **基线**: P3（69 单测）→ 评审后 **80 单测**，修复 5 个真实状态一致性 bug

---

## 1. 评审结论：发现并修复 5 个真实 bug

P3 引入审核③④与 `reported→知识` 流程后，状态映射/Schema 在多处未同步更新，导致**归因/知识阶段 API 会出错**。

### 🔴 Bug 1：`set_stage` 状态映射过时（[task_repo.py](../../src/app/traceback/repositories/task_repo.py)）

P1 把 `reported` 映射为 `status="done"`。P3 后 `reported` 不再是终态（其后还有知识沉淀流程），但映射未更新；且 `review_attribution`/`knowledge_review`/`done`/`knowledge_sunk` 完全未映射。

**影响**：任务在审核③④期间 `status` 字段错误（如报告后 status="done" 但实际在等知识审核），进度/审核校验语义不一致。

**修复**：改为 `_STAGE_STATUS` 全阶段映射表（done→done，review_*→reviewing，其余→analyzing），显式 status 优先。

### 🔴 Bug 2：`TaskProgress` schema Literal 未含 P3 阶段（[task.py](../../src/app/traceback/schemas/task.py)）

`current_stage`/`review_stage` 的 Literal 仅含 P1/P2 阶段，缺 `review_attribution`/`knowledge_review`/`knowledge` 等。

**影响**：进度接口在归因/知识阶段返回 `current_stage="knowledge_review"` → **Pydantic 422 校验错误**（API 集成测试捕获）。

**修复**：扩展 `AnalysisStage` 与 `review_stage` Literal 含全部 P3 阶段。

### 🔴 Bug 3/4/5：审核/进度 stage 映射与枚举缺失

- `/review` 的 `expected_stage` 映射缺 `knowledge→knowledge_review`（[analysis.py](../../src/app/traceback/api/v1/analysis.py)）
- `/progress` 的 `review_stage` 映射缺 `knowledge_review→knowledge`
- `ReviewRequest.stage` 枚举缺 `knowledge`（[review.py](../../src/app/traceback/schemas/review.py)）

**影响**：知识审核④提交时校验失败 / 进度不显示 knowledge 审核态。

**修复**：三处补齐 `knowledge` 映射。

---

## 2. 验证手段

| 手段 | 覆盖 | 结果 |
|------|------|------|
| **API 集成测试**（新增 11 项） | knowledge 15 端点 happy/error + 状态映射回归 | ✅ 捕获 Bug 2 |
| 全量单测 | 80 项 | ✅ 全过 |
| ruff | lint | ✅ 全清 |
| UAT 脚本扩展 | 审核①②③④全链路 + 知识闭环校验 | ✅ 可运行 |

新增测试文件：[test_knowledge_api.py](../../src/app/traceback/tests/test_knowledge_api.py)

---

## 3. 真实环境验证（Docker 不可用，提供脚本与步骤）

本机无 Docker，真实栈 UAT 无法在此执行（与 P1 加固同）。已就绪：

```powershell
# 1. 起栈 (PG/Mongo/MinIO/Redis)
docker compose -f src/app/traceback/docker-compose.yml up -d
# 2. 配置 .env (DEEPSEEK_API_KEY)
# 3. 起 API (启动时自动 seed 知识库)
.venv\Scripts\python.exe -m src.app.traceback.main
# 4. 跑 UAT (审核①②③④ + 报告 + 知识闭环)
.venv\Scripts\python.exe -m src.app.traceback.scripts.run_uat
```

UAT 脚本现覆盖完整 4 审核链路 + 归因结果 + 知识闭环（相似案例可检索）。

### 进程内验证（已通过）

- 沉淀→检索闭环（`test_sink_then_retrieve_loop`）：draft 不检索、发布后命中
- 相似案例 Top3 > 60%、RAG 强制引用 + 无结果拒答
- 知识 API 15 端点（创建/查询/发布/相似检索/IOC 批量/处置推荐/问答）

---

## 4. 已知限制（移交 P4）

- **Neo4j/Milvus 真实集成**：当前用 PG 行内冗余（IOC 图谱）+ 内存 TTP 相似（向量），接口可替换；生产部署时切换
- **知识评价/点赞/统计/导入导出**（F-007-10~12）：推迟 P4
- **真实栈 UAT**：需 Docker 环境，脚本已就绪待执行
