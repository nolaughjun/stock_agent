# 溯源分析系统 — 四阶段全程实施总结

> **日期**: 2026-08-04  
> **状态**: P1~P4 全部完成 · 从 PRD 到可实施代码的全链路转化收官  
> **代码**: [src/app/traceback/](../../src/app/traceback/) · **设计**: [docs/design/](./)  
> **指标**: 84 个 Python 模块 · 38 个 API 路由 · 93 项单测全通过 · ruff 全清

---

## 1. 总览

本项目从一个 2979 行的产品需求文档（[traceback_system_prd.md](../traceback_system_prd.md)）出发，经四阶段设计评审（G0~G4）与四阶段迭代实施，交付了一套基于 AI 多智能体协同的安全事件溯源分析平台。

```
PRD (2979行)
  └─ 设计评审 G0: 系统设计图 + 四阶段路线图 (00_overview_and_roadmap.md)
       └─ G1~G4: 四阶段技术设计 (01~04) 逐阶段评审通过
            └─ 实施 P1→P4: 每阶段代码 + 单测 + 评审/加固
                 └─ 交付: 端到端闭环 + 企业级生产化
```

**端到端闭环**：任务 → 证据收集 → 时间线 → 攻击链 → 归因 → 报告 → 知识沉淀，含人工审核①②③④ interrupt 中断-恢复。

### 核心数据

| 维度 | 数量 |
|------|------|
| Python 模块 | 84 |
| 专业智能体 | 6（+ 编排） |
| 数据源/取证适配器 | 11（基础 5 + 深度 5 + OpenCTI） |
| API 路由 | 38 |
| 单元测试 | 93（11 文件） |
| 设计文档 | 7（路线图 + 4 阶段设计 + 2 评审报告 + 本总结） |
| Git 提交 | 10（按 Sprint/阶段） |

---

## 2. 四阶段交付索引

### P1 — MVP 核心溯源流水线 + 加固

| 项 | 内容 |
|----|------|
| 设计 | [01_phase1_design.md](01_phase1_design.md) |
| 提交 | M1 基础架构 → S2 证据收集 → S3 时间线+攻击链 → S4 报告+MVP → 加固 |
| 评审/加固 | [p1_hardening.md](p1_hardening.md) |
| 交付 | LangGraph 编排(6智能体+审核①② interrupt)、三段式存储、5 基础适配器、报告(MD/PDF/SVG)、TTP 黄金样本评测(F1>0.8)、API 集成测试、UAT 脚本 |
| 单测 | 52（骨架 10 + 证据 8 + 时间线/攻击链 10 + 报告 5 + TTP 评测 5 + API 11 + SIEM 3） |

**里程碑修复**：中断后有效阶段从 `snapshot.next` 推导（否则进度误判/审核 400）。

### P2 — 威胁归因与深度取证

| 项 | 内容 |
|----|------|
| 设计 | [02_phase2_design.md](02_phase2_design.md) |
| 提交 | S5 归因智能体+OpenCTI → S6 深度取证 |
| 交付 | AttributionAgent(三因子加权 TTP0.5/IOC0.3/工具0.2)、OpenCTI(GraphQL+种子降级)、相似案例匹配、5 深度取证适配器(Zeek/Velociraptor/YARA/CAPE/Volatility，mock降级)、归因审核③ |
| 单测 | +10（归因算法/特征提取/相似案例/OpenCTI种子/黄金样本Top1>70%/深度适配器/UAT-002） → 62 |

**里程碑修复**：`_extract_features` 仅用白名单校验过的 technique_id（防脏 TTP 污染归因）。

### P3 — 知识沉淀与智能应用 + 评审

| 项 | 内容 |
|----|------|
| 设计 | [03_phase3_design.md](03_phase3_design.md) |
| 提交 | S8 知识库建设 → S9 检索问答 → S10 智能应用 → 评审修复 |
| 评审 | [p3_review.md](p3_review.md) |
| 交付 | 五大知识库(案例/IOC/TTP/Playbook/文章)、KnowledgeSink 自动提取、RAG 问答(强制引用+无结果拒答)、知识审核④、知识 API 15端点、旁路 IOC 匹配 |
| 单测 | +7(知识) +11(API/状态映射) → 80 |

**评审修复 5 个真实 bug**：审核③④接入后 `set_stage`/`TaskProgress`/review 映射未同步（API 在归因/知识阶段会 422）。

### P4 — 企业级生产化与运营

| 项 | 内容 |
|----|------|
| 设计 | [04_phase4_design.md](04_phase4_design.md) |
| 提交 | S11 安全多租户 → S12 可观测 → S13 部署 |
| 交付 | 多租户行级隔离(tenant_id)、JWT(access/refresh+jti防重放)、RBAC(6角色)、bcrypt、审计(audit_logs)、轻量Prometheus指标(/metrics)、Dockerfile/K8s/CI 部署制品 |
| 单测 | +13(安全/RBAC/租户/指标) → 93 |

---

## 3. 系统架构

### 3.1 智能体编排（LangGraph 状态机 + 4 审核 interrupt）

```
START → collect_evidence → build_timeline → [审核①] → analyze_chain → [审核②]
      → attribute → [审核③] → report → knowledge_sink → [审核④] → END
```

6 专业智能体（[agents/](../../src/app/traceback/agents/)）+ 编排（[orchestration/](../../src/app/traceback/orchestration/)）：

| 智能体 | 阶段 | 核心能力 |
|--------|------|---------|
| EvidenceCollector | 证据收集 | LLM查询计划 → 并行适配器 → 去重评估 → 三段式存储 |
| TimelineBuilder | 时间线 | LLM/规则事件提取 → 排序 → 关键节点标记 → 关联 |
| AttackChainAgent | 攻击链 | 入口反向溯源 → 横向移动 → TTP白名单标注 → 路径图 |
| AttributionAgent | 归因 | 特征提取 → OpenCTI三因子加权 → LLM画像 |
| Reporter | 报告 | LLM叙事 + 模板渲染 + SVG路径图 + MD/PDF转换 |
| KnowledgeSink | 知识沉淀 | 自动提取案例/IOC/TTP/Playbook 草稿 |

**审核中断-恢复**：4 个 `interrupt` 节点（时间线/攻击链/归因/知识），`Command(resume=decision)` 恢复，有效阶段从 `snapshot.next` 推导。

### 3.2 三段式存储（对齐 §5）

| 存储 | 内容 |
|------|------|
| PostgreSQL | 任务/证据引用/攻击链(JSONB)/归因/审核/checkpoint/TTP/Playbook/IOC/审计 |
| MongoDB | 证据元数据/时间线事件/知识案例/知识文章 |
| MinIO | 证据原始文件/报告(PDF/MD) |
| Redis | Celery 队列/缓存/jti 防重放（P4 多 worker 时） |

### 3.3 API 分层（38 路由，[api/v1/](../../src/app/traceback/api/v1/)）

| 模块 | 路由 | 说明 |
|------|------|------|
| auth | 3 | login/refresh/me（JWT） |
| tasks | 5 | 任务 CRUD |
| analysis | 4 | analyze/progress/review/resume |
| results | 5 | timeline/attack-chain/attribution/evidences/report |
| knowledge | 15 | cases/iocs/playbooks/articles/qa |
| 系统 | 2 | health/metrics |

### 3.4 安全（P4，[core/security.py](../../src/app/traceback/core/security.py)）

- **JWT**：access 30min/refresh 7d，携带 tenant_id/role/jti；jti 防重放（内存 set，Redis-ready）
- **RBAC**：6 角色 × `resource:action` 权限粒度，`require_permission` 依赖
- **多租户**：`TenantContext` contextvar + 行级 tenant_id 隔离
- **审计**：audit_logs 追加写不可篡改

---

## 4. 关键技术决策（贯穿全程）

| 决策 | 理由 |
|------|------|
| **LLM + 规则双路径** | 有 `DEEPSEEK_API_KEY` 走 LLM，无则规则兜底——无 key 环境可端到端跑通，单测确定性（conftest 禁 LLM） |
| **mock/种子降级贯穿** | OpenCTI/Milvus/Neo4j/深度取证无真实实例时返回种子数据——P1~P3 全程可在本机确定性测试 |
| **三因子算法定归因，LLM 仅画像** | 防 LLM 幻觉（TR-001 对策） |
| **ATT&CK 白名单约束 TTP** | 未命中标 unknown 降置信，TTP 准确率 F1>0.8 |
| **RAG 强制引用 + 无结果拒答** | 防幻觉（P3 风险对策） |
| **IOC 图谱用 PG 行内冗余 / 向量用内存相似** | 避免 Neo4j/Milvus 重型依赖，接口可替换（P4 生产再换） |
| **既有路由零改动升级 JWT** | `verify_token` 委托 `current_user_dep`，自动获得鉴权+租户注入 |
| **轻量 Prometheus 无依赖** | 手写 Counter/Gauge/Histogram，生产可换 prometheus_client |
| **PG checkpointer opt-in / Celery 脚手架懒加载** | MemorySaver 单进程一致已测，多 worker 待 PG checkpointer 启用 |

---

## 5. 代码地图

```
src/app/traceback/
├── api/v1/              # auth, tasks, analysis, results, knowledge (38 路由)
├── agents/              # 6 专业智能体 + base (LLM+规则双路径)
├── orchestration/       # graph(状态机) / nodes / runner / state / checkpointer
├── adapters/            # base+manager+registry + SIEM/EDR/DNS/MISP/Timesketch/OpenCTI + 5深度
├── repositories/        # task/evidence/timeline/attack_chain/attribution/knowledge/audit
├── core/                # config/logging/llm/db/tenant/security/metrics/celery_app
├── services/            # report_service/path_graph/mitre/similar_cases/deep_forensics/qa
├── schemas/             # task/evidence/timeline/attack_chain/attribution/review/knowledge
├── data/                # init.sql + 种子JSON(威胁组织/案例/知识/MITRE白名单/黄金样本)
├── templates/           # report_detail.md.j2
├── tests/               # 11 文件 93 测试
├── scripts/run_uat.py   # 端到端 UAT(审核①②③④+知识闭环)
├── deploy/              # Dockerfile + k8s.yaml
└── docker-compose.yml   # PG/Mongo/MinIO/Redis
```

---

## 6. 质量保障矩阵

### 6.1 测试构成（93 项）

| 文件 | 数 | 覆盖 |
|------|----|------|
| test_skeleton | 10 | 编排 interrupt/resume 端到端、适配器降级、阶段进度 |
| test_evidence_collection | 8 | mock 数据、并行收集、去重、SIEM DSL 白名单/防注入 |
| test_timeline_attack_chain | 10 | ATT&CK 白名单、规则提取、入口/横向/TTP/路径图 |
| test_report | 5 | SVG 路径图、Reporter 规则叙事、UAT mini 管道 |
| test_ttp_eval | 5 | TTP 黄金样本 F1>0.8、防幻觉 |
| test_api | 11 | 任务 CRUD/进度/审核/报告下载 API 集成 |
| test_siem_adapter | 3 | OpenSearch DSL 真实路径(mock httpx) |
| test_attribution | 10 | 三因子算法、相似案例、OpenCTI 种子、归因 Top1>70%、UAT-002 |
| test_knowledge | 7 | 案例提取、相似 Top3>60%、处置推荐、IOC 匹配、RAG 引用/拒答、闭环 |
| test_knowledge_api | 11 | 知识 15 端点 + 状态映射回归 |
| test_security | 13 | JWT/jti防重放/RBAC/密码/租户/指标 |

### 6.2 质量门（设计 §11，全部达标）

| 门 | 指标 | 达标 |
|----|------|------|
| TTP 标注准确率 | F1 > 0.8 | ✅ 黄金样本 3 场景 |
| 归因 Top1 准确率 | > 70% | ✅ 黄金样本 4 场景 |
| 相似案例 Top3 命中 | > 60% | ✅ 3 场景 |
| 案例提取完整度 | 关键字段齐 | ✅ 攻击类型/TTP/IOC/标题 |
| 代码质量 | lint 全清 | ✅ ruff + ruff.toml |

---

## 7. 已知技术债与后续路线

| 项 | 现状 | 后续 |
|----|------|------|
| 真实栈 UAT | 脚本就绪，本机无 Docker | 部署环境跑 [run_uat.py](../../src/app/traceback/scripts/run_uat.py)（审核①②③④+知识闭环） |
| 真实 LLM 评测 | 单测 conftest 禁 LLM 走规则 | 配 key 后用黄金样本独立评测 |
| Neo4j/Milvus | PG 行内冗余 + 内存相似 | 生产部署换真实图谱/向量库（接口可替换） |
| Celery 多 worker | asyncio 默认 + 脚手架懒加载 | 启用 PG checkpointer + Celery |
| 性能压测/故障演练 | 未做 | Locust 10 并发/100万日志、Chaos 工程 |
| 攻击模拟 (CALDERA) | 推迟 | 后续版本 |

---

## 8. 如何运行

### 8.1 单测（无需外部服务）

```powershell
.venv\Scripts\python.exe -m pytest src\app\traceback\tests\ -q
```

### 8.2 本地开发栈

```powershell
docker compose -f src\app\traceback\docker-compose.yml up -d   # PG/Mongo/MinIO/Redis
.venv\Scripts\python.exe -m src.app.traceback.main              # API :8000
# 访问 http://localhost:8000/docs
```

### 8.3 生产部署（P4）

```bash
# 镜像
docker build -f src/app/traceback/deploy/Dockerfile -t traceback:latest .
# K8s
kubectl apply -f src/app/traceback/deploy/k8s.yaml
# CI: .github/workflows/traceback-ci.yml (lint/单测/安全扫描/License/镜像)
```

---

## 9. 评审门回顾

| 门 | 内容 | 状态 |
|----|------|------|
| G0 | 系统设计图 + 四阶段路线图 | ✅ |
| G1 | P1 MVP（评审 6 问 + 加固） | ✅ |
| G2 | P2 归因+深度取证（评审 6 问） | ✅ |
| G3 | P3 知识闭环（评审 6 问 + 状态一致性修复） | ✅ |
| G4 | P4 生产化（评审 8 问） | ✅ |

**全链路转化完成**：PRD → 设计（G0~G4）→ 实施（P1~P4）→ 企业级生产化。

---

*文档结束。各阶段详见对应设计文档与本总结引用的代码文件。*
