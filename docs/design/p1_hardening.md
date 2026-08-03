# P1 加固报告

> **日期**: 2026-07-27  
> **范围**: 溯源分析系统 P1 MVP 加固（lint / 质量评测 / API 测试 / 真实适配器测试 / UAT）  
> **基线**: P1 MVP（S1~S4，33 单测）→ 加固后 **52 单测**，ruff 全清

---

## 1. 加固成果总览

| 项 | 加固前 | 加固后 |
|----|--------|--------|
| 单测数 | 33 | **52**（+19：TTP 评测 5、API 11、SIEM 真实路径 3） |
| TTP 准确率（§11 质量门） | ❌ 未验证 | ✅ precision/recall/F1 > 0.8（3 场景黄金样本） |
| API 层测试 | ❌ 零覆盖 | ✅ 11 项（CRUD/进度/审核/报告下载/错误码） |
| 真实 SIEM 路径 | ❌ 仅测 mock | ✅ OpenSearch DSL 查询/解析/降级/健康检查 |
| lint | ❌ 25 错误 | ✅ ruff 全清（+ ruff.toml 配置） |
| 端到端 UAT | ❌ 无 | ✅ [run_uat.py](../../src/app/traceback/scripts/run_uat.py) 脚本 + 文档 |

---

## 2. TTP 标注质量门（§11）

**问题**：设计 §11 要求 TTP 标注准确率 > 80%，但此前未验证。

**方案**：构建黄金样本评测集 [data/golden_ttp_samples.json](../../src/app/traceback/data/golden_ttp_samples.json)（3 场景：勒索钓鱼、凭据转储+横向、噪声+未知事件），评测 harness 跑端到端（TimelineBuilder 规则提取 → AttackChainAgent ATT&CK 白名单标注），计算 precision/recall/F1。

**结果**（[test_ttp_eval.py](../../src/app/traceback/tests/test_ttp_eval.py)）：
- 每场景 precision ≥ 0.8、recall ≥ 0.8
- 整体 F1 > 0.8
- **防幻觉**：未知事件不产出未命中白名单的 technique（白名单约束生效）

---

## 3. API 集成测试

**问题**：API 层（18 路由）零测试。

**方案**：`httpx.AsyncClient + ASGITransport`（async 原生、不触发 lifespan、无需 greenlet），`dependency_overrides` 注入空会话 + monkeypatch 仓库。

**覆盖**（[test_api.py](../../src/app/traceback/tests/test_api.py)，11 项）：
- 任务 CRUD（创建/校验422/404/列表/取消）
- 进度查询（awaiting_review/review_stage/percent）
- 审核（错误阶段 400 TB-1002、正确触发 resume）
- 报告下载（404 TB-1005、PDF 200、非法格式 400 TB-1004）

---

## 4. 真实 SIEM 适配器路径

**问题**：SIEM 适配器此前只测了 mock 模式，真实 OpenSearch DSL 路径未验证。

**方案**：注入 mock httpx AsyncClient，验证 DSL 构造（白名单字段）、查询执行、hits 解析为证据、错误降级、健康检查（[test_siem_adapter.py](../../src/app/traceback/tests/test_siem_adapter.py)，3 项）。

---

## 5. 代码质量

- ruff 扫描修复 25 错误（F401 未用导入、F841 未用变量、E402 导入位置）
- 新增 [ruff.toml](../../ruff.toml)：main.py 的 sys.path 注入后导入豁免 E402（src/ layout 既有模式）
- llm.py / report_service.py 清理冗余变量

---

## 6. 端到端 UAT

**脚本**：[scripts/run_uat.py](../../src/app/traceback/scripts/run_uat.py)

跑通完整闭环（创建任务 → 自动分析 → 审核①② approve → 报告生成），断言报告完整度（时间线/关键事件/入口/概述/攻击链/处置建议/ATT&CK 技术/路径图）+ 30 分钟 SLA（NFR-004）。

### 运行步骤

```powershell
# 1. 起基础设施栈
docker compose -f src/app/traceback/docker-compose.yml up -d

# 2. 配置 .env (DEEPSEEK_API_KEY; 可选 checkpoint_backend=postgres)

# 3. 起 API 服务
.venv\Scripts\python.exe -m src.app.traceback.main

# 4. 跑 UAT (另一终端)
.venv\Scripts\python.exe -m src.app.traceback.scripts.run_uat
```

无真实 SIEM/EDR 时走 mock 证据（`evidence_mock_mode=True`），仍可完整跑通验证闭环。配置真实数据源（`SIEM_BASE_URL`/`MISP_URL`）后即可对接实际环境。

---

## 7. 已知限制（移交 P2）

- **真实栈 UAT 需 Docker**：本机无 Docker，UAT 脚本已就绪待真实环境执行；进程内 mini UAT（`test_mini_uat_pipeline`）已验证管道逻辑。
- **LLM 路径未在 CI 评测**：单测 conftest 禁用 LLM 走规则兜底（确定性）；真实 LLM 的 TTP 准确率需配 key 后用 golden samples 独立评测。
- **PDF 转换需 GTK**：weasyprint 在 Windows 缺原生库，优雅降级仅存 MD；生产 Linux 容器可用。
- **Celery/PG checkpointer 待启用**：脚手架/opt-in 就绪，多 worker 部署时启用。
