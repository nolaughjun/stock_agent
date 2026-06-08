"""
二连板买入逻辑回测系统 - 优化版

改进内容：
====================
1. 删除"高开7-9%"场景 - 改为不参与（原胜率45%，平均亏损-1.29%）
2. 删除"低开4-8%"场景 - 改为不参与（原胜率33.3%，平均亏损-2.89%）
3. 增加情绪过滤 - 基于涨停家数判断市场情绪
4. 分级止损 - 第1天-10%，第2天-8%，第3天-6%

优化后买入逻辑：
====================
高开时：
- 高开0-2%：在-1%点买入（相对于昨日收盘价），仓位30%
- 高开2-3%：以开盘价买入，仓位50%（核心场景）
- 高开3-4%：不参与
- 高开4-5%：不参与（改为观望，原胜率37.5%）
- 高开5-7%：不买入
- 高开7-9%：不参与（删除，原亏损严重）
- 高开>9%：不参与

低开时：
- 低开0-4%：以-3.5%点买入，仓位40%（核心场景）
- 低开4-8%：不参与（删除，原接飞刀）
- 其它低开场景：不参与

卖出逻辑：
====================
- 分级止损：持仓第1天-10%，第2天-8%，第3天-6%
- 止盈：盈利超过15%
- 到期卖出：持仓超过3天
"""
from pathlib import Path
import sys

# 添加项目路径，确保模块导入正常
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

try:
    from app.stock_agent.data.data_loader import DataLoader, MarketType
except ImportError:
    from stock_agent.data.data_loader import DataLoader, MarketType


@dataclass
class BacktestResult:
    """回测结果"""
    start_date: str
    end_date: str
    market: str = "sha"
    total_trades: int = 0
    win_rate: float = 0.0
    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    avg_profit: float = 0.0
    avg_hold_days: float = 0.0
    trades_by_scene: Dict = field(default_factory=dict)
    trades_by_month: Dict = field(default_factory=dict)
    trade_records: List = field(default_factory=list)
    # 新增统计
    filtered_by_sentiment: int = 0  # 被情绪过滤掉的次数
    sentiment_stats: Dict = field(default_factory=dict)  # 情绪统计


@dataclass
class BuySignal:
    """买入信号"""
    code: str
    buy_price: float
    buy_type: str
    open_gap: float
    target_price: float
    limit_close: float
    scene: str
    can_buy: bool
    position_ratio: float = 0.3  # 仓位比例


class Board2BacktesterOptimized:
    """二连板买入逻辑回测器 - 优化版"""

    def __init__(
        self,
        initial_capital: float = 100000,
        take_profit_pct: float = 15.0,
        max_hold_days: int = 3,
        use_sentiment_filter: bool = True,  # 是否使用情绪过滤
    ):
        self.initial_capital = initial_capital
        self.take_profit_pct = take_profit_pct
        self.max_hold_days = max_hold_days
        self.use_sentiment_filter = use_sentiment_filter

        # 分级止损配置
        self.stop_loss_by_day = {
            0: -10.0,  # 第1天（买入当天）
            1: -10.0,  # 第2天
            2: -8.0,   # 第3天
            3: -6.0,   # 第4天及以后
        }

        self.data_loader = DataLoader()
        self.all_data = {}

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

    def _get_trade_dates(self, start_date: str, end_date: str) -> List[pd.Timestamp]:
        """获取交易日"""
        if not self.all_data:
            return []

        first_df = list(self.all_data.values())[0]
        df = first_df[(first_df.index >= start_date) & (first_df.index <= end_date)]
        return list(df.index)

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

    def _calc_market_sentiment(self, date: pd.Timestamp) -> Tuple[int, str, float]:
        """
        计算市场情绪

        Returns:
            (涨停家数, 情绪阶段, 情绪得分)
        """
        limit_up_count = 0
        limit_down_count = 0
        up_count = 0
        down_count = 0

        for code, df in self.all_data.items():
            df_day = df[df.index <= date]
            if len(df_day) < 2:
                continue

            today = df_day.iloc[-1]
            yesterday = df_day.iloc[-2]

            pct = (today['close'] - yesterday['close']) / yesterday['close'] * 100
            limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10

            if pct > 0:
                up_count += 1
            else:
                down_count += 1

            if pct >= limit_pct - 0.5:
                limit_up_count += 1
            elif pct <= -limit_pct + 0.5:
                limit_down_count += 1

        # 情绪得分
        total = up_count + down_count
        if total > 0:
            breadth = up_count / total
        else:
            breadth = 0.5

        # 涨停家数得分
        if limit_up_count >= 100:
            limit_score = 1.0
        elif limit_up_count >= 60:
            limit_score = 0.7
        elif limit_up_count >= 30:
            limit_score = 0.5
        else:
            limit_score = 0.3

        sentiment_score = (breadth * 0.4 + limit_score * 0.6) * 100

        # 情绪阶段判断
        if limit_up_count < 30:
            phase = "冰点"
        elif limit_up_count < 50:
            phase = "低迷"
        elif limit_up_count < 80:
            phase = "正常"
        elif limit_up_count < 120:
            phase = "活跃"
        else:
            phase = "过热"

        return limit_up_count, phase, sentiment_score

    def _check_buy_condition(self, code: str, open_gap: float, open_price: float,
                             high: float, low: float, limit_close: float) -> BuySignal:
        """
        检查买入条件并返回买入信号（优化版）
        """
        # 高开场景
        if open_gap >= 0:
            if 0 <= open_gap < 2:
                # 高开0-2%：在-1%点买入，仓位30%
                target_price = limit_close * 0.99
                can_buy = low <= target_price
                scene = f"高开0-2%，目标-1%买入"
                return BuySignal(
                    code=code,
                    buy_price=target_price,
                    buy_type="低吸买入",
                    open_gap=open_gap,
                    target_price=target_price,
                    limit_close=limit_close,
                    scene=scene,
                    can_buy=can_buy,
                    position_ratio=0.3
                )

            elif 2 <= open_gap < 3:
                # 高开2-3%：以开盘价买入，仓位50%（核心场景）
                scene = f"高开2-3%，开盘价买入"
                return BuySignal(
                    code=code,
                    buy_price=open_price,
                    buy_type="开盘买入",
                    open_gap=open_gap,
                    target_price=open_price,
                    limit_close=limit_close,
                    scene=scene,
                    can_buy=True,
                    position_ratio=0.5
                )

            elif 3 <= open_gap < 4:
                # 高开3-4%：不参与
                scene = f"高开3-4%，不参与"
                return BuySignal(
                    code=code,
                    buy_price=0,
                    buy_type="不参与",
                    open_gap=open_gap,
                    target_price=0,
                    limit_close=limit_close,
                    scene=scene,
                    can_buy=False,
                    position_ratio=0
                )

            elif 4 <= open_gap < 5:
                # 高开4-5%：不参与（优化：改为观望）
                scene = f"高开4-5%，不参与"
                return BuySignal(
                    code=code,
                    buy_price=0,
                    buy_type="不参与",
                    open_gap=open_gap,
                    target_price=0,
                    limit_close=limit_close,
                    scene=scene,
                    can_buy=False,
                    position_ratio=0
                )

            elif 5 <= open_gap < 7:
                # 高开5-7%：不买入
                scene = f"高开5-7%，不参与"
                return BuySignal(
                    code=code,
                    buy_price=0,
                    buy_type="不参与",
                    open_gap=open_gap,
                    target_price=0,
                    limit_close=limit_close,
                    scene=scene,
                    can_buy=False,
                    position_ratio=0
                )

            elif 7 <= open_gap < 9:
                # 高开7-9%：不参与（优化：删除此场景）
                scene = f"高开7-9%，不参与"
                return BuySignal(
                    code=code,
                    buy_price=0,
                    buy_type="不参与",
                    open_gap=open_gap,
                    target_price=0,
                    limit_close=limit_close,
                    scene=scene,
                    can_buy=False,
                    position_ratio=0
                )

            else:
                # 高开>=9%：不参与
                scene = f"高开>=9%，不参与"
                return BuySignal(
                    code=code,
                    buy_price=0,
                    buy_type="不参与",
                    open_gap=open_gap,
                    target_price=0,
                    limit_close=limit_close,
                    scene=scene,
                    can_buy=False,
                    position_ratio=0
                )

        # 低开场景
        else:
            if -4 <= open_gap < 0:
                # 低开0-4%：以-3.5%点买入，仓位40%（核心场景）
                target_price = limit_close * 0.965
                can_buy = low <= target_price
                scene = f"低开0-4%，目标-3.5%买入"
                return BuySignal(
                    code=code,
                    buy_price=target_price,
                    buy_type="低吸买入",
                    open_gap=open_gap,
                    target_price=target_price,
                    limit_close=limit_close,
                    scene=scene,
                    can_buy=can_buy,
                    position_ratio=0.4
                )

            elif -8 <= open_gap < -4:
                # 低开4-8%：不参与（优化：删除此场景）
                scene = f"低开4-8%，不参与"
                return BuySignal(
                    code=code,
                    buy_price=0,
                    buy_type="不参与",
                    open_gap=open_gap,
                    target_price=0,
                    limit_close=limit_close,
                    scene=scene,
                    can_buy=False,
                    position_ratio=0
                )

            else:
                # 其它低开场景：不参与
                scene = f"低开>8%，不参与"
                return BuySignal(
                    code=code,
                    buy_price=0,
                    buy_type="不参与",
                    open_gap=open_gap,
                    target_price=0,
                    limit_close=limit_close,
                    scene=scene,
                    can_buy=False,
                    position_ratio=0
                )

    def _find_board2_stocks(self, date: pd.Timestamp) -> List[Dict]:
        """找出当日涨停的二连板股票"""
        board2_stocks = []

        for code, df in self.all_data.items():
            df_day = df[df.index <= date]
            if len(df_day) < 20:
                continue

            today = df_day.iloc[-1]
            yesterday = df_day.iloc[-2]

            pct = (today['close'] - yesterday['close']) / yesterday['close'] * 100
            limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10

            if pct < limit_pct - 0.5 or pct > limit_pct + 1:
                continue

            board_count = self._count_boards(df_day, limit_pct)

            if board_count != 2:
                continue

            board2_stocks.append({
                'code': code,
                'close': today['close'],
                'high': today['high'],
                'low': today['low'],
                'amount': today['amount'],
                'limit_pct': limit_pct,
            })

        return board2_stocks

    def _get_stop_loss_pct(self, hold_days: int) -> float:
        """根据持仓天数获取止损比例"""
        if hold_days <= 0:
            return self.stop_loss_by_day[0]
        elif hold_days == 1:
            return self.stop_loss_by_day[1]
        elif hold_days == 2:
            return self.stop_loss_by_day[2]
        else:
            return self.stop_loss_by_day[3]

    def _process_positions(
        self,
        positions: List[Dict],
        date: pd.Timestamp
    ) -> Tuple[List[Dict], List[Dict]]:
        """处理持仓：检查分级止损、止盈、到期"""
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

            # 1. 分级止损
            stop_loss_pct = self._get_stop_loss_pct(hold_days)
            if profit_pct <= stop_loss_pct:
                exit_reason = f"止损({profit_pct:.1f}%，第{hold_days+1}天阈值{stop_loss_pct}%)"

            # 2. 止盈
            elif profit_pct >= self.take_profit_pct:
                exit_reason = f"止盈({profit_pct:.1f}%)"

            # 3. 到期
            elif hold_days >= self.max_hold_days:
                exit_reason = "到期"

            if exit_reason:
                trade_record = {
                    'buy_date': pos['buy_date'].strftime("%Y-%m-%d"),
                    'stock_code': pos['code'],
                    'buy_price': buy_price,
                    'sell_date': date.strftime("%Y-%m-%d"),
                    'sell_price': current_price,
                    'profit_pct': profit_pct,
                    'hold_days': hold_days,
                    'is_win': profit_pct > 0,
                    'exit_reason': exit_reason,
                    'scene': pos.get('scene', ''),
                    'open_gap': pos.get('open_gap', 0),
                    'buy_type': pos.get('buy_type', ''),
                    'position_ratio': pos.get('position_ratio', 0.3),
                }

                closed_trades.append(trade_record)
            else:
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
        print("二连板买入逻辑回测 - 优化版")
        print("=" * 80)
        print(f"回测区间: {start_date} - {end_date}")
        print(f"市场: {market}")
        print(f"初始资金: {self.initial_capital:,.0f}")
        print(f"止盈: {self.take_profit_pct}%")
        print(f"最大持仓: {self.max_hold_days}天")
        print(f"分级止损: 第1天-10%, 第2天-10%, 第3天-8%, 第4天+-6%")
        print(f"情绪过滤: {'开启' if self.use_sentiment_filter else '关闭'}")
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
        trades_by_scene = {}
        trades_by_month = {}
        filtered_by_sentiment = 0
        sentiment_stats = {}

        # 统计各种场景
        scene_stats = {
            '高开0-2%': {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0},
            '高开2-3%': {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0},
            '高开3-4%': {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0},
            '高开4-5%': {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0},
            '高开5-7%': {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0},
            '高开7-9%': {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0},
            '高开>=9%': {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0},
            '低开0-4%': {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0},
            '低开4-8%': {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0},
            '低开>8%': {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0},
        }

        total_dates = len(trade_dates)

        # 主循环
        for i, date in enumerate(trade_dates):
            if (i + 1) % 20 == 0:
                print(f"\r回测进度: {i+1}/{total_dates} ({(i+1)/total_dates*100:.1f}%)", end="")

            # 计算市场情绪
            limit_up_count, sentiment_phase, sentiment_score = self._calc_market_sentiment(date)

            # 记录情绪统计
            date_str = date.strftime("%Y-%m-%d")
            sentiment_stats[date_str] = {
                'limit_up_count': limit_up_count,
                'phase': sentiment_phase,
                'score': sentiment_score
            }

            # 情绪过滤：冰点或过热时减少操作
            sentiment_filter_active = False
            if self.use_sentiment_filter:
                if limit_up_count < 30 or limit_up_count > 120:
                    sentiment_filter_active = True

            # 处理持仓
            positions, closed_trades = self._process_positions(positions, date)

            for trade in closed_trades:
                trade_records.append(trade)

            # 找出昨日的二连板股票（今日开盘买入）
            if i > 0 and not sentiment_filter_active:
                prev_date = trade_dates[i - 1]
                board2_stocks = self._find_board2_stocks(prev_date)

                for stock in board2_stocks:
                    code = stock['code']
                    limit_close = stock['close']

                    df = self.all_data.get(code)
                    if df is None:
                        continue

                    df_today = df[df.index <= date]
                    if len(df_today) < 1 or df_today.index[-1] != date:
                        continue

                    today = df_today.iloc[-1]
                    open_price = today['open']
                    high = today['high']
                    low = today['low']

                    open_gap = (open_price - limit_close) / limit_close * 100

                    signal = self._check_buy_condition(
                        code, open_gap, open_price, high, low, limit_close
                    )

                    # 确定场景分类
                    if open_gap >= 0:
                        if open_gap < 2:
                            scene_key = '高开0-2%'
                        elif open_gap < 3:
                            scene_key = '高开2-3%'
                        elif open_gap < 4:
                            scene_key = '高开3-4%'
                        elif open_gap < 5:
                            scene_key = '高开4-5%'
                        elif open_gap < 7:
                            scene_key = '高开5-7%'
                        elif open_gap < 9:
                            scene_key = '高开7-9%'
                        else:
                            scene_key = '高开>=9%'
                    else:
                        if open_gap >= -4:
                            scene_key = '低开0-4%'
                        elif open_gap >= -8:
                            scene_key = '低开4-8%'
                        else:
                            scene_key = '低开>8%'

                    scene_stats[scene_key]['attempts'] += 1

                    if signal.can_buy and signal.buy_price > 0:
                        scene_stats[scene_key]['can_buy_count'] += 1

                        if not any(p['code'] == code for p in positions):
                            positions.append({
                                'code': code,
                                'buy_date': date,
                                'buy_price': signal.buy_price,
                                'scene': signal.scene,
                                'open_gap': open_gap,
                                'buy_type': signal.buy_type,
                                'position_ratio': signal.position_ratio,
                            })

            elif sentiment_filter_active and i > 0:
                # 记录被情绪过滤的次数
                prev_date = trade_dates[i - 1]
                board2_stocks = self._find_board2_stocks(prev_date)
                filtered_by_sentiment += len(board2_stocks)

        print()

        # 按场景统计交易结果
        for trade in trade_records:
            scene = trade.get('scene', '')
            profit = trade.get('profit_pct', 0)

            for scene_key in scene_stats.keys():
                if scene_key in scene:
                    scene_stats[scene_key]['count'] += 1
                    scene_stats[scene_key]['profit'] += profit
                    if trade['is_win']:
                        scene_stats[scene_key]['wins'] += 1
                    break

            month = trade['buy_date'][:7]
            if month not in trades_by_month:
                trades_by_month[month] = {'count': 0, 'profit': 0, 'wins': 0}
            trades_by_month[month]['count'] += 1
            trades_by_month[month]['profit'] += profit
            if trade['is_win']:
                trades_by_month[month]['wins'] += 1

        return self._calc_result(
            trade_records, start_date, end_date,
            scene_stats, trades_by_month,
            filtered_by_sentiment, sentiment_stats
        )

    def _empty_result(self, start_date: str, end_date: str) -> BacktestResult:
        """返回空结果"""
        return BacktestResult(
            start_date=start_date, end_date=end_date,
            total_trades=0, win_rate=0, total_return=0,
            annual_return=0, max_drawdown=0, sharpe_ratio=0,
            profit_factor=0, avg_profit=0, avg_hold_days=0,
            trades_by_scene={}, trades_by_month={}, trade_records=[],
            filtered_by_sentiment=0, sentiment_stats={}
        )

    def _calc_result(
        self,
        trade_records: List[Dict],
        start_date: str,
        end_date: str,
        trades_by_scene: Dict,
        trades_by_month: Dict,
        filtered_by_sentiment: int,
        sentiment_stats: Dict
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

        wins = df[df['is_win']]['profit_pct']
        losses = df[~df['is_win']]['profit_pct']

        if len(losses) > 0 and losses.mean() != 0:
            profit_factor = abs(wins.mean() / losses.mean())
        else:
            profit_factor = 0

        days = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days
        years = max(days / 365, 1)
        annual_return = total_return / years

        if len(df) > 1:
            sharpe_ratio = df['profit_pct'].mean() / df['profit_pct'].std() * np.sqrt(252/3)
        else:
            sharpe_ratio = 0

        return BacktestResult(
            start_date=start_date, end_date=end_date,
            total_trades=total_trades, win_rate=win_rate,
            total_return=total_return, annual_return=annual_return,
            max_drawdown=0,
            sharpe_ratio=sharpe_ratio, profit_factor=profit_factor,
            avg_profit=avg_profit, avg_hold_days=avg_hold_days,
            trades_by_scene=trades_by_scene, trades_by_month=trades_by_month,
            trade_records=trade_records,
            filtered_by_sentiment=filtered_by_sentiment,
            sentiment_stats=sentiment_stats
        )


def generate_report(result: BacktestResult, output_path: str = None) -> str:
    """生成详细分析报告"""
    lines = []

    start_fmt = f"{result.start_date[:4]}-{result.start_date[4:6]}-{result.start_date[6:]}"
    end_fmt = f"{result.end_date[:4]}-{result.end_date[4:6]}-{result.end_date[6:]}"

    lines.append("# 二连板买入逻辑回测报告 - 优化版")
    lines.append("")
    lines.append("## 一、策略优化说明")
    lines.append("")
    lines.append("### 优化内容")
    lines.append("| 优化项 | 原策略 | 优化后 | 优化理由 |")
    lines.append("|--------|--------|--------|----------|")
    lines.append("| 高开7-9% | 开盘价买入 | **不参与** | 原胜率45%，平均亏损-1.29% |")
    lines.append("| 低开4-8% | -6%点买入 | **不参与** | 原胜率33.3%，接飞刀行为 |")
    lines.append("| 高开4-5% | 开盘价买入 | **不参与** | 原胜率37.5%，风险较高 |")
    lines.append("| 止损方式 | 固定8% | **分级止损** | 适应不同持仓阶段 |")
    lines.append("| 情绪过滤 | 无 | **新增** | 冰点/过热时减少操作 |")
    lines.append("")

    lines.append("### 优化后买入逻辑")
    lines.append("")
    lines.append("#### 高开场景")
    lines.append("| 开盘涨幅 | 操作 | 仓位 | 说明 |")
    lines.append("|----------|------|------|------|")
    lines.append("| 高开0-2% | -1%点买入 | 30% | 低吸买入 |")
    lines.append("| 高开2-3% | 开盘价买入 | **50%** | 核心场景 |")
    lines.append("| 高开3-4% | 不参与 | - | 陷阱区 |")
    lines.append("| 高开4-5% | 不参与 | - | 优化：改为观望 |")
    lines.append("| 高开5-7% | 不参与 | - | 高位风险大 |")
    lines.append("| 高开7-9% | 不参与 | - | 优化：删除 |")
    lines.append("| 高开≥9% | 不参与 | - | 过于激进 |")
    lines.append("")

    lines.append("#### 低开场景")
    lines.append("| 开盘涨幅 | 操作 | 仓位 | 说明 |")
    lines.append("|----------|------|------|------|")
    lines.append("| 低开0-4% | -3.5%点买入 | **40%** | 核心场景 |")
    lines.append("| 低开4-8% | 不参与 | - | 优化：删除 |")
    lines.append("| 低开>8% | 不参与 | - | 走势太弱 |")
    lines.append("")

    lines.append("### 分级止损")
    lines.append("| 持仓天数 | 止损阈值 | 说明 |")
    lines.append("|----------|----------|------|")
    lines.append("| 第1天 | -10% | 容忍大波动 |")
    lines.append("| 第2天 | -10% | 容忍大波动 |")
    lines.append("| 第3天 | -8% | 收紧止损 |")
    lines.append("| 第4天+ | -6% | 快到期，严格止损 |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 回测概况
    lines.append("## 二、回测概况")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 回测区间 | {start_fmt} 至 {end_fmt} |")
    lines.append(f"| 总交易次数 | {result.total_trades} 笔 |")
    lines.append(f"| 胜率 | {result.win_rate:.2f}% |")
    lines.append(f"| 总收益 | {result.total_return:.2f}% |")
    lines.append(f"| 年化收益 | {result.annual_return:.2f}% |")
    lines.append(f"| 夏普比率 | {result.sharpe_ratio:.2f} |")
    lines.append(f"| 盈亏比 | {result.profit_factor:.2f} |")
    lines.append(f"| 平均收益 | {result.avg_profit:.2f}% |")
    lines.append(f"| 平均持仓 | {result.avg_hold_days:.1f}天 |")
    lines.append(f"| 情绪过滤次数 | {result.filtered_by_sentiment} 次 |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 各场景表现
    lines.append("## 三、各场景表现分析")
    lines.append("")
    lines.append("### 高开场景")
    lines.append("")
    lines.append("| 场景 | 出现次数 | 可买入次数 | 实际交易 | 胜率 | 平均收益 | 总收益 | 操作建议 |")
    lines.append("|------|----------|------------|----------|------|----------|--------|----------|")

    high_open_scenes = ['高开0-2%', '高开2-3%', '高开3-4%', '高开4-5%', '高开5-7%', '高开7-9%', '高开>=9%']

    for scene in high_open_scenes:
        stats = result.trades_by_scene.get(scene, {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0})
        attempts = stats.get('attempts', 0)
        can_buy = stats.get('can_buy_count', 0)
        count = stats['count']

        if count > 0:
            wr = stats['wins'] / count * 100
            avg = stats['profit'] / count
        else:
            wr = 0
            avg = 0

        if scene in ['高开3-4%', '高开4-5%', '高开5-7%', '高开7-9%', '高开>=9%']:
            suggest = "不参与"
        elif wr >= 60 and avg > 2:
            suggest = "**推荐参与**"
        elif wr >= 50:
            suggest = "可参与"
        else:
            suggest = "谨慎参与"

        lines.append(f"| **{scene}** | {attempts} | {can_buy} | {count} | {wr:.1f}% | {avg:+.2f}% | {stats['profit']:+.2f}% | {suggest} |")

    lines.append("")
    lines.append("### 低开场景")
    lines.append("")
    lines.append("| 场景 | 出现次数 | 可买入次数 | 实际交易 | 胜率 | 平均收益 | 总收益 | 操作建议 |")
    lines.append("|------|----------|------------|----------|------|----------|--------|----------|")

    low_open_scenes = ['低开0-4%', '低开4-8%', '低开>8%']

    for scene in low_open_scenes:
        stats = result.trades_by_scene.get(scene, {'count': 0, 'profit': 0, 'wins': 0, 'attempts': 0, 'can_buy_count': 0})
        attempts = stats.get('attempts', 0)
        can_buy = stats.get('can_buy_count', 0)
        count = stats['count']

        if count > 0:
            wr = stats['wins'] / count * 100
            avg = stats['profit'] / count
        else:
            wr = 0
            avg = 0

        if scene in ['低开4-8%', '低开>8%']:
            suggest = "不参与"
        elif wr >= 60 and avg > 2:
            suggest = "**推荐参与**"
        elif wr >= 50:
            suggest = "可参与"
        else:
            suggest = "谨慎参与"

        lines.append(f"| **{scene}** | {attempts} | {can_buy} | {count} | {wr:.1f}% | {avg:+.2f}% | {stats['profit']:+.2f}% | {suggest} |")

    lines.append("")
    lines.append("---")
    lines.append("")

    # 场景深度分析
    lines.append("## 四、场景深度分析")
    lines.append("")

    best_scene = None
    best_wr = 0
    best_avg = 0

    for scene, stats in result.trades_by_scene.items():
        if stats['count'] >= 5:
            wr = stats['wins'] / stats['count'] * 100
            avg = stats['profit'] / stats['count']
            if wr > best_wr or (wr == best_wr and avg > best_avg):
                best_wr = wr
                best_avg = avg
                best_scene = scene

    if best_scene:
        lines.append(f"### 最佳场景：{best_scene}")
        lines.append("")
        stats = result.trades_by_scene[best_scene]
        lines.append(f"- **胜率**: {best_wr:.1f}%")
        lines.append(f"- **平均收益**: {best_avg:+.2f}%")
        lines.append(f"- **交易次数**: {stats['count']}笔")
        lines.append(f"- **总收益**: {stats['profit']:+.2f}%")
        lines.append("")

    lines.append("---")
    lines.append("")

    # 月度收益
    if result.trades_by_month:
        lines.append("## 五、月度收益分析")
        lines.append("")
        lines.append("| 月份 | 交易次数 | 胜率 | 月度收益 |")
        lines.append("|------|----------|------|----------|")

        for month in sorted(result.trades_by_month.keys()):
            stats = result.trades_by_month[month]
            if stats['count'] > 0:
                wr = stats['wins'] / stats['count'] * 100
                lines.append(f"| **{month}** | {stats['count']} | {wr:.1f}% | {stats['profit']:+.2f}% |")

        lines.append("")
        lines.append("---")
        lines.append("")

    # 详细交易记录
    if result.trade_records and len(result.trade_records) > 0:
        lines.append("## 六、详细交易记录")
        lines.append("")
        lines.append("| 序号 | 代码 | 买入日期 | 买入价 | 卖出日期 | 危出价 | 涨幅 | 持仓天数 | 开盘涨幅 | 场景 | 危出原因 |")
        lines.append("|------|------|----------|--------|----------|--------|------|----------|----------|------|----------|")

        sorted_records = sorted(result.trade_records, key=lambda x: x.get('buy_date', ''))

        for i, trade in enumerate(sorted_records, 1):
            code = trade.get('stock_code', 'N/A')
            buy_date = trade.get('buy_date', 'N/A')
            buy_price = trade.get('buy_price', 0)
            sell_date = trade.get('sell_date', 'N/A')
            sell_price = trade.get('sell_price', 0)
            profit = trade.get('profit_pct', 0)
            hold_days = trade.get('hold_days', 0)
            open_gap = trade.get('open_gap', 0)
            scene = trade.get('scene', '')
            exit_reason = trade.get('exit_reason', 'N/A')

            profit_str = f"+{profit:.2f}%" if profit >= 0 else f"{profit:.2f}%"
            open_gap_str = f"+{open_gap:.2f}%" if open_gap >= 0 else f"{open_gap:.2f}%"

            lines.append(f"| {i} | {code} | {buy_date} | {buy_price:.2f} | {sell_date} | {sell_price:.2f} | {profit_str} | {hold_days}天 | {open_gap_str} | {scene} | {exit_reason} |")

        lines.append("")
        lines.append("---")
        lines.append("")

    # 策略总结
    lines.append("## 七、策略总结")
    lines.append("")

    high_open_count = 0
    high_open_profit = 0
    high_open_wins = 0
    low_open_count = 0
    low_open_profit = 0
    low_open_wins = 0

    for scene in high_open_scenes:
        stats = result.trades_by_scene.get(scene, {'count': 0, 'profit': 0, 'wins': 0})
        high_open_count += stats['count']
        high_open_profit += stats['profit']
        high_open_wins += stats['wins']

    for scene in low_open_scenes:
        stats = result.trades_by_scene.get(scene, {'count': 0, 'profit': 0, 'wins': 0})
        low_open_count += stats['count']
        low_open_profit += stats['profit']
        low_open_wins += stats['wins']

    if high_open_count > 0:
        high_wr = high_open_wins / high_open_count * 100
        high_avg = high_open_profit / high_open_count
        lines.append(f"### 高开场景总体表现")
        lines.append(f"- 交易次数: {high_open_count}笔")
        lines.append(f"- 胜率: {high_wr:.1f}%")
        lines.append(f"- 平均收益: {high_avg:+.2f}%")
        lines.append(f"- 总收益: {high_open_profit:+.2f}%")
        lines.append("")

    if low_open_count > 0:
        low_wr = low_open_wins / low_open_count * 100
        low_avg = low_open_profit / low_open_count
        lines.append(f"### 低开场景总体表现")
        lines.append(f"- 交易次数: {low_open_count}笔")
        lines.append(f"- 胜率: {low_wr:.1f}%")
        lines.append(f"- 平均收益: {low_avg:+.2f}%")
        lines.append(f"- 总收益: {low_open_profit:+.2f}%")
        lines.append("")

    lines.append("### 核心发现")
    lines.append("")

    findings = []

    if best_scene:
        findings.append(f"1. **最佳参与场景**: {best_scene}，胜率{best_wr:.1f}%，平均收益{best_avg:+.2f}%")

    if high_open_count > 0 and low_open_count > 0:
        if high_wr > low_wr:
            findings.append(f"2. **高开优于低开**: 高开胜率{high_wr:.1f}% > 低开胜率{low_wr:.1f}%")
        else:
            findings.append(f"2. **低开优于高开**: 低开胜率{low_wr:.1f}% > 高开胜率{high_wr:.1f}%")

    for f in findings:
        lines.append(f)

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*报告生成时间: {end_fmt}*")

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
            'stop_loss_pct', 'trailing_pct', 'buy_gap'
        ]

        for field in pct_fields:
            if field in formatted and formatted[field] is not None:
                try:
                    formatted[field] = round(float(formatted[field]), 2)
                except (ValueError, TypeError):
                    pass

        formatted_records.append(formatted)

    return formatted_records


def run_board2_backtest_optimized(
    start_date: str = None,
    end_date: str = None,
    market: str = "sha",
    output_dir: str = None
):
    """执行二连板买入逻辑回测 - 优化版"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")

    if start_date is None:
        start_date = "20260101"

    backtester = Board2BacktesterOptimized(
        initial_capital=100000,
        take_profit_pct=15.0,
        max_hold_days=3,
        use_sentiment_filter=True
    )

    result = backtester.run_backtest(start_date, end_date, market)

    if output_dir is None:
        output_dir = Path(__file__).parent.parent / "outputs"
    output_path = Path(output_dir)

    output_subdir = output_path / "board2_backtest"
    output_subdir.mkdir(parents=True, exist_ok=True)

    report_file = output_subdir / f"board2_backtest_optimized_{market}_{start_date}_{end_date}.md"
    report = generate_report(result, str(report_file))

    if result.trade_records:
        trades_file = output_subdir / f"board2_backtest_optimized_trades_{market}_{start_date}_{end_date}.csv"
        formatted_records = format_trade_records_for_csv(result.trade_records)
        pd.DataFrame(formatted_records).to_csv(trades_file, index=False, encoding='utf-8-sig')
        print(f"交易记录已保存: {trades_file}")

    print("\n" + report)
    return result


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    run_board2_backtest_optimized(
        start_date="20260101",
        end_date=datetime.now().strftime("%Y%m%d"),
        market="sha"
    )
