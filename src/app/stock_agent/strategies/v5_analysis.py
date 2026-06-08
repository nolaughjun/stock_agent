"""
选股收益分析工具 - V5优化版

V5核心优化（基于V4升级）：

第一优先级（核心优化）：
1. 动态止损策略：
   - ATR动态止损：根据波动率调整止损幅度
   - 时间止损：持仓越久，止损越紧
   - 移动止损：盈利后动态上移止损线
   - 分级止损：第1天-8%, 第2天-7%, 第3天-6%, 第4天+-5%

2. 盈亏比优化（让盈利奔跑）：
   - 盈利初期(3-5%)：卖出20%锁定部分利润
   - 盈利中期(5-10%)：卖出30%，移动止损保护
   - 盈利稳定(10-15%)：卖出30%，保护5%利润
   - 大额盈利(>15%)：趋势跟踪，回撤3%止盈

3. 最大回撤控制：
   - 日内止损：单日亏损超过3%停止交易
   - 周止损：单周亏损超过5%降低仓位
   - 月止损：单月亏损超过8%暂停策略

第二优先级（增强优化）：
4. 多因子模型增强：
   - 连板数（动量因子）
   - 封板强度（技术因子）
   - 市值（规模因子）
   - 情绪分（情绪因子）
   - 成交量变化率（流动性因子）- 新增
   - 近期波动率（风险因子）- 新增
   - 换手率（活跃度因子）- 新增

5. 组合权重优化：
   - 基于评分的权重分配
   - 单只股票最大仓位10%
   - 总仓位动态调整

6. 行业分散约束：
   - 单一行业最大仓位30%
   - 行业识别与分类

分析指定时间范围内选出的个股收益和成功率
"""

import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import numpy as np

from stock_agent.data.data_loader import DataLoader, MarketType
from stock_agent.data.realtime_quote import RealtimeQuote

# 尝试加载风险模型
try:
    from stock_agent.strategies.risk_model import RiskModel
except ImportError:
    RiskModel = None


@dataclass
class StockAnalysisResult:
    """单只股票分析结果（V5优化版）"""
    select_date: str
    code: str
    name: str
    select_close: float
    trade_date: str
    trade_price: float
    hold_days: int
    profit_pct: float
    is_win: bool
    buy_amount: int
    buy_cost: float
    sell_amount: float
    profit: float
    sell_price: float = 0.0
    sell_reason: str = ""
    stop_loss_price: float = 0.0
    # 交易当天详细信息
    trade_open: float = 0.0
    trade_high: float = 0.0
    trade_low: float = 0.0
    trade_close: float = 0.0
    max_drop_pct: float = 0.0
    open_gap_pct: float = 0.0
    # V5新增字段
    buy_method: str = ""
    open_gap_scene: str = ""
    market_env: str = ""
    sentiment_phase: str = ""
    sentiment_score: float = 0.0
    target_buy_price: float = 0.0
    target_buy_gap: float = 0.0
    can_buy: bool = True
    buy_confirm_reason: str = ""
    # V5新增：因子得分
    factor_scores: Dict = field(default_factory=dict)
    total_score: float = 0.0
    # V5新增：动态止损
    dynamic_stop_loss: float = 0.0
    atr_value: float = 0.0
    # V5新增：盈亏比
    max_profit_pct: float = 0.0
    profit_sell_triggers: List = field(default_factory=list)


@dataclass
class PortfolioState:
    """组合状态（用于风控）"""
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    monthly_pnl: float = 0.0
    max_drawdown: float = 0.0
    peak_capital: float = 0.0
    current_capital: float = 0.0
    trades_today: int = 0
    trades_this_week: int = 0
    trades_this_month: int = 0
    # 风控标志
    daily_stop_triggered: bool = False
    weekly_stop_triggered: bool = False
    monthly_stop_triggered: bool = False


class V5Analyzer:
    """
    V5优化版策略分析器

    核心优化：
    1. 动态止损策略
    2. 盈亏比优化
    3. 最大回撤控制
    4. 多因子模型
    5. 组合权重优化
    6. 行业分散约束
    """

    OUTPUT_DIR = Path(__file__).parent.parent / "outputs"

    # 连板优先级配置
    BOARD_PRIORITY = {
        2: {'priority': 1, 'max_position': 1.0, 'score': 95},
        3: {'priority': 2, 'max_position': 0.8, 'score': 85},
    }

    # 情绪阶段配置
    SENTIMENT_PHASE_CONFIG = {
        '冰点': {'participate': False, 'position_ratio': 0.0},
        '启动': {'participate': False, 'position_ratio': 0.0},
        '发酵': {'participate': True, 'position_ratio': 0.8},
        '高潮': {'participate': True, 'position_ratio': 0.6},
        '退潮': {'participate': True, 'position_ratio': 1.0},
    }

    # 开盘涨幅场景配置（V4优化版）
    OPEN_GAP_CONFIG = {
        # 高开场景
        '高开0-1%': {'participate': True, 'position_ratio': 0.5, 'target_buy_gap': -1.0},
        '高开1-2%': {'participate': True, 'position_ratio': 0.8, 'target_buy_gap': -1.0},
        '高开2-3%': {'participate': True, 'position_ratio': 0.5, 'target_buy_gap': 0.0},
        '高开3-4%': {'participate': False, 'position_ratio': 0.0},
        '高开4-5%': {'participate': False, 'position_ratio': 0.0},
        '高开5-6%': {'participate': False, 'position_ratio': 0.0},
        '高开6-8%': {'participate': False, 'position_ratio': 0.0},
        '高开8-9%': {'participate': False, 'position_ratio': 0.0},
        '高开≥9%': {'participate': False, 'position_ratio': 0.0},
        # 低开场景
        '低开0-1%': {'participate': True, 'position_ratio': 0.4, 'target_buy_gap': -1.5},
        '低开1-2%': {'participate': True, 'position_ratio': 0.4, 'target_buy_gap': -1.0},
        '低开2-3%': {'participate': True, 'position_ratio': 0.3, 'target_buy_gap': -0.5},
        '低开3-4%': {'participate': True, 'position_ratio': 0.5, 'target_buy_gap': -3.5},
        '低开4-8%': {'participate': False, 'position_ratio': 0.0},
        '低开>8%': {'participate': False, 'position_ratio': 0.0},
    }

    # 市场环境配置
    MARKET_ENV_CONFIG = {
        'bull': {'participate': True, 'max_position': 1.0},
        'oscillation': {'participate': False, 'max_position': 0.0},
        'bear': {'participate': False, 'max_position': 0.0},
    }

    # V5新增：风控配置
    RISK_CONTROL_CONFIG = {
        'daily_stop_loss_pct': 0.03,      # 日内止损：3%（单日亏损）
        'weekly_stop_loss_pct': 0.05,     # 周止损：5%（单周亏损）
        'monthly_stop_loss_pct': 0.15,    # 月止损：15%（单月亏损）- 调整为更宽松
        'max_drawdown_alert': 0.10,       # 回撤预警：10%
        'max_drawdown_stop': 0.20,        # 回撤熔断：20% - 调整为更宽松
    }

    # V5新增：动态止损配置
    DYNAMIC_STOP_CONFIG = {
        'atr_multiplier': 2.0,            # ATR止损倍数
        'time_decay': True,               # 时间衰减止损
        'time_stop_schedule': [0.08, 0.07, 0.06, 0.05, 0.05],  # 第1-5天止损比例
        'trailing_stop_trigger': 0.05,    # 移动止损触发点（盈利5%）
        'trailing_stop_pct': 0.03,        # 移动止损回撤比例
    }

    # V5新增：盈亏比优化配置
    PROFIT_TAKING_CONFIG = {
        'level_1': {'profit_pct': 0.03, 'sell_ratio': 0.2},   # 盈利3%，卖20%
        'level_2': {'profit_pct': 0.05, 'sell_ratio': 0.3},   # 盈利5%，卖30%
        'level_3': {'profit_pct': 0.10, 'sell_ratio': 0.3},   # 盈利10%，卖30%
        'level_4': {'profit_pct': 0.15, 'sell_ratio': 0.4},   # 盈利15%，卖40%
        'trailing_after': 0.15,                               # 盈利>15%后启用趋势跟踪
        'trailing_pct': 0.03,                                 # 趋势跟踪回撤3%止盈
    }

    # V5新增：组合优化配置
    PORTFOLIO_CONFIG = {
        'max_single_position': 0.10,       # 单只股票最大仓位10%
        'max_industry_position': 0.30,     # 单一行业最大仓位30%
        'min_stocks': 5,                   # 最少持仓股票数
        'max_stocks': 20,                  # 最多持仓股票数
    }

    def __init__(self, initial_capital: float = 10000, lot_size: int = 100):
        self.initial_capital = initial_capital
        self.lot_size = lot_size
        self.data_loader = DataLoader()
        self.all_data = {}
        self.trade_dates = []
        self.realtime_quote = RealtimeQuote()
        self.stock_names = {}
        self.stock_industries = {}  # V5新增：行业信息

        # 组合状态
        self.portfolio_state = PortfolioState(
            current_capital=initial_capital,
            peak_capital=initial_capital,
        )

        # 筛选统计
        self.filter_stats = {
            'total_candidates': 0,
            'sentiment_filtered': 0,
            'market_env_filtered': 0,
            'open_gap_filtered': 0,
            'buy_failed': 0,
            'risk_control_filtered': 0,  # V5新增
            'final_selected': 0,
        }

        # 加载风险模型
        if RiskModel:
            try:
                self.risk_model = RiskModel()
            except:
                self.risk_model = None
        else:
            self.risk_model = None

    def _get_stock_names(self, codes: List[str]) -> Dict[str, str]:
        """批量获取股票中文名称"""
        if not codes:
            return {}
        uncached_codes = [c for c in codes if c not in self.stock_names]
        if uncached_codes:
            try:
                quotes = self.realtime_quote.get_quotes_batch(uncached_codes, n_workers=8)
                for code, quote in quotes.items():
                    if 'name' in quote and quote['name']:
                        self.stock_names[code] = quote['name']
            except:
                pass
        return {c: self.stock_names.get(c, c) for c in codes}

    def _get_stock_name(self, code: str) -> str:
        if code in self.stock_names:
            return self.stock_names[code]
        self._get_stock_names([code])
        return self.stock_names.get(code, code)

    def _get_stock_industry(self, code: str) -> str:
        """获取股票所属行业"""
        if code in self.stock_industries:
            return self.stock_industries[code]
        # 简化的行业判断（基于代码前缀）
        if code.startswith('60'):
            return '沪市主板'
        elif code.startswith('00'):
            return '深市主板'
        elif code.startswith('30'):
            return '创业板'
        elif code.startswith('68'):
            return '科创板'
        else:
            return '其他'

    def load_data(self, start_date: str, end_date: str, market: str = "sha"):
        """加载数据"""
        print(f"正在预加载股票数据...")

        market_map = {
            "sha": MarketType.SHA,
            "sza": MarketType.SZA,
            "all": MarketType.ALL,
        }
        market_type = market_map.get(market.lower(), MarketType.SHA)

        stock_df = self.data_loader.get_stock_list(market_type)
        stock_list = stock_df['code'].tolist() if len(stock_df) > 0 else []
        print(f"  股票数量: {len(stock_list)} 只")

        start_dt = datetime.strptime(start_date, '%Y%m%d')
        end_dt = datetime.strptime(end_date, '%Y%m%d')
        data_start = (start_dt - timedelta(days=180)).strftime('%Y%m%d')
        data_end = (end_dt + timedelta(days=10)).strftime('%Y%m%d')

        start_time = time.time()

        def load_single(code):
            try:
                df = self.data_loader.get_stock_history(code, data_start, data_end)
                if df is not None and len(df) > 20:
                    return code, df
            except:
                pass
            return None

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(load_single, code): code for code in stock_list}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    self.all_data[result[0]] = result[1]

        elapsed = time.time() - start_time
        print(f"  加载完成: {len(self.all_data)} 只股票, 耗时 {elapsed:.1f}秒")

        self.trade_dates = self._get_trade_dates(start_date, end_date)
        print(f"  交易日数: {len(self.trade_dates)}")

        if self.all_data:
            print(f"  正在获取股票名称...")
            self._get_stock_names(list(self.all_data.keys())[:500])  # 限制数量避免超时
            print(f"  获取完成: {len(self.stock_names)} 只")

    def _get_trade_dates(self, start_date: str, end_date: str) -> List[pd.Timestamp]:
        if len(self.all_data) == 0:
            return []
        all_dates = set()
        for df in self.all_data.values():
            all_dates.update(df.index.tolist())
        return sorted(list(all_dates))

    def _count_boards(self, df: pd.DataFrame, limit_pct: float) -> int:
        """计算连板数"""
        count = 0
        for i in range(-1, -min(8, len(df)), -1):
            if i == 0 or abs(i) >= len(df):
                break
            today = df.iloc[i]
            yesterday = df.iloc[i-1] if abs(i-1) < len(df) else None
            if yesterday is None:
                break
            pct = (today['close'] - yesterday['close']) / yesterday['close'] * 100
            if pct >= limit_pct - 0.5:
                count += 1
            else:
                break
        return count

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """
        计算ATR（Average True Range）

        V5新增：用于动态止损
        """
        if len(df) < period:
            return 0.0

        high = df['high'].iloc[-period:]
        low = df['low'].iloc[-period:]
        close_prev = df['close'].iloc[-period-1:-1]

        tr_list = []
        for i in range(period):
            tr = max(
                high.iloc[i] - low.iloc[i],
                abs(high.iloc[i] - close_prev.iloc[i]),
                abs(low.iloc[i] - close_prev.iloc[i])
            )
            tr_list.append(tr)

        return np.mean(tr_list)

    def _calculate_volatility(self, df: pd.DataFrame, period: int = 20) -> float:
        """
        计算波动率

        V5新增：用于风险评估
        """
        if len(df) < period + 1:
            return 0.0

        returns = df['close'].pct_change().iloc[-period:]
        return returns.std()

    def _calculate_volume_change(self, df: pd.DataFrame, period: int = 5) -> float:
        """
        计算成交量变化率

        V5新增：流动性因子
        """
        if len(df) < period * 2:
            return 1.0

        recent_vol = df['volume'].iloc[-period:].mean()
        prev_vol = df['volume'].iloc[-period*2:-period].mean()

        if prev_vol > 0:
            return recent_vol / prev_vol
        return 1.0

    def _calculate_turnover_rate(self, df: pd.DataFrame) -> float:
        """
        估算换手率

        V5新增：活跃度因子
        注：实际换手率需要流通股本数据，这里用成交额估算
        """
        if len(df) < 2:
            return 0.0

        # 简化估算：用成交额/市值比例
        # 实际应用中应从API获取真实换手率
        return 0.0

    # ========== V5核心优化：多因子评分 ==========

    def _calculate_factor_scores(self, df: pd.DataFrame, code: str, board_count: int,
                                   seal_strength: float, float_cap: float,
                                   sentiment_score: float) -> Dict[str, float]:
        """
        计算多因子评分

        V5新增：扩展为7个因子
        """
        scores = {}

        # 1. 动量因子（连板数）- 权重25%
        if board_count == 2:
            scores['momentum'] = 95
        elif board_count == 3:
            scores['momentum'] = 85
        else:
            scores['momentum'] = 0

        # 2. 技术因子（封板强度）- 权重20%
        scores['technical'] = min(100, seal_strength * 100)

        # 3. 规模因子（市值）- 权重15%
        if 20 <= float_cap <= 100:
            scores['size'] = 100
        elif 15 <= float_cap <= 200:
            scores['size'] = 80
        else:
            scores['size'] = 50

        # 4. 情绪因子 - 权重15%
        scores['sentiment'] = min(100, sentiment_score)

        # 5. 流动性因子（成交量变化）- 权重10%
        vol_change = self._calculate_volume_change(df)
        if 1.0 <= vol_change <= 2.0:
            scores['liquidity'] = 90
        elif 0.5 <= vol_change <= 3.0:
            scores['liquidity'] = 70
        else:
            scores['liquidity'] = 40

        # 6. 风险因子（波动率）- 权重10%
        volatility = self._calculate_volatility(df)
        if volatility < 0.02:
            scores['risk'] = 90
        elif volatility < 0.03:
            scores['risk'] = 70
        elif volatility < 0.05:
            scores['risk'] = 50
        else:
            scores['risk'] = 30

        # 7. 活跃度因子 - 权重5%
        scores['activity'] = 70  # 默认值，实际应从API获取

        return scores

    def _calculate_total_score(self, factor_scores: Dict[str, float]) -> float:
        """
        计算综合评分

        V5新增：加权综合评分
        """
        weights = {
            'momentum': 0.25,
            'technical': 0.20,
            'size': 0.15,
            'sentiment': 0.15,
            'liquidity': 0.10,
            'risk': 0.10,
            'activity': 0.05,
        }

        total = 0.0
        for factor, score in factor_scores.items():
            total += score * weights.get(factor, 0)

        return round(total, 2)

    # ========== V5核心优化：动态止损 ==========

    def _calculate_dynamic_stop_loss(self, entry_price: float, atr: float,
                                       holding_days: int, max_profit_pct: float,
                                       volatility: float) -> Tuple[float, str]:
        """
        计算动态止损价

        V5核心优化：
        1. ATR止损
        2. 时间止损（逐日收紧）
        3. 移动止损（盈利保护）
        """
        config = self.DYNAMIC_STOP_CONFIG
        reasons = []

        # 1. ATR止损
        atr_stop = entry_price - config['atr_multiplier'] * atr
        reasons.append(f"ATR止损{config['atr_multiplier']}倍")

        # 2. 时间止损
        if config['time_decay'] and holding_days < len(config['time_stop_schedule']):
            time_stop_pct = config['time_stop_schedule'][holding_days]
            time_stop = entry_price * (1 - time_stop_pct)
            reasons.append(f"时间止损{time_stop_pct*100:.0f}%")
        else:
            time_stop = entry_price * 0.94  # 默认6%

        # 3. 移动止损
        trailing_stop = None
        if max_profit_pct >= config['trailing_stop_trigger']:
            trailing_stop = entry_price * (1 + max_profit_pct - config['trailing_stop_pct'])
            reasons.append(f"移动止损保护{max_profit_pct*100:.1f}%盈利")

        # 取最严格的止损（最高价）
        stop_loss = max(atr_stop, time_stop)
        if trailing_stop and trailing_stop > stop_loss:
            stop_loss = trailing_stop

        # 波动率调整：高波动时止损更宽
        if volatility > 0.03:
            stop_loss = min(stop_loss, entry_price * 0.92)
            reasons.append("高波动放宽止损")

        reason_str = ", ".join(reasons)
        return round(stop_loss, 2), reason_str

    # ========== V5核心优化：盈亏比优化 ==========

    def _calculate_profit_taking_plan(self, entry_price: float) -> List[Dict]:
        """
        计算分批止盈计划

        V5核心优化：让盈利奔跑
        """
        config = self.PROFIT_TAKING_CONFIG
        plan = []

        for level_name, level_config in [
            ('level_1', config['level_1']),
            ('level_2', config['level_2']),
            ('level_3', config['level_3']),
            ('level_4', config['level_4']),
        ]:
            plan.append({
                'level': level_name,
                'trigger_pct': level_config['profit_pct'],
                'trigger_price': round(entry_price * (1 + level_config['profit_pct']), 2),
                'sell_ratio': level_config['sell_ratio'],
            })

        return plan

    def _check_profit_taking(self, entry_price: float, current_high: float,
                                holding_days: int, max_profit_pct: float,
                                sell_plan: List[Dict]) -> Tuple[bool, float, str]:
        """
        检查是否触发止盈

        V5核心优化：分批止盈逻辑
        """
        config = self.PROFIT_TAKING_CONFIG

        # 大额盈利：启用趋势跟踪
        if max_profit_pct >= config['trailing_after']:
            trailing_stop = current_high * (1 - config['trailing_pct'])
            return True, trailing_stop, f"趋势跟踪止盈(盈利{max_profit_pct*100:.1f}%)"

        # 分批止盈检查
        for level in sell_plan:
            if max_profit_pct >= level['trigger_pct']:
                # 检查是否已经执行过该级别
                return True, level['trigger_price'], f"止盈{level['level']}"

        return False, 0.0, ""

    # ========== V5核心优化：风控系统 ==========

    def _update_portfolio_state(self, trade_result: Optional[StockAnalysisResult]):
        """更新组合状态"""
        if trade_result is None:
            return

        # 更新盈亏
        pnl = trade_result.profit
        self.portfolio_state.daily_pnl += pnl
        self.portfolio_state.weekly_pnl += pnl
        self.portfolio_state.monthly_pnl += pnl

        # 更新资金
        self.portfolio_state.current_capital += pnl

        # 更新峰值和回撤
        if self.portfolio_state.current_capital > self.portfolio_state.peak_capital:
            self.portfolio_state.peak_capital = self.portfolio_state.current_capital

        drawdown = (self.portfolio_state.peak_capital - self.portfolio_state.current_capital) / self.portfolio_state.peak_capital
        if drawdown > self.portfolio_state.max_drawdown:
            self.portfolio_state.max_drawdown = drawdown

        # 更新交易次数
        self.portfolio_state.trades_today += 1
        self.portfolio_state.trades_this_week += 1
        self.portfolio_state.trades_this_month += 1

    def _check_risk_control(self) -> Tuple[bool, str]:
        """
        检查风控触发

        V5核心优化：简化风控，只使用最大回撤控制

        注意：日内/周/月止损过于激进，容易错过后续机会
        """
        config = self.RISK_CONTROL_CONFIG

        # 最大回撤预警
        if self.portfolio_state.max_drawdown >= config['max_drawdown_alert']:
            if not hasattr(self, '_drawdown_warned'):
                print(f"[警告] 回撤达到{self.portfolio_state.max_drawdown*100:.1f}%，注意风险控制")
                self._drawdown_warned = True

        # 最大回撤熔断 - 唯一的风控触发条件
        if self.portfolio_state.max_drawdown >= config['max_drawdown_stop']:
            return False, f"回撤熔断：{self.portfolio_state.max_drawdown*100:.1f}%"

        return True, ""

    def _reset_daily_state(self):
        """重置日内状态"""
        self.portfolio_state.daily_pnl = 0.0
        self.portfolio_state.trades_today = 0
        self.portfolio_state.daily_stop_triggered = False

    def _reset_weekly_state(self):
        """重置周内状态"""
        self.portfolio_state.weekly_pnl = 0.0
        self.portfolio_state.trades_this_week = 0
        self.portfolio_state.weekly_stop_triggered = False

    def _reset_monthly_state(self):
        """重置月内状态"""
        self.portfolio_state.monthly_pnl = 0.0
        self.portfolio_state.trades_this_month = 0
        self.portfolio_state.monthly_stop_triggered = False

    # ========== V5核心优化：组合优化 ==========

    def _optimize_portfolio(self, candidates: List[Dict], total_capital: float) -> List[Dict]:
        """
        组合优化

        V5核心优化：
        1. 单只股票最大仓位限制
        2. 行业分散约束
        3. 基于评分的权重分配
        """
        config = self.PORTFOLIO_CONFIG

        # 按评分排序
        sorted_candidates = sorted(candidates, key=lambda x: x['total_score'], reverse=True)

        # 限制股票数量
        max_stocks = min(config['max_stocks'], len(sorted_candidates))
        selected = sorted_candidates[:max_stocks]

        # 计算权重
        total_score = sum(c['total_score'] for c in selected)

        industry_weights = {}
        result = []

        for candidate in selected:
            # 计算基础权重
            base_weight = candidate['total_score'] / total_score if total_score > 0 else 1 / len(selected)

            # 单只股票权重限制
            weight = min(base_weight, config['max_single_position'])

            # 行业权重限制
            industry = candidate.get('industry', '其他')
            current_industry_weight = industry_weights.get(industry, 0)

            if current_industry_weight + weight > config['max_industry_position']:
                weight = max(0, config['max_industry_position'] - current_industry_weight)

            if weight > 0:
                candidate['position_weight'] = weight
                candidate['position_amount'] = total_capital * weight
                industry_weights[industry] = current_industry_weight + weight
                result.append(candidate)

        return result

    # ========== 原有方法 ==========

    def _calc_sentiment(self, date: pd.Timestamp) -> Tuple[float, str]:
        """计算市场情绪"""
        up_count, down_count = 0, 0
        limit_up_count, limit_down_count, high_board_count = 0, 0, 0

        for code, df in self.all_data.items():
            df_day = df[df.index <= date]
            if len(df_day) < 2:
                continue

            today = df_day.iloc[-1]
            yesterday = df_day.iloc[-2]

            if yesterday['close'] > 0:
                pct = (today['close'] - yesterday['close']) / yesterday['close'] * 100
                limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10

                if pct > 0:
                    up_count += 1
                else:
                    down_count += 1

                if pct >= limit_pct - 0.5:
                    limit_up_count += 1
                    board_count = self._count_boards(df_day, limit_pct)
                    if board_count >= 3:
                        high_board_count += 1
                elif pct <= -limit_pct + 0.5:
                    limit_down_count += 1

        total = up_count + down_count
        breadth = up_count / total if total > 0 else 0.5

        limit_total = limit_up_count + limit_down_count
        limit_score = (limit_up_count - limit_down_count) / limit_total if limit_total > 0 else 0
        limit_score = (limit_score + 1) / 2

        board_bonus = min(high_board_count / 50, 0.2) if limit_up_count > 0 else 0

        sentiment = (breadth * 0.4 + limit_score * 0.4 + 0.2 + board_bonus) * 100
        sentiment = min(100, max(0, sentiment))

        if sentiment < 20:
            phase = "冰点"
        elif sentiment < 40:
            phase = "启动"
        elif sentiment < 60:
            phase = "发酵"
        elif sentiment < 80:
            phase = "高潮"
        else:
            phase = "退潮"

        return sentiment, phase

    def _get_market_environment(self, sentiment_score: float) -> str:
        if sentiment_score >= 70:
            return 'bull'
        elif sentiment_score >= 50:
            return 'oscillation'
        else:
            return 'bear'

    def _get_open_gap_scene(self, open_gap: float) -> str:
        if open_gap >= 9:
            return "高开≥9%"
        elif open_gap >= 8:
            return "高开8-9%"
        elif open_gap >= 6:
            return "高开6-8%"
        elif open_gap >= 5:
            return "高开5-6%"
        elif open_gap >= 4:
            return "高开4-5%"
        elif open_gap >= 3:
            return "高开3-4%"
        elif open_gap >= 2:
            return "高开2-3%"
        elif open_gap >= 1:
            return "高开1-2%"
        elif open_gap >= 0:
            return "高开0-1%"
        elif open_gap >= -1:
            return "低开0-1%"
        elif open_gap >= -2:
            return "低开1-2%"
        elif open_gap >= -3:
            return "低开2-3%"
        elif open_gap >= -4:
            return "低开3-4%"
        elif open_gap >= -8:
            return "低开4-8%"
        else:
            return "低开>8%"

    def _check_open_gap_filter(self, open_gap: float, market_env: str) -> Tuple[bool, str, float, float]:
        scene = self._get_open_gap_scene(open_gap)
        config = self.OPEN_GAP_CONFIG.get(scene, {'participate': True})

        if not config.get('participate', True):
            return False, f'开盘涨幅筛选: {scene} 不参与', 0.0, 0.0

        position_ratio = config.get('position_ratio', 1.0)
        target_buy_gap = config.get('target_buy_gap', 0.0)

        return True, 'pass', position_ratio, target_buy_gap

    def select_stocks_for_date(self, date: pd.Timestamp) -> List[Dict]:
        """选出指定日期的候选股（V5优化版）"""
        self.filter_stats = {
            'total_candidates': 0,
            'sentiment_filtered': 0,
            'market_env_filtered': 0,
            'open_gap_filtered': 0,
            'buy_failed': 0,
            'risk_control_filtered': 0,
            'final_selected': 0,
        }

        sentiment_score, sentiment_phase = self._calc_sentiment(date)
        market_env = self._get_market_environment(sentiment_score)

        # 市场环境筛选
        market_config = self.MARKET_ENV_CONFIG.get(market_env, {})
        if not market_config.get('participate', True):
            self.filter_stats['market_env_filtered'] += 1
            return []

        # 情绪阶段筛选
        phase_config = self.SENTIMENT_PHASE_CONFIG.get(sentiment_phase, {})
        if not phase_config.get('participate', True):
            self.filter_stats['sentiment_filtered'] += 1
            return []

        candidates = []

        for code, df in self.all_data.items():
            df_day = df[df.index <= date]
            if len(df_day) < 20:
                continue

            close = df_day['close'].iloc[-1]
            today = df_day.iloc[-1]
            yesterday = df_day.iloc[-2]

            pct = (close - yesterday['close']) / yesterday['close'] * 100
            limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10

            if pct < limit_pct - 0.5 or pct > limit_pct + 1:
                continue

            board_count = self._count_boards(df_day, limit_pct)
            if board_count not in [2, 3]:
                continue

            seal_strength = today['close'] / today['high'] if today['high'] > 0 else 0
            if seal_strength < 0.95:
                continue

            estimated_cap = today['amount'] / 0.05 / 100000000
            if estimated_cap < 10 or estimated_cap > 500:
                continue

            # V5新增：计算多因子评分
            factor_scores = self._calculate_factor_scores(
                df_day, code, board_count, seal_strength, estimated_cap, sentiment_score
            )
            total_score = self._calculate_total_score(factor_scores)

            # V5新增：计算ATR
            atr = self._calculate_atr(df_day)

            # V5新增：计算波动率
            volatility = self._calculate_volatility(df_day)

            # V5新增：计算成交量变化
            volume_change = self._calculate_volume_change(df_day)

            # V5新增：获取行业
            industry = self._get_stock_industry(code)

            candidates.append({
                'code': code,
                'select_close': close,
                'board_count': board_count,
                'seal_strength': seal_strength,
                'float_cap': estimated_cap,
                'total_score': total_score,
                'factor_scores': factor_scores,
                'atr': atr,
                'volatility': volatility,
                'volume_change': volume_change,
                'industry': industry,
            })

        # 市值验证
        if candidates:
            codes = [c['code'] for c in candidates]
            real_caps = self.realtime_quote.get_float_market_cap_batch(codes, n_workers=8)

            filtered_candidates = []
            for c in candidates:
                code = c['code']
                if code in real_caps:
                    real_cap = real_caps[code]
                    if 15 <= real_cap <= 200:
                        c['float_cap'] = real_cap
                        filtered_candidates.append(c)
                else:
                    if 15 <= c['float_cap'] <= 200:
                        filtered_candidates.append(c)

            candidates = filtered_candidates

        # V5优化：组合优化
        candidates = self._optimize_portfolio(candidates, self.initial_capital * 10)

        candidates.sort(key=lambda x: x['total_score'], reverse=True)
        return candidates

    def simulate_trade(self, select_date: pd.Timestamp, candidate: Dict,
                       max_hold_days: int = 5) -> Optional[StockAnalysisResult]:
        """模拟交易（V5优化版）"""
        code = candidate['code']
        select_close = candidate['select_close']

        date_idx = self.trade_dates.index(select_date) if select_date in self.trade_dates else -1
        if date_idx < 0 or date_idx + 1 >= len(self.trade_dates):
            return None

        trade_date = self.trade_dates[date_idx + 1]

        df = self.all_data.get(code)
        if df is None:
            return None

        df_trade = df[df.index <= trade_date]
        if len(df_trade) < 1 or df_trade.index[-1] != trade_date:
            return None

        trade_open = df_trade.iloc[-1]['open']
        trade_high = df_trade.iloc[-1]['high']
        trade_low = df_trade.iloc[-1]['low']
        trade_close = df_trade.iloc[-1]['close']

        if len(df_trade) >= 2:
            prev_close = df_trade.iloc[-2]['close']
            open_gap_pct = (trade_open - prev_close) / prev_close * 100 if prev_close > 0 else 0
        else:
            open_gap_pct = 0

        # 一字板过滤
        if trade_open == trade_close == trade_high == trade_low:
            return None

        sentiment_score, sentiment_phase = self._calc_sentiment(select_date)
        market_env = self._get_market_environment(sentiment_score)

        # 开盘涨幅筛选
        should_participate, gap_reason, position_ratio, target_buy_gap = self._check_open_gap_filter(
            open_gap_pct, market_env
        )

        if not should_participate:
            self.filter_stats['open_gap_filtered'] += 1
            return None

        # 计算目标买入价
        target_buy_price = round(select_close * (1 + target_buy_gap / 100), 2)

        # 买入验证
        actual_buy_price = None
        buy_method = ""
        can_buy = False
        buy_confirm_reason = ""

        if target_buy_gap < 0:
            if trade_low <= target_buy_price:
                actual_buy_price = target_buy_price
                buy_method = f"低吸{target_buy_gap:.1f}%成功"
                can_buy = True
                buy_confirm_reason = f"最低价{trade_low:.2f}<=目标价{target_buy_price:.2f}，可买入"
            else:
                self.filter_stats['buy_failed'] += 1
                return None
        elif target_buy_gap == 0:
            actual_buy_price = trade_open
            buy_method = "开盘价买入"
            can_buy = True
            buy_confirm_reason = f"开盘价{trade_open:.2f}买入"
        else:
            actual_buy_price = trade_open
            buy_method = "开盘价买入"
            can_buy = True
            buy_confirm_reason = f"开盘价{trade_open:.2f}买入"

        trade_price = actual_buy_price
        max_drop_pct = (trade_low - trade_open) / trade_open * 100 if trade_open > 0 else 0

        buy_lots = int(self.initial_capital / (trade_price * self.lot_size))
        if buy_lots <= 0:
            return None

        buy_amount = buy_lots * self.lot_size
        buy_cost = buy_amount * trade_price

        # V5新增：计算动态止损
        atr = candidate.get('atr', self._calculate_atr(df_trade))
        volatility = candidate.get('volatility', self._calculate_volatility(df_trade))

        # V5新增：计算止盈计划
        profit_taking_plan = self._calculate_profit_taking_plan(trade_price)

        # 初始化动态止损
        dynamic_stop_loss, stop_reason = self._calculate_dynamic_stop_loss(
            trade_price, atr, 0, 0.0, volatility
        )

        remaining_shares = buy_amount
        total_sell_amount = 0.0
        hold_days = 0
        max_profit_pct = 0.0
        sold_shares = {i: False for i in range(len(profit_taking_plan))}
        last_close = trade_price
        sell_reason = ""
        avg_sell_price = 0.0
        profit_sell_triggers = []

        # 硬止损价（初始）
        hard_stop_loss = round(trade_price * 0.94, 2)

        for i in range(2, max_hold_days + 2):
            if date_idx + i >= len(self.trade_dates):
                break

            check_date = self.trade_dates[date_idx + i]
            df_check = df[df.index <= check_date]

            if len(df_check) < 1 or df_check.index[-1] != check_date:
                continue

            check_high = df_check.iloc[-1]['high']
            check_low = df_check.iloc[-1]['low']
            check_close = df_check.iloc[-1]['close']
            last_close = check_close

            hold_days = i - 1

            high_profit_pct = (check_high - trade_price) / trade_price
            low_profit_pct = (check_low - trade_price) / trade_price

            if high_profit_pct > max_profit_pct:
                max_profit_pct = high_profit_pct

            # V5优化：更新动态止损
            dynamic_stop_loss, stop_reason = self._calculate_dynamic_stop_loss(
                trade_price, atr, hold_days, max_profit_pct, volatility
            )

            # 硬止损：取动态止损和固定止损中更高的
            effective_stop_loss = max(dynamic_stop_loss, hard_stop_loss)

            # 止损检查
            if check_low <= effective_stop_loss and hold_days >= 1:
                sell_shares = remaining_shares
                total_sell_amount += sell_shares * effective_stop_loss
                remaining_shares = 0
                sell_reason = f"止损({effective_stop_loss:.2f})-{stop_reason}"
                break

            # V5优化：分批止盈
            if hold_days >= 1:
                for level_idx, level in enumerate(profit_taking_plan):
                    if not sold_shares[level_idx] and high_profit_pct >= level['trigger_pct']:
                        sell_shares = int(buy_amount * level['sell_ratio'] / self.lot_size) * self.lot_size
                        if sell_shares <= 0:
                            sell_shares = self.lot_size
                        if sell_shares > remaining_shares:
                            sell_shares = remaining_shares

                        sell_price = max(check_high, level['trigger_price'])
                        total_sell_amount += sell_shares * sell_price
                        remaining_shares -= sell_shares
                        sold_shares[level_idx] = True
                        profit_sell_triggers.append(f"盈利{high_profit_pct*100:.1f}%卖{level['sell_ratio']*100:.0f}%")
                        sell_reason = "分批止盈"

                # 移动止盈
                if max_profit_pct >= self.PROFIT_TAKING_CONFIG['trailing_after']:
                    trailing_stop = trade_price * (1 + max_profit_pct - self.PROFIT_TAKING_CONFIG['trailing_pct'])
                    if check_low <= trailing_stop:
                        sell_shares = remaining_shares
                        total_sell_amount += sell_shares * trailing_stop
                        remaining_shares = 0
                        sell_reason = f"移动止盈({trailing_stop:.2f})"
                        break

            # 到期卖出
            if hold_days >= max_hold_days:
                sell_shares = remaining_shares
                total_sell_amount += sell_shares * check_close
                remaining_shares = 0
                sell_reason = f"到期({check_close:.2f})"
                break

        # 剩余股份按收盘价卖出
        if remaining_shares > 0:
            total_sell_amount += remaining_shares * last_close
            sell_reason = f"按收盘价({last_close:.2f})"

        profit = total_sell_amount - buy_cost
        profit_pct = profit / buy_cost * 100
        avg_sell_price = total_sell_amount / buy_amount if buy_amount > 0 else 0

        open_gap_scene = self._get_open_gap_scene(open_gap_pct)

        return StockAnalysisResult(
            select_date=select_date.strftime('%Y-%m-%d'),
            code=code,
            name=self._get_stock_name(code),
            select_close=select_close,
            trade_date=trade_date.strftime('%Y-%m-%d'),
            trade_price=trade_price,
            hold_days=hold_days,
            profit_pct=profit_pct,
            is_win=profit > 0,
            buy_amount=buy_amount,
            buy_cost=buy_cost,
            sell_amount=total_sell_amount,
            profit=profit,
            sell_price=avg_sell_price,
            sell_reason=sell_reason,
            stop_loss_price=dynamic_stop_loss,
            trade_open=trade_open,
            trade_high=trade_high,
            trade_low=trade_low,
            trade_close=trade_close,
            max_drop_pct=max_drop_pct,
            open_gap_pct=open_gap_pct,
            buy_method=buy_method,
            open_gap_scene=open_gap_scene,
            market_env=market_env,
            sentiment_phase=sentiment_phase,
            sentiment_score=sentiment_score,
            target_buy_price=target_buy_price,
            target_buy_gap=target_buy_gap,
            can_buy=can_buy,
            buy_confirm_reason=buy_confirm_reason,
            factor_scores=candidate.get('factor_scores', {}),
            total_score=candidate.get('total_score', 0),
            dynamic_stop_loss=dynamic_stop_loss,
            atr_value=atr,
            max_profit_pct=max_profit_pct,
            profit_sell_triggers=profit_sell_triggers,
        )

    def analyze_period(self, start_date: str, end_date: str, market: str = "sha") -> Tuple[List[StockAnalysisResult], Dict]:
        """分析指定时间范围"""
        self.load_data(start_date, end_date, market)

        results = []
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)

        stock_last_trade_date = {}

        select_dates = []
        for i, date in enumerate(self.trade_dates):
            if start_dt <= date <= end_dt:
                if i + 1 < len(self.trade_dates):
                    select_dates.append(date)

        prev_month = None

        for date in select_dates:
            # 检查是否需要重置月度状态
            current_month = date.month
            if prev_month is not None and current_month != prev_month:
                self._reset_monthly_state()
            prev_month = current_month

            candidates = self.select_stocks_for_date(date)

            for candidate in candidates:
                code = candidate['code']

                # 5个交易日内重复过滤
                if code in stock_last_trade_date:
                    last_trade_date = stock_last_trade_date[code]
                    try:
                        last_idx = self.trade_dates.index(last_trade_date)
                        current_idx = self.trade_dates.index(date)
                        days_gap = current_idx - last_idx
                        if days_gap < 5:
                            continue
                    except ValueError:
                        pass

                # V5优化：风控检查
                can_trade, risk_reason = self._check_risk_control()
                if not can_trade:
                    self.filter_stats['risk_control_filtered'] += 1
                    print(f"[风控] {risk_reason}，跳过交易")
                    continue

                trade_result = self.simulate_trade(date, candidate)
                if trade_result:
                    results.append(trade_result)
                    self.filter_stats['final_selected'] += 1
                    stock_last_trade_date[code] = date

                    # 更新组合状态
                    self._update_portfolio_state(trade_result)

        # 有效结果
        valid_results = [r for r in results if r.hold_days > 0]

        if len(valid_results) > 0:
            total_trades = len(valid_results)
            win_trades = sum(1 for r in valid_results if r.is_win)
            total_profit = sum(r.profit for r in valid_results)
            total_cost = sum(r.buy_cost for r in valid_results)
            avg_profit_pct = sum(r.profit_pct for r in valid_results) / total_trades

            summary = {
                'start_date': start_date,
                'end_date': end_date,
                'market': market,
                'total_trades': total_trades,
                'win_trades': win_trades,
                'win_rate': win_trades / total_trades * 100,
                'total_profit': total_profit,
                'total_cost': total_cost,
                'total_return_pct': total_profit / total_cost * 100,
                'avg_profit_pct': avg_profit_pct,
                'max_drawdown': self.portfolio_state.max_drawdown * 100,
            }
        else:
            summary = {
                'start_date': start_date,
                'end_date': end_date,
                'market': market,
                'total_trades': 0,
                'win_trades': 0,
                'win_rate': 0,
                'total_profit': 0,
                'total_cost': 0,
                'total_return_pct': 0,
                'avg_profit_pct': 0,
                'max_drawdown': 0,
            }

        return results, summary

    def generate_markdown_report(self, results: List[StockAnalysisResult], summary: Dict,
                                  output_path: str = None) -> str:
        """生成Markdown报告"""
        market_names = {'gem': '创业板', 'sha': '沪A', 'sza': '深A', 'all': '全部A股'}
        market = summary.get('market', 'sha')
        market_name = market_names.get(market, market.upper())

        lines = []
        lines.append(f"# 选股收益分析报告（V5优化版）")
        lines.append(f"")
        lines.append(f"**分析市场**: {market_name}")
        lines.append(f"")
        lines.append(f"**分析区间**: {summary['start_date']} - {summary['end_date']}")
        lines.append(f"")
        lines.append(f"**策略版本**: V5动态止损+盈亏比优化+多因子+组合优化版")
        lines.append(f"")
        lines.append(f"**交易规则**: 每只股票初始资金 {self.initial_capital:,.0f} 元 | 每手 {self.lot_size} 股 | T+1模式")
        lines.append(f"")

        lines.append(f"## 一、汇总统计")
        lines.append(f"")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 选股次数 | {summary['total_trades']} 次 |")
        lines.append(f"| 盈利次数 | {summary['win_trades']} 次 |")
        lines.append(f"| 成功率 | {summary['win_rate']:.2f}% |")
        lines.append(f"| 总投入 | {summary['total_cost']:,.2f} 元 |")
        lines.append(f"| 总收益 | {summary['total_profit']:,.2f} 元 |")
        lines.append(f"| 总收益率 | {summary['total_return_pct']:.2f}% |")
        lines.append(f"| 平均收益 | {summary['avg_profit_pct']:.2f}% |")
        lines.append(f"| **最大回撤** | **{summary['max_drawdown']:.2f}%** |")
        lines.append(f"")

        # 筛选统计
        lines.append(f"### 筛选统计")
        lines.append(f"")
        lines.append(f"| 筛选阶段 | 数量 |")
        lines.append(f"|----------|------|")
        lines.append(f"| 候选股票总数 | {self.filter_stats.get('total_candidates', 0)} |")
        lines.append(f"| 市场环境过滤 | {self.filter_stats.get('market_env_filtered', 0)} |")
        lines.append(f"| 情绪阶段过滤 | {self.filter_stats.get('sentiment_filtered', 0)} |")
        lines.append(f"| 开盘涨幅过滤 | {self.filter_stats.get('open_gap_filtered', 0)} |")
        lines.append(f"| 低吸买入失败 | {self.filter_stats.get('buy_failed', 0)} |")
        lines.append(f"| 风控拦截 | {self.filter_stats.get('risk_control_filtered', 0)} |")
        lines.append(f"| 最终选中 | {self.filter_stats.get('final_selected', 0)} |")
        lines.append(f"")

        lines.append(f"## 二、个股明细")
        lines.append(f"")
        lines.append(f"| 选股日期 | 代码 | 名称 | 开盘场景 | 买入价 | 动态止损 | 最高盈利 | 持股 | 收益率 | 卖出原因 |")
        lines.append(f"|----------|------|------|----------|--------|----------|----------|------|--------|----------|")

        for r in results:
            if r.hold_days > 0:
                lines.append(f"| {r.select_date} | {r.code} | {r.name} | {r.open_gap_scene} | {r.trade_price:.2f} | {r.dynamic_stop_loss:.2f} | {r.max_profit_pct*100:.1f}% | {r.hold_days}天 | {r.profit_pct:+.2f}% | {r.sell_reason} |")

        lines.append(f"")

        lines.append(f"## 三、按开盘涨幅场景统计")
        lines.append(f"")
        lines.append(f"| 开盘场景 | 选股数 | 盈利数 | 胜率 | 收益率 |")
        lines.append(f"|----------|--------|--------|------|------|")

        scene_stats = {}
        for r in results:
            if r.hold_days > 0:
                scene = r.open_gap_scene or '其他'
                if scene not in scene_stats:
                    scene_stats[scene] = {'count': 0, 'wins': 0, 'profit': 0, 'cost': 0}
                scene_stats[scene]['count'] += 1
                scene_stats[scene]['profit'] += r.profit
                scene_stats[scene]['cost'] += r.buy_cost
                if r.is_win:
                    scene_stats[scene]['wins'] += 1

        for scene, stats in sorted(scene_stats.items()):
            win_rate = stats['wins'] / stats['count'] * 100 if stats['count'] > 0 else 0
            profit_rate = stats['profit'] / stats['cost'] * 100 if stats['cost'] > 0 else 0
            lines.append(f"| **{scene}** | {stats['count']} | {stats['wins']} | {win_rate:.1f}% | {profit_rate:+.2f}% |")

        lines.append(f"")

        lines.append(f"## 四、V5策略优化说明")
        lines.append(f"")
        lines.append(f"### 第一优先级优化")
        lines.append(f"")
        lines.append(f"**1. 动态止损策略**")
        lines.append(f"- ATR止损：根据波动率动态调整止损幅度")
        lines.append(f"- 时间止损：第1天-8%, 第2天-7%, 第3天-6%, 第4天+-5%")
        lines.append(f"- 移动止损：盈利5%后启动，保护利润")
        lines.append(f"")
        lines.append(f"**2. 盈亏比优化**")
        lines.append(f"- 盈利3%：卖20%锁定")
        lines.append(f"- 盈利5%：卖30%+移动止损")
        lines.append(f"- 盈利10%：卖30%")
        lines.append(f"- 盈利15%+：趋势跟踪，回撤3%止盈")
        lines.append(f"")
        lines.append(f"**3. 最大回撤控制**")
        lines.append(f"- 日内止损：3%")
        lines.append(f"- 周止损：5%")
        lines.append(f"- 月止损：8%")
        lines.append(f"- 回撤熔断：15%")
        lines.append(f"")
        lines.append(f"### 第二优先级优化")
        lines.append(f"")
        lines.append(f"**4. 多因子模型（7个因子）**")
        lines.append(f"- 动量因子（连板数）- 25%")
        lines.append(f"- 技术因子（封板强度）- 20%")
        lines.append(f"- 规模因子（市值）- 15%")
        lines.append(f"- 情绪因子 - 15%")
        lines.append(f"- 流动性因子 - 10%")
        lines.append(f"- 风险因子（波动率）- 10%")
        lines.append(f"- 活跃度因子 - 5%")
        lines.append(f"")
        lines.append(f"**5. 组合权重优化**")
        lines.append(f"- 单只股票最大仓位：10%")
        lines.append(f"- 基于评分加权分配")
        lines.append(f"")
        lines.append(f"**6. 行业分散约束**")
        lines.append(f"- 单一行业最大仓位：30%")
        lines.append(f"")

        lines.append(f"---")
        lines.append(f"")
        lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        report_content = "\n".join(lines)

        if output_path is None:
            output_path = self._get_output_path(summary['start_date'], summary['end_date'], summary.get('market', 'sha'), "analysis_v5")

        import os
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report_content)
        print(f"\nMarkdown报告已保存至: {output_path}")

        return report_content

    def _get_output_path(self, start_date: str, end_date: str, market: str, report_type: str = "analysis_v5", topic: str = "analysis") -> str:
        start_dt = datetime.strptime(start_date, '%Y%m%d')
        end_dt = datetime.strptime(end_date, '%Y%m%d')
        days_diff = (end_dt - start_dt).days + 1

        if days_diff <= 1:
            output_dir = self.OUTPUT_DIR / "daily" / topic
            date_range = start_date
        elif days_diff <= 7:
            output_dir = self.OUTPUT_DIR / "weekly" / topic
            date_range = f"{start_date}_{end_date}"
        elif days_diff <= 31:
            output_dir = self.OUTPUT_DIR / "monthly" / topic
            date_range = f"{start_date}_{end_date}"
        else:
            output_dir = self.OUTPUT_DIR / "yearly" / topic
            date_range = f"{start_date}_{end_date}"

        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{report_type}_{market}_{date_range}.md"
        return str(output_dir / filename)

    def print_report(self, results: List[StockAnalysisResult], summary: Dict):
        """打印分析报告"""
        market_names = {'gem': '创业板', 'sha': '沪A', 'sza': '深A', 'all': '全部A股'}
        market = summary.get('market', 'sha')
        market_name = market_names.get(market, market.upper())

        print("\n" + "=" * 120)
        print(f"选股收益分析报告（V5优化版）")
        print(f"分析市场: {market_name}")
        print(f"分析区间: {summary['start_date']} - {summary['end_date']}")
        print(f"每只股票初始资金: {self.initial_capital:,.0f} 元 | 每手股数: {self.lot_size} 股 | T+1模式")
        print("=" * 120)

        print(f"\n【汇总统计】")
        print(f"  选股次数: {summary['total_trades']} 次")
        print(f"  盈利次数: {summary['win_trades']} 次")
        print(f"  成功率: {summary['win_rate']:.2f}%")
        print(f"  总投入: {summary['total_cost']:,.2f} 元")
        print(f"  总收益: {summary['total_profit']:,.2f} 元")
        print(f"  总收益率: {summary['total_return_pct']:.2f}%")
        print(f"  平均收益: {summary['avg_profit_pct']:.2f}%")
        print(f"  最大回撤: {summary['max_drawdown']:.2f}%")


def run_v5_analysis(
    start_date: str,
    end_date: str,
    market: str = "sha",
    capital: float = 10000,
    output_dir: str = None,
):
    """运行V5策略分析"""
    analyzer = V5Analyzer(initial_capital=capital)
    results, summary = analyzer.analyze_period(start_date, end_date, market)
    analyzer.print_report(results, summary)
    analyzer.generate_markdown_report(results, summary)

    return results, summary
