"""
股票顾问 - 龙头情绪选股策略分析报告生成器

功能：
1. 支持两种模式：
   - 个股模式：针对用户输入的个股进行分析
   - 选股模式：先调用leader-scan选股，再调用大模型生成专业分析报告
2. 基于V3四层筛选深度优化策略
3. 综合市场行情分析个股
4. 提供具体操作策略（价位、仓位、止损止盈）
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from pathlib import Path
import os
import json
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from stock_agent.data.realtime_quote import RealtimeQuote, MarketSentimentCalculator
from stock_agent.data.data_loader import DataLoader


@dataclass
class StockAdvice:
    """个股建议结果"""
    code: str
    name: str

    # 行情数据
    price: float
    change_pct: float
    high: float
    low: float
    open: float
    prev_close: float
    volume: float  # 万手
    amount: float  # 亿

    # 估值数据
    float_mv: float  # 流通市值(亿)

    # 技术指标
    board_count: int  # 连板数
    is_limit: bool    # 是否涨停
    limit_price: float  # 涨停价
    limit_down_price: float  # 跌停价
    distance_limit: float  # 距涨停%
    amplitude: float  # 振幅

    # 风险评估
    risk_score: int
    risk_level: str
    risk_factors: List[str]

    # 市场情绪
    market_sentiment: float
    market_phase: str

    # 综合评分 (0-100)
    total_score: int

    # 操作建议
    action: str  # BUY / HOLD / SELL / WATCH
    action_cn: str  # 中文建议
    position_pct: float  # 建议仓位比例
    confidence: str  # HIGH / MEDIUM / LOW

    # 具体策略
    buy_price_range: tuple  # (最低买入价, 最高买入价)
    stop_loss_price: float
    stop_loss_pct: float
    take_profit_prices: List[Dict]  # 止盈价位列表

    # 建议理由
    reasons: List[str]
    warnings: List[str]

    # 更新时间
    timestamp: str


@dataclass
class ScanResult:
    """选股结果"""
    scan_date: str
    market: str
    sentiment_score: float
    sentiment_phase: str
    up_count: int
    down_count: int
    limit_up_count: int
    limit_down_count: int
    high_board_count: int

    # 候选股
    two_board_candidates: List[Dict]
    three_board_candidates: List[Dict]
    high_board_candidates: List[Dict]

    # 涨停股详情
    limit_up_stocks: List[Dict]

    # 报告文件路径
    report_file: str


class StockAdvisor:
    """
    股票顾问类

    支持两种模式：
    1. 个股模式：针对用户输入的个股进行分析
    2. 选股模式：先调用leader-scan选股，再调用大模型生成专业分析报告
    """

    def __init__(self):
        self.quote = RealtimeQuote()
        self.sentiment_calc = MarketSentimentCalculator()
        self.data_loader = DataLoader()

    # ==================== 选股模式 ====================

    def run_leader_scan(
        self,
        scan_date: str = None,
        market: str = "all",
        min_seal: float = 0.95,
    ) -> ScanResult:
        """
        执行龙头情绪选股扫描

        Args:
            scan_date: 扫描日期
            market: 市场类型
            min_seal: 最小封板强度

        Returns:
            ScanResult 对象
        """
        from stock_agent.main import run_leader_sentiment_scan

        result = run_leader_sentiment_scan(
            scan_date=scan_date,
            market=market,
            min_seal=min_seal,
            output_dir=str(Path(__file__).parent.parent / "outputs"),
            push=False,
        )

        if not result:
            return None

        # 解析报告文件获取详细信息
        report_file = result.get('report_file')
        detailed_data = self._parse_scan_report(report_file) if report_file else {}

        return ScanResult(
            scan_date=detailed_data.get('scan_date', scan_date or datetime.now().strftime('%Y%m%d')),
            market=market,
            sentiment_score=result.get('sentiment_score', 0),
            sentiment_phase=result.get('sentiment_phase', '未知'),
            up_count=detailed_data.get('up_count', 0),
            down_count=detailed_data.get('down_count', 0),
            limit_up_count=detailed_data.get('limit_up_count', 0),
            limit_down_count=detailed_data.get('limit_down_count', 0),
            high_board_count=detailed_data.get('high_board_count', 0),
            two_board_candidates=detailed_data.get('two_board_candidates', []),
            three_board_candidates=detailed_data.get('three_board_candidates', []),
            high_board_candidates=detailed_data.get('high_board_candidates', []),
            limit_up_stocks=detailed_data.get('limit_up_stocks', []),
            report_file=report_file,
        )

    def _parse_scan_report(self, report_file: str) -> Dict:
        """解析扫描报告文件"""
        if not report_file or not os.path.exists(report_file):
            return {}

        with open(report_file, 'r', encoding='utf-8') as f:
            content = f.read()

        data = {
            'two_board_candidates': [],
            'three_board_candidates': [],
            'high_board_candidates': [],
            'limit_up_stocks': [],
        }
        lines = content.split('\n')

        current_section = None
        current_table = []
        in_table = False
        in_trading_strategy = False
        current_stock_code = None

        for i, line in enumerate(lines):
            # 解析基本信息
            if '| 选股日期 |' in line:
                parts = line.split('|')
                if len(parts) >= 3:
                    data['scan_date'] = parts[2].strip().replace('-', '')
            elif '| 上涨家数 |' in line:
                parts = line.split('|')
                if len(parts) >= 3:
                    data['up_count'] = int(parts[2].strip())
            elif '| 下跌家数 |' in line:
                parts = line.split('|')
                if len(parts) >= 3:
                    data['down_count'] = int(parts[2].strip())
            elif '| 涨停家数 |' in line:
                parts = line.split('|')
                if len(parts) >= 3:
                    data['limit_up_count'] = int(parts[2].strip())
            elif '| 跌停家数 |' in line:
                parts = line.split('|')
                if len(parts) >= 3:
                    data['limit_down_count'] = int(parts[2].strip())
            elif '| 3连板以上 |' in line:
                parts = line.split('|')
                if len(parts) >= 3:
                    data['high_board_count'] = int(parts[2].strip())

            # 解析候选股表格
            if '【2板候选】' in line:
                current_section = 'two_board'
            elif '【3板候选】' in line:
                current_section = 'three_board'
            elif '【高位板】' in line:
                current_section = 'high_board'
            elif '| 优先级 | 代码 |' in line or '| 序号 | 代码 |' in line:
                in_table = True
                current_table = []
            elif in_table and line.startswith('|') and '**' in line:
                # 解析表格行，如：| ⭐⭐⭐ | **603032** | 德新科技 | 27.69 | 64.2亿 | 低风险 | - |
                parts = [p.strip() for p in line.split('|')]
                if len(parts) >= 8:
                    code = parts[2].replace('**', '')
                    try:
                        stock_info = {
                            'code': code,
                            'name': parts[3] if parts[3] else self._get_stock_name(code),  # 名称在第3列
                            'close': float(parts[4]),
                            'float_mv': float(parts[5].replace('亿', '')),
                            'risk_level': parts[6],
                            'risk_factors': parts[7] if parts[7] != '-' else '',
                        }
                        if current_section == 'two_board':
                            stock_info['board'] = 2
                            data['two_board_candidates'].append(stock_info)
                        elif current_section == 'three_board':
                            stock_info['board'] = 3
                            data['three_board_candidates'].append(stock_info)
                        elif current_section == 'high_board':
                            stock_info['board'] = 4
                            data['high_board_candidates'].append(stock_info)
                    except:
                        pass
            elif in_table and (not line.startswith('|') or '------' in line):
                if '------' in line:
                    continue
                if not line.startswith('|'):
                    in_table = False

        return data

    def _get_stock_name(self, code: str) -> str:
        """获取股票名称（使用腾讯财经API）"""
        try:
            # 使用腾讯财经API
            if code.startswith('6'):
                market = 'sh'
            else:
                market = 'sz'

            import requests
            url = f'http://qt.gtimg.cn/q={market}{code}'
            headers = {'Referer': 'http://finance.qq.com'}
            response = requests.get(url, headers=headers, timeout=5)
            response.encoding = 'gbk'
            data = response.text

            # 解析数据：v_sh603032="1~德新科技~603032..."
            import re
            match = re.search(r'=\"(.*)\"', data)
            if match:
                items = match.group(1).split('~')
                if len(items) >= 2:
                    return items[1]  # 股票名称在第2个字段
        except:
            pass

        return code  # 如果获取失败，返回代码

    def generate_strategy_report(
        self,
        scan_result: ScanResult,
        use_llm: bool = True,
    ) -> str:
        """
        生成龙头情绪选股策略分析报告

        Args:
            scan_result: 选股结果
            use_llm: 是否使用大模型

        Returns:
            报告内容
        """
        if use_llm:
            return self._generate_llm_strategy_report(scan_result)
        else:
            return self._generate_basic_strategy_report(scan_result)

    def _get_llm_config(self) -> Dict:
        """
        获取大模型配置

        支持多种大模型：
        - DeepSeek (默认)
        - Anthropic Claude

        Returns:
            配置字典，包含 provider, api_key, model 等
        """
        config = {
            'provider': 'deepseek',  # 默认使用 DeepSeek
            'api_key': None,
            'base_url': None,
            'model': None,
        }

        # 优先从环境变量读取
        # DeepSeek 配置
        deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
        if deepseek_key:
            config['provider'] = 'deepseek'
            config['api_key'] = deepseek_key
            config['base_url'] = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
            config['model'] = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
            return config

        # Anthropic 配置
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if anthropic_key:
            config['provider'] = 'anthropic'
            config['api_key'] = anthropic_key
            config['model'] = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6-20250514")
            return config

        # 从配置文件读取
        config_path = Path(__file__).parent.parent / "config" / "api_keys.json"
        if config_path.exists():
            try:
                # 使用 utf-8-sig 处理可能的 BOM
                with open(config_path, 'r', encoding='utf-8-sig') as f:
                    file_config = json.load(f)

                # 优先使用 DeepSeek
                if file_config.get("deepseek_api_key"):
                    config['provider'] = 'deepseek'
                    config['api_key'] = file_config["deepseek_api_key"]
                    config['base_url'] = file_config.get("deepseek_base_url", "https://api.deepseek.com")
                    config['model'] = file_config.get("deepseek_model", "deepseek-chat")
                    return config

                # 其次使用 Anthropic
                if file_config.get("anthropic_api_key"):
                    config['provider'] = 'anthropic'
                    config['api_key'] = file_config["anthropic_api_key"]
                    config['model'] = file_config.get("anthropic_model", "claude-sonnet-4-6-20250514")
                    return config

            except Exception as e:
                print(f"[警告] 读取配置文件失败: {e}")

        return config

    def _generate_llm_strategy_report(self, scan_result: ScanResult) -> str:
        """使用大模型生成专业分析报告"""
        prompt = self._build_strategy_prompt(scan_result)

        # 获取大模型配置
        llm_config = self._get_llm_config()

        if not llm_config['api_key']:
            print("\n" + "=" * 70)
            print("[错误] 未配置大模型API Key，无法生成专业报告")
            print("=" * 70)
            print("\n支持的大模型：")
            print("  1. DeepSeek (推荐)")
            print("  2. Anthropic Claude")
            print("\n配置方式（任选其一）：")
            print("\n【方式1】设置环境变量：")
            print("  set DEEPSEEK_API_KEY=your_deepseek_api_key")
            print("  或")
            print("  set ANTHROPIC_API_KEY=your_anthropic_api_key")
            print("\n【方式2】创建配置文件 config/api_keys.json：")
            print('  {')
            print('    "deepseek_api_key": "your_deepseek_api_key",')
            print('    "deepseek_model": "deepseek-chat"')
            print('  }')
            print("\n使用 --no-llm 参数可跳过大模型，生成基础报告")
            print("=" * 70)
            return self._generate_basic_strategy_report(scan_result)

        # 根据provider调用不同的大模型
        provider = llm_config['provider']

        try:
            print(f"\n[调用大模型] 使用 {provider.upper()} 正在生成专业分析报告...")

            if provider == 'deepseek':
                return self._call_deepseek(llm_config, prompt)
            elif provider == 'anthropic':
                return self._call_anthropic(llm_config, prompt)
            else:
                print(f"[错误] 不支持的大模型: {provider}")
                return self._generate_basic_strategy_report(scan_result)

        except Exception as e:
            print(f"\n[错误] 大模型调用失败: {e}")
            return self._generate_basic_strategy_report(scan_result)

    def _call_deepseek(self, config: Dict, prompt: str) -> str:
        """调用 DeepSeek API"""
        try:
            import openai

            client = openai.OpenAI(
                api_key=config['api_key'],
                base_url=f"{config['base_url']}/v1"
            )

            response = client.chat.completions.create(
                model=config['model'],
                messages=[
                    {"role": "user", "content": prompt}
                ],
                max_tokens=8192,
                temperature=0.7,
            )

            print("[调用大模型] DeepSeek 报告生成成功!")
            return response.choices[0].message.content

        except ImportError:
            print("\n[错误] 未安装openai库，请运行: pip install openai")
            return self._generate_basic_strategy_report(None)

    def _call_anthropic(self, config: Dict, prompt: str) -> str:
        """调用 Anthropic Claude API"""
        try:
            from anthropic import Anthropic

            client = Anthropic(api_key=config['api_key'])

            response = client.messages.create(
                model=config['model'],
                max_tokens=8192,
                messages=[
                    {"role": "user", "content": prompt}
                ]
            )

            print("[调用大模型] Claude 报告生成成功!")
            return response.content[0].text

        except ImportError:
            print("\n[错误] 未安装anthropic库，请运行: pip install anthropic")
            return self._generate_basic_strategy_report(None)

    def _build_strategy_prompt(self, scan_result: ScanResult) -> str:
        """构建策略分析提示词"""

        # 读取完整报告内容
        report_content = ""
        if scan_result.report_file and os.path.exists(scan_result.report_file):
            with open(scan_result.report_file, 'r', encoding='utf-8') as f:
                report_content = f.read()

        # 格式化候选股信息（包含股票名称）
        two_board_info = ""
        for s in scan_result.two_board_candidates[:10]:
            name = s.get('name', s.get('code', '未知'))
            two_board_info += f"- {s.get('code', '未知')} ({name}): 收盘价{s.get('close', 0):.2f}元, 市值{s.get('float_mv', 0):.1f}亿, {s.get('board', 0)}板, 风险等级:{s.get('risk_level', '未知')}\n"

        three_board_info = ""
        for s in scan_result.three_board_candidates[:10]:
            name = s.get('name', s.get('code', '未知'))
            three_board_info += f"- {s.get('code', '未知')} ({name}): 收盘价{s.get('close', 0):.2f}元, 市值{s.get('float_mv', 0):.1f}亿, {s.get('board', 0)}板, 风险等级:{s.get('risk_level', '未知')}\n"

        high_board_info = ""
        for s in scan_result.high_board_candidates[:10]:
            name = s.get('name', s.get('code', '未知'))
            high_board_info += f"- {s.get('code', '未知')} ({name}): 收盘价{s.get('close', 0):.2f}元, 市值{s.get('float_mv', 0):.1f}亿, {s.get('board', 0)}板, 风险等级:{s.get('risk_level', '未知')}\n"

        prompt = f'''你是一名资深的A股交易分析师。请严格按照以下格式生成《龙头情绪选股策略分析报告》，不要偏离格式。

# 龙头情绪选股策略分析报告（{scan_result.scan_date}操作）

**分析日期**: {scan_result.scan_date}
**数据来源**: {scan_result.market}市场扫描
**策略版本**: V3深度优化版

---

## 第一部分：市场大环境分析

### 一、市场情绪概览

| 指标 | 数值 |
|------|------|
| 情绪分 | **{scan_result.sentiment_score:.1f} 分** |
| 情绪阶段 | **{scan_result.sentiment_phase}** |
| 上涨家数 | {scan_result.up_count} |
| 下跌家数 | {scan_result.down_count} |
| 涨停家数 | {scan_result.limit_up_count} |
| 跌停家数 | {scan_result.limit_down_count} |
| 3连板以上 | {scan_result.high_board_count} |

### 二、市场特征分析

1. **情绪分析**：当前情绪分{scan_result.sentiment_score:.1f}分，处于{scan_result.sentiment_phase}。该阶段的历史胜率和收益特征（简要说明）。

2. **涨跌结构**：涨停{scan_result.limit_up_count}只vs跌停{scan_result.limit_down_count}只，上涨{scan_result.up_count}家vs下跌{scan_result.down_count}家。市场强弱判断（2-3句话）。

3. **连板结构**：高位股数量、资金接力意愿（2-3句话）。

4. **市场判断**：当前属于牛市/震荡市/熊市，操作建议（1-2句话）。

---

## 第二部分：明日候选股筛选（V3四层筛选）

### 筛选过程

| 筛选层 | 条件 | 结果 |
|--------|------|------|
| 第一层 | 连板数筛选（2板优先） | 说明通过情况 |
| 第二层 | 情绪阶段筛选 | 当前阶段是否适合参与 |
| 第三层 | 市值筛选（15-200亿） | 说明通过情况 |
| 第四层 | 风险评分筛选 | 说明风险等级分布 |

### 最终候选股清单

按优先级排序如下：

| 优先级 | 代码 | 名称 | 连板数 | 收盘价 | 市值 | 风险等级 | 推荐理由 |
|--------|------|------|--------|--------|------|----------|----------|
| ⭐⭐⭐ | 代码 | 名称 | 2板 | 价格 | 市值 | 低风险 | 理由 |
| ⭐⭐⭐ | 代码 | 名称 | 2板 | 价格 | 市值 | 低风险 | 理由 |
| ⭐⭐ | 代码 | 名称 | 3板 | 价格 | 市值 | 中风险 | 理由 |
| ... | ... | ... | ... | ... | ... | ... | ... |

---

## 第三部分：核心操作标的详细分析

对每只核心候选股进行详细分析：

### 【重点推荐】代码 名称（连板数，风险等级）

**基本信息**
- 收盘价: XX元
- 流通市值: XX亿
- 连板数: X板
- 风险评分: XX分（风险等级）

**建仓策略**（总仓位XX%）

| 批次 | 价位 | 比例 | 条件 |
|------|------|------|------|
| 第一批 | XX.XX | 40% | 竞价/开盘价≤高开2% |
| 第二批 | XX.XX | 40% | 开盘30分钟内确认强势 |
| 第三批 | XX.XX | 20% | 封板前/涨停确认 |
| **预估均价** | **XX.XX** | - | 分批建仓后均价 |

**卖出策略**

| 策略 | 价位 | 说明 |
|------|------|------|
| 止损价 | XX.XX | 亏损6% |
| 止盈1 | XX.XX | +5%卖30% |
| 止盈2 | XX.XX | +10%卖30% |
| 止盈3 | XX.XX | +15%卖40% |
| 移动止盈触发 | XX.XX | 盈利5%后启动 |

**操作建议**: 具体操作建议（2-3句话）

---

（对每只候选股重复上述格式）

---

## 第四部分：个股综合分析

对每只候选股的技术面和资金面进行简要分析：

### 代码 名称 技术分析

**技术面**: （分析K线形态、均线、成交量等）

**资金面**: （分析成交额、换手率、资金流向等）

**操作建议**: （具体建议）

**风险提示**: （主要风险点）

---

## 第五部分：明日操作总结

### 推荐操作顺序

| 优先级 | 代码 | 名称 | 连板 | 建议仓位 | 风险等级 | 核心理由 |
|--------|------|------|------|----------|----------|----------|
| ⭐⭐⭐ | 代码 | 名称 | 2板 | 60% | 低风险 | 理由 |
| ⭐⭐⭐ | 代码 | 名称 | 2板 | 50% | 低风险 | 理由 |
| ⭐⭐ | 代码 | 3板 | 30% | 中风险 | 理由 |
| ⭐ | 代码 | X板 | 观望 | 高风险 | 理由 |

### 仓位建议

基于当前情绪阶段（{scan_result.sentiment_phase}），建议总仓位：XX%-XX%

### 风险提示

| 风险项 | 说明 | 应对策略 |
|--------|------|----------|
| 风险1 | 说明 | 应对策略 |
| 风险2 | 说明 | 应对策略 |
| 风险3 | 说明 | 应对策略 |

### 关键时间节点

| 时间 | 操作 |
|------|------|
| 9:15-9:25 | 观察候选股竞价情况 |
| 9:25 | 重新计算情绪分，确认是否交易 |
| 9:30-9:45 | 观察开盘走势，确认建仓 |
| 9:45-10:30 | 执行分批买入 |

---

## 第六部分：V3策略核心要点

### 四层筛选机制

1. **连板筛选**: 2板优先（仓位100%），3板次之（仓位80%），4板观望
2. **情绪筛选**: 退潮期优先，高潮期谨慎
3. **市值筛选**: 15-200亿
4. **风险筛选**: 低风险优先

### 止盈止损纪律

| 类型 | 条件 |
|------|------|
| 硬止损 | 亏损6%必须离场 |
| 移动止盈 | 盈利5%后回撤3%止盈 |
| 分批止盈 | +5%卖30%，+10%卖30%，+15%卖40% |
| 最大持仓 | 5天到期强制卖出 |

---

*分析基于V3深度优化版策略*

---

## 原始详细数据

以下是系统生成的原始数据，包含具体的建仓和卖出价格：

```
{report_content[:6000]}
```

请严格按照上述格式生成报告，确保表格对齐、数据准确、分析专业。'''
        return prompt

    def _generate_basic_strategy_report(self, scan_result: ScanResult) -> str:
        """生成基础策略报告"""
        lines = []

        # 报告头部
        lines.append("# 龙头情绪选股策略分析报告")
        lines.append("")
        lines.append(f"**选股日期**: {scan_result.scan_date}")
        lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        # 市场环境
        lines.append("## 一、市场环境分析")
        lines.append("")
        lines.append(f"- 情绪分: {scan_result.sentiment_score:.1f}分")
        lines.append(f"- 情绪阶段: {scan_result.sentiment_phase}")
        lines.append(f"- 涨停: {scan_result.limit_up_count}只")
        lines.append(f"- 跌停: {scan_result.limit_down_count}只")
        lines.append("")

        # 候选股
        lines.append("## 二、候选股清单")
        lines.append("")

        if scan_result.two_board_candidates:
            lines.append("### 2板候选股")
            lines.append("")
            for s in scan_result.two_board_candidates:
                lines.append(f"- {s.get('code')}: {s.get('close', 0):.2f}元")
            lines.append("")

        if scan_result.three_board_candidates:
            lines.append("### 3板候选股")
            lines.append("")
            for s in scan_result.three_board_candidates:
                lines.append(f"- {s.get('code')}: {s.get('close', 0):.2f}元")
            lines.append("")

        return "\n".join(lines)

    # ==================== 个股模式 ====================

    def analyze_stock(self, code: str, market_sentiment: float = None,
                      market_phase: str = None) -> StockAdvice:
        """
        分析单只股票

        Args:
            code: 股票代码
            market_sentiment: 市场情绪分数（可选，不传则自动计算）
            market_phase: 市场情绪阶段（可选）

        Returns:
            StockAdvice 对象
        """
        # 1. 获取实时行情
        quote_data = self.quote.get_quote_sina(code)
        if 'error' in quote_data:
            return None

        # 2. 获取市值
        float_mv = self.quote.get_float_market_cap(code)
        if float_mv is None:
            float_mv = 0

        # 3. 获取市场情绪
        if market_sentiment is None:
            sentiment = self.sentiment_calc.calculate_sentiment('all')
            if sentiment:
                market_sentiment = sentiment.score
                market_phase = sentiment.phase
            else:
                market_sentiment = 50
                market_phase = "中性"

        # 4. 计算技术指标
        price = quote_data['price']
        prev_close = quote_data['prev_close']

        # 涨停价计算
        if code.startswith('30') or code.startswith('68'):
            limit_pct = 20
        else:
            limit_pct = 10

        limit_price = round(prev_close * (1 + limit_pct / 100), 2)
        limit_down_price = round(prev_close * (1 - limit_pct / 100), 2)
        distance_limit = (limit_price - price) / price * 100 if price > 0 else 0
        amplitude = (quote_data['high'] - quote_data['low']) / prev_close * 100 if prev_close > 0 else 0

        # 判断涨停
        is_limit = quote_data['change_pct'] >= limit_pct - 0.5

        # 计算连板数
        board_count = self._calculate_board_count(code, limit_pct)

        # 5. 风险评估
        risk_score, risk_level, risk_factors = self._calculate_risk(
            code, price, float_mv, board_count, quote_data['amount']
        )

        # 6. 计算综合评分
        total_score = self._calculate_total_score(
            quote_data['change_pct'], board_count, float_mv,
            risk_score, market_sentiment, is_limit
        )

        # 7. 生成操作建议
        action, action_cn, position_pct, confidence = self._generate_action(
            total_score, risk_level, market_sentiment, board_count, is_limit
        )

        # 8. 生成具体策略
        buy_range, stop_loss, stop_pct, take_profits = self._generate_strategy(
            price, prev_close, action, board_count
        )

        # 9. 生成建议理由
        reasons, warnings = self._generate_reasons(
            quote_data, board_count, float_mv, risk_factors,
            market_sentiment, market_phase
        )

        return StockAdvice(
            code=code,
            name=quote_data['name'],
            price=price,
            change_pct=quote_data['change_pct'],
            high=quote_data['high'],
            low=quote_data['low'],
            open=quote_data['open'],
            prev_close=prev_close,
            volume=quote_data['volume'] / 10000,
            amount=quote_data['amount'] / 100000000,
            float_mv=float_mv,
            board_count=board_count,
            is_limit=is_limit,
            limit_price=limit_price,
            limit_down_price=limit_down_price,
            distance_limit=distance_limit,
            amplitude=amplitude,
            risk_score=risk_score,
            risk_level=risk_level,
            risk_factors=risk_factors,
            market_sentiment=market_sentiment,
            market_phase=market_phase,
            total_score=total_score,
            action=action,
            action_cn=action_cn,
            position_pct=position_pct,
            confidence=confidence,
            buy_price_range=buy_range,
            stop_loss_price=stop_loss,
            stop_loss_pct=stop_pct,
            take_profit_prices=take_profits,
            reasons=reasons,
            warnings=warnings,
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )

    def analyze_batch(self, codes: List[str]) -> List[StockAdvice]:
        """批量分析股票"""
        print("正在获取市场情绪...")
        try:
            sentiment_data = self.sentiment_calc.get_market_sentiment_fast()
            market_sentiment = sentiment_data['sentiment_score']
            market_phase = sentiment_data['phase']
            print(f"市场情绪: {market_sentiment:.0f}分 ({market_phase})")
        except Exception as e:
            print(f"[提示] 市场情绪获取失败: {e}")
            market_sentiment = 50
            market_phase = "中性"

        results = []
        for code in codes:
            advice = self.analyze_stock(code, market_sentiment, market_phase)
            if advice:
                results.append(advice)

        results.sort(key=lambda x: x.total_score, reverse=True)
        return results

    def _calculate_board_count(self, code: str, limit_pct: float) -> int:
        """计算连板数"""
        try:
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=15)).strftime('%Y%m%d')
            df = self.data_loader.get_stock_history(code, start_date, end_date)

            if df is None or len(df) < 2:
                return 0

            board = 0
            for i in range(-1, -min(10, len(df)), -1):
                if i - 1 < -len(df):
                    break
                today = df.iloc[i]
                yesterday = df.iloc[i - 1]
                pct = (today['close'] - yesterday['close']) / yesterday['close'] * 100

                if pct >= limit_pct - 0.5:
                    board += 1
                else:
                    break

            return board
        except:
            return 0

    def _calculate_risk(self, code: str, price: float, float_mv: float,
                        board_count: int, amount: float) -> tuple:
        """计算风险评分"""
        risk_score = 0
        risk_factors = []

        if board_count >= 4:
            risk_score += 25
            risk_factors.append(f'{board_count}板高位风险')
        elif board_count == 3:
            risk_score += 10
            risk_factors.append('3板加速期')

        if float_mv < 30:
            risk_score += 15
            risk_factors.append(f'小市值{float_mv:.1f}亿')
        elif float_mv > 200:
            risk_score += 5
            risk_factors.append(f'大市值{float_mv:.1f}亿')

        if amount > 10:
            risk_score += 10
            risk_factors.append('成交额放大')

        if risk_score < 15:
            risk_level = '低风险'
        elif risk_score < 35:
            risk_level = '中风险'
        else:
            risk_level = '高风险'

        return risk_score, risk_level, risk_factors

    def _calculate_total_score(self, change_pct: float, board_count: int,
                               float_mv: float, risk_score: int,
                               market_sentiment: float, is_limit: bool) -> int:
        """计算综合评分 (0-100)"""
        score = 50

        if change_pct > 0:
            score += min(change_pct * 2, 20)
        else:
            score += change_pct * 2

        if board_count >= 2:
            score += board_count * 5

        if 30 <= float_mv <= 150:
            score += 10
        elif 150 < float_mv <= 200:
            score += 5

        if is_limit:
            score += 10

        score -= risk_score * 0.5

        if market_sentiment >= 60:
            score += 5
        elif market_sentiment < 40:
            score -= 10

        return max(0, min(100, int(score)))

    def _generate_action(self, total_score: int, risk_level: str,
                         market_sentiment: float, board_count: int,
                         is_limit: bool) -> tuple:
        """生成操作建议"""
        if total_score >= 70 and risk_level != '高风险':
            action = 'BUY'
            action_cn = '建议买入'
            confidence = 'HIGH'
            position_pct = 0.4 if market_sentiment >= 50 else 0.2
        elif total_score >= 50:
            if is_limit and board_count <= 2:
                action = 'BUY'
                action_cn = '可轻仓买入'
                confidence = 'MEDIUM'
                position_pct = 0.2
            else:
                action = 'WATCH'
                action_cn = '建议观望'
                confidence = 'MEDIUM'
                position_pct = 0
        elif total_score >= 30:
            action = 'WATCH'
            action_cn = '建议观望'
            confidence = 'LOW'
            position_pct = 0
        else:
            action = 'SELL'
            action_cn = '建议卖出/回避'
            confidence = 'HIGH'
            position_pct = 0

        if risk_level == '高风险':
            if action == 'BUY':
                action = 'WATCH'
                action_cn = '风险较高，建议观望'
                position_pct = 0

        return action, action_cn, position_pct, confidence

    def _generate_strategy(self, price: float, prev_close: float,
                           action: str, board_count: int) -> tuple:
        """生成具体操作策略"""
        if action == 'BUY':
            buy_low = round(price * 0.98, 2)
            buy_high = round(price * 1.00, 2)
            buy_range = (buy_low, buy_high)
        else:
            buy_range = (0, 0)

        stop_loss = round(price * 0.94, 2)
        stop_pct = 6.0

        take_profits = [
            {'price': round(price * 1.05, 2), 'pct': 5, 'sell_ratio': 0.3, 'desc': '+5%卖30%'},
            {'price': round(price * 1.10, 2), 'pct': 10, 'sell_ratio': 0.3, 'desc': '+10%卖30%'},
            {'price': round(price * 1.15, 2), 'pct': 15, 'sell_ratio': 0.4, 'desc': '+15%卖40%'},
        ]

        return buy_range, stop_loss, stop_pct, take_profits

    def _generate_reasons(self, quote_data: Dict, board_count: int,
                          float_mv: float, risk_factors: List[str],
                          market_sentiment: float, market_phase: str) -> tuple:
        """生成建议理由"""
        reasons = []
        warnings = []

        if quote_data['change_pct'] > 5:
            reasons.append(f"涨幅{quote_data['change_pct']:.1f}%，走势强势")
        if board_count >= 2:
            reasons.append(f"{board_count}连板，上涨动能强")

        if 30 <= float_mv <= 150:
            reasons.append(f"市值{float_mv:.1f}亿，资金易推动")

        if market_sentiment >= 60:
            reasons.append(f"市场情绪{market_sentiment:.0f}分({market_phase})，环境友好")
        elif market_sentiment < 40:
            warnings.append(f"市场情绪{market_sentiment:.0f}分，环境偏弱")

        for rf in risk_factors:
            warnings.append(f"风险: {rf}")

        return reasons, warnings

    def format_advice(self, advice: StockAdvice) -> str:
        """格式化单股建议"""
        lines = []
        lines.append("=" * 70)
        lines.append(f"【{advice.name}】{advice.code} - 操作建议")
        lines.append("=" * 70)
        lines.append(f"分析时间: {advice.timestamp}")
        lines.append("")

        lines.append(f"【综合评分】{advice.total_score}分")
        lines.append(f"【操作建议】{advice.action_cn} (置信度: {advice.confidence})")
        if advice.action == 'BUY':
            lines.append(f"【建议仓位】{advice.position_pct*100:.0f}%")
        lines.append("")

        lines.append("【行情概览】")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 现价 | {advice.price:.2f} 元 |")
        lines.append(f"| 涨跌幅 | {advice.change_pct:+.2f}% |")
        lines.append(f"| 今开/最高/最低 | {advice.open:.2f} / {advice.high:.2f} / {advice.low:.2f} |")
        lines.append(f"| 成交额 | {advice.amount:.2f} 亿 |")
        lines.append(f"| 流通市值 | {advice.float_mv:.1f} 亿 |")
        lines.append(f"| 连板数 | {advice.board_count} 板 |")
        lines.append("")

        lines.append("【风险评估】")
        lines.append(f"| 等级 | {advice.risk_level} ({advice.risk_score}分) |")
        if advice.risk_factors:
            lines.append(f"| 因素 | {', '.join(advice.risk_factors)} |")
        lines.append("")

        if advice.action == 'BUY':
            lines.append("【操作策略】")
            lines.append(f"| 策略 | 价格 | 说明 |")
            lines.append(f"|------|------|------|")
            buy_low, buy_high = advice.buy_price_range
            lines.append(f"| 买入区间 | {buy_low:.2f} - {buy_high:.2f} | 当前价附近分批买入 |")
            lines.append(f"| 止损价 | {advice.stop_loss_price:.2f} | -{advice.stop_loss_pct:.0f}%止损 |")
            for tp in advice.take_profit_prices:
                lines.append(f"| 止盈 | {tp['price']:.2f} | {tp['desc']} |")
            lines.append("")

        if advice.reasons:
            lines.append("【看多理由】")
            for r in advice.reasons:
                lines.append(f"- {r}")
            lines.append("")

        if advice.warnings:
            lines.append("【风险提示】")
            for w in advice.warnings:
                lines.append(f"- {w}")
            lines.append("")

        lines.append("=" * 70)
        return '\n'.join(lines)


def run_stock_advisor(
    codes: List[str] = None,
    market: str = "all",
    scan_date: str = None,
    use_llm: bool = True,
    output_dir: str = None,
    push: bool = False,
):
    """
    运行股票顾问分析

    支持两种模式：
    1. 个股模式：codes不为空时，分析指定股票
    2. 选股模式：codes为空时，先选股再生成分析报告

    Args:
        codes: 股票代码列表（个股模式）
        market: 市场类型（选股模式）
        scan_date: 选股日期（选股模式）
        use_llm: 是否使用大模型
        output_dir: 输出目录
        push: 是否推送到微信
    """
    advisor = StockAdvisor()

    # 判断模式
    if codes:
        # ========== 个股模式 ==========
        print("=" * 70)
        print("股票顾问 - 个股操作建议分析")
        print("=" * 70)
        print(f"分析股票: {', '.join(codes)}")
        print("=" * 70)

        print("\n正在分析...")
        advices = advisor.analyze_batch(codes)

        if not advices:
            print("无有效分析结果")
            return None

        # 输出到控制台
        print("\n")
        for a in advices:
            print(advisor.format_advice(a))
            print()

        # 生成报告
        report = _generate_individual_report(advices)

        # 保存报告
        if output_dir is None:
            output_dir = Path(__file__).parent.parent / "outputs"

        output_path = Path(output_dir)
        output_subdir = output_path / "daily" / "stock_advice"
        output_subdir.mkdir(parents=True, exist_ok=True)

        date_str = datetime.now().strftime('%Y%m%d')
        report_file = output_subdir / f"stock_advice_{date_str}.md"

        if report_file.exists():
            report_file = output_subdir / f"stock_advice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)

        print("\n" + "=" * 70)
        print(f"报告已保存: {report_file}")

        result = {'advices': advices, 'report_file': str(report_file)}

    else:
        # ========== 选股模式 ==========
        print("=" * 80)
        print("龙头情绪选股策略分析报告")
        print("=" * 80)
        print(f"选股日期: {scan_date or '今天'}")
        print(f"市场: {market}")
        print(f"使用大模型: {'是' if use_llm else '否'}")
        print("=" * 80)

        # 步骤1：选股扫描
        print("\n[步骤1] 执行龙头情绪选股扫描...")
        scan_result = advisor.run_leader_scan(
            scan_date=scan_date,
            market=market,
        )

        if not scan_result:
            print("选股扫描失败")
            return None

        print(f"  扫描完成: 情绪分 {scan_result.sentiment_score:.1f}分")
        print(f"  2板候选: {len(scan_result.two_board_candidates)}只")
        print(f"  3板候选: {len(scan_result.three_board_candidates)}只")

        # 步骤2：生成策略分析报告
        print("\n[步骤2] 生成龙头情绪选股策略分析报告...")
        report = advisor.generate_strategy_report(scan_result, use_llm=use_llm)

        # 步骤3：保存报告
        print("\n[步骤3] 保存报告...")
        if output_dir is None:
            output_dir = Path(__file__).parent.parent / "outputs"

        output_path = Path(output_dir)
        output_subdir = output_path / "daily" / "leader_report"
        output_subdir.mkdir(parents=True, exist_ok=True)

        date_str = scan_result.scan_date or datetime.now().strftime('%Y%m%d')
        report_file = output_subdir / f"leader_strategy_report_{market}_{date_str}.md"

        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)

        print(f"  报告已保存: {report_file}")

        result = {
            'scan_result': scan_result,
            'report': report,
            'report_file': str(report_file),
        }

    # 推送到微信
    if push:
        print("\n推送到微信...")
        try:
            from stock_agent.notifiers.wechat_notifier import WechatConfig, WechatNotifier

            config_path = Path(__file__).parent.parent / "wechat_config.json"
            if config_path.exists():
                wechat_config = WechatConfig.from_file(str(config_path))
                notifier = WechatNotifier(wechat_config)

                title = f"【龙头策略分析报告】{scan_result.scan_date if 'scan_result' in dir() else datetime.now().strftime('%Y-%m-%d')}"

                if wechat_config.method == "serverchan":
                    success = notifier._send_serverchan(title, result.get('report', ''))
                elif wechat_config.method == "wework":
                    success = notifier._send_wework(title, result.get('report', ''))
                else:
                    success = notifier.send_custom_message(title, result.get('report', ''))

                if success:
                    print("推送成功!")
                else:
                    print("推送失败")
            else:
                print("未配置微信推送")
        except Exception as e:
            print(f"推送异常: {e}")

    # 输出报告
    if 'report' in result:
        print("\n" + "=" * 80)
        print(result['report'])
        print("=" * 80)

    return result


def _generate_individual_report(advices: List[StockAdvice]) -> str:
    """生成个股模式报告"""
    lines = []
    lines.append("# 股票操作建议报告")
    lines.append("")
    lines.append(f"**分析时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # 汇总表
    lines.append("## 一、汇总概览")
    lines.append("")
    lines.append("| 优先级 | 代码 | 名称 | 现价 | 涨幅 | 市值 | 连板 | 评分 | 风险 | 建议 | 仓位 |")
    lines.append("|:------:|:----:|:----:|:----:|:----:|:----:|:----:|:----:|:----:|:----:|:----:|")

    for i, a in enumerate(advices, 1):
        pos_str = f"{a.position_pct*100:.0f}%" if a.position_pct > 0 else "-"
        # 根据评分和风险计算优先级
        if a.total_score >= 70 and a.risk_level == '低风险':
            priority = '⭐⭐⭐'
        elif a.total_score >= 50 and a.risk_level in ['低风险', '中风险']:
            priority = '⭐⭐'
        else:
            priority = '⭐'
        lines.append(f"| {priority} | {a.code} | {a.name} | {a.price:.2f} | {a.change_pct:+.1f}% | "
                    f"{a.float_mv:.0f}亿 | {a.board_count}板 | {a.total_score}分 | "
                    f"{a.risk_level} | {a.action_cn} | {pos_str} |")

    lines.append("")

    # 分类建议
    buy_list = [a for a in advices if a.action == 'BUY']
    watch_list = [a for a in advices if a.action == 'WATCH']
    sell_list = [a for a in advices if a.action == 'SELL']

    if buy_list:
        lines.append("### 建议买入")
        lines.append("")
        for a in buy_list:
            lines.append(f"- **{a.code}** ({a.name}): {a.price:.2f}元, "
                       f"买入区间 {a.buy_price_range[0]:.2f}-{a.buy_price_range[1]:.2f}, "
                       f"止损 {a.stop_loss_price:.2f}")
        lines.append("")

    if watch_list:
        lines.append("### 建议观望")
        lines.append("")
        for a in watch_list:
            lines.append(f"- **{a.code}** ({a.name}): {a.price:.2f}元, 评分{a.total_score}分")
        lines.append("")

    if sell_list:
        lines.append("### 建议回避")
        lines.append("")
        for a in sell_list:
            lines.append(f"- **{a.code}** ({a.name}): {a.price:.2f}元")
        lines.append("")

    # 详细分析
    lines.append("## 二、个股详细分析")
    lines.append("")

    for a in advices:
        lines.append(f"### {a.code} {a.name}")
        lines.append("")
        lines.append(f"**基本信息**: 现价{a.price:.2f}元, 涨幅{a.change_pct:+.2f}%, "
                    f"市值{a.float_mv:.1f}亿, {a.board_count}板")
        lines.append("")
        lines.append(f"**风险评估**: {a.risk_level} ({a.risk_score}分)")
        if a.risk_factors:
            lines.append(f"- 风险因素: {', '.join(a.risk_factors)}")
        lines.append("")
        lines.append(f"**操作建议**: **{a.action_cn}** (评分{a.total_score}分)")
        lines.append("")

        if a.action == 'BUY':
            lines.append("**操作策略**:")
            lines.append(f"- 买入区间: {a.buy_price_range[0]:.2f} - {a.buy_price_range[1]:.2f}")
            lines.append(f"- 止损价: {a.stop_loss_price:.2f} (-{a.stop_loss_pct:.0f}%)")
            lines.append(f"- 止盈: +5%卖30%, +10%卖30%, +15%卖40%")
            lines.append(f"- 建议仓位: {a.position_pct*100:.0f}%")
            lines.append("")

        if a.reasons:
            lines.append("**看多理由**:")
            for r in a.reasons:
                lines.append(f"- {r}")
            lines.append("")

        if a.warnings:
            lines.append("**风险提示**:")
            for w in a.warnings:
                lines.append(f"- {w}")
            lines.append("")

    lines.append("---")
    lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    return '\n'.join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="股票顾问 - 龙头情绪选股策略分析")
    parser.add_argument("codes", nargs="*", help="股票代码列表（个股模式）")
    parser.add_argument("--market", default="all", choices=["sha", "sza", "all"], help="市场类型（选股模式，默认全部A股）")
    parser.add_argument("--date", default=None, help="选股日期 (YYYYMMDD)，默认今天")
    parser.add_argument("--no-llm", action="store_true", help="不使用大模型生成分析")
    parser.add_argument("--push", action="store_true", help="推送报告到微信")
    parser.add_argument("--output", default=None, help="输出目录")

    args = parser.parse_args()

    run_stock_advisor(
        codes=args.codes if args.codes else None,
        market=args.market,
        scan_date=args.date,
        use_llm=not args.no_llm,
        output_dir=args.output,
        push=args.push,
    )
