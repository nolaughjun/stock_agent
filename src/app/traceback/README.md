# 溯源分析系统 — P1 实现

> 对齐 [01_phase1_design.md](../../../docs/design/01_phase1_design.md) · Sprint 1 (M1 基础架构) 已完成

## 当前进度

| Sprint | 里程碑 | 状态 | 说明 |
|--------|--------|------|------|
| S1 | M1 基础架构 | ✅ 已完成 | 目录骨架、core、schemas、智能体/适配器基座、LangGraph 编排(含 interrupt 审核)、API、Docker Compose、单测 9/9 通过 |
| S2 | M2 任务管理+证据收集 | ✅ 已完成 | 证据三段式存储、真实 SIEM/MISP 适配器(+mock 降级)、EvidenceCollector 智能体、task_runner 服务、证据查询 API、单测 18/18 |
| S3 | M3 时间线+攻击链 | ✅ 已完成 | ATT&CK 白名单、TimelineBuilder(LLM+规则兜底)、AttackChainAgent(入口反向溯源/横向/TTP/路径图)、timeline/attack-chain API、单测 28/28 |
| S4 | M4 报告+MVP | ✅ 已完成 | Reporter(叙事+模板+SVG路径图)、MD/PDF/Word 转换+MinIO 存储、报告下载 API、PG checkpointer(opt-in)、UAT 迷你管道、单测 33/33 |
| P2 加固 | 质量门/测试 | ✅ 已完成 | TTP 黄金样本评测(F1>0.8)、API 集成测试(11)、SIEM 真实路径、ruff 全清、UAT 脚本、单测 52/52 |
| P2-S5 | 归因智能体 | ✅ 已完成 | AttributionAgent(三因子加权+LLM画像)、OpenCTI(GraphQL+种子降级)、相似案例匹配、归因审核③、/attribution API |
| P2-S6 | 深度取证 | ✅ 已完成 | Zeek/Velociraptor/YARA/CAPE/Volatility 适配器(mock降级)+DeepForensicsCollector 异步调度 |

## P1 MVP 完成总结

溯源分析系统 P1 端到端最小闭环已打通: **任务 → 证据收集 → 时间线 → 攻击链 → (归因桩) → 报告**, 含人工审核①②中断-恢复。

- **33 项单测全通过**, 确定性运行(测试 conftest 禁用 LLM 走规则兜底, 不触网)
- **6 智能体**: EvidenceCollector / TimelineBuilder / AttackChainAgent / Attribution(桩) / Reporter + 编排
- **LLM + 规则双路径**: 有 DEEPSEEK_API_KEY 走 LLM, 无则规则兜底, 无 key 环境可端到端跑通
- **三段式存储**: MinIO(原始/报告) / Mongo(元数据/时间线) / PG(任务/引用/攻击链/审核)
- **生产化预留**: PG checkpointer(opt-in)、Celery 脚手架(懒加载)、适配器降级

## 已交付(M1)

- **目录结构** [src/app/traceback/](.) — 严格对齐设计 §2.3
- **基础设施** [docker-compose.yml](docker-compose.yml) + [data/init.sql](data/init.sql) — PG/Mongo/MinIO/Redis
- **core** [core/](core/) — config(pydantic_settings)、logging(loguru)、llm(DeepSeek+结构化输出+重试)、db(PG/Mongo/MinIO/Redis 懒加载)
- **schemas** [schemas/](schemas/) — task/evidence/timeline/attack_chain/review 全量 Pydantic 模型
- **适配器基座** [adapters/](adapters/) — BaseAdapter(降级)、ComponentManager、5 个 P1 stub 适配器、注册工厂
- **LangGraph 编排** [orchestration/](orchestration/) — 状态机、interrupt 审核中断-恢复、运行器
- **API** [api/v1/](api/v1/) — 任务管理/分析控制/结果查询 + Token 鉴权依赖
- **报告模板** [templates/](templates/) + [services/report_service.py](services/report_service.py)
- **单测** [tests/test_skeleton.py](tests/test_skeleton.py) — 9 项全通过(含 interrupt/resume 端到端)

## 快速开始

### 1. 跑单测(无需外部服务)

```powershell
.venv\Scripts\python.exe -m pytest src/app/traceback/tests/test_skeleton.py -v
```

### 2. 启动基础设施

```powershell
docker compose -f src/app/traceback/docker-compose.yml up -d
```

服务端口(避免本机冲突):
- PostgreSQL `:55432` (user/pass/db = traceback/traceback_dev/traceback)
- MongoDB `:57017`
- MinIO `:59000`(API) / `:59001`(控制台)
- Redis `:56379`

### 3. 启动 API

在 `.env` 配置 `DEEPSEEK_API_KEY` 后:

```powershell
.venv\Scripts\python.exe -m src.app.traceback.main
# 访问 http://localhost:8000/docs
```

### 4. 典型流程

```bash
# 创建任务
curl -X POST http://localhost:8000/api/v1/traceback/tasks \
  -H "Content-Type: application/json" \
  -d '{"task_name":"勒索病毒事件","asset_ips":["10.0.0.5"]}'

# 启动分析 → 停在审核①
curl -X POST http://localhost:8000/api/v1/traceback/tasks/{id}/analyze

# 查进度
curl http://localhost:8000/api/v1/traceback/tasks/{id}/progress

# 提交审核① approve → 自动恢复至审核②
curl -X POST http://localhost:8000/api/v1/traceback/tasks/{id}/review \
  -H "Content-Type: application/json" \
  -d '{"stage":"timeline","action":"approve","reviewer":"alice"}'
```

## 架构要点

- **编排核心**: LangGraph 状态机 + `interrupt`/`Command(resume=...)` 实现人工审核①②中断-恢复(设计 §3.4)。单测 `test_graph_interrupt_and_resume` 验证 approve 全链路、`test_graph_revise_loops_back` 验证 revise 回退。
- **三段式存储**: 原始数据 MinIO、元数据 Mongo、轻量引用 PG(设计 §5.1)。
- **降级策略**: 适配器 `safe_call` + manager 健康检查, 单源失败不阻塞整体(设计 §7.3)。
- **占位实现**: 归因智能体为 P1 桩(置信度 0, P2 替换); 5 个适配器为 stub(S2 对接真实接口); 业务节点为 stub(S2~S4 填充)。

## 已知技术决策

1. **包名 `traceback` 与 stdlib 冲突**: 通过 pytest `--import-mode=importlib` + 根 [conftest.py](../../../conftest.py) 解决(见 [pytest.ini](../../../pytest.ini))。运行时用绝对导入 `src.app.traceback` 不受影响。
2. **日志用 loguru**(非设计文档的 structlog): 沿用项目既有约定(investment_research), 保持一致性。
3. **M1 检查点用 MemorySaver**: 进程内; PG checkpointer(设计 §5.1 `langgraph_checkpoints`)留 S4 增强, 当前业务状态已同步 PG tasks 表保证进度可查询。
4. **M1 分析驱动用 asyncio 后台任务**: 设计 §2.1 的 Celery 化留 S2(需 Redis broker 已就绪)。

## 下一步(S2)

1. 实现 `EvidenceCollector` 智能体: LLM 查询计划生成 → 并行适配器 → 证据评估打分。
2. 对接 1 套真实 SIEM(OpenSearch DSL) + MISP(pymisp), 其余继续 mock(TR-002 对策)。
3. 引入 Celery + Redis 替换 asyncio 后台任务。
4. 证据三段式存储落地(MinIO/Mongo/PG)。
