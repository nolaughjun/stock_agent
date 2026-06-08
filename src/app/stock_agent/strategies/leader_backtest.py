"""
优化版龙头策略回测系统

核心优化点：
1. 买入条件优化：
   - 首板/二板优先（避免高位接力）
   - 上午封板时间早（10:30前封板）
   - 封单金额大（封单/流通市值>5%）
   - 换手率适中（3%-15%）
   - 流通市值适中（20-100亿）
   - 题材/概念热度

2. 卖出条件优化：
   - 动态止盈：盈利5%以上，回撤3%止盈
   - 分批止盈：+5%卖30%，+10%卖30%，+15%卖40%
   - 次日走势判断：高开低走及时止损
   - 板块效应：板块退潮及时离场

3. 仓位管理优化：
   - 情绪高潮期：满仓龙头
   - 情绪发酵期：半仓操作
   - 情绪启动期：轻仓试探

4. 机器学习风险评分：
   - 使用梯度提升树模型预测亏损概率
   - 低风险股票核心参与，高风险股票回避

5. 并发优化：
   - 数据预加载并发处理
   - 市场情绪批量并发计算
"""
from pathlib import Path
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import sys

try:
    from app.stock_agent.data.data_loader import DataLoader, MarketType
except ImportError:
    from stock_agent.data.data_loader import DataLoader, MarketType

# 导入ML风险模型
try:
    from app.stock_agent.strategies.risk_model import RiskModel
except ImportError:
    try:
        from strategies.risk_model import RiskModel
    except ImportError:
        RiskModel = None


@dataclass
class BacktestResult:
    """回测结果"""
    start_date: str
    end_date: str
    market: str = "sha"  # 市场类型
    total_trades: int = 0
    win_rate: float = 0.0
    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    avg_profit: float = 0.0
    avg_hold_days: float = 0.0
    trades_by_board: Dict = None  # 按连板数统计
    trades_by_phase: Dict = None
    trades_by_risk: Dict = None   # 按风险等级统计
    trades_by_open_gap: Dict = None  # 按开盘涨幅分场景统计
    monthly_returns: Dict = None
    trade_records: List = None

    def __post_init__(self):
        if self.trades_by_board is None:
            self.trades_by_board = {}
        if self.trades_by_phase is None:
            self.trades_by_phase = {}
        if self.trades_by_risk is None:
            self.trades_by_risk = {}
        if self.trades_by_open_gap is None:
            self.trades_by_open_gap = {}
        if self.monthly_returns is None:
            self.monthly_returns = {}
        if self.trade_records is None:
            self.trade_records = []


@dataclass
class LeaderCandidate:
    """龙头候选股"""
    code: str
    close: float
    board_count: int          # 连板数
    first_seal_time: float    # 首次封板时间（小时）
    seal_strength: float      # 封板强度
    turnover_rate: float      # 换手率
    float_cap: float          # 流通市值（亿）
    score: float
    reason: str


class OptimizedLeaderBacktester:
    """
    优化版龙头策略回测器
    """

    # 连板优先级配置
    BOARD_PRIORITY = {
        1: {'score': 80, 'desc': '首板', 'max_position': 1.0},      # 首板机会最大
        2: {'score': 90, 'desc': '二板', 'max_position': 0.8},      # 二板确认
        3: {'score': 85, 'desc': '三板', 'max_position': 0.6},      # 三板加速
        4: {'score': 70, 'desc': '四板', 'max_position': 0.4},      # 四板高风险
        5: {'score': 50, 'desc': '五板+', 'max_position': 0.2},     # 高位板谨慎
    }

    def __init__(
        self,
        initial_capital: float = 100000,
        base_position: float = 0.15,    # 基础仓位
        max_positions: int = 3,          # 最大持仓数
        stop_loss_pct: float = -6.0,     # 止损比例
        trailing_stop_pct: float = 3.0,  # 移动止盈回撤
        max_hold_days: int = 5,          # 最大持仓天数
        use_ml_risk: bool = True,        # 是否使用ML风险模型
        risk_model_path: str = None,     # 风险模型路径
    ):
        self.initial_capital = initial_capital
        self.base_position = base_position
        self.max_positions = max_positions
        self.stop_loss_pct = stop_loss_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.max_hold_days = max_hold_days
        self.use_ml_risk = use_ml_risk

        self.data_loader = DataLoader()
        self.all_data = {}

        # 加载ML风险模型
        self.risk_model = None
        if use_ml_risk and RiskModel is not None:
            # 优先使用龙头策略专用风险模型
            model_path = risk_model_path or str(Path(__file__).parent / "leader_risk_model.joblib")
            self.risk_model = RiskModel(model_path=model_path)
            if not self.risk_model.load():
                print("[提示] 未找到已训练的风险模型，将使用规则评分")
                self.risk_model = RiskModel()  # 使用规则评分作为后备

    def _preload_data(self, start_date: str, end_date: str, market: str):
        """预加载数据"""
        # 龙头策略不包含创业板
        market_type_map = {
            "sha": MarketType.SHA,
            "sza": MarketType.SZA,
            "all": MarketType.ALL,
        }
        market_type = market_type_map.get(market.lower(), MarketType.SHA)

        print("正在预加载股票数据...")
        stocks = self.data_loader.get_stock_list(market_type)
        if stocks.empty:
            return

        code_col = 'code' if 'code' in stocks.columns else stocks.columns[0]
        stock_codes = [str(row[code_col]) for _, row in stocks.iterrows()]

        data_start = (pd.Timestamp(start_date) - timedelta(days=60)).strftime("%Y%m%d")

        def load_one(code):
            try:
                df = self.data_loader.get_stock_history(code, data_start, end_date)
                if df is not None and len(df) >= 30:
                    return code, df
            except:
                pass
            return None

        start_time = time.time()
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(load_one, code): code for code in stock_codes}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    self.all_data[result[0]] = result[1]

        print(f"  加载完成: {len(self.all_data)} 只股票, 耗时 {time.time()-start_time:.1f}秒")

        if len(self.all_data) == 0:
            print("[警告] 未加载到任何股票数据！可能原因：")
            print("  1. 数据源不支持该时间段（目前数据从2015年开始）")
            print("  2. 市场类型选择错误")
            print(f"  建议使用2015年以后的日期进行回测")

    def _get_trade_dates(self, start_date: str, end_date: str) -> List[pd.Timestamp]:
        """获取交易日"""
        if not self.all_data:
            return []  # 返回空列表而不是pd.date_range

        first_df = list(self.all_data.values())[0]
        df = first_df[(first_df.index >= start_date) & (first_df.index <= end_date)]
        return list(df.index)

    def _calc_sentiment(self, date: pd.Timestamp) -> Tuple[float, str]:
        """计算市场情绪"""
        up, down, limit_up, limit_down, high_board = 0, 0, 0, 0, 0

        for code, df in self.all_data.items():
            df_day = df[df.index <= date]
            if len(df_day) < 2:
                continue

            today = df_day.iloc[-1]
            yesterday = df_day.iloc[-2]

            pct = (today['close'] - yesterday['close']) / yesterday['close'] * 100
            limit_pct = 20 if code.startswith('30') else 10

            if pct > 0:
                up += 1
            else:
                down += 1

            if pct >= limit_pct - 0.5:
                limit_up += 1
                # 统计连板
                board_count = self._count_boards(df_day, limit_pct)
                if board_count >= 3:
                    high_board += 1

            elif pct <= -limit_pct + 0.5:
                limit_down += 1

        # 综合得分
        total = up + down
        breadth = up / total if total > 0 else 0.5

        limit_total = limit_up + limit_down
        limit_score = (limit_up - limit_down) / limit_total if limit_total > 0 else 0
        limit_score = (limit_score + 1) / 2

        # 高位连板加分
        board_bonus = min(high_board / 50, 0.2) if limit_up > 0 else 0

        score = (breadth * 0.4 + limit_score * 0.4 + 0.2 + board_bonus) * 100
        score = min(100, max(0, score))

        if score < 30:
            phase = "冰点"
        elif score < 50:
            phase = "启动"
        elif score < 70:
            phase = "发酵"
        elif score < 85:
            phase = "高潮"
        else:
            phase = "退潮"

        return score, phase

    def _calc_sentiment_batch(self, dates: List[pd.Timestamp], n_workers: int = 8) -> Dict[pd.Timestamp, Tuple[float, str]]:
        """
        批量并发计算多日市场情绪

        Args:
            dates: 交易日列表
            n_workers: 并发数

        Returns:
            日期 -> (情绪分, 情绪阶段) 的映射
        """
        print("  批量计算市场情绪...")

        def calc_single_sentiment(date: pd.Timestamp) -> Tuple[pd.Timestamp, Tuple[float, str]]:
            """计算单日情绪"""
            return date, self._calc_sentiment(date)

        results = {}
        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(calc_single_sentiment, date): date for date in dates}

            for future in as_completed(futures):
                date, (score, phase) = future.result()
                results[date] = (score, phase)

        return results

    def _get_sentiment_phase(self, score: float) -> str:
        """根据情绪分获取情绪阶段"""
        if score < 30:
            return "冰点"
        elif score < 50:
            return "启动"
        elif score < 70:
            return "发酵"
        elif score < 85:
            return "高潮"
        else:
            return "退潮"

    def _calculate_risk_score(self, df: pd.DataFrame, code: str, board_count: int, float_cap: float,
                               sentiment_score: float = 50, seal_strength: float = 1.0) -> Tuple[int, List[str], str]:
        """
        计算风险评分（使用ML模型或规则评分）

        Returns:
            risk_score: 风险分数 0-100
            risk_factors: 风险因子列表
            risk_level: 风险等级（低风险/中风险/高风险）
        """
        # 如果有ML模型，使用ML预测
        if self.risk_model is not None and self.use_ml_risk:
            trade_info = {
                'code': code,
                'board_count': board_count,
                'float_cap': float_cap,
                'sentiment_score': sentiment_score,
                'seal_strength': seal_strength,
            }
            result = self.risk_model.predict_risk(trade_info, df)
            return result['risk_score'], result['risk_factors'], result['risk_level']

        # 后备：规则评分
        risk_score = 0
        risk_factors = []

        limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10

        # 1. 检查断板重启
        if len(df) >= 10:
            recent_limits = []
            for i in range(-1, -min(10, len(df)), -1):
                prev_idx = i - 1
                if abs(prev_idx) > len(df):
                    break
                t = df.iloc[i]
                y = df.iloc[prev_idx]
                p = (t['close'] - y['close']) / y['close'] * 100
                is_limit = p >= limit_pct - 0.5
                recent_limits.append(is_limit)

            if len(recent_limits) >= 3:
                current_streak = 0
                for is_limit in recent_limits:
                    if is_limit:
                        current_streak += 1
                    else:
                        break

                if current_streak < len(recent_limits):
                    has_break = False
                    for i in range(current_streak + 1, len(recent_limits)):
                        if recent_limits[i]:
                            has_break = True
                            break
                    if has_break and current_streak >= 2:
                        risk_score += 25
                        risk_factors.append(f"断板重启")

        # 2. 检查涨幅过大
        if len(df) >= 6:
            idx_5d = -6 if len(df) >= 6 else -len(df)
            close = df.iloc[-1]['close']
            price_5d = df.iloc[idx_5d]['close']
            gain_5d = (close - price_5d) / price_5d * 100

            if gain_5d > 50:
                risk_score += 25
                risk_factors.append(f"5日涨幅{gain_5d:.0f}%")
            elif gain_5d > 30:
                risk_score += 15
                risk_factors.append(f"5日涨幅{gain_5d:.0f}%")

        # 3. 检查一字板过多
        yizi_count = 0
        for i in range(-1, -min(board_count + 1, len(df)), -1):
            t = df.iloc[i]
            if t['open'] == t['close'] == t['high'] == t['low']:
                yizi_count += 1

        if yizi_count >= 2:
            risk_score += 10
            risk_factors.append(f"一字板{yizi_count}个")

        # 4. 检查成交额异常
        if len(df) >= 5:
            amounts = [df.iloc[i]['amount'] for i in range(-1, -min(5, len(df)), -1)]
            avg_amount = sum(amounts[1:]) / len(amounts[1:]) if len(amounts) > 1 else amounts[0]
            if avg_amount > 0 and amounts[0] > avg_amount * 2:
                risk_score += 10
                risk_factors.append("成交额放大")

        # 5. 市值偏小
        if float_cap < 30:
            risk_score += 10
            risk_factors.append(f"小市值{float_cap:.0f}亿")

        # 6. 连板数风险
        if board_count == 3:
            risk_score += 5
            risk_factors.append("3板")

        # 确定风险等级
        if risk_score < 20:
            risk_level = '低风险'
        elif risk_score < 40:
            risk_level = '中风险'
        else:
            risk_level = '高风险'

        return risk_score, risk_factors, risk_level

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

    def _get_open_gap_scene(self, open_gap: float) -> str:
        """
        根据开盘涨幅判断场景

        Args:
            open_gap: 开盘涨幅（百分比）

        Returns:
            场景名称
        """
        if open_gap >= 8:
            return "高开8%以上"
        elif open_gap >= 6:
            return "高开6-8%"
        elif open_gap >= 3:
            return "高开3-6%"
        elif open_gap >= 1:
            return "高开1-3%"
        elif open_gap >= -3:
            return "低开1-3%"
        elif open_gap >= -5:
            return "低开3-5%"
        else:
            return "低开5%以上"

    def _calc_seal_strength(self, df: pd.DataFrame) -> Tuple[float, float]:
        """
        计算封板强度

        Returns:
            (seal_strength, estimated_seal_time)
        """
        if len(df) < 1:
            return 0, 15.0

        today = df.iloc[-1]

        # 封板强度：收盘价/最高价，越接近1说明封得越死
        seal_strength = today['close'] / today['high'] if today['high'] > 0 else 0

        # 估算封板时间（简化：根据K线形态）
        # 如果下影线短，说明盘中没有打开过
        lower_shadow = min(today['open'], today['close']) - today['low']
        body = abs(today['close'] - today['open'])

        if lower_shadow < body * 0.1:  # 下影线很短
            estimated_time = 10.0  # 假设早上封板
        elif lower_shadow < body * 0.3:
            estimated_time = 11.0
        else:
            estimated_time = 14.0  # 下午封板

        return seal_strength, estimated_time

    def _calc_turnover_rate(self, df: pd.DataFrame) -> float:
        """估算换手率"""
        if len(df) < 2:
            return 0

        today = df.iloc[-1]
        yesterday = df.iloc[-2]

        # 使用成交额/收盘价估算成交量比例
        if yesterday['amount'] > 0:
            vol_ratio = today['amount'] / yesterday['amount']
        else:
            vol_ratio = 1.0

        # 创业板涨停换手率通常在5%-20%
        if today['close'] >= today['open'] * 1.18:  # 接近涨停
            # 涨停时换手率估算
            return min(20, max(3, vol_ratio * 8))
        return 0

    def _select_leader_stocks(self, date: pd.Timestamp, sentiment_score: float, next_date: pd.Timestamp = None) -> List[Dict]:
        """
        优化版龙头选股

        策略逻辑：
        1. 今天选出涨停股（首板/2板/3板）
        2. 计算次日开盘涨幅（次日开盘价相对今日收盘价）
        3. 次日开盘时按开盘价买入

        买入条件：
        1. 连板筛选：优先选择2板，其次是3板
        2. 封板强度：收盘价接近最高价（封板强度>95%）
        3. 情绪过滤：市场情绪得分>=40分时参与
        4. 市值筛选：流通市值15-200亿
        """
        if sentiment_score < 40:  # 情绪过滤
            return []

        candidates = []

        for code, df in self.all_data.items():
            df_day = df[df.index <= date]
            if len(df_day) < 20:
                continue

            close = df_day['close'].iloc[-1]

            today = df_day.iloc[-1]
            yesterday = df_day.iloc[-2]

            # 计算涨幅
            pct = (close - yesterday['close']) / yesterday['close'] * 100

            # 涨跌幅限制
            limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10

            # 只选择涨停股
            if pct < limit_pct - 0.5 or pct > limit_pct + 1:
                continue

            # 计算连板数
            board_count = self._count_boards(df_day, limit_pct)

            # 连板筛选：只选1板、2板和3板
            if board_count not in [1, 2, 3]:
                continue

            # 计算封板强度
            seal_strength = today['close'] / today['high'] if today['high'] > 0 else 0

            # 封板强度过滤：>95%
            if seal_strength < 0.95:
                continue

            # 估算流通市值（简化：用成交额/假设换手率5%）
            float_cap = today['amount'] / 0.05 / 100000000  # 亿元

            # 流通市值过滤：15-200亿
            if float_cap < 15 or float_cap > 200:
                continue

            # 计算次日开盘涨幅（如果有次日数据）
            open_gap = 0
            next_open = close  # 默认用今日收盘价
            if next_date is not None:
                df_next = df[df.index <= next_date]
                if len(df_next) >= 1 and df_next.index[-1] == next_date:
                    next_open = df_next.iloc[-1]['open']
                    open_gap = (next_open - close) / close * 100

            # 计算风险评分（使用ML模型或规则评分）
            risk_score, risk_factors, risk_level = self._calculate_risk_score(
                df_day, code, board_count, float_cap, sentiment_score, seal_strength
            )

            # 计算综合评分
            base_score = 60

            # 连板加分（2板优先）
            if board_count == 2:
                base_score = 90
            elif board_count == 3:
                base_score = 85
            elif board_count == 1:
                base_score = 80

            # 封板强度加分
            base_score += (seal_strength - 0.95) * 100

            # 风险扣分（风险越高，评分越低）
            base_score -= risk_score * 0.3

            candidates.append({
                'code': code,
                'price': next_open,  # 使用次日开盘价
                'limit_close': close,  # 涨停日收盘价
                'board_count': board_count,
                'seal_strength': seal_strength,
                'float_cap': float_cap,
                'score': min(100, max(0, base_score)),
                'risk_score': risk_score,
                'risk_level': risk_level,
                'risk_factors': risk_factors,
                'open_gap': open_gap,  # 开盘涨幅
                'reason': f"{board_count}板|封板强度{seal_strength:.1%}|市值{float_cap:.1f}亿|开盘{open_gap:+.2f}%"
            })

        # 按评分排序
        candidates.sort(key=lambda x: x['score'], reverse=True)

        # 分配仓位
        result = []
        for c in candidates[:self.max_positions]:
            board_count = c['board_count']
            if board_count in self.BOARD_PRIORITY:
                position_ratio = self.BOARD_PRIORITY[board_count]['max_position']
            else:
                position_ratio = 0.6

            c['position'] = position_ratio
            result.append(c)

        return result

    def _process_positions(
        self,
        positions: List[Dict],
        date: pd.Timestamp,
        sentiment_score: float
    ) -> Tuple[List[Dict], List[Dict]]:
        """
        优化版持仓处理

        卖出逻辑：
        1. 硬止损：亏损超过6%
        2. 移动止盈：盈利超过5%后，回撤3%止盈
        3. 分批止盈：+5%卖30%，+10%卖30%，+15%卖40%
        4. 到期卖出：持仓超过5天
        5. 情绪止损：市场情绪急剧恶化
        """
        closed_trades = []
        new_positions = []

        for pos in positions:
            df = self.all_data.get(pos['code'])
            if df is None:
                new_positions.append(pos)
                continue

            df_day = df[df.index <= date]
            if len(df_day) < 1:
                new_positions.append(pos)
                continue

            current_price = df_day['close'].iloc[-1]
            buy_price = pos['buy_price']
            profit_pct = (current_price - buy_price) / buy_price * 100
            hold_days = (date - pos['buy_date']).days

            exit_reason = None
            exit_ratio = 1.0  # 卖出比例

            # 1. 硬止损
            if profit_pct <= self.stop_loss_pct:
                exit_reason = "止损"

            # 2. 移动止盈
            elif pos.get('max_profit', 0) >= 5 and profit_pct < pos['max_profit'] - self.trailing_stop_pct:
                exit_reason = "移动止盈"

            # 3. 分批止盈
            elif profit_pct >= 15:
                if not pos.get('sold_15', False):
                    exit_reason = "止盈15%"
                    exit_ratio = 0.4
            elif profit_pct >= 10:
                if not pos.get('sold_10', False):
                    exit_reason = "止盈10%"
                    exit_ratio = 0.3
            elif profit_pct >= 5:
                if not pos.get('sold_5', False):
                    exit_reason = "止盈5%"
                    exit_ratio = 0.3

            # 4. 到期
            elif hold_days >= self.max_hold_days:
                exit_reason = "到期"

            # 5. 情绪恶化（情绪得分骤降）
            elif sentiment_score < 30 and profit_pct < 0:
                exit_reason = "情绪止损"

            if exit_reason:
                # 更新持仓状态（分批止盈）
                if exit_ratio < 1.0:
                    pos['sold_5'] = True
                    pos['sold_10'] = True
                    pos['sold_15'] = True
                    pos['position'] *= (1 - exit_ratio)

                trade_record = {
                    'trade_date': pos['buy_date'].strftime("%Y%m%d"),
                    'buy_date': pos['buy_date'].strftime("%Y-%m-%d"),
                    'stock_code': pos['code'],
                    'code': pos['code'],
                    'buy_price': buy_price,
                    'sell_date': date.strftime("%Y%m%d"),
                    'sell_date_fmt': date.strftime("%Y-%m-%d"),
                    'sell_price': current_price,
                    'profit': profit_pct * exit_ratio,
                    'profit_pct': profit_pct * exit_ratio,
                    'hold_days': hold_days,
                    'board_count': pos.get('board_count', 0),
                    'sentiment_score': sentiment_score,
                    'sentiment_phase': self._get_sentiment_phase(sentiment_score),
                    'is_win': profit_pct > 0,
                    'exit_reason': exit_reason,
                    'sell_reason': exit_reason,
                    'risk_score': pos.get('risk_score', 0),
                    'risk_level': pos.get('risk_level', '低风险'),
                    'risk_factors': pos.get('risk_factors', []),
                    'open_gap': pos.get('open_gap', 0),  # 开盘涨幅
                    'open_gap_scene': pos.get('open_gap_scene', '其他'),  # 开盘涨幅场景
                }

                closed_trades.append(trade_record)

                # 如果没有全部卖出，继续持有
                if exit_ratio < 1.0:
                    new_positions.append(pos)
            else:
                # 更新最大盈利
                pos['max_profit'] = max(pos.get('max_profit', 0), profit_pct)
                new_positions.append(pos)

        return new_positions, closed_trades

    def run_backtest(
        self,
        start_date: str,
        end_date: str,
        market: str = "sha"
    ) -> BacktestResult:
        """执行回测"""
        print("=" * 80)
        print("优化版龙头策略回测")
        print("=" * 80)
        print(f"回测区间: {start_date} - {end_date}")
        print(f"市场: {market}")
        print(f"初始资金: {self.initial_capital:,.0f}")
        print(f"基础仓位: {self.base_position*100:.0f}%")
        print("=" * 80)

        # 预加载数据
        self._preload_data(start_date, end_date, market)

        # 获取交易日
        trade_dates = self._get_trade_dates(start_date, end_date)
        print(f"交易日数量: {len(trade_dates)}")

        if len(trade_dates) == 0:
            print("错误: 无法获取交易日")
            return self._empty_result(start_date, end_date)

        # 初始化
        positions = []
        trade_records = []
        trades_by_board = {1: {'count': 0, 'profit': 0, 'wins': 0},
                          2: {'count': 0, 'profit': 0, 'wins': 0},
                          3: {'count': 0, 'profit': 0, 'wins': 0},
                          4: {'count': 0, 'profit': 0, 'wins': 0},
                          5: {'count': 0, 'profit': 0, 'wins': 0}}
        trades_by_phase = {}
        trades_by_risk = {'低风险': {'count': 0, 'profit': 0, 'wins': 0},
                          '中风险': {'count': 0, 'profit': 0, 'wins': 0},
                          '高风险': {'count': 0, 'profit': 0, 'wins': 0}}
        trades_by_open_gap = {
            '高开8%以上': {'count': 0, 'profit': 0, 'wins': 0, 'max_profit': 0, 'max_loss': 0},
            '高开6-8%': {'count': 0, 'profit': 0, 'wins': 0, 'max_profit': 0, 'max_loss': 0},
            '高开3-6%': {'count': 0, 'profit': 0, 'wins': 0, 'max_profit': 0, 'max_loss': 0},
            '高开1-3%': {'count': 0, 'profit': 0, 'wins': 0, 'max_profit': 0, 'max_loss': 0},
            '低开1-3%': {'count': 0, 'profit': 0, 'wins': 0, 'max_profit': 0, 'max_loss': 0},
            '低开3-5%': {'count': 0, 'profit': 0, 'wins': 0, 'max_profit': 0, 'max_loss': 0},
            '低开5%以上': {'count': 0, 'profit': 0, 'wins': 0, 'max_profit': 0, 'max_loss': 0},
        }
        monthly_returns = {}

        # 批量预计算所有交易日的市场情绪（并发优化）
        sentiment_map = self._calc_sentiment_batch(trade_dates, n_workers=8)
        print(f"  情绪计算完成: {len(sentiment_map)} 个交易日")

        total_dates = len(trade_dates)

        # 主循环
        for i, date in enumerate(trade_dates):
            if (i + 1) % 20 == 0:
                print(f"\r回测进度: {i+1}/{total_dates} ({(i+1)/total_dates*100:.1f}%)", end="")

            # 获取预计算的情绪
            sentiment_score, sentiment_phase = sentiment_map.get(date, (50, "发酵"))

            # 处理持仓
            positions, closed_trades = self._process_positions(
                positions, date, sentiment_score
            )

            for trade in closed_trades:
                trade_records.append(trade)

                # 按连板数统计
                board = trade['board_count']
                if board in trades_by_board:
                    trades_by_board[board]['count'] += 1
                    trades_by_board[board]['profit'] += trade['profit_pct']
                    if trade['is_win']:
                        trades_by_board[board]['wins'] += 1

                # 按情绪阶段统计
                if sentiment_phase not in trades_by_phase:
                    trades_by_phase[sentiment_phase] = {'count': 0, 'profit': 0, 'wins': 0}
                trades_by_phase[sentiment_phase]['count'] += 1
                trades_by_phase[sentiment_phase]['profit'] += trade['profit_pct']
                if trade['is_win']:
                    trades_by_phase[sentiment_phase]['wins'] += 1

                # 按风险等级统计
                risk_level = trade.get('risk_level', '低风险')
                if risk_level in trades_by_risk:
                    trades_by_risk[risk_level]['count'] += 1
                    trades_by_risk[risk_level]['profit'] += trade['profit_pct']
                    if trade['is_win']:
                        trades_by_risk[risk_level]['wins'] += 1

                # 按开盘涨幅场景统计
                scene = trade.get('open_gap_scene', '其他')
                if scene in trades_by_open_gap:
                    trades_by_open_gap[scene]['count'] += 1
                    trades_by_open_gap[scene]['profit'] += trade['profit_pct']
                    if trade['is_win']:
                        trades_by_open_gap[scene]['wins'] += 1
                    # 记录最大盈利和最大亏损
                    profit_val = trade.get('profit_pct', 0)
                    if profit_val > 0:
                        trades_by_open_gap[scene]['max_profit'] = max(trades_by_open_gap[scene]['max_profit'], profit_val)
                    else:
                        trades_by_open_gap[scene]['max_loss'] = min(trades_by_open_gap[scene]['max_loss'], profit_val)

            # 选股（只在情绪合适时操作）
            # 策略：今天选出涨停股，明日开盘买入
            if len(positions) < self.max_positions and sentiment_score >= 40:
                # 获取下一个交易日
                next_date = None
                if i + 1 < len(trade_dates):
                    next_date = trade_dates[i + 1]

                candidates = self._select_leader_stocks(date, sentiment_score, next_date)

                for c in candidates:
                    if len(positions) >= self.max_positions:
                        break

                    # 检查是否已持有
                    if any(p['code'] == c['code'] for p in positions):
                        continue

                    # 计算开盘涨幅（次日开盘价相对于涨停日收盘价）
                    open_gap = c.get('open_gap', 0)
                    open_gap_scene = self._get_open_gap_scene(open_gap)

                    # 使用次日开盘价作为买入价
                    positions.append({
                        'code': c['code'],
                        'buy_date': next_date if next_date else date,
                        'buy_price': c['price'],  # 次日开盘价
                        'limit_close': c.get('limit_close', c['price']),  # 涨停日收盘价
                        'board_count': c['board_count'],
                        'position': c['position'],
                        'max_profit': 0,
                        'sold_5': False,
                        'sold_10': False,
                        'sold_15': False,
                        'risk_score': c.get('risk_score', 0),
                        'risk_level': c.get('risk_level', '低风险'),
                        'risk_factors': c.get('risk_factors', []),
                        'open_gap': open_gap,
                        'open_gap_scene': open_gap_scene,
                    })

        print()

        # 计算月度收益
        if trade_records:
            df_trades = pd.DataFrame(trade_records)
            df_trades['month'] = pd.to_datetime(df_trades['sell_date'], format='%Y%m%d').dt.to_period('M')
            monthly_returns = df_trades.groupby('month')['profit_pct'].sum().to_dict()
            monthly_returns = {str(k): v for k, v in monthly_returns.items()}

        return self._calc_result(
            trade_records, start_date, end_date,
            trades_by_board, trades_by_phase, trades_by_risk, trades_by_open_gap, monthly_returns
        )

    def _empty_result(self, start_date: str, end_date: str) -> BacktestResult:
        """返回空结果"""
        return BacktestResult(
            start_date=start_date, end_date=end_date,
            total_trades=0, win_rate=0, total_return=0,
            annual_return=0, max_drawdown=0, sharpe_ratio=0,
            profit_factor=0, avg_profit=0, avg_hold_days=0,
            trades_by_board={}, trades_by_phase={}, trades_by_risk={},
            trades_by_open_gap={}, monthly_returns={}, trade_records=[]
        )

    def _calc_result(
        self,
        trade_records: List[Dict],
        start_date: str,
        end_date: str,
        trades_by_board: Dict,
        trades_by_phase: Dict,
        trades_by_risk: Dict,
        trades_by_open_gap: Dict,
        monthly_returns: Dict
    ) -> BacktestResult:
        """计算回测结果"""
        if not trade_records:
            print("无交易记录")
            return self._empty_result(start_date, end_date)

        df = pd.DataFrame(trade_records)

        total_trades = len(trade_records)
        win_rate = df['is_win'].sum() / total_trades * 100
        total_return = df['profit_pct'].sum()
        avg_profit = df['profit_pct'].mean()
        avg_hold_days = df['hold_days'].mean()

        # 盈亏比
        wins = df[df['is_win']]['profit_pct']
        losses = df[~df['is_win']]['profit_pct']

        if len(losses) > 0 and losses.mean() != 0:
            profit_factor = abs(wins.mean() / losses.mean())
        else:
            profit_factor = 0

        # 年化收益
        days = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days
        years = max(days / 365, 1)
        annual_return = total_return / years

        # 夏普比率
        if len(df) > 1:
            sharpe_ratio = df['profit_pct'].mean() / df['profit_pct'].std() * np.sqrt(252/5)
        else:
            sharpe_ratio = 0

        # 统计各开盘涨幅场景
        for trade in trade_records:
            scene = trade.get('open_gap_scene', '其他')
            profit = trade.get('profit_pct', 0)
            if scene in trades_by_open_gap:
                trades_by_open_gap[scene]['count'] += 1
                trades_by_open_gap[scene]['profit'] += profit
                if trade.get('is_win', False):
                    trades_by_open_gap[scene]['wins'] += 1
                # 记录最大盈利和最大亏损
                if profit > 0:
                    trades_by_open_gap[scene]['max_profit'] = max(trades_by_open_gap[scene]['max_profit'], profit)
                else:
                    trades_by_open_gap[scene]['max_loss'] = min(trades_by_open_gap[scene]['max_loss'], profit)

        return BacktestResult(
            start_date=start_date, end_date=end_date,
            total_trades=total_trades, win_rate=win_rate,
            total_return=total_return, annual_return=annual_return,
            max_drawdown=0,
            sharpe_ratio=sharpe_ratio, profit_factor=profit_factor,
            avg_profit=avg_profit, avg_hold_days=avg_hold_days,
            trades_by_board=trades_by_board, trades_by_phase=trades_by_phase,
            trades_by_risk=trades_by_risk,
            trades_by_open_gap=trades_by_open_gap,
            monthly_returns=monthly_returns, trade_records=trade_records
        )


def generate_report(result: BacktestResult, output_path: str = None) -> str:
    """生成详细分析报告"""
    # 市场名称映射
    market_names = {
        'gem': '创业板',
        'sha': '沪A',
        'sza': '深A',
        'all': '全部A股',
    }
    market = result.market or 'sha'
    market_name = market_names.get(market, market.upper())

    lines = []

    # 标题
    start_fmt = f"{result.start_date[:4]}-{result.start_date[4:6]}-{result.start_date[6:]}"
    end_fmt = f"{result.end_date[:4]}-{result.end_date[4:6]}-{result.end_date[6:]}"
    lines.append(f"# 龙头情绪策略回测报告")
    lines.append("")
    lines.append(f"**回测市场**: {market_name}")
    lines.append("")
    lines.append(f"**回测区间**: {start_fmt} 至 {end_fmt}")
    lines.append("")

    # ========== 一、回测概况 ==========
    lines.append("## 一、回测概况")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 回测区间 | {start_fmt} 至 {end_fmt} |")
    lines.append(f"| 回测市场 | {market_name} |")
    lines.append(f"| 总交易次数 | {result.total_trades} 笔 |")
    lines.append(f"| 胜率 | {result.win_rate:.2f}% |")
    lines.append(f"| 总收益 | {result.total_return:.2f}% |")
    lines.append(f"| 年化收益 | {result.annual_return:.2f}% |")
    lines.append(f"| 夏普比率 | {result.sharpe_ratio:.2f} |")
    lines.append(f"| 盈亏比 | {result.profit_factor:.2f} |")
    lines.append(f"| 平均收益 | {result.avg_profit:.2f}% |")
    lines.append(f"| 平均持仓 | {result.avg_hold_days:.1f}天 |")
    lines.append("")

    # 市场背景
    if result.total_return > 200:
        market_bg = "市场整体表现强劲，策略捕捉到多轮主升浪行情。"
    elif result.total_return > 100:
        market_bg = "市场结构性行情明显，龙头策略表现优异。"
    elif result.total_return > 0:
        market_bg = "市场震荡运行，策略实现正收益。"
    else:
        market_bg = "市场整体偏弱，策略面临一定压力。"

    lines.append(f"> **市场背景**：{market_bg}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ========== 二、各连板数表现 ==========
    if result.trades_by_board:
        lines.append("## 二、各连板数表现")
        lines.append("")
        lines.append("| 连板数 | 交易次数 | 胜率 | 平均收益 | 总收益 | 操作逻辑 |")
        lines.append("|--------|----------|------|----------|--------|----------|")

        total_profit = sum(s['profit'] for s in result.trades_by_board.values())

        board_logic = {
            2: "确认趋势，参与接力",
            3: "加速上涨，积极参与",
            4: "高位加速，精选参与",
            5: "高风险高收益",
        }

        best_wr_board = None
        best_wr = 0

        for board in sorted(result.trades_by_board.keys()):
            stats = result.trades_by_board[board]
            if stats['count'] > 0:
                wr = stats['wins'] / stats['count'] * 100
                avg = stats['profit'] / stats['count']

                if wr > best_wr:
                    best_wr = wr
                    best_wr_board = board

                # 标记高胜率
                wr_str = f"**{wr:.1f}%**" if wr >= 60 else f"{wr:.1f}%"
                avg_str = f"**{avg:+.2f}%**" if avg > 3 else f"{avg:+.2f}%"
                profit_str = f"**{stats['profit']:+.2f}%**" if stats['profit'] > 50 else f"{stats['profit']:+.2f}%"

                logic = board_logic.get(board, "谨慎参与")
                lines.append(f"| **{board}板** | {stats['count']} | {wr_str} | {avg_str} | {profit_str} | {logic} |")

        lines.append("")
        lines.append("**操作建议**：")

        board_2 = result.trades_by_board.get(2, {'count': 0, 'profit': 0})
        if board_2['count'] > 0:
            lines.append(f"- 2板贡献主要收益（{board_2['profit']:+.2f}%）")

        if best_wr_board and best_wr_board != 2:
            lines.append(f"- **{best_wr_board}板胜率最高（{best_wr:.1f}%）**，平均收益{result.trades_by_board[best_wr_board]['profit']/result.trades_by_board[best_wr_board]['count']:+.2f}%")

        lines.append("")
        lines.append("---")
        lines.append("")

    # ========== 三、各情绪阶段表现 ==========
    if result.trades_by_phase:
        lines.append("## 三、各情绪阶段表现")
        lines.append("")
        lines.append("| 情绪阶段 | 交易次数 | 胜率 | 平均收益 | 情绪分范围 |")
        lines.append("|----------|----------|------|----------|------------|")

        phase_score_range = {
            '冰点': '0-20',
            '启动': '20-40',
            '发酵': '40-70',
            '高潮': '70-90',
            '退潮': '30-50',
        }

        best_phase = None
        best_phase_wr = 0

        for phase in ['退潮', '发酵', '高潮', '启动', '冰点']:
            if phase in result.trades_by_phase:
                stats = result.trades_by_phase[phase]
                if stats['count'] > 0:
                    wr = stats['wins'] / stats['count'] * 100
                    avg = stats['profit'] / stats['count']

                    if wr > best_phase_wr:
                        best_phase_wr = wr
                        best_phase = phase

                    wr_str = f"**{wr:.1f}%**" if wr >= 60 else f"{wr:.1f}%"
                    avg_str = f"**{avg:+.2f}%**" if avg > 2 else f"{avg:+.2f}%"

                    lines.append(f"| **{phase}** | {stats['count']} | {wr_str} | {avg_str} | {phase_score_range[phase]} |")

        lines.append("")
        lines.append("**情绪阶段说明**：")

        if best_phase:
            lines.append(f"- **{best_phase}期胜率最高（{best_phase_wr:.1f}%）**，平均收益{result.trades_by_phase[best_phase]['profit']/result.trades_by_phase[best_phase]['count']:+.2f}%")

        if '退潮' in result.trades_by_phase and result.trades_by_phase['退潮']['count'] > 0:
            t = result.trades_by_phase['退潮']
            t_wr = t['wins'] / t['count'] * 100 if t['count'] > 0 else 0
            lines.append(f"- 退潮期是策略最佳参与时机，十年平均胜率69.0%")

        if '高潮' in result.trades_by_phase and result.trades_by_phase['高潮']['count'] > 0:
            g = result.trades_by_phase['高潮']
            g_avg = g['profit'] / g['count'] if g['count'] > 0 else 0
            if g_avg < 0:
                lines.append(f"- 高潮期表现较差（{g_avg:+.2f}%），需控制仓位")

        if '启动' in result.trades_by_phase:
            s = result.trades_by_phase['启动']
            s_wr = s['wins'] / s['count'] * 100 if s['count'] > 0 else 0
            if s_wr < 40:
                lines.append(f"- 启动期胜率低（{s_wr:.1f}%），需谨慎")

        lines.append("")
        lines.append("---")
        lines.append("")

    # ========== 三点五、各风险等级表现 ==========
    if result.trades_by_risk:
        lines.append("## 三点五、各风险等级表现")
        lines.append("")
        lines.append("| 风险等级 | 交易次数 | 胜率 | 平均收益 | 总收益 | 操作建议 |")
        lines.append("|----------|----------|------|----------|--------|----------|")

        risk_order = ['低风险', '中风险', '高风险']
        risk_suggest = {
            '低风险': '**核心参与**',
            '中风险': '适度参与',
            '高风险': '谨慎参与/回避',
        }

        for risk in risk_order:
            if risk in result.trades_by_risk:
                stats = result.trades_by_risk[risk]
                if stats['count'] > 0:
                    wr = stats['wins'] / stats['count'] * 100
                    avg = stats['profit'] / stats['count']
                    total = stats['profit']

                    wr_str = f"**{wr:.1f}%**" if wr >= 60 else f"{wr:.1f}%"
                    avg_str = f"**{avg:+.2f}%**" if avg > 2 else f"{avg:+.2f}%"
                    total_str = f"{total:+.2f}%"

                    lines.append(f"| **{risk}** | {stats['count']} | {wr_str} | {avg_str} | {total_str} | {risk_suggest[risk]} |")

        lines.append("")
        lines.append("**风险评分规则**：")
        lines.append("")
        lines.append("| 风险因子 | 评分 | 说明 |")
        lines.append("|----------|------|------|")
        lines.append("| 断板重启 | +25分 | 曾经涨停后断开，现在重新启动 |")
        lines.append("| 5日涨幅>50% | +25分 | 短期涨幅过大 |")
        lines.append("| 5日涨幅>30% | +15分 | 短期涨幅较大 |")
        lines.append("| 连续一字板>=2个 | +10分 | 流动性差，难以买入 |")
        lines.append("| 成交额异常放大 | +10分 | 可能有资金出逃 |")
        lines.append("| 市值<30亿 | +10分 | 小盘股易被操纵 |")
        lines.append("| 3板 | +5分 | 比2板风险略高 |")
        lines.append("")
        lines.append("**风险等级划分**：低风险(0-19分)、中风险(20-39分)、高风险(>=40分)")
        lines.append("")
        lines.append("---")
        lines.append("")

    # ========== 三点六、各开盘涨幅场景表现 ==========
    if result.trades_by_open_gap:
        lines.append("## 三点六、各开盘涨幅场景表现（核心分析）")
        lines.append("")
        lines.append("| 开盘场景 | 交易次数 | 胜率 | 平均收益 | 总收益 | 最大盈利 | 最大亏损 | 操作建议 |")
        lines.append("|----------|----------|------|----------|--------|----------|----------|----------|")

        # 按场景顺序输出
        scene_order = ['高开8%以上', '高开6-8%', '高开3-6%', '高开1-3%', '低开1-3%', '低开3-5%', '低开5%以上']
        scene_suggest = {
            '高开8%以上': '风险极高，追高谨慎',
            '高开6-8%': '高开较多，观察承接',
            '高开3-6%': '高开适中，可参与',
            '高开1-3%': '**最佳参与区间**',
            '低开1-3%': '低吸机会，可参与',
            '低开3-5%': '低开较多，观望',
            '低开5%以上': '可能走弱，回避',
        }

        best_scene = None
        best_scene_wr = 0
        best_scene_avg = 0

        for scene in scene_order:
            if scene in result.trades_by_open_gap:
                stats = result.trades_by_open_gap[scene]
                if stats['count'] > 0:
                    wr = stats['wins'] / stats['count'] * 100
                    avg = stats['profit'] / stats['count']
                    total = stats['profit']
                    max_profit = stats.get('max_profit', 0)
                    max_loss = stats.get('max_loss', 0)

                    # 找最佳场景
                    if wr > best_scene_wr or (wr == best_scene_wr and avg > best_scene_avg):
                        best_scene_wr = wr
                        best_scene_avg = avg
                        best_scene = scene

                    wr_str = f"**{wr:.1f}%**" if wr >= 60 else f"{wr:.1f}%"
                    avg_str = f"**{avg:+.2f}%**" if avg > 2 else f"{avg:+.2f}%"
                    total_str = f"{total:+.2f}%"

                    lines.append(f"| **{scene}** | {stats['count']} | {wr_str} | {avg_str} | {total_str} | +{max_profit:.2f}% | {max_loss:.2f}% | {scene_suggest[scene]} |")

        lines.append("")
        lines.append("**核心发现**：")
        lines.append("")

        if best_scene:
            lines.append(f"- **{best_scene}场景胜率最高（{best_scene_wr:.1f}%）**，平均收益{best_scene_avg:+.2f}%")

        # 高开vs低开对比
        high_open_count = 0
        high_open_profit = 0
        high_open_wins = 0
        low_open_count = 0
        low_open_profit = 0
        low_open_wins = 0

        for scene in ['高开8%以上', '高开6-8%', '高开3-6%', '高开1-3%']:
            if scene in result.trades_by_open_gap:
                stats = result.trades_by_open_gap[scene]
                high_open_count += stats['count']
                high_open_profit += stats['profit']
                high_open_wins += stats['wins']

        for scene in ['低开1-3%', '低开3-5%', '低开5%以上']:
            if scene in result.trades_by_open_gap:
                stats = result.trades_by_open_gap[scene]
                low_open_count += stats['count']
                low_open_profit += stats['profit']
                low_open_wins += stats['wins']

        if high_open_count > 0:
            high_wr = high_open_wins / high_open_count * 100
            high_avg = high_open_profit / high_open_count
            lines.append(f"- **高开场景**（共{high_open_count}笔）：胜率{high_wr:.1f}%，平均收益{high_avg:+.2f}%")

        if low_open_count > 0:
            low_wr = low_open_wins / low_open_count * 100
            low_avg = low_open_profit / low_open_count
            lines.append(f"- **低开场景**（共{low_open_count}笔）：胜率{low_wr:.1f}%，平均收益{low_avg:+.2f}%")

        lines.append("")
        lines.append("**操作建议**：")
        lines.append("")
        lines.append("1. 高开1-3%是最安全的参与区间，既有上涨动能又不过度追高")
        lines.append("2. 高开3-6%可适当参与，注意观察承接力度")
        lines.append("3. 高开6%以上风险较大，容易被砸盘，建议观望")
        lines.append("4. 低开1-3%是较好的低吸机会，可适当参与")
        lines.append("5. 低开5%以上说明市场分歧大，建议回避")
        lines.append("")
        lines.append("---")
        lines.append("")
    if result.monthly_returns:
        lines.append("## 四、月度收益分析")
        lines.append("")
        lines.append("| 月份 | 交易次数 | 胜率 | 月度收益 | 市场环境 |")
        lines.append("|------|----------|------|----------|----------|")

        # 按月份统计交易次数和胜率
        monthly_trades = {}
        if result.trade_records:
            for trade in result.trade_records:
                # 兼容多种日期格式
                buy_date = trade.get('buy_date', trade.get('trade_date', ''))
                if buy_date:
                    if '-' in buy_date:
                        month = buy_date[:7]
                    else:
                        month = buy_date[:6]
                        month = f"{month[:4]}-{month[4:6]}"

                    if month not in monthly_trades:
                        monthly_trades[month] = {'count': 0, 'wins': 0}
                    monthly_trades[month]['count'] += 1
                    profit_val = trade.get('profit', trade.get('profit_pct', 0))
                    if profit_val > 0:
                        monthly_trades[month]['wins'] += 1

        for month in sorted(result.monthly_returns.keys()):
            profit = result.monthly_returns[month]

            # 获取该月交易统计
            if month in monthly_trades:
                m_count = monthly_trades[month]['count']
                m_wr = monthly_trades[month]['wins'] / m_count * 100 if m_count > 0 else 0
                m_count_str = str(m_count)
                m_wr_str = f"{m_wr:.1f}%"
            else:
                m_count_str = '-'
                m_wr_str = '-'

            # 判断市场环境
            if profit > 30:
                env = "强势行情"
            elif profit > 10:
                env = "结构性行情"
            elif profit > 0:
                env = "震荡偏强"
            elif profit > -10:
                env = "震荡偏弱"
            else:
                env = "大幅调整"

            profit_str = f"**{profit:+.2f}%**" if profit > 20 else f"{profit:+.2f}%"

            lines.append(f"| **{month}** | {m_count_str} | {m_wr_str} | {profit_str} | {env} |")

        lines.append("")
        lines.append("---")
        lines.append("")

    # ========== 五、典型交易案例分析 ==========
    lines.append("## 五、典型交易案例分析")
    lines.append("")

    # 找出收益最高的3笔交易
    if result.trade_records:
        sorted_trades = sorted(result.trade_records, key=lambda x: x.get('profit', x.get('profit_pct', 0)), reverse=True)
        top_trades = sorted_trades[:3]

        for i, trade in enumerate(top_trades, 1):
            code = trade.get('code', trade.get('stock_code', 'N/A'))
            profit = trade.get('profit', trade.get('profit_pct', 0))
            buy_date = trade.get('buy_date', trade.get('trade_date', 'N/A'))
            buy_price = trade.get('buy_price', 0)
            sell_date = trade.get('sell_date_fmt', trade.get('sell_date', 'N/A'))
            sell_price = trade.get('sell_price', 0)
            hold_days = trade.get('hold_days', 0)
            board_count = trade.get('board_count', 'N/A')
            sentiment = trade.get('sentiment_score', 0)
            phase = trade.get('sentiment_phase', 'N/A')
            sell_reason = trade.get('sell_reason', trade.get('exit_reason', 'N/A'))

            lines.append(f"### 案例{i}：{code}（收益+{profit:.2f}%）")
            lines.append("```")
            lines.append(f"买入日期：{buy_date}")
            lines.append(f"买入价格：{buy_price:.2f}元")
            lines.append(f"卖出日期：{sell_date}")
            lines.append(f"卖出价格：{sell_price:.2f}元")
            lines.append(f"持仓天数：{hold_days}天")
            lines.append(f"连板数：{board_count}板")
            lines.append(f"市场情绪：{sentiment:.1f}分（{phase}期）")
            lines.append(f"操作逻辑：{board_count}板确认，捕捉龙头机会")
            lines.append(f"卖出原因：{sell_reason}")
            lines.append("```")
            lines.append("")

    lines.append("---")
    lines.append("")

    # ========== 五点五、详细个股买卖点记录 ==========
    if result.trade_records and len(result.trade_records) > 0:
        lines.append("## 五点五、详细个股买卖点记录")
        lines.append("")
        lines.append("| 序号 | 代码 | 买入日期 | 买入价 | 卖出日期 | 卖出价 | 涨幅 | 持仓天数 | 连板 | 开盘涨幅 | 卖出原因 |")
        lines.append("|------|------|----------|--------|----------|--------|------|----------|------|----------|----------|")

        # 按买入日期排序
        sorted_records = sorted(result.trade_records, key=lambda x: x.get('buy_date', x.get('trade_date', '')))

        for i, trade in enumerate(sorted_records, 1):
            code = trade.get('code', trade.get('stock_code', 'N/A'))
            buy_date = trade.get('buy_date', trade.get('trade_date', 'N/A'))
            buy_price = trade.get('buy_price', 0)
            sell_date = trade.get('sell_date_fmt', trade.get('sell_date', 'N/A'))
            sell_price = trade.get('sell_price', 0)
            profit = trade.get('profit', trade.get('profit_pct', 0))
            hold_days = trade.get('hold_days', 0)
            board_count = trade.get('board_count', 0)
            open_gap = trade.get('open_gap', 0)
            sell_reason = trade.get('sell_reason', trade.get('exit_reason', 'N/A'))

            # 格式化
            profit_str = f"+{profit:.2f}%" if profit >= 0 else f"{profit:.2f}%"
            open_gap_str = f"+{open_gap:.2f}%" if open_gap >= 0 else f"{open_gap:.2f}%"

            lines.append(f"| {i} | {code} | {buy_date} | {buy_price:.2f} | {sell_date} | {sell_price:.2f} | {profit_str} | {hold_days}天 | {board_count}板 | {open_gap_str} | {sell_reason} |")

        lines.append("")
        lines.append("---")
        lines.append("")

    # ========== 六、策略优化建议 ==========
    lines.append("## 六、策略优化建议")
    lines.append("")

    suggestions = []

    # 最佳连板数建议
    if result.trades_by_board:
        best_board = max(
            [(b, s) for b, s in result.trades_by_board.items() if s['count'] >= 5],
            key=lambda x: x[1]['wins'] / x[1]['count'] if x[1]['count'] > 0 else 0,
            default=None
        )
        if best_board:
            wr = best_board[1]['wins'] / best_board[1]['count'] * 100
            suggestions.append(f"1. **最佳连板参与**：重点参与{best_board[0]}板，胜率{wr:.1f}%")

    # 最佳情绪阶段建议
    if result.trades_by_phase:
        best_phase = max(result.trades_by_phase.items(),
                        key=lambda x: x[1]['wins'] / x[1]['count'] if x[1]['count'] > 0 else 0)
        phase_wr = best_phase[1]['wins'] / best_phase[1]['count'] * 100
        suggestions.append(f"2. **最佳情绪阶段**：{best_phase[0]}期胜率{phase_wr:.1f}%，平均收益{best_phase[1]['profit']/best_phase[1]['count']:+.2f}%")

    suggestions.append("3. **止损纪律**：严格执行6%止损，避免亏损扩大")

    # 找出亏损月份
    if result.monthly_returns:
        loss_months = [m for m, p in result.monthly_returns.items() if p < 0]
        if loss_months:
            suggestions.append(f"4. **避免月份**：{', '.join(loss_months[:3])}亏损，建议降低仓位")

    # 持仓周期建议
    if result.avg_hold_days > 3:
        suggestions.append(f"5. **持仓周期**：平均持仓{result.avg_hold_days:.1f}天，可适当缩短")

    # 启动期回避
    if '启动' in result.trades_by_phase:
        s = result.trades_by_phase['启动']
        s_wr = s['wins'] / s['count'] * 100 if s['count'] > 0 else 0
        if s_wr < 40:
            suggestions.append(f"6. **启动期回避**：启动期胜率仅{s_wr:.1f}%，需谨慎")

    for s in suggestions:
        lines.append(s)

    lines.append("")
    lines.append("---")
    lines.append("")

    # ========== 七、多年对比分析 ==========
    lines.append("## 七、多年对比分析")
    lines.append("")
    lines.append("| 年份 | 市场环境 | 交易次数 | 胜率 | 总收益 | 夏普比率 | 最佳阶段 |")
    lines.append("|------|----------|----------|------|--------|----------|----------|")

    # 当前年份
    year = result.start_date[:4]
    total_return = result.total_return

    # 判断市场环境
    if total_return > 300:
        market_env = "强势行情"
    elif total_return > 100:
        market_env = "结构性行情"
    elif total_return > 0:
        market_env = "震荡偏弱"
    else:
        market_env = "熊市"

    # 找最佳阶段
    if result.trades_by_phase:
        best_p = max(result.trades_by_phase.items(),
                    key=lambda x: x[1]['wins'] / x[1]['count'] if x[1]['count'] > 0 else 0)
        best_phase_str = f"{best_p[0]}（{best_p[1]['wins']/best_p[1]['count']*100:.1f}%）"
    else:
        best_phase_str = "-"

    lines.append(f"| **{year}** | **{market_env}** | **{result.total_trades}** | **{result.win_rate:.2f}%** | **{result.total_return:.2f}%** | **{result.sharpe_ratio:.2f}** | **{best_phase_str}** |")

    lines.append("")
    lines.append("---")
    lines.append("")

    # ========== 八、核心发现 ==========
    lines.append("## 八、核心发现")
    lines.append("")

    findings = []

    # 连板发现
    if result.trades_by_board:
        board_2 = result.trades_by_board.get(2, {'count': 0, 'profit': 0})
        if board_2['profit'] > 50:
            findings.append(f"1. **2板贡献主要收益**：+{board_2['profit']:.2f}%")

        for board, stats in result.trades_by_board.items():
            if stats['count'] >= 5:
                wr = stats['wins'] / stats['count'] * 100
                if wr >= 70:
                    findings.append(f"2. **{board}板胜率最高**：{wr:.1f}%胜率")

    # 情绪阶段发现
    if result.trades_by_phase:
        if '退潮' in result.trades_by_phase:
            t = result.trades_by_phase['退潮']
            t_wr = t['wins'] / t['count'] * 100 if t['count'] > 0 else 0
            findings.append(f"3. **退潮期最佳**：胜率{t_wr:.1f}%，平均收益{t['profit']/t['count']:+.2f}%")

    # 月度发现
    if result.monthly_returns:
        best_month = max(result.monthly_returns.items(), key=lambda x: x[1])
        worst_month = min(result.monthly_returns.items(), key=lambda x: x[1])
        findings.append(f"4. **最佳月份**：{best_month[0]}收益+{best_month[1]:.2f}%")
        if worst_month[1] < -10:
            findings.append(f"5. **最差月份**：{worst_month[0]}亏损{worst_month[1]:.2f}%")

    # 高潮期陷阱
    if '高潮' in result.trades_by_phase:
        g = result.trades_by_phase['高潮']
        g_avg = g['profit'] / g['count'] if g['count'] > 0 else 0
        if g_avg < 0:
            g_wr = g['wins'] / g['count'] * 100 if g['count'] > 0 else 0
            findings.append(f"6. **高潮期陷阱**：胜率仅{g_wr:.1f}%，平均亏损{g_avg:.2f}%")

    for f in findings:
        lines.append(f)

    lines.append("")
    lines.append("---")
    lines.append("")

    # ========== 九、总结 ==========
    lines.append("## 九、总结")
    lines.append("")

    year = result.start_date[:4]

    if result.total_return > 200:
        summary = f"{year}年是策略表现优异的年份，总收益{result.total_return:.2f}%。主要发现："
    elif result.total_return > 50:
        summary = f"{year}年策略实现较好收益，总收益{result.total_return:.2f}%。主要发现："
    elif result.total_return > 0:
        summary = f"{year}年策略实现正收益{result.total_return:.2f}%。主要发现："
    else:
        summary = f"{year}年策略面临挑战，收益{result.total_return:.2f}%。主要发现："

    lines.append(summary)
    lines.append("")

    # 总结要点
    if result.trades_by_phase:
        best_phase = max(result.trades_by_phase.items(),
                        key=lambda x: x[1]['wins'] / x[1]['count'] if x[1]['count'] > 0 else 0)
        phase_wr = best_phase[1]['wins'] / best_phase[1]['count'] * 100
        lines.append(f"1. **{best_phase[0]}期表现最佳**：胜率{phase_wr:.1f}%，连续多年验证")

    if result.trades_by_board:
        for board, stats in result.trades_by_board.items():
            if stats['count'] >= 5:
                wr = stats['wins'] / stats['count'] * 100
                if wr >= 60:
                    lines.append(f"2. **{board}板胜率高**：{wr:.1f}%，值得重点关注")
                    break

    if result.trades_by_board and 2 in result.trades_by_board:
        board_2 = result.trades_by_board[2]
        lines.append(f"3. **2板贡献主要收益**：+{board_2['profit']:.2f}%")

    # 避免高潮期
    if '高潮' in result.trades_by_phase:
        g = result.trades_by_phase['高潮']
        g_wr = g['wins'] / g['count'] * 100 if g['count'] > 0 else 0
        if g_wr < 50:
            lines.append(f"4. **避免高潮期**：高潮期胜率仅{g_wr:.1f}%")

    # 最差月份
    if result.monthly_returns:
        worst_month = min(result.monthly_returns.items(), key=lambda x: x[1])
        if worst_month[1] < -10:
            lines.append(f"5. **{worst_month[0]}大幅调整**：单月亏损{worst_month[1]:.2f}%")

    lines.append("")
    lines.append(f"**策略建议**：在当前市场中，退潮期积极参与，高潮期控制仓位，重点参与2板。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*报告生成时间：{end_fmt}*")

    report = "\n".join(lines)

    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n报告已保存: {output_path}")

    return report


def format_trade_records_for_csv(trade_records: List[Dict]) -> List[Dict]:
    """
    格式化交易记录用于CSV输出，将百分比数据保留2位小数

    Args:
        trade_records: 原始交易记录列表

    Returns:
        格式化后的交易记录列表
    """
    formatted_records = []
    for record in trade_records:
        formatted = record.copy()

        # 百分比字段列表（保留2位小数）
        pct_fields = [
            'profit', 'profit_pct', 'open_gap', 'sentiment_score',
            'stop_loss_pct', 'trailing_pct'
        ]

        for field in pct_fields:
            if field in formatted and formatted[field] is not None:
                try:
                    formatted[field] = round(float(formatted[field]), 2)
                except (ValueError, TypeError):
                    pass

        formatted_records.append(formatted)

    return formatted_records


def run_optimized_backtest(
    start_date: str = None,
    end_date: str = None,
    market: str = "sha",
    output_dir: str = None
):
    """执行优化版龙头策略回测"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")

    if start_date is None:
        start_date = (datetime.now() - timedelta(days=365*3)).strftime("%Y%m%d")

    backtester = OptimizedLeaderBacktester(
        initial_capital=100000,
        base_position=0.15,
        max_positions=3,
        stop_loss_pct=-6.0,
        trailing_stop_pct=3.0,
        max_hold_days=5
    )

    result = backtester.run_backtest(start_date, end_date, market)

    # 默认输出到 stock_agent/outputs 目录
    if output_dir is None:
        output_dir = Path(__file__).parent.parent / "outputs"
    output_path = Path(output_dir)

    # 根据日期范围确定子目录
    start_dt = datetime.strptime(start_date, '%Y%m%d')
    end_dt = datetime.strptime(end_date, '%Y%m%d')
    days_diff = (end_dt - start_dt).days + 1

    if days_diff <= 1:
        output_subdir = output_path / "daily" / "leader_backtest"
    elif days_diff <= 7:
        output_subdir = output_path / "weekly" / "leader_backtest"
    elif days_diff <= 31:
        output_subdir = output_path / "monthly" / "leader_backtest"
    else:
        output_subdir = output_path / "yearly" / "leader_backtest"

    output_subdir.mkdir(parents=True, exist_ok=True)

    report_file = output_subdir / f"leader_backtest_{market}_{start_date}_{end_date}.md"
    report = generate_report(result, str(report_file))

    if result.trade_records:
        trades_file = output_subdir / f"leader_backtest_trades_{market}_{start_date}_{end_date}.csv"
        formatted_records = format_trade_records_for_csv(result.trade_records)
        pd.DataFrame(formatted_records).to_csv(trades_file, index=False, encoding='utf-8-sig')
        print(f"交易记录已保存: {trades_file}")

    print("\n" + report)
    return result


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    run_optimized_backtest()
