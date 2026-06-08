"""
开盘涨幅场景分析工具

基于V2策略，分析不同开盘涨幅场景下的盈利情况和成功率
- 高开场景：1%, 1-2%, 2-3%, ..., 9-10%
- 低开场景：1%, 1-2%, 2-3%, ..., 9-10%

分析维度：
1. 各场景交易次数
2. 各场景胜率
3. 各场景平均收益
4. 各场景总收益
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from stock_agent.data.data_loader import DataLoader, MarketType


class OpenGapAnalyzer:
    """开盘涨幅场景分析器"""

    # 输出目录
    OUTPUT_DIR = Path(__file__).parent.parent / "outputs"

    # 连板优先级配置
    BOARD_PRIORITY = {
        2: {'priority': 1, 'max_position': 0.8},
        3: {'priority': 2, 'max_position': 0.7},
    }

    def __init__(self, initial_capital: float = 10000, lot_size: int = 100):
        self.initial_capital = initial_capital
        self.lot_size = lot_size
        self.data_loader = DataLoader()
        self.all_data = {}
        self.trade_dates = []

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

        # 扩大日期范围
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
        根据开盘涨幅判断场景（细化版本，每1%一个区间）

        Args:
            open_gap: 开盘涨幅（百分比）

        Returns:
            场景名称
        """
        # 高开场景
        if open_gap >= 10:
            return "高开≥10%"
        elif open_gap >= 9:
            return "高开9-10%"
        elif open_gap >= 8:
            return "高开8-9%"
        elif open_gap >= 7:
            return "高开7-8%"
        elif open_gap >= 6:
            return "高开6-7%"
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
        # 低开场景
        elif open_gap >= -1:
            return "低开0-1%"
        elif open_gap >= -2:
            return "低开1-2%"
        elif open_gap >= -3:
            return "低开2-3%"
        elif open_gap >= -4:
            return "低开3-4%"
        elif open_gap >= -5:
            return "低开4-5%"
        elif open_gap >= -6:
            return "低开5-6%"
        elif open_gap >= -7:
            return "低开6-7%"
        elif open_gap >= -8:
            return "低开7-8%"
        elif open_gap >= -9:
            return "低开8-9%"
        elif open_gap >= -10:
            return "低开9-10%"
        else:
            return "低开≥10%"

    def _select_leader_stocks(self, date: pd.Timestamp, sentiment_score: float,
                               next_date: pd.Timestamp = None) -> List[Dict]:
        """选股逻辑"""
        if sentiment_score < 40:
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

            # 估算流通市值
            float_cap = today['amount'] / 0.05 / 100000000

            # 流通市值过滤：15-200亿
            if float_cap < 15 or float_cap > 200:
                continue

            # 计算次日开盘涨幅
            open_gap = 0
            next_open = close
            if next_date is not None:
                df_next = df[df.index <= next_date]
                if len(df_next) >= 1 and df_next.index[-1] == next_date:
                    next_open = df_next.iloc[-1]['open']
                    open_gap = (next_open - close) / close * 100

            # 计算评分
            base_score = 60
            if board_count == 2:
                base_score = 90
            elif board_count == 3:
                base_score = 85
            elif board_count == 1:
                base_score = 80

            base_score += (seal_strength - 0.95) * 100

            candidates.append({
                'code': code,
                'price': next_open,
                'limit_close': close,
                'board_count': board_count,
                'seal_strength': seal_strength,
                'float_cap': float_cap,
                'score': min(100, max(0, base_score)),
                'open_gap': open_gap,
                'open_gap_scene': self._get_open_gap_scene(open_gap),
            })

        candidates.sort(key=lambda x: x['score'], reverse=True)
        return candidates[:3]

    def _simulate_trade(self, code: str, buy_date: pd.Timestamp, buy_price: float,
                        sell_date: pd.Timestamp) -> Dict:
        """
        模拟交易

        卖出条件：
        1. 硬止损：亏损超过6%
        2. 移动止盈：盈利超过5%后，回撤3%止盈
        3. 分批止盈：+5%卖30%，+10%卖30%，+15%卖40%
        4. 到期卖出：持仓超过5天
        """
        df = self.all_data.get(code)
        if df is None:
            return None

        df_period = df[(df.index >= buy_date) & (df.index <= sell_date)]
        if len(df_period) == 0:
            return None

        # T+1约束：买入当天不能卖出
        hold_days = 0
        max_profit = 0
        total_profit_pct = 0
        remaining_position = 1.0

        sold_5 = False
        sold_10 = False
        sold_15 = False

        for i, (date, row) in enumerate(df_period.iterrows()):
            if i == 0:
                # 买入当天，不计入持仓天数
                continue

            hold_days += 1
            current_price = row['close']
            profit_pct = (current_price - buy_price) / buy_price * 100

            # 更新最高盈利
            max_profit = max(max_profit, profit_pct)

            # 检查卖出条件
            exit_reason = None
            sell_ratio = 1.0

            # 1. 硬止损
            if profit_pct <= -6:
                exit_reason = "止损"
                sell_ratio = remaining_position

            # 2. 移动止盈
            elif max_profit >= 5 and profit_pct < max_profit - 3:
                exit_reason = "移动止盈"
                sell_ratio = remaining_position

            # 3. 分批止盈
            elif profit_pct >= 15 and not sold_15:
                exit_reason = "止盈15%"
                sell_ratio = remaining_position * 0.4
                sold_15 = True
            elif profit_pct >= 10 and not sold_10:
                exit_reason = "止盈10%"
                sell_ratio = remaining_position * 0.3 / remaining_position if remaining_position > 0 else 0
                sold_10 = True
            elif profit_pct >= 5 and not sold_5:
                exit_reason = "止盈5%"
                sell_ratio = remaining_position * 0.3 / remaining_position if remaining_position > 0 else 0
                sold_5 = True

            # 4. 到期卖出
            elif hold_days >= 5:
                exit_reason = "到期"
                sell_ratio = remaining_position

            if exit_reason:
                trade_profit = profit_pct * sell_ratio
                total_profit_pct += trade_profit
                remaining_position -= sell_ratio

                if remaining_position <= 0.01:
                    return {
                        'hold_days': hold_days,
                        'profit_pct': total_profit_pct,
                        'exit_reason': exit_reason,
                        'max_profit': max_profit,
                    }

        # 如果还有剩余仓位，按最后一天收盘价卖出
        if remaining_position > 0.01 and hold_days > 0:
            last_price = df_period.iloc[-1]['close']
            final_profit_pct = (last_price - buy_price) / buy_price * 100
            total_profit_pct += final_profit_pct * remaining_position

            return {
                'hold_days': hold_days,
                'profit_pct': total_profit_pct,
                'exit_reason': "收盘卖出",
                'max_profit': max_profit,
            }

        return None

    def run_analysis(self, start_date: str, end_date: str, market: str = "sha") -> Dict:
        """执行分析"""
        print("=" * 80)
        print("开盘涨幅场景分析")
        print("=" * 80)
        print(f"分析区间: {start_date} - {end_date}")
        print(f"市场: {market}")
        print("=" * 80)

        # 预加载数据
        self._preload_data(start_date, end_date, market)

        # 获取交易日
        trade_dates = self._get_trade_dates(start_date, end_date)
        print(f"交易日数量: {len(trade_dates)}")

        if not trade_dates:
            print("无法获取交易日")
            return {}

        # 初始化统计数据
        # 开盘涨幅场景统计
        scene_stats = {}

        # 按连板数统计
        board_stats = {}

        # 交易记录
        trade_records = []

        total_dates = len(trade_dates)

        # 主循环
        for i, date in enumerate(trade_dates):
            if (i + 1) % 20 == 0:
                print(f"\r分析进度: {i+1}/{total_dates} ({(i+1)/total_dates*100:.1f}%)", end="")

            # 计算情绪
            sentiment_score, sentiment_phase = self._calc_sentiment(date)

            # 选股（获取下一个交易日）
            next_date = None
            if i + 1 < len(trade_dates):
                next_date = trade_dates[i + 1]

            candidates = self._select_leader_stocks(date, sentiment_score, next_date)

            for c in candidates:
                if next_date is None:
                    continue

                # 模拟交易
                # 找到卖出日期（最多持有5天）
                sell_date_idx = min(i + 6, len(trade_dates) - 1)
                sell_date = trade_dates[sell_date_idx]

                trade_result = self._simulate_trade(
                    c['code'], next_date, c['price'], sell_date
                )

                if trade_result and trade_result['hold_days'] > 0:
                    scene = c['open_gap_scene']
                    board = c['board_count']

                    # 初始化场景统计
                    if scene not in scene_stats:
                        scene_stats[scene] = {
                            'count': 0,
                            'wins': 0,
                            'total_profit': 0,
                            'max_profit': 0,
                            'max_loss': 0,
                            'trades': []
                        }

                    # 初始化连板统计
                    if board not in board_stats:
                        board_stats[board] = {
                            'count': 0,
                            'wins': 0,
                            'total_profit': 0,
                        }

                    # 更新统计
                    profit = trade_result['profit_pct']
                    is_win = profit > 0

                    scene_stats[scene]['count'] += 1
                    scene_stats[scene]['total_profit'] += profit
                    if is_win:
                        scene_stats[scene]['wins'] += 1
                    scene_stats[scene]['max_profit'] = max(scene_stats[scene]['max_profit'], profit)
                    scene_stats[scene]['max_loss'] = min(scene_stats[scene]['max_loss'], profit)

                    board_stats[board]['count'] += 1
                    board_stats[board]['total_profit'] += profit
                    if is_win:
                        board_stats[board]['wins'] += 1

                    trade_records.append({
                        'code': c['code'],
                        'select_date': date.strftime('%Y-%m-%d'),
                        'buy_date': next_date.strftime('%Y-%m-%d'),
                        'buy_price': c['price'],
                        'open_gap': c['open_gap'],
                        'open_gap_scene': scene,
                        'board_count': board,
                        'profit_pct': profit,
                        'hold_days': trade_result['hold_days'],
                        'is_win': is_win,
                        'exit_reason': trade_result['exit_reason'],
                        'max_profit': trade_result['max_profit'],
                    })

        print()

        return {
            'scene_stats': scene_stats,
            'board_stats': board_stats,
            'trade_records': trade_records,
            'total_trades': len(trade_records),
        }

    def generate_report(self, result: Dict, start_date: str, end_date: str, market: str,
                        output_path: str = None) -> str:
        """生成分析报告"""
        lines = []

        # 市场名称映射
        market_names = {
            'gem': '创业板',
            'sha': '沪A',
            'sza': '深A',
            'all': '全部A股',
        }
        market_name = market_names.get(market, market.upper())

        lines.append(f"# 开盘涨幅场景分析报告")
        lines.append("")
        lines.append(f"**分析市场**: {market_name}")
        lines.append("")
        lines.append(f"**分析区间**: {start_date} - {end_date}")
        lines.append("")
        lines.append(f"**交易规则**: 每只股票初始资金 {self.initial_capital:.0f} 元 | V2策略选股")
        lines.append("")

        # 一、汇总统计
        lines.append("## 一、汇总统计")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|------|------|")
        lines.append(f"| 总交易次数 | {result['total_trades']} 次 |")

        if result['trade_records']:
            df = pd.DataFrame(result['trade_records'])
            total_wins = df['is_win'].sum()
            win_rate = total_wins / len(df) * 100
            total_profit = df['profit_pct'].sum()
            avg_profit = df['profit_pct'].mean()

            lines.append(f"| 盈利次数 | {total_wins} 次 |")
            lines.append(f"| 胜率 | {win_rate:.3f}% |")
            lines.append(f"| 总收益 | {total_profit:.3f}% |")
            lines.append(f"| 平均收益 | {avg_profit:.3f}% |")

        lines.append("")
        lines.append("---")
        lines.append("")

        # 二、高开场景分析
        lines.append("## 二、高开场景分析")
        lines.append("")
        lines.append("| 开盘涨幅 | 交易次数 | 胜率 | 平均收益 | 总收益 | 最大盈利 | 最大亏损 | 评估 |")
        lines.append("|----------|----------|------|----------|--------|----------|----------|------|")

        high_open_scenes = [
            "高开0-1%", "高开1-2%", "高开2-3%", "高开3-4%", "高开4-5%",
            "高开5-6%", "高开6-7%", "高开7-8%", "高开8-9%", "高开9-10%", "高开≥10%"
        ]

        for scene in high_open_scenes:
            if scene in result['scene_stats']:
                stats = result['scene_stats'][scene]
                if stats['count'] > 0:
                    win_rate = stats['wins'] / stats['count'] * 100
                    avg_profit = stats['total_profit'] / stats['count']

                    # 评估
                    if win_rate >= 60 and avg_profit > 2:
                        eval_text = "**优秀**"
                    elif win_rate >= 50 and avg_profit > 0:
                        eval_text = "良好"
                    elif win_rate >= 40 or avg_profit > 0:
                        eval_text = "一般"
                    else:
                        eval_text = "较差"

                    lines.append(f"| {scene} | {stats['count']} | {win_rate:.3f}% | {avg_profit:+.3f}% | {stats['total_profit']:+.3f}% | +{stats['max_profit']:.3f}% | {stats['max_loss']:.3f}% | {eval_text} |")

        lines.append("")
        lines.append("---")
        lines.append("")

        # 三、低开场景分析
        lines.append("## 三、低开场景分析")
        lines.append("")
        lines.append("| 开盘跌幅 | 交易次数 | 胜率 | 平均收益 | 总收益 | 最大盈利 | 最大亏损 | 评估 |")
        lines.append("|----------|----------|------|----------|--------|----------|----------|------|")

        low_open_scenes = [
            "低开0-1%", "低开1-2%", "低开2-3%", "低开3-4%", "低开4-5%",
            "低开5-6%", "低开6-7%", "低开7-8%", "低开8-9%", "低开9-10%", "低开≥10%"
        ]

        for scene in low_open_scenes:
            if scene in result['scene_stats']:
                stats = result['scene_stats'][scene]
                if stats['count'] > 0:
                    win_rate = stats['wins'] / stats['count'] * 100
                    avg_profit = stats['total_profit'] / stats['count']

                    # 评估
                    if win_rate >= 60 and avg_profit > 2:
                        eval_text = "**优秀**"
                    elif win_rate >= 50 and avg_profit > 0:
                        eval_text = "良好"
                    elif win_rate >= 40 or avg_profit > 0:
                        eval_text = "一般"
                    else:
                        eval_text = "较差"

                    lines.append(f"| {scene} | {stats['count']} | {win_rate:.3f}% | {avg_profit:+.3f}% | {stats['total_profit']:+.3f}% | +{stats['max_profit']:.3f}% | {stats['max_loss']:.3f}% | {eval_text} |")

        lines.append("")
        lines.append("---")
        lines.append("")

        # 四、按连板数统计
        lines.append("## 四、按连板数统计")
        lines.append("")
        lines.append("| 连板数 | 交易次数 | 胜率 | 平均收益 | 总收益 |")
        lines.append("|--------|----------|------|----------|--------|")

        for board in sorted(result['board_stats'].keys()):
            stats = result['board_stats'][board]
            if stats['count'] > 0:
                win_rate = stats['wins'] / stats['count'] * 100
                avg_profit = stats['total_profit'] / stats['count']
                lines.append(f"| {board}板 | {stats['count']} | {win_rate:.3f}% | {avg_profit:+.3f}% | {stats['total_profit']:+.3f}% |")

        lines.append("")
        lines.append("---")
        lines.append("")

        # 五、核心发现
        lines.append("## 五、核心发现")
        lines.append("")

        findings = []

        # 找出最佳高开场景
        best_high = None
        best_high_score = -999
        for scene in high_open_scenes:
            if scene in result['scene_stats']:
                stats = result['scene_stats'][scene]
                if stats['count'] >= 3:
                    win_rate = stats['wins'] / stats['count']
                    avg_profit = stats['total_profit'] / stats['count']
                    score = win_rate * 100 + avg_profit * 10
                    if score > best_high_score:
                        best_high_score = score
                        best_high = (scene, stats)

        if best_high:
            scene, stats = best_high
            win_rate = stats['wins'] / stats['count'] * 100
            avg_profit = stats['total_profit'] / stats['count']
            findings.append(f"1. **最佳高开场景**: {scene}，胜率{win_rate:.1f}%，平均收益{avg_profit:+.2f}%")

        # 找出最佳低开场景
        best_low = None
        best_low_score = -999
        for scene in low_open_scenes:
            if scene in result['scene_stats']:
                stats = result['scene_stats'][scene]
                if stats['count'] >= 3:
                    win_rate = stats['wins'] / stats['count']
                    avg_profit = stats['total_profit'] / stats['count']
                    score = win_rate * 100 + avg_profit * 10
                    if score > best_low_score:
                        best_low_score = score
                        best_low = (scene, stats)

        if best_low:
            scene, stats = best_low
            win_rate = stats['wins'] / stats['count'] * 100
            avg_profit = stats['total_profit'] / stats['count']
            findings.append(f"2. **最佳低开场景**: {scene}，胜率{win_rate:.1f}%，平均收益{avg_profit:+.2f}%")

        # 高开 vs 低开对比
        high_total_count = 0
        high_total_profit = 0
        high_total_wins = 0

        for scene in high_open_scenes:
            if scene in result['scene_stats']:
                stats = result['scene_stats'][scene]
                high_total_count += stats['count']
                high_total_profit += stats['total_profit']
                high_total_wins += stats['wins']

        low_total_count = 0
        low_total_profit = 0
        low_total_wins = 0

        for scene in low_open_scenes:
            if scene in result['scene_stats']:
                stats = result['scene_stats'][scene]
                low_total_count += stats['count']
                low_total_profit += stats['total_profit']
                low_total_wins += stats['wins']

        if high_total_count > 0:
            high_wr = high_total_wins / high_total_count * 100
            high_avg = high_total_profit / high_total_count
            findings.append(f"3. **高开整体表现**: 共{high_total_count}笔，胜率{high_wr:.1f}%，平均收益{high_avg:+.2f}%")

        if low_total_count > 0:
            low_wr = low_total_wins / low_total_count * 100
            low_avg = low_total_profit / low_total_count
            findings.append(f"4. **低开整体表现**: 共{low_total_count}笔，胜率{low_wr:.1f}%，平均收益{low_avg:+.2f}%")

        for f in findings:
            lines.append(f)

        lines.append("")
        lines.append("---")
        lines.append("")

        # 六、操作建议
        lines.append("## 六、操作建议")
        lines.append("")

        suggestions = []

        # 根据高开场景给建议
        high_good_scenes = []
        for scene in high_open_scenes:
            if scene in result['scene_stats']:
                stats = result['scene_stats'][scene]
                if stats['count'] >= 3:
                    win_rate = stats['wins'] / stats['count'] * 100
                    avg_profit = stats['total_profit'] / stats['count']
                    if win_rate >= 50 and avg_profit > 0:
                        high_good_scenes.append((scene, win_rate, avg_profit))

        if high_good_scenes:
            suggestions.append("**高开参与建议**:")
            for scene, wr, avg in high_good_scenes[:3]:
                suggestions.append(f"- {scene}可参与，胜率{wr:.1f}%，平均收益{avg:+.2f}%")

        # 根据低开场景给建议
        low_good_scenes = []
        for scene in low_open_scenes:
            if scene in result['scene_stats']:
                stats = result['scene_stats'][scene]
                if stats['count'] >= 3:
                    win_rate = stats['wins'] / stats['count'] * 100
                    avg_profit = stats['total_profit'] / stats['count']
                    if win_rate >= 50 and avg_profit > 0:
                        low_good_scenes.append((scene, win_rate, avg_profit))

        if low_good_scenes:
            suggestions.append("**低开参与建议**:")
            for scene, wr, avg in low_good_scenes[:3]:
                suggestions.append(f"- {scene}可参与，胜率{wr:.1f}%，平均收益{avg:+.2f}%")

        # 回避场景
        bad_scenes = []
        all_scenes = high_open_scenes + low_open_scenes
        for scene in all_scenes:
            if scene in result['scene_stats']:
                stats = result['scene_stats'][scene]
                if stats['count'] >= 3:
                    win_rate = stats['wins'] / stats['count'] * 100
                    avg_profit = stats['total_profit'] / stats['count']
                    if win_rate < 40 or avg_profit < -2:
                        bad_scenes.append((scene, win_rate, avg_profit))

        if bad_scenes:
            suggestions.append("**回避场景**:")
            for scene, wr, avg in bad_scenes[:3]:
                suggestions.append(f"- {scene}风险较大，胜率{wr:.1f}%，平均收益{avg:+.2f}%")

        for s in suggestions:
            lines.append(s)

        lines.append("")
        lines.append("---")
        lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

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
            'profit_pct', 'open_gap', 'max_profit', 'buy_price',
            'sell_price'
        ]

        for field in pct_fields:
            if field in formatted and formatted[field] is not None:
                try:
                    formatted[field] = round(float(formatted[field]), 2)
                except (ValueError, TypeError):
                    pass

        formatted_records.append(formatted)

    return formatted_records


def run_open_gap_analysis(
    start_date: str = None,
    end_date: str = None,
    market: str = "sha",
    output_dir: str = None
):
    """执行开盘涨幅场景分析"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y%m%d")

    if start_date is None:
        start_date = "20260101"

    analyzer = OpenGapAnalyzer()
    result = analyzer.run_analysis(start_date, end_date, market)

    # 输出目录
    if output_dir is None:
        output_dir = analyzer.OUTPUT_DIR

    output_path = Path(output_dir)

    # 根据日期范围确定子目录
    start_dt = datetime.strptime(start_date, '%Y%m%d')
    end_dt = datetime.strptime(end_date, '%Y%m%d')
    days_diff = (end_dt - start_dt).days + 1

    if days_diff <= 1:
        output_subdir = output_path / "daily" / "open_gap_analysis"
    elif days_diff <= 7:
        output_subdir = output_path / "weekly" / "open_gap_analysis"
    elif days_diff <= 31:
        output_subdir = output_path / "monthly" / "open_gap_analysis"
    else:
        output_subdir = output_path / "yearly" / "open_gap_analysis"

    output_subdir.mkdir(parents=True, exist_ok=True)

    report_file = output_subdir / f"open_gap_analysis_{market}_{start_date}_{end_date}.md"
    report = analyzer.generate_report(result, start_date, end_date, market, str(report_file))

    # 保存交易记录CSV
    if result['trade_records']:
        formatted_records = format_trade_records_for_csv(result['trade_records'])
        df = pd.DataFrame(formatted_records)
        csv_file = output_subdir / f"open_gap_analysis_trades_{market}_{start_date}_{end_date}.csv"
        df.to_csv(csv_file, index=False, encoding='utf-8-sig')
        print(f"交易记录已保存: {csv_file}")

    print("\n" + report)
    return result


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    run_open_gap_analysis()
