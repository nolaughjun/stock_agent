# 报告智能体系统

基于 LangGraph 和 LLM 的自动化报告生成系统。

## 项目结构

```
src/app/day24/
├── state.py              # 状态定义
├── graph.py              # 状态图构建
├── main.py               # 主入口
├── tools/                # 工具层
│   ├── __init__.py
│   ├── llm.py            # LLM 调用
│   └── export.py         # 报告导出
├── nodes/                # 节点层
│   ├── __init__.py
│   ├── plan.py           # 规划节点
│   ├── gather_data.py    # 数据收集
│   ├── draft.py          # 草案撰写
│   ├── review.py         # 质量审核
│   ├── revise.py         # 修改节点
│   ├── human_review.py   # 人工审核
│   ├── finalize.py       # 最终输出
│   └── error_handler.py  # 错误处理
└── tests/                # 测试
    ├── conftest.py
    ├── test_nodes.py
    └── test_security.py

docs/
├── PRD.md                # 产品需求文档
├── system_design.md      # 系统概要设计
├── detailed_design.md    # 详细设计
├── test_cases.md         # 测试用例
└── security_scan_report.md  # 安全扫描报告
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

创建 `.env` 文件：

```env
DEEPSEEK_API_KEY=sk-your-api-key
TAVILY_API_KEY=tvly-your-api-key
LANGSMITH_API_KEY=your-langsmith-key  # 可选
```

### 3. 运行系统

```bash
# 交互模式
python src/app/day24/main.py

# 批量模式
python src/app/day24/main.py --topic "AI发展趋势报告"
```

## 功能特性

- ✅ 自动生成报告大纲
- ✅ 网络数据自动收集
- ✅ 智能草案撰写
- ✅ 自动质量审核
- ✅ 人工审核支持 (Human-in-the-loop)
- ✅ Markdown + PDF 导出
- ✅ 可配置质量阈值
- ✅ 完整的错误处理和重试机制

## 报告类型

| 类型 | 说明 |
|-----|------|
| research | 研究报告 |
| analysis | 分析报告 |
| summary | 总结报告 |
| proposal | 提案报告 |

## 流程说明

```
START → planning → researching → drafting → reviewing
                                              ↓
                               ┌──────────────┴──────────────┐
                               ↓                             ↓
                            revising                     finished → END
                               ↓
                          (回 human_review)
```

## 测试

```bash
# 运行所有测试
pytest src/app/day24/tests/ -v

# 运行安全测试
pytest src/app/day24/tests/test_security.py -v

# 覆盖率报告
pytest src/app/day24/tests/ --cov=src/app/day24 --cov-report=html
```

## 安全说明

本项目已通过红蓝对抗安全扫描，主要安全措施包括：

- API Key 环境变量存储
- 输入验证和过滤
- 路径穿越防护
- 日志脱敏
- 速率限制

详见 [security_scan_report.md](docs/security_scan_report.md)

## 文档

- [PRD 产品需求文档](docs/PRD.md)
- [系统概要设计](docs/system_design.md)
- [详细设计](docs/detailed_design.md)
- [测试用例](docs/test_cases.md)
- [安全扫描报告](docs/security_scan_report.md)

## 技术栈

- Python 3.14
- LangGraph (状态图框架)
- DeepSeek API (LLM)
- Tavily API (搜索)
- pypandoc (PDF 导出)

## License

MIT
