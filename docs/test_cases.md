# 报告智能体系统 - 测试用例

**版本**: v1.0  
**日期**: 2026-05-08

---

## 1. 测试策略

### 1.1 测试范围

| 测试类型 | 覆盖范围 | 工具 |
|---------|---------|------|
| 单元测试 | 各节点函数、工具函数 | pytest |
| 集成测试 | 完整流程、状态流转 | pytest |
| 边界测试 | 异常输入、资源限制 | pytest |
| 安全测试 | 注入攻击、路径穿越 | 手动 + 自动 |

### 1.2 测试环境

```bash
# 安装测试依赖
pip install pytest pytest-cov pytest-asyncio

# 运行测试
pytest src/app/day24/tests/ -v

# 覆盖率报告
pytest src/app/day24/tests/ --cov=src/app/day24 --cov-report=html
```

---

## 2. 单元测试用例

### 2.1 状态定义测试

```python
# tests/test_state.py

import pytest
from state import (
    ReportState,
    ResearchSource,
    ReportDraft,
    QualityCheckResult,
    append_item,
    create_initial_state,
)


class TestAppendItem:
    """测试 append_item reducer"""
    
    def test_append_to_empty_list(self):
        """测试追加到空列表"""
        result = append_item(None, [1, 2, 3])
        assert result == [1, 2, 3]
    
    def test_append_to_existing_list(self):
        """测试追加到已有列表"""
        result = append_item([1, 2], [3, 4])
        assert result == [1, 2, 3, 4]
    
    def test_append_single_item(self):
        """测试追加单个元素"""
        result = append_item([1, 2], 3)
        assert result == [1, 2, 3]
    
    def test_append_none(self):
        """测试追加 None"""
        result = append_item([1, 2], None)
        assert result == [1, 2]


class TestCreateInitialState:
    """测试状态初始化"""
    
    def test_create_with_required_fields(self):
        """测试必填字段初始化"""
        state = create_initial_state(topic="测试主题")
        assert state["topic"] == "测试主题"
        assert state["current_step"] == "init"
        assert state["retry_count"] == 0
    
    def test_create_with_all_fields(self):
        """测试完整字段初始化"""
        state = create_initial_state(
            topic="测试主题",
            abstract="测试概要",
            report_type="research",
            language="中文",
            quality_threshold=0.6,
            max_retry=5,
        )
        assert state["abstract"] == "测试概要"
        assert state["report_type"] == "research"
        assert state["quality_threshold"] == 0.6
        assert state["max_retry"] == 5
    
    def test_create_timestamp(self):
        """测试时间戳生成"""
        state = create_initial_state(topic="测试")
        assert "create_at" in state
        assert state["create_at"].endswith("Z") or "+" in state["create_at"]
```

### 2.2 规划节点测试

```python
# tests/test_plan_node.py

import pytest
from unittest.mock import patch, MagicMock
from nodes.plan import plan_tasks
from state import ReportState, create_initial_state


class TestPlanTasks:
    """测试规划节点"""
    
    @patch("nodes.plan.chat")
    def test_plan_success(self, mock_chat):
        """测试规划成功"""
        mock_chat.return_value = '''{
            "outline": ["背景", "现状", "分析", "结论"],
            "search_queries": ["关键词1", "关键词2"]
        }'''
        
        state = create_initial_state(topic="AI发展趋势")
        result = plan_tasks(state)
        
        assert result["current_step"] == "researching"
        assert len(result["outline"]) == 4
        assert len(result["search_queries"]) == 2
        assert result["error_msg"] is None
    
    @patch("nodes.plan.chat")
    def test_plan_json_parse_error(self, mock_chat):
        """测试 JSON 解析失败"""
        mock_chat.return_value = "invalid json"
        
        state = create_initial_state(topic="测试主题")
        result = plan_tasks(state)
        
        assert result["current_step"] == "failed"
        assert result["error_msg"] is not None
    
    @patch("nodes.plan.chat")
    def test_plan_llm_error(self, mock_chat):
        """测试 LLM 调用失败"""
        mock_chat.side_effect = Exception("API Error")
        
        state = create_initial_state(topic="测试主题")
        result = plan_tasks(state)
        
        assert result["current_step"] == "failed"
        assert "API Error" in result["error_msg"]
```

### 2.3 数据收集节点测试

```python
# tests/test_gather_data.py

import pytest
from unittest.mock import patch, MagicMock
from nodes.gather_data import gather_data, _collect_single
from state import ReportState, create_initial_state


class TestGatherData:
    """测试数据收集节点"""
    
    @patch("nodes.gather_data.tavily_search")
    @patch("nodes.gather_data.chat")
    def test_collect_single_success(self, mock_chat, mock_search):
        """测试单条收集成功"""
        mock_search.return_value = {
            "answer": "测试答案",
            "results": [{"title": "测试", "url": "http://test.com", "content": "内容"}]
        }
        mock_chat.return_value = '''{
            "key_findings": ["发现1"],
            "data_points": ["数据1"],
            "summary": "摘要",
            "relevance_score": 0.8,
            "completeness_score": 0.7,
            "credibility_score": 0.9,
            "issues": ""
        }'''
        
        source, extracted = _collect_single("AI发展", "AI趋势")
        
        assert source["source_type"] == "web"
        assert source["query"] == "AI趋势"
        assert extracted["relevance_score"] == 0.8
    
    @patch("nodes.gather_data.tavily_search")
    def test_collect_single_search_failure(self, mock_search):
        """测试搜索失败"""
        mock_search.side_effect = Exception("Network Error")
        
        state = create_initial_state(topic="测试")
        state["search_queries"] = ["测试关键词"]
        
        result = gather_data(state)
        
        # 应该继续处理，不中断
        assert "research_sources" in result
```

### 2.4 工具函数测试

```python
# tests/test_tools.py

import pytest
from unittest.mock import patch, MagicMock
from tools.llm import chat, tavily_search, now_iso
from tools.export import export_markdown, export_pdf


class TestLLMTool:
    """测试 LLM 工具"""
    
    @patch("tools.llm.deepseek")
    def test_chat_success(self, mock_deepseek):
        """测试聊天成功"""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="回复内容"))]
        mock_deepseek.chat.completions.create.return_value = mock_response
        
        result = chat([{"role": "user", "content": "测试"}])
        
        assert result == "回复内容"
    
    @patch("tools.llm.deepseek")
    def test_chat_json_mode(self, mock_deepseek):
        """测试 JSON 模式"""
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"key": "value"}'))]
        mock_deepseek.chat.completions.create.return_value = mock_response
        
        result = chat([{"role": "user", "content": "测试"}], json_mode=True)
        
        assert '{"key": "value"}' in result


class TestSearchTool:
    """测试搜索工具"""
    
    @patch("tools.llm.requests.post")
    def test_tavily_search_success(self, mock_post):
        """测试 Tavily 搜索成功"""
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "answer": "AI答案",
            "results": [{"title": "标题", "url": "url", "content": "内容"}]
        }
        
        result = tavily_search("测试查询")
        
        assert result["answer"] == "AI答案"
        assert len(result["results"]) == 1
    
    @patch("tools.llm.requests.post")
    def test_tavily_search_failure(self, mock_post):
        """测试搜索失败"""
        mock_post.side_effect = Exception("Network Error")
        
        result = tavily_search("测试查询")
        
        assert "搜索失败" in result["answer"]


class TestExportTool:
    """测试导出工具"""
    
    def test_export_markdown(self, tmp_path):
        """测试 Markdown 导出"""
        from tools.export import export_markdown
        
        output_path = export_markdown(
            content="# 测试报告\n\n内容",
            output_dir=str(tmp_path),
            filename="test_report"
        )
        
        assert Path(output_path).exists()
        assert "测试报告" in Path(output_path).read_text(encoding="utf-8")
```

---

## 3. 集成测试用例

### 3.1 完整流程测试

```python
# tests/test_graph.py

import pytest
from unittest.mock import patch, MagicMock
from graph import build_graph, app
from state import create_initial_state


class TestFullFlow:
    """测试完整流程"""
    
    @patch("tools.llm.tavily_search")
    @patch("tools.llm.deepseek")
    def test_complete_flow_success(self, mock_deepseek, mock_search):
        """测试完整成功流程"""
        # Mock LLM 响应
        mock_llm_response = MagicMock()
        mock_llm_response.choices = [MagicMock(message=MagicMock(content='{"outline": ["背景", "分析"], "search_queries": ["AI"]}'))]
        mock_deepseek.chat.completions.create.return_value = mock_llm_response
        
        # Mock 搜索响应
        mock_search.return_value = {
            "answer": "搜索结果",
            "results": [{"title": "标题", "url": "url", "content": "内容"}]
        }
        
        state = create_initial_state(topic="AI发展趋势报告")
        result = app.invoke(state)
        
        assert result["current_step"] == "finished" or "final_report" in result
    
    def test_flow_with_human_approval(self):
        """测试人工审核通过流程"""
        # 创建图
        graph = build_graph()
        
        # 初始状态
        init_state = create_initial_state(topic="测试报告")
        
        # 执行到 human_review 暂停
        # ... 测试代码
```

### 3.2 状态恢复测试

```python
# tests/test_checkpoint.py

import pytest
from graph import build_graph
from state import create_initial_state


class TestCheckpoint:
    """测试检查点恢复"""
    
    def test_state_persistence(self):
        """测试状态持久化"""
        graph = build_graph()
        
        thread_id = "test-thread-001"
        config = {"configurable": {"thread_id": thread_id}}
        
        # 执行部分流程
        init_state = create_initial_state(topic="测试主题")
        graph.invoke(init_state, config=config)
        
        # 获取状态
        saved_state = graph.get_state(config)
        
        assert saved_state is not None
        assert saved_state.values.get("topic") == "测试主题"
    
    def test_state_recovery(self):
        """测试状态恢复"""
        graph = build_graph()
        
        thread_id = "test-thread-002"
        config = {"configurable": {"thread_id": thread_id}}
        
        # 第一次执行（到中断点）
        init_state = create_initial_state(topic="测试主题")
        graph.invoke(init_state, config=config)
        
        # 恢复执行
        graph.update_state(
            config,
            {"human_decision": "approve"},
            as_node="human_review"
        )
        result = graph.invoke(None, config=config)
        
        assert result is not None
```

---

## 4. 边界测试用例

### 4.1 输入边界测试

```python
# tests/test_boundary.py

import pytest
from state import create_initial_state, validate_topic


class TestInputBoundary:
    """测试输入边界"""
    
    def test_empty_topic(self):
        """测试空主题"""
        with pytest.raises(ValueError, match="不能为空"):
            validate_topic("")
    
    def test_long_topic(self):
        """测试超长主题"""
        long_topic = "A" * 1000
        result = validate_topic(long_topic)
        assert len(result) == 500  # 应被截断
    
    def test_special_characters(self):
        """测试特殊字符"""
        topic = "测试<script>alert('xss')</script>"
        # 应该过滤或转义
        result = validate_topic(topic)
        assert "<script>" not in result
    
    def test_unicode_input(self):
        """测试 Unicode 输入"""
        topic = "测试报告 🔥🎉"
        result = validate_topic(topic)
        assert "🔥" in result  # 应保留 emoji


class TestResourceBoundary:
    """测试资源边界"""
    
    def test_max_retry(self):
        """测试最大重试次数"""
        state = create_initial_state(topic="测试", max_retry=2)
        state["retry_count"] = 2
        
        # 超过最大重试应该终止
        # ... 测试逻辑
    
    def test_large_content(self):
        """测试大内容处理"""
        large_content = "A" * 100000  # 100KB
        
        # 应该能处理或截断
        # ... 测试逻辑
    
    def test_many_search_queries(self):
        """测试大量检索词"""
        queries = [f"关键词{i}" for i in range(100)]
        
        # 应该限制或分批处理
        # ... 测试逻辑
```

### 4.2 并发测试

```python
# tests/test_concurrent.py

import pytest
import asyncio
from graph import build_graph
from state import create_initial_state


class TestConcurrent:
    """测试并发场景"""
    
    @pytest.mark.asyncio
    async def test_concurrent_tasks(self):
        """测试并发任务"""
        graph = build_graph()
        
        async def run_task(i):
            state = create_initial_state(topic=f"任务{i}")
            config = {"configurable": {"thread_id": f"task-{i}"}}
            return graph.invoke(state, config=config)
        
        # 并发执行 5 个任务
        tasks = [run_task(i) for i in range(5)]
        results = await asyncio.gather(*tasks)
        
        assert len(results) == 5
```

---

## 5. 安全测试用例

### 5.1 注入攻击测试

```python
# tests/test_security.py

import pytest
from tools.export import validate_path
from state import validate_topic


class TestSecurity:
    """安全测试"""
    
    def test_path_traversal(self):
        """测试路径穿越攻击"""
        with pytest.raises(PermissionError):
            validate_path("../../../etc/passwd", "/safe/dir")
    
    def test_xss_injection(self):
        """测试 XSS 注入"""
        topic = "<script>alert('xss')</script>"
        # 应该被过滤或转义
        result = validate_topic(topic)
        assert "<script>" not in result
    
    def test_sql_injection(self):
        """测试 SQL 注入（如果适用）"""
        topic = "'; DROP TABLE reports; --"
        # 应该被安全处理
        result = validate_topic(topic)
        assert result  # 不应该崩溃
    
    def test_api_key_exposure(self):
        """测试 API Key 不泄露"""
        from tools.llm import sanitize_for_logging
        
        log_text = "API Key: sk-abc123def456 called"
        result = sanitize_for_logging(log_text)
        
        assert "sk-abc123def456" not in result
        assert "sk-***" in result
    
    def test_command_injection(self):
        """测试命令注入"""
        topic = "test; rm -rf /"
        # 应该被安全处理
        result = validate_topic(topic)
        assert "rm -rf" not in result or result  # 过滤或不执行
```

### 5.2 权限测试

```python
class TestPermissions:
    """权限测试"""
    
    def test_file_write_permission(self, tmp_path):
        """测试文件写入权限限制"""
        from tools.export import export_markdown
        
        # 应该只能写入安全目录
        output_path = export_markdown(
            content="# 测试",
            output_dir=str(tmp_path),
            filename="test"
        )
        
        assert Path(output_path).parent == tmp_path
    
    def test_arbitrary_file_read(self):
        """测试任意文件读取防护"""
        # 应该禁止读取系统敏感文件
        # ... 测试逻辑
```

---

## 6. 性能测试用例

### 6.1 响应时间测试

```python
# tests/test_performance.py

import pytest
import time
from graph import build_graph
from state import create_initial_state


class TestPerformance:
    """性能测试"""
    
    @pytest.mark.timeout(600)  # 10分钟超时
    def test_full_flow_time(self):
        """测试完整流程时间"""
        graph = build_graph()
        
        start = time.time()
        state = create_initial_state(topic="测试报告")
        # graph.invoke(state)
        elapsed = time.time() - start
        
        assert elapsed < 600  # 应在 10 分钟内完成
    
    @pytest.mark.timeout(30)
    def test_llm_call_time(self):
        """测试 LLM 调用时间"""
        from tools.llm import chat
        
        start = time.time()
        # chat([{"role": "user", "content": "测试"}])
        elapsed = time.time() - start
        
        assert elapsed < 30  # 应在 30 秒内完成
```

---

## 7. 测试覆盖率目标

| 模块 | 目标覆盖率 |
|-----|----------|
| state.py | > 95% |
| tools/ | > 85% |
| nodes/ | > 80% |
| graph.py | > 75% |
| **总体** | **> 80%** |

---

## 8. 测试执行命令

```bash
# 运行所有测试
pytest src/app/day24/tests/ -v

# 运行特定测试文件
pytest src/app/day24/tests/test_state.py -v

# 运行特定测试类
pytest src/app/day24/tests/test_state.py::TestAppendItem -v

# 运行特定测试方法
pytest src/app/day24/tests/test_state.py::TestAppendItem::test_append_to_empty_list -v

# 生成覆盖率报告
pytest src/app/day24/tests/ --cov=src/app/day24 --cov-report=html --cov-report=term

# 运行安全测试
pytest src/app/day24/tests/test_security.py -v

# 运行性能测试
pytest src/app/day24/tests/test_performance.py -v --timeout=600
```

---

## 9. Mock 策略

### 9.1 LLM Mock

```python
@pytest.fixture
def mock_llm():
    """Mock LLM 响应"""
    with patch("tools.llm.deepseek") as mock:
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content='{"result": "success"}'))]
        mock.chat.completions.create.return_value = response
        yield mock
```

### 9.2 搜索 Mock

```python
@pytest.fixture
def mock_search():
    """Mock 搜索响应"""
    with patch("tools.llm.tavily_search") as mock:
        mock.return_value = {
            "answer": "模拟答案",
            "results": [{"title": "标题", "url": "url", "content": "内容"}]
        }
        yield mock
```

### 9.3 文件系统 Mock

```python
@pytest.fixture
def temp_output_dir(tmp_path):
    """临时输出目录"""
    return tmp_path
```
