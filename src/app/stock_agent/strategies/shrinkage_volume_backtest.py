"""
缩量涨停风险回测对比脚本

对比V3策略在以下两种情况下的表现：
1. 不过滤缩量涨停（原V3策略）
2. 过滤缩量涨停（新增风险检测）

缩量涨停定义：2板成交额 < 首板成交额的70%

使用方法：
    cd e:\test_python
    python src\app\stock_agent\strategies\shrinkage_volume_backtest.py --start 20260101 --end 20261231 --market sha
"""
from pathlib import Path
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from stock_agent.data.data_loader import DataLoader, MarketType


@dataclass
class BacktestResult:
    """回测结果"""
    total_trades: int = 0
    win_rate: float = 0.0
    avg_profit: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    # 缩量涨停统计
    shrinkage_trades: int = 0
    shrinkage_win_rate: float = 0.0
    shrinkage_avg_profit: float = 0.0


class ShrinkageVolumeBacktest:
    """缩量涨停回测对比"""

    def __init__(self):
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
        board_count = 0
        for i in range(-1, -min(10, len(df)), -1):
            if i - 1 < -len(df):
                break
            today = df.iloc[i]
            yesterday = df.iloc[i - 1]
            pct = (today['close'] - yesterday['close']) / yesterday['close'] * 100
            if pct >= limit_pct - 0.5:
                board_count += 1
            else:
                break
        return board_count

    def _detect_shrinkage_volume(self, df: pd.DataFrame, board_count: int) -> Tuple[bool, float]:
        """
        检测缩量涨停

        Returns:
            (是否缩量涨停, 成交额比例)
        """
        if board_count != 2 or len(df) < 3:
            return False, 1.0

        # 今日(2板)成交额
        today_amount = df.iloc[-1]['amount']
        # 首板成交额
        first_board_amount = df.iloc[-2]['amount']

        if first_board_amount <= 0:
            return False, 1.0

        ratio = today_amount / first_board_amount
        return ratio < 0.7, ratio

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
                board_count = self._count_boards(df_day, limit_pct)
                if board_count >= 3:
                    high_board += 1
            elif pct <= -limit_pct + 0.5:
                limit_down += 1

        total = up + down
        breadth = up / total if total > 0 else 0.5

        limit_total = limit_up + limit_down
        limit_score = (limit_up - limit_down) / limit_total if limit_total > 0 else 0
        limit_score = (limit_score + 1) / 2

        board_bonus = min(high_board / 50, 0.2) if limit_up > 0 else 0
        sentiment = (breadth * 0.4 + limit_score * 0.4 + 0.2 + board_bonus) * 100
        sentiment = min(100, max(0, sentiment))

        if sentiment < 30:
            phase = '冰点'
        elif sentiment < 50:
            phase = '启动'
        elif sentiment < 70:
            phase = '发酵'
        elif sentiment < 85:
            phase = '高潮'
        else:
            phase = '退潮'

        return sentiment, phase

    def run_backtest(
        self,
        start_date: str,
        end_date: str,
        market: str = "sha",
        filter_shrinkage: bool = False,
    ) -> BacktestResult:
        """
        运行回测

        Args:
            filter_shrinkage: 是否过滤缩量涨停
        """
        self._preload_data(start_date, end_date, market)
        trade_dates = self._get_trade_dates(start_date, end_date)

        if not trade_dates:
            return None

        trade_records = []
        shrinkage_records = []  # 缩量涨停交易记录

        positions = []

        print(f"\n运行回测 (过滤缩量涨停: {filter_shrinkage})...")

        for i, date in enumerate(trade_dates):
            if (i + 1) % 50 == 0:
                print(f"  进度: {i+1}/{len(trade_dates)}")

            # 更新持仓
            new_positions = []
            for pos in positions:
                df = self.all_data.get(pos['code'])
                if df is None:
                    continue

                df_day = df[df.index <= date]
                if len(df_day) < 1:
                    continue

                today = df_day.iloc[-1]
                current_profit = (today['close'] - pos['buy_price']) / pos['buy_price'] * 100

                # 检查止损
                if current_profit <= -6:
                    record = {
                        'profit': current_profit,
                        'hold_days': pos['hold_days'],
                        'board_count': pos['board_count'],
                        'is_shrinkage': pos.get('is_shrinkage', False),
                    }
                    trade_records.append(record)
                    if pos.get('is_shrinkage', False):
                        shrinkage_records.append(record)
                    continue

                # 检查持仓天数
                if pos['hold_days'] >= 5:
                    record = {
                        'profit': current_profit,
                        'hold_days': pos['hold_days'],
                        'board_count': pos['board_count'],
                        'is_shrinkage': pos.get('is_shrinkage', False),
                    }
                    trade_records.append(record)
                    if pos.get('is_shrinkage', False):
                        shrinkage_records.append(record)
                    continue

                pos['hold_days'] += 1
                pos['max_price'] = max(pos.get('max_price', pos['buy_price']), today['high'])

                # 移动止损
                if pos['max_price'] > pos['buy_price'] * 1.05:
                    if today['close'] < pos['max_price'] * 0.97:
                        record = {
                            'profit': current_profit,
                            'hold_days': pos['hold_days'],
                            'board_count': pos['board_count'],
                            'is_shrinkage': pos.get('is_shrinkage', False),
                        }
                        trade_records.append(record)
                        if pos.get('is_shrinkage', False):
                            shrinkage_records.append(record)
                        continue

                new_positions.append(pos)

            positions = new_positions

            # 扫描候选股
            sentiment, phase = self._calc_sentiment(date)

            # V3情绪筛选
            if phase in ['冰点', '启动']:
                continue

            candidates = []
            for code, df in self.all_data.items():
                df_day = df[df.index <= date]
                if len(df_day) < 2:
                    continue

                today = df_day.iloc[-1]
                yesterday = df_day.iloc[-2]

                limit_pct = 20 if code.startswith('30') else 10
                pct = (today['close'] - yesterday['close']) / yesterday['close'] * 100

                if pct < limit_pct - 0.5:
                    continue

                board_count = self._count_boards(df_day, limit_pct)

                # V3: 剔除0板
                if board_count == 0:
                    continue

                # V3: 只参与2板和3板
                if board_count not in [2, 3]:
                    continue

                # 检测缩量涨停
                is_shrinkage, ratio = self._detect_shrinkage_volume(df_day, board_count)

                # 如果启用过滤且是缩量涨停，跳过
                if filter_shrinkage and is_shrinkage:
                    continue

                candidates.append({
                    'code': code,
                    'board_count': board_count,
                    'close': today['close'],
                    'is_shrinkage': is_shrinkage,
                    'volume_ratio': ratio,
                })

            # 买入（最多3只）
            if candidates and len(positions) < 3:
                for cand in candidates[:3 - len(positions)]:
                    positions.append({
                        'code': cand['code'],
                        'buy_date': date,
                        'buy_price': cand['close'],
                        'hold_days': 0,
                        'max_price': cand['close'],
                        'board_count': cand['board_count'],
                        'is_shrinkage': cand['is_shrinkage'],
                    })

        # 计算结果
        if not trade_records:
            return None

        df_trades = pd.DataFrame(trade_records)

        total_trades = len(df_trades)
        win_rate = (df_trades['profit'] > 0).sum() / total_trades * 100
        avg_profit = df_trades['profit'].mean()
        total_return = df_trades['profit'].sum()

        # 缩量涨停统计
        shrinkage_trades = len(shrinkage_records)
        shrinkage_win_rate = 0
        shrinkage_avg_profit = 0
        if shrinkage_records:
            df_shrinkage = pd.DataFrame(shrinkage_records)
            shrinkage_win_rate = (df_shrinkage['profit'] > 0).sum() / shrinkage_trades * 100
            shrinkage_avg_profit = df_shrinkage['profit'].mean()

        return BacktestResult(
            total_trades=total_trades,
            win_rate=win_rate,
            avg_profit=avg_profit,
            total_return=total_return,
            shrinkage_trades=shrinkage_trades,
            shrinkage_win_rate=shrinkage_win_rate,
            shrinkage_avg_profit=shrinkage_avg_profit,
        )


def run_comparison(start_date: str = "20260101", end_date: str = "20261231", market: str = "sha"):
    """运行对比回测"""
    print("=" * 80)
    print("缩量涨停风险回测对比")
    print("=" * 80)
    print(f"时间范围: {start_date} - {end_date}")
    print(f"市场: {market}")
    print("=" * 80)

    backtest = ShrinkageVolumeBacktest()

    # 1. 不过滤缩量涨停
    print("\n[测试1] 不过滤缩量涨停（原V3策略）")
    print("-" * 80)
    result_no_filter = backtest.run_backtest(start_date, end_date, market, filter_shrinkage=False)

    # 2. 过滤缩量涨停
    print("\n[测试2] 过滤缩量涨停（新增风险检测）")
    print("-" * 80)
    backtest.all_data = {}  # 清空缓存
    result_with_filter = backtest.run_backtest(start_date, end_date, market, filter_shrinkage=True)

    if result_no_filter is None or result_with_filter is None:
        print("\n回测失败")
        return

    # 对比结果
    print("\n" + "=" * 80)
    print("对比结果")
    print("=" * 80)
    print(f"\n{'指标':<20} {'不过滤':<15} {'过滤':<15} {'变化':<15}")
    print("-" * 70)
    print(f"{'总交易次数':<20} {result_no_filter.total_trades:<15} {result_with_filter.total_trades:<15} {result_with_filter.total_trades - result_no_filter.total_trades:+d}")
    print(f"{'胜率':<20} {result_no_filter.win_rate:.2f}%{'':<9} {result_with_filter.win_rate:.2f}%{'':<9} {result_with_filter.win_rate - result_no_filter.win_rate:+.2f}%")
    print(f"{'平均收益':<20} {result_no_filter.avg_profit:+.2f}%{'':<8} {result_with_filter.avg_profit:+.2f}%{'':<8} {result_with_filter.avg_profit - result_no_filter.avg_profit:+.2f}%")

    # 缩量涨停效果分析
    if result_no_filter.shrinkage_trades > 0:
        print("\n" + "=" * 80)
        print("缩量涨停股票效果分析")
        print("=" * 80)
        print(f"缩量涨停交易次数: {result_no_filter.shrinkage_trades}")
        print(f"缩量涨停胜率: {result_no_filter.shrinkage_win_rate:.2f}%")
        print(f"缩量涨停平均收益: {result_no_filter.shrinkage_avg_profit:+.2f}%")
        print(f"整体胜率: {result_no_filter.win_rate:.2f}%")
        print(f"整体平均收益: {result_no_filter.avg_profit:+.2f}%")

        print("\n[结论]")
        if result_no_filter.shrinkage_win_rate < result_no_filter.win_rate:
            print(f"  缩量涨停胜率({result_no_filter.shrinkage_win_rate:.2f}%) < 整体胜率({result_no_filter.win_rate:.2f}%)")
            print("  说明缩量涨停确实是不利信号")
        else:
            print(f"  缩量涨停胜率({result_no_filter.shrinkage_win_rate:.2f}%) >= 整体胜率({result_no_filter.win_rate:.2f}%)")
            print("  说明缩量涨停风险不明显")

        if result_no_filter.shrinkage_avg_profit < result_no_filter.avg_profit:
            print(f"  缩量涨停收益({result_no_filter.shrinkage_avg_profit:+.2f}%) < 整体收益({result_no_filter.avg_profit:+.2f}%)")
            print("  说明缩量涨停股票表现较差")
        else:
            print(f"  缩量涨停收益({result_no_filter.shrinkage_avg_profit:+.2f}%) >= 整体收益({result_no_filter.avg_profit:+.2f}%)")
            print("  说明缩量涨停风险不明显")

    # 最终建议
    print("\n" + "=" * 80)
    print("最终建议")
    print("=" * 80)

    if result_with_filter.win_rate > result_no_filter.win_rate and result_with_filter.avg_profit > result_no_filter.avg_profit:
        print("\n[建议] 过滤缩量涨停后，胜率和收益都有提升，建议采用该策略")
    elif result_with_filter.win_rate > result_no_filter.win_rate:
        print("\n[建议] 过滤缩量涨停后，胜率提升但收益下降，需权衡")
    elif result_with_filter.avg_profit > result_no_filter.avg_profit:
        print("\n[建议] 过滤缩量涨停后，收益提升但胜率下降，需权衡")
    else:
        print("\n[建议] 过滤缩量涨停后效果不明显，可能需要进一步优化")

    return result_no_filter, result_with_filter


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="缩量涨停风险回测对比")
    parser.add_argument("--start", default="20260101", help="开始日期")
    parser.add_argument("--end", default="20261231", help="结束日期")
    parser.add_argument("--market", default="sha", choices=["sha", "sza", "all"], help="市场类型")

    args = parser.parse_args()
    run_comparison(args.start, args.end, args.market)
