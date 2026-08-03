# 报告智能体系统 - 详细设计

**版本**: v1.0  
**日期**: 2026-05-08

---

## 1. 状态定义详细设计

### 1.1 完整状态结构

```python
# state.py

from __future__ import annotations
from typing import Annotated, Literal, TypedDict
from datetime import datetime
from langgraph.graph.message import add_messages


def append_item(existing: list | None, new: list | None) -> list:
    """列表追加 reducer"""
    if existing is None:
        existing = []
    if new is None:
        return existing
    return existing + (new if isinstance(new, list) else [new])


class ResearchSource(TypedDict):
    """数据来源"""
    source_type: Literal["web", "database", "document", "api"]
    query: str
    content: str
    url: str
    retrieved_at: str  # ISO8601


class ReportDraft(TypedDict):
    """报告草案版本"""
    version: str              # 如 "1.0.1"
    content: str              # Markdown 内容
    sections: list[str]       # 已完成章节
    created_at: str           # ISO8601
    change_note: str          # 修改说明


class QualityCheckResult(TypedDict):
    """质量检测结果"""
    ispass: bool
    score: float              # 0.0 ~ 1.0
    issue: str
    suggestions: list[str]


class ReportState(TypedDict):
    """报告生成状态"""
    
    # === 任务输入（只读）===
    topic: str
    abstract: str
    report_type: Literal["research", "analysis", "summary", "proposal"]
    language: str
    create_at: str
    quality_threshold: float  # 可配置，默认 0.55
    
    # === 流程控制 ===
    current_step: Literal[
        "init", "planning", "researching", "drafting",
        "reviewing", "revising", "finished", "failed"
    ]
    retry_count: int
    max_retry: int
    error_msg: str | None
    
    # === 规划产物 ===
    outline: list[str]
    search_queries: list[str]
    
    # === 数据收集 ===
    research_sources: Annotated[list[ResearchSource], append_item]
    
    # === 对话历史 ===
    messages: Annotated[list, add_messages]
    
    # === 报告版本 ===
    draft_versions: Annotated[list[ReportDraft], append_item]
    current_draft: str | None
    
    # === 质量控制 ===
    quality_checks: Annotated[list[QualityCheckResult], append_item]
    revision_instructions: list[str]
    
    # === 人工审核 ===
    human_decision: Literal["approve", "revise"] | None
    human_comments: str | None
    
    # === 最终输出 ===
    final_report: str | None
    export_path: str | None
```

### 1.2 状态初始化

```python
def create_initial_state(
    topic: str,
    abstract: str = "",
    report_type: str = "research",
    language: str = "中文",
    quality_threshold: float = 0.55,
    max_retry: int = 3,
) -> ReportState:
    """创建初始状态"""
    return {
        "topic": topic,
        "abstract": abstract,
        "report_type": report_type,
        "language": language,
        "create_at": datetime.now(UTC).isoformat(),
        "quality_threshold": quality_threshold,
        "current_step": "init",
        "retry_count": 0,
        "max_retry": max_retry,
        "error_msg": None,
        "outline": [],
        "search_queries": [],
        "research_sources": [],
        "messages": [],
        "draft_versions": [],
        "current_draft": None,
        "quality_checks": [],
        "revision_instructions": [],
        "human_decision": None,
        "human_comments": None,
        "final_report": None,
        "export_path": None,
    }
```

---

## 2. 节点详细设计

### 2.1 规划节点 (plan_tasks)

**功能**: 根据主题生成报告大纲和检索词

**输入**:
- topic: 报告主题
- abstract: 报告概要
- report_type: 报告类型
- language: 语言

**输出**:
- outline: 章节大纲列表
- search_queries: 检索词列表
- current_step: "researching"

**Prompt 设计**:

```python
PLAN_SYSTEM = """你是一位专业的研究报告规划专家。
请根据用户提供的报告主题、概要和类型，输出严格的 JSON 格式。
不要输出任何其他内容。"""

PLAN_USER = """请为以下报告生成详细的规划方案：

主题：{topic}
概要：{abstract}
报告类型：{report_type}
语言：{language}

请输出如下 JSON 格式：
{{
  "outline": ["章节1", "章节2", "章节3", "章节4", "章节5"],
  "search_queries": ["检索词1", "检索词2", "检索词3", "检索词4", "检索词5"]
}}

要求：
- outline：5~7个章节，覆盖背景、现状、分析、结论等维度
- search_queries：5~8个精准检索词，用于后续网络数据收集
- 语言与报告语言保持一致
"""
```

**错误处理**:
- JSON 解析失败 → 重试
- LLM 调用失败 → 标记 failed

---

### 2.2 数据收集节点 (gather_data)

**功能**: 执行网络搜索并结构化提取信息

**处理流程**:
1. 遍历 search_queries
2. 对每个检索词调用 Tavily 搜索
3. 调用 LLM 提取结构化信息
4. 聚合所有结果进行质量评分
5. 根据评分决定下一步

**单条收集 Prompt**:

```python
EXTRACT_SYSTEM = """你是一位严谨的数据分析师，擅长从搜索结果中提取结构化信息。
请严格按照用户要求的 JSON 格式输出，不要包含任何其他内容。"""

EXTRACT_USER = """请从以下搜索结果中提取与报告主题相关的关键信息：

报告主题：{topic}
检索词：{query}

搜索结果：
{search_content}

请输出如下 JSON：
{{
  "key_findings": ["核心发现1", "核心发现2", "核心发现3"],
  "data_points": ["具体数据/事实1", "具体数据/事实2"],
  "summary": "本次搜索的综合摘要（200字以内）",
  "relevance_score": 0.85,
  "completeness_score": 0.75,
  "credibility_score": 0.80,
  "issues": "存在的问题描述（无问题则为空字符串）"
}}
"""
```

**聚合评分 Prompt**:

```python
AGGREGATE_USER = """请将以下多条数据收集结果整合：

报告主题：{topic}
报告大纲：{outline}

各检索词的收集结果：
{all_results}

请输出如下 JSON：
{{
  "integrated_summary": "整合后的综合摘要（500字以内）",
  "key_facts": ["关键事实1", "关键事实2", "关键事实3"],
  "data_gaps": ["缺失信息1", "缺失信息2"],
  "overall_quality_score": 0.80,
  "quality_passed": true,
  "quality_issues": "质量问题描述（无则为空字符串）",
  "suggestions": ["改进建议1", "改进建议2"]
}}
"""
```

**路由逻辑**:
- quality_passed → drafting
- !quality_passed && retry < max → planning (重新规划)
- !quality_passed && retry >= max → drafting (降级处理)

---

### 2.3 草案撰写节点 (generate_draft)

**功能**: 根据大纲和数据撰写报告草案

**Prompt 设计**:

```python
DRAFT_SYSTEM = """你是一位专业的报告撰写专家。
请根据提供的大纲和资料，撰写结构清晰、内容充实的专业报告。"""

DRAFT_USER = """请根据以下信息撰写报告草案：

主题：{topic}
报告类型：{report_type}
语言：{language}

报告大纲：
{outline}

参考资料：
{research_data}

要求：
1. 使用 Markdown 格式
2. 每个章节需要有明确的标题和内容
3. 内容要有数据支撑，引用资料中的关键发现
4. 语言流畅，逻辑清晰
5. 总字数控制在 3000-5000 字

请直接输出报告内容，不要包含其他说明。
"""
```

**版本管理**:
- 每次生成新草案，版本号递增
- 记录 change_note

---

### 2.4 质量审核节点 (quality_review)

**功能**: 自动检测报告质量

**Prompt 设计**:

```python
REVIEW_SYSTEM = """你是一位严格的报告质检员。
请对报告草案进行多维度质量检测，输出 JSON 格式结果。"""

REVIEW_USER = """请对以下报告草案进行质量检测：

报告主题：{topic}
报告大纲：{outline}

报告内容：
{draft}

请从以下维度进行检测：
1. 结构完整性：是否覆盖大纲所有章节
2. 内容一致性：是否存在自相矛盾
3. 数据支撑：是否有足够的数据和事实支撑
4. 语言质量：语言是否流畅、专业

请输出如下 JSON：
{{
  "ispass": true,
  "score": 0.85,
  "issue": "存在的问题描述",
  "suggestions": ["改进建议1", "改进建议2"],
  "dimensions": {{
    "structure": 0.90,
    "consistency": 0.85,
    "data_support": 0.80,
    "language": 0.85
  }}
}}
"""
```

---

### 2.5 人工审核节点 (human_review)

**功能**: 等待人工审核决策

**实现方式**:
- 使用 `interrupt()` 暂停执行
- 通过 `update_state()` 恢复

```python
from langgraph.types import interrupt

def human_review_node(state: ReportState) -> dict:
    """人工审核节点"""
    decision = interrupt({
        "task_id": state.get("task_id"),
        "topic": state["topic"],
        "draft": state["current_draft"],
        "quality_score": state["quality_checks"][-1]["score"] if state["quality_checks"] else 0,
        "prompt": "请审阅报告草案，返回决策：approve 或 revise",
    })
    
    return {
        "human_decision": decision.get("decision", "approve"),
        "human_comments": decision.get("comments", ""),
    }
```

---

### 2.6 修改节点 (revise)

**功能**: 根据审核意见修改报告

**Prompt 设计**:

```python
REVISE_SYSTEM = """你是一位专业的报告修改专家。
请根据审核意见对报告进行针对性修改。"""

REVISE_USER = """请根据以下审核意见修改报告：

原报告：
{original_draft}

审核意见：
{review_comments}

人工反馈：
{human_comments}

要求：
1. 针对性地解决审核意见中指出的问题
2. 保持报告原有的结构和优点
3. 不要大幅删减有效内容
4. 直接输出修改后的报告，不要包含其他说明
"""
```

---

### 2.7 最终输出节点 (finalize)

**功能**: 生成最终报告并导出

**处理流程**:
1. 创建输出目录
2. 生成 Markdown 文件
3. 转换为 PDF
4. 更新 export_path

```python
def finalize_node(state: ReportState) -> dict:
    """最终输出节点"""
    output_dir = Path("outputs")
    output_dir.mkdir(exist_ok=True)
    
    task_id = state.get("task_id", "unknown")
    draft = state["current_draft"]
    
    # 生成 Markdown
    md_content = f"# {state['topic']}\n\n{draft}"
    md_path = output_dir / f"{task_id}.md"
    md_path.write_text(md_content, encoding="utf-8")
    
    # 转换 PDF
    pdf_path = output_dir / f"{task_id}.pdf"
    try:
        pypandoc.convert_file(
            str(md_path),
            "pdf",
            outputfile=str(pdf_path),
            extra_args=[
                "--pdf-engine=xelatex",
                "-V", "mainfont=Microsoft YaHei",
                "-V", "geometry:margin=1in",
            ],
        )
    except Exception as e:
        logger.warning(f"PDF 转换失败: {e}")
    
    return {
        "final_report": draft,
        "export_path": str(pdf_path),
        "current_step": "finished",
    }
```

---

### 2.8 错误处理节点 (error_handler)

**功能**: 处理流程中的错误

```python
def error_handler_node(state: ReportState) -> dict:
    """错误处理节点"""
    logger.error(f"[error_handler] 错误: {state['error_msg']}")
    
    retry_count = state["retry_count"]
    max_retry = state["max_retry"]
    
    if retry_count >= max_retry:
        return {
            "current_step": "failed",
            "error_msg": f"达到最大重试次数 ({max_retry})，任务失败",
        }
    
    # 重试：回到 planning
    return {
        "current_step": "planning",
        "retry_count": retry_count + 1,
    }
```

---

## 3. 图构建详细设计

### 3.1 完整图定义

```python
# graph.py

from typing import Literal
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import MemorySaver

from state import ReportState
from nodes import (
    plan_tasks,
    gather_data,
    generate_draft,
    quality_review,
    human_review,
    revise_draft,
    finalize,
    error_handler,
)


def route_after_planning(state: ReportState) -> Literal["gather_data", "error_handler"]:
    if state.get("error_msg"):
        return "error_handler"
    return "gather_data"


def route_after_gather(state: ReportState) -> Literal["generate_draft", "planning", "error_handler"]:
    if state.get("error_msg"):
        return "error_handler"
    if state["current_step"] == "planning":
        return "planning"  # 回退重新规划
    return "generate_draft"


def route_after_review(state: ReportState) -> Literal["human_review", "revise", "error_handler"]:
    if state.get("error_msg"):
        return "error_handler"
    
    quality = state["quality_checks"][-1] if state["quality_checks"] else None
    threshold = state.get("quality_threshold", 0.55)
    
    if quality and quality["score"] >= threshold:
        return "human_review"
    return "revise"


def route_after_human(state: ReportState) -> Literal["finalize", "revise", END]:
    decision = state.get("human_decision", "approve")
    retry = state.get("retry_count", 0)
    max_retry = state.get("max_retry", 3)
    
    if decision == "approve":
        return "finalize"
    if retry >= max_retry:
        return END  # 达到重试上限，终止
    return "revise"


def route_after_revise(state: ReportState) -> Literal["quality_review", "human_review"]:
    # 修改后直接进入人工审核（跳过自动审核）
    return "human_review"


def route_error(state: ReportState) -> Literal["planning", END]:
    if state["retry_count"] >= state["max_retry"]:
        return END
    return "planning"


def build_graph():
    """构建报告生成图"""
    builder = StateGraph(ReportState)
    
    # 添加节点
    builder.add_node("planning", plan_tasks)
    builder.add_node("gather_data", gather_data)
    builder.add_node("generate_draft", generate_draft)
    builder.add_node("quality_review", quality_review)
    builder.add_node("human_review", human_review)
    builder.add_node("revise", revise_draft)
    builder.add_node("finalize", finalize)
    builder.add_node("error_handler", error_handler)
    
    # 定义边
    builder.add_edge(START, "planning")
    
    builder.add_conditional_edges("planning", route_after_planning, {
        "gather_data": "gather_data",
        "error_handler": "error_handler",
    })
    
    builder.add_conditional_edges("gather_data", route_after_gather, {
        "generate_draft": "generate_draft",
        "planning": "planning",
        "error_handler": "error_handler",
    })
    
    builder.add_edge("generate_draft", "quality_review")
    
    builder.add_conditional_edges("quality_review", route_after_review, {
        "human_review": "human_review",
        "revise": "revise",
        "error_handler": "error_handler",
    })
    
    builder.add_conditional_edges("human_review", route_after_human, {
        "finalize": "finalize",
        "revise": "revise",
        END: END,
    })
    
    builder.add_conditional_edges("revise", route_after_revise, {
        "quality_review": "quality_review",
        "human_review": "human_review",
    })
    
    builder.add_edge("finalize", END)
    
    builder.add_conditional_edges("error_handler", route_error, {
        "planning": "planning",
        END: END,
    })
    
    # Checkpoint
    checkpointer = MemorySaver()
    
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_review"],  # 人工审核前暂停
    )


app = build_graph()
```

---

## 4. 错误处理策略

### 4.1 错误分类

| 错误类型 | 描述 | 处理策略 |
|---------|------|---------|
| LLMError | LLM 调用失败 | 重试 3 次 |
| SearchError | 搜索失败 | 使用模拟数据继续 |
| ParseError | JSON 解析失败 | 重试或降级处理 |
| ExportError | 导出失败 | 记录日志，仅输出 MD |

### 4.2 重试策略

```python
RETRY_CONFIG = {
    "max_retry": 3,
    "retry_delay": 1.0,  # 秒
    "exponential_backoff": True,
}
```

### 4.3 熔断策略

```python
CIRCUIT_BREAKER = {
    "failure_threshold": 5,    # 连续失败次数
    "reset_timeout": 300,      # 熔断重置时间（秒）
}
```

---

## 5. Prompt 模板汇总

### 5.1 规划阶段

| Prompt 名称 | 用途 | 模型 |
|------------|------|------|
| PLAN_SYSTEM | 系统提示 | deepseek-chat |
| PLAN_USER | 用户提示 | deepseek-chat |

### 5.2 收集阶段

| Prompt 名称 | 用途 | 模型 |
|------------|------|------|
| EXTRACT_SYSTEM | 提取系统提示 | deepseek-chat |
| EXTRACT_USER | 提取用户提示 | deepseek-chat |
| AGGREGATE_SYSTEM | 聚合系统提示 | deepseek-chat |
| AGGREGATE_USER | 聚合用户提示 | deepseek-chat |

### 5.3 撰写阶段

| Prompt 名称 | 用途 | 模型 |
|------------|------|------|
| DRAFT_SYSTEM | 撰写系统提示 | deepseek-chat |
| DRAFT_USER | 撰写用户提示 | deepseek-chat |

### 5.4 审核阶段

| Prompt 名称 | 用途 | 模型 |
|------------|------|------|
| REVIEW_SYSTEM | 审核系统提示 | deepseek-reasoner |
| REVIEW_USER | 审核用户提示 | deepseek-reasoner |

### 5.5 修改阶段

| Prompt 名称 | 用途 | 模型 |
|------------|------|------|
| REVISE_SYSTEM | 修改系统提示 | deepseek-chat |
| REVISE_USER | 修改用户提示 | deepseek-chat |

---

## 6. 配置管理

### 6.1 配置项

```python
# config.py

from dataclasses import dataclass


@dataclass
class ReportConfig:
    """报告生成配置"""
    
    # LLM 配置
    llm_model: str = "deepseek-chat"
    reason_model: str = "deepseek-reasoner"
    max_tokens: int = 4096
    temperature: float = 0.1
    
    # 质量配置
    quality_threshold: float = 0.55
    
    # 重试配置
    max_retry: int = 3
    
    # 搜索配置
    search_max_results: int = 5
    search_timeout: int = 20
    
    # 输出配置
    output_dir: str = "outputs"
    
    # 路径配置
    safe_base_dir: str = "./outputs"
```

### 6.2 环境变量

```bash
# .env
DEEPSEEK_API_KEY=sk-xxx
TAVILY_API_KEY=tvly-xxx
LANGSMITH_API_KEY=lsv__xxx
LANGSMITH_TRACING=true
LANGSMITH_PROJECT=report-agent
```

---

## 7. 安全设计详情

### 7.1 输入验证

```python
def validate_topic(topic: str) -> str:
    """验证报告主题"""
    if not topic or not topic.strip():
        raise ValueError("报告主题不能为空")
    if len(topic) > 500:
        raise ValueError("报告主题长度不能超过500字符")
    # 过滤危险字符
    return topic.strip()[:500]


def validate_path(path: str, base_dir: str) -> Path:
    """验证路径安全性"""
    full_path = Path(base_dir) / path
    if not str(full_path.resolve()).startswith(str(Path(base_dir).resolve())):
        raise PermissionError(f"非法路径访问: {path}")
    return full_path
```

### 7.2 敏感信息处理

```python
def sanitize_for_logging(text: str) -> str:
    """日志脱敏"""
    # 隐藏 API Key
    import re
    text = re.sub(r'sk-[a-zA-Z0-9]+', 'sk-***', text)
    text = re.sub(r'tvly-[a-zA-Z0-9]+', 'tvly-***', text)
    return text
```
