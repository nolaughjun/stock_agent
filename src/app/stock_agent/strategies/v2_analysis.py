"""
选股收益分析工具 - V3优化版

整合V3核心优化：
1. 市场环境判断：牛市（情绪分>=70）参与，震荡市/熊市空仓观望
2. 情绪阶段筛选：退潮期核心参与，启动期/冰点期回避
3. 开盘涨幅筛选：精细化买入策略（低吸、开盘买入、回避陷阱区）
4. 实际买入确认：只有当日最低价触及目标买入价才算"能买进"

分析指定时间范围内选出的个股收益和成功率：
- 每只个股初始资金10000元
- T+1模式：买入当天不能卖出
- 每手100股
"""

import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# 尝试导入 dateutil，如果不存在则使用简单方法
try:
    import dateutil.relativedelta as relativedelta
    HAS_DATEUTIL = True
except ImportError:
    HAS_DATEUTIL = False

from stock_agent.data.data_loader import DataLoader, MarketType
from stock_agent.data.realtime_quote import RealtimeQuote

# 尝试加载风险模型
try:
    from stock_agent.strategies.risk_model import RiskModel
except ImportError:
    RiskModel = None


@dataclass
class StockAnalysisResult:
    """单只股票分析结果（V3优化版）"""
    select_date: str           # 选股日期
    code: str                  # 股票代码
    name: str                  # 股票名称
    select_close: float        # 选股日收盘价
    trade_date: str            # 交易日期
    trade_price: float         # 交易价格（实际买入价）
    hold_days: int             # 持股时间
    profit_pct: float          # 收益率
    is_win: bool               # 是否盈利
    buy_amount: int            # 买入股数
    buy_cost: float            # 买入成本
    sell_amount: float         # 卖出金额
    profit: float              # 盈亏金额
    sell_price: float = 0.0    # 卖出均价
    sell_reason: str = ""      # 卖出原因
    stop_loss_price: float = 0.0  # 止损价
    # 交易当天详细信息
    trade_open: float = 0.0    # 交易当天开盘价
    trade_high: float = 0.0    # 交易当天最高价
    trade_low: float = 0.0     # 交易当天最低价
    trade_close: float = 0.0   # 交易当天收盘价
    max_drop_pct: float = 0.0  # 交易当天最大跌幅（最低价相对开盘价）
    open_gap_pct: float = 0.0  # 交易日开盘涨跌幅
    # V3新增字段
    buy_method: str = ""       # 买入方法（开盘价买入/低吸买入）
    open_gap_scene: str = ""   # 开盘涨幅场景
    market_env: str = ""       # 市场环境（bull/oscillation/bear）
    sentiment_phase: str = ""  # 情绪阶段
    sentiment_score: float = 0.0  # 情绪分
    target_buy_price: float = 0.0  # 目标买入价
    # 买入验证字段
    can_buy: bool = True       # 是否能按条件买入（关键：低吸场景必须最低价触及目标价）
    buy_confirm_reason: str = ""  # 买入确认原因
    # V4新增因子字段
    volume_change_rate: float = 0.0  # 成交量变化率(%)
    volume_ratio: float = 1.0        # 量比
    turnover_rate: float = 0.0       # 换手率(%)
    volatility: float = 0.0          # 近期波动率(%)
    factor_score: float = 0.0        # 因子评分

class V2Analyzer:
    """
    基于V3策略的个股选股分析器

    整合V3核心优化：
    1. 市场环境判断：牛市参与，震荡市/熊市空仓
    2. 开盘涨幅筛选：精细化买入策略
    3. 情绪阶段筛选：退潮期核心参与
    """

    # 输出目录
    OUTPUT_DIR = Path(__file__).parent.parent / "outputs"

    # 连板优先级配置（V3优化：只参与2板和3板）
    BOARD_PRIORITY = {
        2: {'priority': 1, 'max_position': 1.0, 'score': 95},
        3: {'priority': 2, 'max_position': 0.8, 'score': 85},
    }

    # 情绪阶段配置（基于十年数据）
    SENTIMENT_PHASE_CONFIG = {
        '冰点': {'participate': False, 'position_ratio': 0.0, 'reason': '十年胜率10.3%，严格回避'},
        '启动': {'participate': False, 'position_ratio': 0.0, 'reason': '十年胜率30.5%，回避陷阱'},
        '发酵': {'participate': True, 'position_ratio': 0.8, 'reason': '十年胜率55.6%，积极参与'},
        '高潮': {'participate': True, 'position_ratio': 0.6, 'reason': '十年胜率56.4%，控制仓位'},
        '退潮': {'participate': True, 'position_ratio': 1.0, 'reason': '十年胜率69.0%，核心参与'},
    }

    # 开盘涨幅场景配置（基于十年数据分析）
    # 核心发现：低开3-4%胜率51.4%最稳定，高开2-3%胜率62.5%
    OPEN_GAP_CONFIG = {
        # ===== 高开场景 =====
        '高开0-1%': {
            'participate': True,
            'position_ratio': 0.3,
            'target_buy_gap': -1.0,  # 目标在-1%低吸
            'reason': '高开低吸策略：目标-1%买入'
        },
        '高开1-2%': {
            'participate': True,
            'position_ratio': 0.3,
            'target_buy_gap': -1.0,
            'reason': '高开低吸策略：目标-1%买入'
        },
        '高开2-3%': {
            'participate': True,
            'position_ratio': 0.5,
            'target_buy_gap': 0.0,  # 开盘价买入
            'reason': '核心场景：开盘价买入，胜率62.5%'
        },
        '高开3-4%': {
            'participate': False,
            'position_ratio': 0.0,
            'reason': '主力陷阱区，回避'
        },
        '高开4-5%': {
            'participate': False,
            'position_ratio': 0.0,
            'reason': '高开过多，回避'
        },
        '高开5-6%': {
            'participate': False,
            'position_ratio': 0.0,
            'reason': '高开过多，回避'
        },
        '高开6-8%': {
            'participate': False,
            'position_ratio': 0.0,
            'reason': '高开过多，回避'
        },
        '高开8-9%': {
            'participate': False,
            'position_ratio': 0.0,
            'reason': '高位接盘风险，回避'
        },
        '高开≥9%': {
            'participate': False,
            'position_ratio': 0.0,
            'reason': '过于激进，不参与'
        },
        # ===== 低开场景 =====
        '低开0-1%': {
            'participate': True,
            'position_ratio': 0.4,
            'target_buy_gap': -3.5,  # 目标在-3.5%低吸
            'reason': '低开低吸策略：目标-3.5%买入'
        },
        '低开1-2%': {
            'participate': True,
            'position_ratio': 0.4,
            'target_buy_gap': -3.5,
            'reason': '低开低吸策略：目标-3.5%买入'
        },
        '低开2-3%': {
            'participate': True,
            'position_ratio': 0.4,
            'target_buy_gap': -3.5,
            'reason': '低开低吸策略：目标-3.5%买入'
        },
        '低开3-4%': {
            'participate': True,
            'position_ratio': 0.5,
            'target_buy_gap': -3.5,
            'reason': '核心场景：十年最稳定，目标-3.5%低吸',
            'priority': 1
        },
        '低开4-8%': {
            'participate': False,
            'position_ratio': 0.0,
            'reason': '接飞刀行为，回避'
        },
        '低开>8%': {
            'participate': False,
            'position_ratio': 0.0,
            'reason': '跌幅过大，不参与'
        },
    }

    # 市场环境配置（基于十年数据）
    MARKET_ENV_CONFIG = {
        'bull': {  # 情绪分>=70
            'participate': True,
            'max_position': 1.0,
            'description': '牛市满仓参与'
        },
        'oscillation': {  # 情绪分50-70
            'participate': True,  # 震荡市也参与
            'max_position': 0.6,
            'description': '震荡市轻仓参与'
        },
        'bear': {  # 情绪分<50
            'participate': False,  # 熊市空仓
            'max_position': 0.0,
            'description': '熊市空仓观望'
        },
    }

    def __init__(self, initial_capital: float = 10000, lot_size: int = 100):
        """
        初始化分析器

        Args:
            initial_capital: 每只股票初始资金
            lot_size: 每手股数
        """
        self.initial_capital = initial_capital
        self.lot_size = lot_size
        self.data_loader = DataLoader()
        self.all_data = {}
        self.trade_dates = []
        self.realtime_quote = RealtimeQuote()
        self.stock_names = {}

        # 筛选统计
        self.filter_stats = {
            'total_candidates': 0,
            'sentiment_filtered': 0,
            'market_env_filtered': 0,
            'open_gap_filtered': 0,
            'final_selected': 0,
        }

        # 加载风险模型
        if RiskModel:
            try:
                model_path = Path(__file__).parent.parent / "outputs" / "risk_model.pkl"
                self.risk_model = RiskModel(str(model_path))
                if model_path.exists():
                    self.risk_model.load(str(model_path))
                    print(f"[信息] 加载ML模型成功: {model_path}")
                else:
                    print(f"[信息] ML模型文件不存在，使用规则评分")
            except Exception as e:
                print(f"[警告] 加载ML模型失败: {e}")
                self.risk_model = None
        else:
            self.risk_model = None

        # 加载选股模型
        self.selection_model = None
        try:
            from stock_agent.strategies.selection_model import StockSelectionModel
            model_path = Path(__file__).parent / "selection_model.joblib"
            if model_path.exists():
                self.selection_model = StockSelectionModel(str(model_path))
                print("[信息] 加载选股模型成功")
        except Exception as e:
            print(f"[提示] 选股模型未加载: {e}")

    def _filter_df_by_date(self, df: pd.DataFrame, date: pd.Timestamp) -> pd.DataFrame:
        """
        按日期过滤 DataFrame，使用高效的方式

        Args:
            df: 股票数据 DataFrame
            date: 截止日期

        Returns:
            过滤后的 DataFrame
        """
        if df is None or len(df) == 0:
            return pd.DataFrame()

        try:
            # 确保 date 是 Timestamp 类型
            if not isinstance(date, pd.Timestamp):
                date = pd.Timestamp(date)

            # 确保 DataFrame 索引是 DatetimeIndex
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)

            # 直接使用索引访问，不创建副本
            try:
                # 使用 searchsorted 找到位置
                idx = df.index.searchsorted(date, side='right')
                result = df.iloc[:idx]
                return result
            except Exception:
                # 备用方案：使用 numpy 数组比较
                index_values = df.index.to_numpy()
                date_value = date.to_numpy64()
                mask = index_values <= date_value
                result = df.iloc[mask]
                return result

        except Exception as e:
            return pd.DataFrame()

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
        """获取单个股票名称"""
        if code in self.stock_names:
            return self.stock_names[code]
        self._get_stock_names([code])
        return self.stock_names.get(code, code)

    def load_data(self, start_date: str, end_date: str, market: str = "sha"):
        """加载数据"""
        print(f"[1/4] 正在预加载股票数据...")

        # 龙头策略不包含创业板
        market_map = {
            "sha": MarketType.SHA,
            "sza": MarketType.SZA,
            "all": MarketType.ALL,
        }
        market_type = market_map.get(market.lower(), MarketType.SHA)

        stock_df = self.data_loader.get_stock_list(market_type)
        stock_list = stock_df['code'].tolist() if len(stock_df) > 0 else []
        print(f"      股票数量: {len(stock_list)} 只")

        start_dt = datetime.strptime(start_date, '%Y%m%d')
        end_dt = datetime.strptime(end_date, '%Y%m%d')
        data_start = (start_dt - timedelta(days=120)).strftime('%Y%m%d')
        data_end = (end_dt + timedelta(days=10)).strftime('%Y%m%d')

        start_time = time.time()
        loaded_count = 0
        print_interval = max(1, len(stock_list) // 10)  # 每10%打印一次

        def load_single(code):
            try:
                df = self.data_loader.get_stock_history(code, data_start, data_end)
                if df is not None and len(df) > 20:
                    # 确保 DataFrame 索引是 DatetimeIndex
                    if not isinstance(df.index, pd.DatetimeIndex):
                        df.index = pd.to_datetime(df.index)
                    return code, df
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(load_single, code): code for code in stock_list}
            for i, future in enumerate(as_completed(futures)):
                result = future.result()
                if result:
                    self.all_data[result[0]] = result[1]
                    loaded_count += 1
                # 打印进度
                if (i + 1) % print_interval == 0 or i + 1 == len(stock_list):
                    pct = (i + 1) / len(stock_list) * 100
                    print(f"      加载进度: {i+1}/{len(stock_list)} ({pct:.0f}%) - 已加载 {loaded_count} 只")

        elapsed = time.time() - start_time
        print(f"[1/4] 加载完成: {len(self.all_data)} 只股票, 耗时 {elapsed:.1f}秒")

        print(f"[2/4] 获取交易日...")
        self.trade_dates = self._get_trade_dates(start_date, end_date)
        print(f"[2/4] 交易日数: {len(self.trade_dates)}")

        if self.all_data:
            print(f"[3/4] 正在获取股票名称...")
            self._get_stock_names(list(self.all_data.keys()))
            print(f"[3/4] 获取完成: {len(self.stock_names)} 只")

    def _get_trade_dates(self, start_date: str, end_date: str) -> List[pd.Timestamp]:
        """获取交易日列表"""
        if len(self.all_data) == 0:
            return []

        all_dates = set()
        for df in self.all_data.values():
            all_dates.update(df.index.tolist())

        return sorted(list(all_dates))

    def _count_boards(self, df: pd.DataFrame, limit_pct: float) -> int:
        """计算连板数"""
        count = 0
        try:
            for i in range(-1, -min(8, len(df)), -1):
                if i == 0 or abs(i) >= len(df):
                    break
                try:
                    today = df.iloc[i].copy()
                    yesterday = df.iloc[i-1].copy() if abs(i-1) <= len(df) else None
                except (IndexError, KeyError, TypeError, ValueError, AttributeError):
                    break
                if yesterday is None:
                    break

                try:
                    pct = (today['close'] - yesterday['close']) / yesterday['close'] * 100
                    if pct >= limit_pct - 0.5:
                        count += 1
                    else:
                        break
                except (KeyError, TypeError, ZeroDivisionError):
                    break
        except Exception:
            pass
        return count

    def _calc_volume_change_rate(self, df: pd.DataFrame) -> Tuple[float, float]:
        """
        计算成交量变化率（流动性因子）

        Args:
            df: 股票历史数据

        Returns:
            (volume_change_rate, volume_ratio)
            - volume_change_rate: 当日成交量相对5日均量的变化率(%)
            - volume_ratio: 当日成交量/5日均量
        """
        if df is None or len(df) < 6:
            return 0.0, 1.0

        try:
            # 当日成交量
            today_volume = df.iloc[-1]['volume']

            # 5日平均成交量（不包含当日）
            ma5_volume = df.iloc[-6:-1]['volume'].mean()

            if ma5_volume > 0:
                volume_ratio = today_volume / ma5_volume
                volume_change_rate = (volume_ratio - 1) * 100
                return round(volume_change_rate, 2), round(volume_ratio, 2)
            else:
                return 0.0, 1.0
        except Exception:
            return 0.0, 1.0

    def _calc_turnover_rate(self, df: pd.DataFrame, float_shares: float) -> float:
        """
        计算换手率（活跃度因子）

        Args:
            df: 股票历史数据
            float_shares: 流通股本（万股）

        Returns:
            换手率(%)
        """
        if df is None or len(df) < 1 or float_shares is None or float_shares <= 0:
            return 0.0

        try:
            # 当日成交量（手）-> 转换为股（1手=100股）
            today_volume = df.iloc[-1]['volume'] * 100

            # 换手率 = 成交量 / 流通股本 * 100
            # 流通股本单位为万股，需要转换为股
            float_shares_units = float_shares * 10000  # 万股 -> 股

            turnover_rate = (today_volume / float_shares_units) * 100
            return round(turnover_rate, 2)
        except Exception:
            return 0.0

    def _calc_recent_volatility(self, df: pd.DataFrame, period: int = 10) -> float:
        """
        计算近期波动率（风险因子）

        使用ATR（平均真实波幅）占收盘价的比例

        Args:
            df: 股票历史数据
            period: 计算周期

        Returns:
            波动率(%)
        """
        if df is None or len(df) < period + 1:
            return 0.0

        try:
            # 计算真实波幅 (True Range)
            recent_df = df.tail(period + 1)

            tr_list = []
            for i in range(1, len(recent_df)):
                high = recent_df.iloc[i]['high']
                low = recent_df.iloc[i]['low']
                prev_close = recent_df.iloc[i-1]['close']

                tr = max(
                    high - low,
                    abs(high - prev_close),
                    abs(low - prev_close)
                )
                tr_list.append(tr)

            # ATR = 平均真实波幅
            atr = sum(tr_list) / len(tr_list) if tr_list else 0

            # 波动率 = ATR / 收盘价 * 100
            close = recent_df.iloc[-1]['close']
            if close > 0:
                volatility = (atr / close) * 100
                return round(volatility, 2)
            else:
                return 0.0
        except Exception:
            return 0.0

    def _calculate_risk_score(self, df: pd.DataFrame, code: str, board_count: int,
                               float_cap: float, sentiment_score: float,
                               seal_strength: float) -> Tuple[int, List[str], str]:
        """计算风险评分"""
        risk_score = 0
        risk_factors = []

        if board_count >= 3:
            risk_score += 15
            risk_factors.append("高位接力")
        elif board_count == 2:
            risk_score += 5

        if float_cap < 20:
            risk_score += 10
            risk_factors.append("小市值")
        elif float_cap > 100:
            risk_score += 5
            risk_factors.append("大市值")

        if seal_strength < 0.97:
            risk_score += 10
            risk_factors.append("封板不牢")

        if sentiment_score < 50:
            risk_score += 15
            risk_factors.append("情绪低迷")

        if self.risk_model is not None:
            try:
                # 构造 trade 字典供 ML 模型使用
                trade_dict = {
                    'code': code,
                    'board_count': board_count,
                    'float_cap': float_cap,
                    'seal_strength': seal_strength,
                    'sentiment_score': sentiment_score,
                }
                result = self.risk_model.predict_risk(trade_dict, df)
                ml_risk = result.get('risk_score', 0)
                risk_score = int(risk_score * 0.5 + ml_risk * 0.5)
            except Exception as e:
                pass

        if risk_score <= 10:
            risk_level = "低风险"
        elif risk_score <= 25:
            risk_level = "中风险"
        else:
            risk_level = "高风险"

        return risk_score, risk_factors, risk_level

    def _calc_sentiment(self, date: pd.Timestamp) -> Tuple[float, str]:
        """
        计算市场情绪得分和阶段

        Returns:
            (sentiment_score, sentiment_phase)
        """
        up_count, down_count = 0, 0
        limit_up_count, limit_down_count, high_board_count = 0, 0, 0

        for code, df in self.all_data.items():
            try:
                df_day = self._filter_df_by_date(df, date)
                if df_day is None or len(df_day) < 2:
                    continue

                # 安全获取数据 - 捕获所有可能的异常
                try:
                    today = df_day.iloc[-1].copy()
                    yesterday = df_day.iloc[-2].copy()
                except (IndexError, KeyError, TypeError, ValueError, AttributeError) as e:
                    continue

                # 验证数据有效性
                if today is None or yesterday is None:
                    continue

                try:
                    close_today = today.get('close', 0) if hasattr(today, 'get') else today['close']
                    close_yesterday = yesterday.get('close', 0) if hasattr(yesterday, 'get') else yesterday['close']
                except (KeyError, TypeError, AttributeError):
                    continue

                if close_yesterday > 0:
                    pct = (close_today - close_yesterday) / close_yesterday * 100
                    limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10

                    if pct > 0:
                        up_count += 1
                    else:
                        down_count += 1

                    if pct >= limit_pct - 0.5:
                        limit_up_count += 1
                        # 计算连板数
                        board_count = self._count_boards(df_day, limit_pct)
                        if board_count >= 3:
                            high_board_count += 1
                    elif pct <= -limit_pct + 0.5:
                        limit_down_count += 1
            except Exception:
                continue

        # 计算情绪分（与V3一致）
        total = up_count + down_count
        breadth = up_count / total if total > 0 else 0.5

        limit_total = limit_up_count + limit_down_count
        limit_score = (limit_up_count - limit_down_count) / limit_total if limit_total > 0 else 0
        limit_score = (limit_score + 1) / 2

        board_bonus = min(high_board_count / 50, 0.2) if limit_up_count > 0 else 0

        sentiment = (breadth * 0.4 + limit_score * 0.4 + 0.2 + board_bonus) * 100
        sentiment = min(100, max(0, sentiment))

        # 确定情绪阶段
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
        """判断市场环境"""
        if sentiment_score >= 70:
            return 'bull'
        elif sentiment_score >= 50:
            return 'oscillation'
        else:
            return 'bear'

    def _get_open_gap_scene(self, open_gap: float) -> str:
        """根据开盘涨幅判断场景"""
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
        """
        检查开盘涨幅筛选

        Args:
            open_gap: 开盘涨幅%
            market_env: 市场环境

        Returns:
            (should_participate, reason, position_ratio, target_buy_gap)
        """
        scene = self._get_open_gap_scene(open_gap)
        config = self.OPEN_GAP_CONFIG.get(scene, {'participate': True})

        if not config.get('participate', True):
            return False, f'开盘涨幅筛选: {scene} 不参与', 0.0, 0.0

        # 检查市场环境要求
        require_market = config.get('require_market')
        if require_market and market_env not in require_market:
            return False, f'开盘涨幅筛选: {scene} 需要{require_market}市场', 0.0, 0.0

        position_ratio = config.get('position_ratio', 1.0)
        target_buy_gap = config.get('target_buy_gap', 0.0)

        return True, 'pass', position_ratio, target_buy_gap

    def select_stocks_for_date(self, date: pd.Timestamp) -> List[Dict]:
        """
        选出指定日期的候选股（V3优化版）

        筛选流程：
        1. 计算情绪分和市场环境
        2. 市场环境筛选（牛市参与，震荡市/熊市空仓）
        3. 情绪阶段筛选
        4. 基本条件筛选（涨停、连板、封板强度、市值）
        """
        # 重置筛选统计
        self.filter_stats = {
            'total_candidates': 0,
            'sentiment_filtered': 0,
            'market_env_filtered': 0,
            'open_gap_filtered': 0,
            'final_selected': 0,
        }

        sentiment_score, sentiment_phase = self._calc_sentiment(date)
        market_env = self._get_market_environment(sentiment_score)

        # V3优化：市场环境筛选
        market_config = self.MARKET_ENV_CONFIG.get(market_env, {})
        if not market_config.get('participate', True):
            self.filter_stats['market_env_filtered'] += 1
            return []

        # V3优化：情绪阶段筛选
        phase_config = self.SENTIMENT_PHASE_CONFIG.get(sentiment_phase, {})
        if not phase_config.get('participate', True):
            self.filter_stats['sentiment_filtered'] += 1
            return []

        candidates = []

        for code, df in self.all_data.items():
            try:
                df_day = self._filter_df_by_date(df, date)
                if df_day is None or len(df_day) < 20:
                    continue

                try:
                    close = df_day['close'].iloc[-1]
                    today = df_day.iloc[-1].copy()
                    yesterday = df_day.iloc[-2].copy()
                except (IndexError, KeyError, TypeError, ValueError, AttributeError):
                    continue

                try:
                    pct = (close - yesterday['close']) / yesterday['close'] * 100
                except (KeyError, TypeError, ZeroDivisionError):
                    continue
                limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10

                if pct < limit_pct - 0.5 or pct > limit_pct + 1:
                    continue

                board_count = self._count_boards(df_day, limit_pct)
                if board_count not in [2, 3]:
                    continue

                seal_strength = today['close'] / today['high'] if today['high'] > 0 else 0
                if seal_strength < 0.95:
                    continue

                # 计算新因子：成交量变化率
                volume_change_rate, volume_ratio = self._calc_volume_change_rate(df_day)

                # 先收集候选股，暂不按估算市值筛选（估算误差太大）
                # 后续会用真实市值筛选
                candidates.append({
                    'code': code,
                    'select_close': close,
                    'board_count': board_count,
                    'seal_strength': seal_strength,
                    'float_cap': None,  # 待获取真实市值
                    'float_shares': None,  # 待获取流通股本
                    'score': 0,  # 待计算
                    'risk_score': 0,
                    'risk_level': '待评估',
                    # 新因子
                    'volume_change_rate': volume_change_rate,
                    'volume_ratio': volume_ratio,
                    'turnover_rate': None,  # 待计算
                    'volatility': None,  # 待计算
                })
            except Exception:
                continue

        if candidates:
            # 从通达信获取流通股本，计算历史市值
            codes = [c['code'] for c in candidates]
            float_shares = self.data_loader.get_float_shares_batch(codes)

            filtered_candidates = []
            for c in candidates:
                code = c['code']
                shares = float_shares.get(code)  # 流通股本（万股）
                historical_price = c['select_close']

                # 计算历史市值 = 流通股本(万股) × 股价(元) / 10000 = 亿元
                if shares and shares > 0:
                    historical_cap = shares * historical_price / 10000  # 亿元
                    c['float_cap'] = historical_cap
                    c['float_shares'] = shares

                    # 计算换手率
                    df_code = self.all_data.get(code)
                    if df_code is not None:
                        c['turnover_rate'] = self._calc_turnover_rate(df_code, shares)
                        c['volatility'] = self._calc_recent_volatility(df_code)

                    # 使用历史市值筛选（15-200亿）
                    if 15 <= historical_cap <= 200:
                        filtered_candidates.append(c)
                # 如果无法获取流通股本，跳过该候选股

            candidates = filtered_candidates

        # 获取真实市值后，重新计算风险评分和得分
        for c in candidates:
            code = c['code']
            df_day = self._filter_df_by_date(self.all_data[code], date)
            board_count = c['board_count']
            seal_strength = c['seal_strength']
            float_cap = c['float_cap']

            risk_score, risk_factors, risk_level = self._calculate_risk_score(
                df_day, code, board_count, float_cap, sentiment_score, seal_strength
            )
            c['risk_score'] = risk_score
            c['risk_level'] = risk_level

            # 基础评分
            base_score = 60
            if board_count == 2:
                base_score = 90
            elif board_count == 3:
                base_score = 85

            # 封板强度加分
            base_score += (seal_strength - 0.95) * 100

            # ===== 新因子评分 =====
            factor_score = 0

            # 使用选股模型进行评分（如果已加载）
            if self.selection_model is not None:
                try:
                    from stock_agent.strategies.selection_model import StockScore
                    stock_score = self.selection_model.score_candidate(
                        c, sentiment_score, use_ml=True
                    )
                    # 使用选股模型的评分
                    c['score'] = stock_score.total_score
                    c['win_probability'] = stock_score.win_probability
                    c['expected_return'] = stock_score.expected_return
                    c['factor_score'] = sum(stock_score.factor_scores.values())
                    c['factor_details'] = stock_score.factor_values
                    continue  # 跳过下面的规则评分
                except Exception:
                    pass  # 如果模型评分失败，使用规则评分

            # 规则评分（后备方案）
            # 1. 成交量变化率评分（流动性因子）
            # 根据因子分析结果调整：缩量(Q1)胜率最高
            volume_change = c.get('volume_change_rate', 0)
            if volume_change < 0:
                factor_score += 5  # 缩量涨停，胜率更高
            elif 0 <= volume_change <= 50:
                factor_score += 3  # 适度放量
            elif volume_change > 200:
                factor_score -= 5  # 过度放量，可能见顶
            elif volume_change > 150:
                factor_score -= 3  # 放量较大

            # 2. 换手率评分（活跃度因子）
            # 根据因子分析结果调整：Q2-Q3区间胜率最高
            turnover_rate = c.get('turnover_rate', 0) or 0
            if 3 <= turnover_rate <= 15:
                factor_score += 5  # 理想换手率
            elif 15 < turnover_rate <= 20:
                factor_score += 2  # 较高换手率
            elif turnover_rate > 25:
                factor_score -= 3  # 过高换手率，可能主力出货

            # 3. 波动率评分（风险因子）
            # 根据因子分析结果调整：Q4区间(中高波动)胜率最高
            volatility = c.get('volatility', 0) or 0
            if 4 <= volatility <= 8:
                factor_score += 3  # 中高波动，胜率较高
            elif 2 <= volatility < 4:
                factor_score += 1  # 适中波动
            elif volatility > 10:
                factor_score -= 3  # 高波动，风险大

            # 综合评分
            total_score = base_score + factor_score - risk_score * 0.3
            c['score'] = min(100, max(0, total_score))

            # 记录因子评分详情
            c['factor_score'] = factor_score
            c['factor_details'] = {
                'volume_change': volume_change,
                'turnover': turnover_rate,
                'volatility': volatility,
            }

        candidates.sort(key=lambda x: x['score'], reverse=True)
        return candidates

    def simulate_trade(self, select_date: pd.Timestamp, candidate: Dict,
                       max_hold_days: int = 5) -> Optional[StockAnalysisResult]:
        """
        模拟交易（V3优化版）

        V3核心优化：
        1. 开盘涨幅筛选：不符合条件的开盘涨幅不参与
        2. 低吸策略：根据开盘涨幅计算目标买入价
        3. 实际买入确认：只有当日最低价触及目标买入价才算"能买进"

        卖出条件：
        1. 硬止损：亏损超过6%止损
        2. 移动止盈：盈利超过5%后回撤3%止盈
        3. 分批止盈：+5%卖30%，+10%卖30%，+15%卖40%
        4. 到期卖出：持仓超过5天强制卖出
        """
        code = candidate['code']
        select_close = candidate['select_close']

        date_idx = self.trade_dates.index(select_date) if select_date in self.trade_dates else -1
        if date_idx < 0 or date_idx + 1 >= len(self.trade_dates):
            return None

        trade_date = self.trade_dates[date_idx + 1]

        df = self.all_data.get(code)
        if df is None:
            return None

        df_trade = self._filter_df_by_date(df, trade_date)
        if len(df_trade) < 1 or df_trade.index[-1] != trade_date:
            return None

        # 获取交易当天的价格数据 - 添加安全访问
        try:
            trade_row = df_trade.iloc[-1].copy()
            trade_open = trade_row['open']
            trade_high = trade_row['high']
            trade_low = trade_row['low']
            trade_close = trade_row['close']
        except (IndexError, KeyError, TypeError, ValueError, AttributeError):
            return None

        # 计算开盘涨跌幅：交易日开盘价相对选股日收盘价的涨跌幅
        if len(df_trade) >= 2:
            try:
                prev_row = df_trade.iloc[-2].copy()
                prev_close = prev_row['close']  # 选股日收盘价
                open_gap_pct = (trade_open - prev_close) / prev_close * 100 if prev_close > 0 else 0
            except (IndexError, KeyError, TypeError, ValueError, AttributeError):
                open_gap_pct = 0
        else:
            open_gap_pct = 0

        # 过滤条件1：交易当天是一字板的跳过（开盘=收盘=最高=最低）
        if trade_open == trade_close == trade_high == trade_low:
            return None

        # ========== V3优化：开盘涨幅筛选 ==========
        # 获取市场环境
        sentiment_score, sentiment_phase = self._calc_sentiment(select_date)
        market_env = self._get_market_environment(sentiment_score)

        # 检查开盘涨幅是否符合条件
        should_participate, gap_reason, position_ratio, target_buy_gap = self._check_open_gap_filter(
            open_gap_pct, market_env
        )

        if not should_participate:
            self.filter_stats['open_gap_filtered'] += 1
            return None

        # ========== 计算目标买入价和实际买入验证 ==========
        # 目标买入价 = 选股日收盘价 * (1 + target_buy_gap / 100)
        target_buy_price = round(select_close * (1 + target_buy_gap / 100), 2)

        # 判断是否能够买进（关键：必须满足买入条件）
        actual_buy_price = None
        buy_method = ""
        can_buy = False
        buy_confirm_reason = ""

        if target_buy_gap < 0:
            # 低吸场景：目标是在低于开盘价的位置买入
            # 例如：低开1-2%场景，目标在-3.5%位置买入
            # 关键条件：最低价必须 <= 目标买入价，才能在目标价位买进
            if trade_low <= target_buy_price:
                # 低吸成功：最低价触及或跌破目标价，可以在目标价买入
                actual_buy_price = target_buy_price
                buy_method = f"低吸{target_buy_gap:.1f}%成功"
                can_buy = True
                buy_confirm_reason = f"最低价{trade_low:.2f}<=目标价{target_buy_price:.2f}，可买入"
            else:
                # 低吸失败：最低价未触及目标价，无法在目标价买入
                return None
        elif target_buy_gap == 0:
            # 开盘价买入场景
            actual_buy_price = trade_open
            buy_method = "开盘价买入"
            can_buy = True
            buy_confirm_reason = f"开盘价{trade_open:.2f}买入"
        else:
            # target_buy_gap > 0 的情况（理论上不应该出现）
            actual_buy_price = trade_open
            buy_method = "开盘价买入"
            can_buy = True
            buy_confirm_reason = f"开盘价{trade_open:.2f}买入"

        # 最终买入价
        trade_price = actual_buy_price

        # 计算最大跌幅：最低价相对开盘价的跌幅
        max_drop_pct = (trade_low - trade_open) / trade_open * 100 if trade_open > 0 else 0

        buy_lots = int(self.initial_capital / (trade_price * self.lot_size))
        if buy_lots <= 0:
            return None

        buy_amount = buy_lots * self.lot_size
        buy_cost = buy_amount * trade_price

        # 计算止损价（买入价的94%，即亏损6%）
        stop_loss_price = round(trade_price * 0.94, 2)

        remaining_shares = buy_amount
        total_sell_amount = 0.0
        hold_days = 0
        max_profit_pct = 0.0
        sold_5pct = False
        sold_10pct = False
        sold_15pct = False
        last_close = trade_price
        sell_reason = ""
        avg_sell_price = 0.0

        for i in range(2, max_hold_days + 2):
            if date_idx + i >= len(self.trade_dates):
                break

            check_date = self.trade_dates[date_idx + i]
            df_check = self._filter_df_by_date(df, check_date)

            if len(df_check) < 1 or df_check.index[-1] != check_date:
                continue

            try:
                check_high = df_check.iloc[-1]['high']
                check_low = df_check.iloc[-1]['low']
                check_close = df_check.iloc[-1]['close']
            except (IndexError, KeyError, TypeError, ValueError, AttributeError):
                continue
            last_close = check_close

            hold_days = i - 1

            high_profit_pct = (check_high - trade_price) / trade_price * 100
            low_profit_pct = (check_low - trade_price) / trade_price * 100

            if high_profit_pct > max_profit_pct:
                max_profit_pct = high_profit_pct

            # 硬止损：当天最低价触及止损价时，按止损价卖出
            if check_low <= stop_loss_price and hold_days >= 1:
                sell_shares = remaining_shares
                total_sell_amount += sell_shares * stop_loss_price  # 按止损价卖出
                remaining_shares = 0
                sell_reason = f"止损({stop_loss_price:.2f})"
                break

            # 分批止盈
            if hold_days >= 1:
                if not sold_5pct and high_profit_pct >= 5:
                    sell_shares = int(buy_amount * 0.3 / self.lot_size) * self.lot_size
                    if sell_shares <= 0:
                        sell_shares = self.lot_size
                    if sell_shares > remaining_shares:
                        sell_shares = remaining_shares
                    total_sell_amount += sell_shares * check_high
                    remaining_shares -= sell_shares
                    sold_5pct = True
                    sell_reason = "分批止盈"

                if not sold_10pct and high_profit_pct >= 10:
                    sell_shares = int(buy_amount * 0.3 / self.lot_size) * self.lot_size
                    if sell_shares <= 0:
                        sell_shares = self.lot_size
                    if sell_shares > remaining_shares:
                        sell_shares = remaining_shares
                    total_sell_amount += sell_shares * check_high
                    remaining_shares -= sell_shares
                    sold_10pct = True

                if not sold_15pct and high_profit_pct >= 15:
                    sell_shares = int(buy_amount * 0.4 / self.lot_size) * self.lot_size
                    if sell_shares <= 0:
                        sell_shares = self.lot_size
                    if sell_shares > remaining_shares:
                        sell_shares = remaining_shares
                    total_sell_amount += sell_shares * check_high
                    remaining_shares -= sell_shares
                    sold_15pct = True

                # 移动止盈
                if max_profit_pct >= 5:
                    trailing_stop_price = trade_price * (1 + max_profit_pct / 100 - 0.03)
                    if check_low <= trailing_stop_price:
                        sell_shares = remaining_shares
                        total_sell_amount += sell_shares * trailing_stop_price
                        remaining_shares = 0
                        sell_reason = f"移动止盈({trailing_stop_price:.2f})"
                        break

            # 到期卖出
            if hold_days >= max_hold_days:
                sell_shares = remaining_shares
                total_sell_amount += sell_shares * check_close
                remaining_shares = 0
                sell_reason = f"到期({check_close:.2f})"
                break

        # 如果还有剩余股份，按最后收盘价卖出
        if remaining_shares > 0:
            total_sell_amount += remaining_shares * last_close
            sell_reason = f"按收盘价({last_close:.2f})"

        profit = total_sell_amount - buy_cost
        profit_pct = profit / buy_cost * 100
        avg_sell_price = total_sell_amount / buy_amount if buy_amount > 0 else 0

        # 获取开盘涨幅场景
        open_gap_scene = self._get_open_gap_scene(open_gap_pct)

        # 获取新因子数据
        volume_change_rate = candidate.get('volume_change_rate', 0) or 0
        volume_ratio = candidate.get('volume_ratio', 1.0) or 1.0
        turnover_rate = candidate.get('turnover_rate', 0) or 0
        volatility = candidate.get('volatility', 0) or 0
        factor_score = candidate.get('factor_score', 0) or 0

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
            stop_loss_price=stop_loss_price,
            # 交易当天详细信息
            trade_open=trade_open,
            trade_high=trade_high,
            trade_low=trade_low,
            trade_close=trade_close,
            max_drop_pct=max_drop_pct,
            open_gap_pct=open_gap_pct,
            # V3新增字段
            buy_method=buy_method,
            open_gap_scene=open_gap_scene,
            market_env=market_env,
            sentiment_phase=sentiment_phase,
            sentiment_score=sentiment_score,
            target_buy_price=target_buy_price,
            # 买入验证字段
            can_buy=can_buy,
            buy_confirm_reason=buy_confirm_reason,
            # V4新增因子字段
            volume_change_rate=volume_change_rate,
            volume_ratio=volume_ratio,
            turnover_rate=turnover_rate,
            volatility=volatility,
            factor_score=factor_score,
        )

    def analyze_period(self, start_date: str, end_date: str,
                       market: str = "sha") -> Tuple[List[StockAnalysisResult], Dict]:
        """分析指定时间范围"""
        self.load_data(start_date, end_date, market)

        results = []
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)

        # 记录每只股票最近的交易日期，用于过滤5个交易日内重复出现
        # key: 股票代码, value: 最近的交易日期
        stock_last_trade_date = {}

        select_dates = []
        for i, date in enumerate(self.trade_dates):
            if start_dt <= date <= end_dt:
                if i + 1 < len(self.trade_dates):
                    select_dates.append(date)

        total_dates = len(select_dates)
        print(f"[4/4] 开始分析 {total_dates} 个交易日...")

        progress_interval = max(1, total_dates // 10)  # 每10%打印一次
        start_time = time.time()

        for idx, date in enumerate(select_dates):
            candidates = self.select_stocks_for_date(date)
            for candidate in candidates:
                code = candidate['code']

                # 过滤条件2：5个交易日内重复出现的跳过
                if code in stock_last_trade_date:
                    last_trade_date = stock_last_trade_date[code]
                    # 计算交易日间隔
                    try:
                        last_idx = self.trade_dates.index(last_trade_date)
                        current_idx = self.trade_dates.index(date)
                        days_gap = current_idx - last_idx
                        if days_gap < 5:  # 5个交易日内重复
                            continue
                    except ValueError:
                        pass

                trade_result = self.simulate_trade(date, candidate)
                if trade_result:
                    results.append(trade_result)
                    self.filter_stats['final_selected'] += 1
                    # 记录该股票的交易日期
                    stock_last_trade_date[code] = date

            # 打印进度
            if (idx + 1) % progress_interval == 0 or idx + 1 == total_dates:
                pct = (idx + 1) / total_dates * 100
                elapsed = time.time() - start_time
                eta = elapsed / (idx + 1) * (total_dates - idx - 1)
                print(f"      分析进度: {idx+1}/{total_dates} ({pct:.0f}%) - 已发现 {len(results)} 笔交易 - ETA: {eta:.0f}秒")

        elapsed = time.time() - start_time
        print(f"[4/4] 分析完成: 共 {len(results)} 笔交易, 耗时 {elapsed:.1f}秒")

        # 只统计已卖出的股票（持股天数>0）
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
            }

        return results, summary

    def analyze_period_batch(
        self,
        start_date: str,
        end_date: str,
        market: str = "sha",
        batch_months: int = 6,
        progress_callback=None
    ) -> Tuple[List[StockAnalysisResult], Dict]:
        """
        分批分析指定时间范围（适用于大数据量训练）

        Args:
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            market: 市场类型
            batch_months: 每批次的月数（默认6个月）
            progress_callback: 进度回调函数 callback(batch_idx, total_batches, batch_result)

        Returns:
            (results, summary) 所有批次的结果和汇总统计
        """
        from datetime import datetime

        start_dt = datetime.strptime(start_date, '%Y%m%d')
        end_dt = datetime.strptime(end_date, '%Y%m%d')

        # 计算批次
        batches = []
        current_start = start_dt

        while current_start < end_dt:
            # 计算批次结束日期
            if HAS_DATEUTIL:
                current_end = current_start + relativedelta.relativedelta(months=batch_months)
            else:
                # 简单方法：每月按30天计算
                current_end = current_start + timedelta(days=batch_months * 30)

            current_end = min(current_end, end_dt)

            batches.append({
                'start': current_start.strftime('%Y%m%d'),
                'end': current_end.strftime('%Y%m%d')
            })

            # 下一批次
            current_start = current_end + timedelta(days=1)

        print(f"\n{'='*60}")
        print(f"批量训练模式: 共 {len(batches)} 个批次")
        print(f"时间范围: {start_date} - {end_date}")
        print(f"每批次: {batch_months} 个月")
        print(f"{'='*60}")

        all_results = []
        all_summaries = []
        batch_start_time = time.time()

        for i, batch in enumerate(batches):
            batch_num = i + 1
            total_batches = len(batches)
            elapsed_batches = time.time() - batch_start_time
            avg_batch_time = elapsed_batches / batch_num if batch_num > 0 else 0
            remaining_time = avg_batch_time * (total_batches - batch_num)

            print(f"\n{'─'*60}")
            print(f"批次 {batch_num}/{total_batches} ({batch_num/total_batches*100:.0f}%)")
            print(f"时间段: {batch['start']} - {batch['end']}")
            if batch_num > 1:
                print(f"已用时间: {elapsed_batches/60:.1f}分钟, 预计剩余: {remaining_time/60:.1f}分钟")
            print(f"{'─'*60}")

            # 清理上一批次的数据
            self.all_data = {}
            self.trade_dates = []
            self.stock_names = {}

            # 运行分析
            try:
                batch_st = time.time()
                results, summary = self.analyze_period(
                    batch['start'],
                    batch['end'],
                    market
                )
                batch_elapsed = time.time() - batch_st

                all_results.extend(results)
                all_summaries.append(summary)

                if progress_callback:
                    progress_callback(i + 1, len(batches), summary)

                # 输出批次结果
                print(f"\n{'─'*60}")
                if summary['total_trades'] > 0:
                    print(f"批次 {batch_num} 结果: {summary['total_trades']} 笔交易, 胜率 {summary['win_rate']:.1f}%, 收益 {summary['total_return_pct']:.2f}%")
                else:
                    print(f"批次 {batch_num} 结果: 无交易")
                print(f"批次耗时: {batch_elapsed:.1f}秒")
                print(f"累计交易: {len(all_results)} 笔")

            except Exception as e:
                print(f"  批次处理失败: {e}")
                import traceback
                traceback.print_exc()

        total_elapsed = time.time() - batch_start_time

        # 汇总统计
        valid_results = [r for r in all_results if r.hold_days > 0]

        if len(valid_results) > 0:
            total_trades = len(valid_results)
            win_trades = sum(1 for r in valid_results if r.is_win)
            total_profit = sum(r.profit for r in valid_results)
            total_cost = sum(r.buy_cost for r in valid_results)
            avg_profit_pct = sum(r.profit_pct for r in valid_results) / total_trades

            final_summary = {
                'start_date': start_date,
                'end_date': end_date,
                'market': market,
                'total_trades': total_trades,
                'win_trades': win_trades,
                'win_rate': win_trades / total_trades * 100,
                'total_profit': total_profit,
                'total_cost': total_cost,
                'total_return_pct': total_profit / total_cost * 100 if total_cost > 0 else 0,
                'avg_profit_pct': avg_profit_pct,
                'batch_count': len(batches),
            }
        else:
            final_summary = {
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
                'batch_count': len(batches),
            }

        print(f"\n{'='*60}")
        print(f"批量训练完成")
        print(f"{'='*60}")
        print(f"总耗时: {total_elapsed/60:.1f} 分钟")
        print(f"总批次: {len(batches)}")
        print(f"总交易: {final_summary['total_trades']} 笔")
        print(f"总胜率: {final_summary['win_rate']:.1f}%")
        print(f"总收益: {final_summary['total_return_pct']:.2f}%")
        print(f"平均每笔收益: {final_summary['avg_profit_pct']:.2f}%")
        print(f"{'='*60}")

        return all_results, final_summary

    def _get_output_path(self, start_date: str, end_date: str, market: str, report_type: str = "analysis", topic: str = "analysis") -> str:
        """根据日期范围和市场确定输出目录和文件名"""
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

    def generate_markdown_report(self, results: List[StockAnalysisResult], summary: Dict,
                                  output_path: str = None) -> str:
        """生成Markdown格式的分析报告"""
        # 市场名称映射
        market_names = {
            'gem': '创业板',
            'sha': '沪A',
            'sza': '深A',
            'all': '全部A股',
        }
        market = summary.get('market', 'sha')
        market_name = market_names.get(market, market.upper())

        lines = []

        lines.append(f"# 选股收益分析报告（V3优化版）")
        lines.append(f"")
        lines.append(f"**分析市场**: {market_name}")
        lines.append(f"")
        lines.append(f"**分析区间**: {summary['start_date']} - {summary['end_date']}")
        lines.append(f"")
        lines.append(f"**策略版本**: V3四层筛选深度优化版")
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
        lines.append(f"")

        # 筛选统计
        if self.filter_stats.get('total_candidates', 0) > 0:
            lines.append(f"### 筛选统计")
            lines.append(f"")
            lines.append(f"| 筛选阶段 | 数量 |")
            lines.append(f"|----------|------|")
            lines.append(f"| 候选股票总数 | {self.filter_stats.get('total_candidates', 0)} |")
            lines.append(f"| 市场环境过滤 | {self.filter_stats.get('market_env_filtered', 0)} |")
            lines.append(f"| 情绪阶段过滤 | {self.filter_stats.get('sentiment_filtered', 0)} |")
            lines.append(f"| 开盘涨幅过滤 | {self.filter_stats.get('open_gap_filtered', 0)} |")
            lines.append(f"| 最终选中 | {self.filter_stats.get('final_selected', 0)} |")
            lines.append(f"")

        lines.append(f"## 二、个股明细（含买入验证）")
        lines.append(f"")
        lines.append(f"| 选股日期 | 代码 | 名称 | 开盘场景 | 开盘涨跌 | 最低价 | 目标价 | 买入价 | 买入方法 | 持股 | 收益率 | 卖出原因 |")
        lines.append(f"|----------|------|------|----------|----------|--------|--------|--------|----------|------|--------|----------|")

        for r in results:
            if r.hold_days > 0:
                lines.append(f"| {r.select_date} | {r.code} | {r.name} | {r.open_gap_scene} | {r.open_gap_pct:+.2f}% | {r.trade_low:.2f} | {r.target_buy_price:.2f} | {r.trade_price:.2f} | {r.buy_method} | {r.hold_days}天 | {r.profit_pct:+.2f}% | {r.sell_reason} |")
            else:
                lines.append(f"| {r.select_date} | {r.code} | {r.name} | {r.open_gap_scene} | {r.open_gap_pct:+.2f}% | {r.trade_low:.2f} | {r.target_buy_price:.2f} | {r.trade_price:.2f} | {r.buy_method} | {r.hold_days}天 | --- | --- |")

        lines.append(f"")

        # 买入验证详情
        lines.append(f"### 买入验证详情")
        lines.append(f"")
        lines.append(f"| 代码 | 名称 | 开盘场景 | 开盘价 | 最低价 | 目标价 | 是否买入 | 买入确认原因 |")
        lines.append(f"|------|------|----------|--------|--------|--------|----------|--------------|")

        for r in results:
            can_buy_str = "✅ 是" if r.can_buy else "❌ 否"
            lines.append(f"| {r.code} | {r.name} | {r.open_gap_scene} | {r.trade_open:.2f} | {r.trade_low:.2f} | {r.target_buy_price:.2f} | {can_buy_str} | {r.buy_confirm_reason} |")

        lines.append(f"")

        # ===== 新增：因子分析 =====
        lines.append(f"### 因子分析详情（V4新增）")
        lines.append(f"")
        lines.append(f"| 代码 | 名称 | 成交量变化% | 量比 | 换手率% | 波动率% | 因子评分 |")
        lines.append(f"|------|------|------------|------|--------|--------|----------|")

        for r in results:
            lines.append(f"| {r.code} | {r.name} | {r.volume_change_rate:+.1f} | {r.volume_ratio:.2f} | {r.turnover_rate:.2f} | {r.volatility:.2f} | {r.factor_score:+.1f} |")

        lines.append(f"")

        # 因子统计
        valid_results = [r for r in results if r.hold_days > 0]
        if valid_results:
            avg_volume_change = sum(r.volume_change_rate for r in valid_results) / len(valid_results)
            avg_volume_ratio = sum(r.volume_ratio for r in valid_results) / len(valid_results)
            avg_turnover = sum(r.turnover_rate for r in valid_results) / len(valid_results)
            avg_volatility = sum(r.volatility for r in valid_results) / len(valid_results)
            avg_factor_score = sum(r.factor_score for r in valid_results) / len(valid_results)

            lines.append(f"**因子平均值统计**：")
            lines.append(f"")
            lines.append(f"| 因子 | 平均值 | 说明 |")
            lines.append(f"|------|--------|------|")
            lines.append(f"| 成交量变化率 | {avg_volume_change:+.2f}% | 放量程度 |")
            lines.append(f"| 量比 | {avg_volume_ratio:.2f} | 相对5日均量倍数 |")
            lines.append(f"| 换手率 | {avg_turnover:.2f}% | 活跃度指标 |")
            lines.append(f"| 波动率 | {avg_volatility:.2f}% | 风险指标 |")
            lines.append(f"| 因子评分 | {avg_factor_score:+.2f} | 综合因子得分 |")
            lines.append(f"")

        lines.append(f"## 三、按市场环境统计")
        lines.append(f"")
        lines.append(f"| 市场环境 | 选股数 | 盈利数 | 胜率 | 收益率 |")
        lines.append(f"|----------|--------|--------|------|------|")

        env_stats = {}
        for r in results:
            if r.hold_days > 0:
                env = r.market_env or 'unknown'
                if env not in env_stats:
                    env_stats[env] = {'count': 0, 'wins': 0, 'profit': 0, 'cost': 0}
                env_stats[env]['count'] += 1
                env_stats[env]['profit'] += r.profit
                env_stats[env]['cost'] += r.buy_cost
                if r.is_win:
                    env_stats[env]['wins'] += 1

        env_names = {'bull': '牛市', 'oscillation': '震荡市', 'bear': '熊市'}
        for env in ['bull', 'oscillation', 'bear']:
            if env in env_stats:
                stats = env_stats[env]
                win_rate = stats['wins'] / stats['count'] * 100 if stats['count'] > 0 else 0
                profit_rate = stats['profit'] / stats['cost'] * 100 if stats['cost'] > 0 else 0
                lines.append(f"| **{env_names[env]}** | {stats['count']} | {stats['wins']} | {win_rate:.2f}% | {profit_rate:+.2f}% |")

        lines.append(f"")

        lines.append(f"## 四、按开盘涨幅场景统计")
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
            lines.append(f"| **{scene}** | {stats['count']} | {stats['wins']} | {win_rate:.2f}% | {profit_rate:+.2f}% |")

        lines.append(f"")

        lines.append(f"## 五、策略说明")
        lines.append(f"")
        lines.append(f"### V3四层筛选机制")
        lines.append(f"")
        lines.append(f"**第一层：连板数筛选**")
        lines.append(f"- 只选2板、3板（排除1板和4板以上）")
        lines.append(f"- 2板为核心参与，3板适度参与")
        lines.append(f"")
        lines.append(f"**第二层：情绪阶段筛选**")
        lines.append(f"- 退潮期：核心参与，十年胜率69%")
        lines.append(f"- 发酵期：积极参与，十年胜率55.6%")
        lines.append(f"- 高潮期：控制仓位，十年胜率56.4%")
        lines.append(f"- 启动期/冰点期：严格回避")
        lines.append(f"")
        lines.append(f"**第三层：开盘涨幅筛选**")
        lines.append(f"- 高开0-2%：目标-1%低吸买入")
        lines.append(f"- 高开2-3%：开盘价买入（核心场景）")
        lines.append(f"- 高开3%以上：回避（主力陷阱区）")
        lines.append(f"- 低开0-4%：目标-3.5%低吸买入（核心场景）")
        lines.append(f"- 低开4%以上：回避（接飞刀）")
        lines.append(f"")
        lines.append(f"**第四层：市场环境筛选**")
        lines.append(f"- 牛市（情绪分>=70）：满仓参与")
        lines.append(f"- 震荡市（情绪分50-70）：空仓观望")
        lines.append(f"- 熊市（情绪分<50）：空仓观望")
        lines.append(f"")

        lines.append(f"### V4新增：因子评分体系")
        lines.append(f"")
        lines.append(f"**成交量变化率（流动性因子）**")
        lines.append(f"- 放量50%-150%：适度放量，+5分")
        lines.append(f"- 放量>150%：过度放量，-3分（可能见顶）")
        lines.append(f"- 缩量（<0%）：缩量涨停，-2分（动力不足）")
        lines.append(f"")
        lines.append(f"**换手率（活跃度因子）**")
        lines.append(f"- 换手率3%-10%：理想区间，+5分")
        lines.append(f"- 换手率10%-15%：较高活跃，+2分")
        lines.append(f"- 换手率>20%：过高活跃，-3分（可能主力出货）")
        lines.append(f"")
        lines.append(f"**波动率（风险因子）**")
        lines.append(f"- 波动率2%-5%：适中波动，+3分")
        lines.append(f"- 波动率>8%：高波动，-3分（风险大）")
        lines.append(f"")

        lines.append(f"### 交易规则")
        lines.append(f"")
        lines.append(f"**买入规则**:")
        lines.append(f"- 选股日次日根据开盘场景确定目标买入价")
        lines.append(f"- **低吸策略（关键）**：")
        lines.append(f"  - 高开0-2%场景：目标价为选股日收盘价的-1%")
        lines.append(f"  - 低开0-4%场景：目标价为选股日收盘价的-3.5%")
        lines.append(f"  - **必须验证**：交易日最低价必须 <= 目标价，才能确认买入成功")
        lines.append(f"  - 如果最低价未触及目标价，该笔交易不成立，不计入统计")
        lines.append(f"- **开盘价买入**：高开2-3%场景，直接以开盘价买入")
        lines.append(f"- 每只股票初始资金{self.initial_capital:,.0f}元，每手{self.lot_size}股")
        lines.append(f"")
        lines.append(f"**卖出条件**:")
        lines.append(f"1. **硬止损**: 亏损超过6%止损")
        lines.append(f"2. **移动止盈**: 盈利超过5%后，从最高点回撤3%止盈")
        lines.append(f"3. **分批止盈**:")
        lines.append(f"   - 盈利达到+5%时卖出30%")
        lines.append(f"   - 盈利达到+10%时卖出30%")
        lines.append(f"   - 盈利达到+15%时卖出40%")
        lines.append(f"4. **到期卖出**: 持仓超过5天强制按收盘价卖出")

        lines.append(f"")
        lines.append(f"## 六、风险提示")
        lines.append(f"")
        lines.append(f"- 本报告基于历史数据分析，不构成投资建议")
        lines.append(f"- 策略过往表现不代表未来收益")
        lines.append(f"- 股票投资存在风险，请谨慎决策")
        lines.append(f"")

        lines.append(f"---")
        lines.append(f"")
        lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        report_content = "\n".join(lines)

        if output_path is None:
            output_path = self._get_output_path(summary['start_date'], summary['end_date'], summary.get('market', 'sha'), "analysis")

        import os
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report_content)
        print(f"\nMarkdown报告已保存至: {output_path}")

        return report_content

    def print_report(self, results: List[StockAnalysisResult], summary: Dict):
        """打印分析报告"""
        # 市场名称映射
        market_names = {
            'gem': '创业板',
            'sha': '沪A',
            'sza': '深A',
            'all': '全部A股',
        }
        market = summary.get('market', 'sha')
        market_name = market_names.get(market, market.upper())

        print("\n" + "=" * 120)
        print(f"选股收益分析报告")
        print(f"分析市场: {market_name}")
        print(f"分析区间: {summary['start_date']} - {summary['end_date']}")
        print(f"每只股票初始资金: {self.initial_capital:,.0f} 元 | 每手股数: {self.lot_size} 股 | T+1模式")
        print("=" * 120)

        print(f"\n【汇总统计】")
        print(f"  选股次数: {summary['total_trades']} 次")
        print(f"  盈利次数: {summary['win_trades']} 次")
        print(f"  成功率: {summary['win_rate']:.3f}%")
        print(f"  总投入: {summary['total_cost']:,.2f} 元")
        print(f"  总收益: {summary['total_profit']:,.2f} 元")
        print(f"  总收益率: {summary['total_return_pct']:.3f}%")
        print(f"  平均收益: {summary['avg_profit_pct']:.3f}%")

        print(f"\n【个股明细】")
        header = f"{'选股日期':<12} {'代码':<8} {'名称':<8} {'买入价':<8} {'止损价':<8} {'卖出价':<8} {'持股':<6} {'收益率':<10} {'卖出原因':<20}"
        print(header)
        print("-" * 120)

        for r in results:
            if r.hold_days > 0:
                print(f"{r.select_date:<12} {r.code:<8} {r.name:<8} {r.trade_price:<8.2f} {r.stop_loss_price:<8.2f} {r.sell_price:<8.2f} {r.hold_days}天   {r.profit_pct:>8.2f}%  {r.sell_reason:<20}")
            else:
                # 持股天数为0，表示未卖出
                print(f"{r.select_date:<12} {r.code:<8} {r.name:<8} {r.trade_price:<8.2f} {r.stop_loss_price:<8.2f} {'---':<8} {r.hold_days}天   {'---':<10}  {'---':<20}")

        print("=" * 120)

    def diagnose_stock(self, code: str, date_str: str, market: str = "all") -> Dict:
        """
        诊断指定股票在指定日期为什么没被选出来

        Args:
            code: 股票代码
            date_str: 选股日期 (YYYYMMDD)
            market: 市场类型

        Returns:
            诊断结果字典
        """
        import pandas as pd

        # 加载数据（如果还没加载）
        if not self.all_data:
            self.load_data(date_str, date_str, market)

        date = pd.Timestamp(date_str)
        result = {
            'code': code,
            'date': date_str,
            'filters': [],
            'passed': True,
            'reason': '',
        }

        print(f"\n{'='*60}")
        print(f"诊断股票: {code} 日期: {date_str}")
        print(f"{'='*60}")

        # 检查1: 数据是否存在
        if code not in self.all_data:
            result['passed'] = False
            result['reason'] = '股票数据不存在'
            print(f"❌ 数据不存在: 股票代码 {code} 不在数据中")
            return result
        else:
            print(f"[OK] 数据存在")

        df = self.all_data[code]
        df_day = self._filter_df_by_date(df, date)

        # 检查2: 数据是否足够
        if len(df_day) < 20:
            result['passed'] = False
            result['reason'] = f'数据不足: 仅{len(df_day)}天'
            print(f"❌ 数据不足: 仅{len(df_day)}天 (需要>=20天)")
            return result
        else:
            print(f"✅ 数据足够: {len(df_day)}天")

        # 获取当日数据
        try:
            close = df_day['close'].iloc[-1]
            today = df_day.iloc[-1].copy()
            yesterday = df_day.iloc[-2].copy()
        except (IndexError, KeyError, TypeError, ValueError, AttributeError):
            result['passed'] = False
            result['reason'] = '数据访问错误'
            print(f"❌ 数据访问错误")
            return result

        # 检查3: 是否涨停
        try:
            pct = (close - yesterday['close']) / yesterday['close'] * 100
        except (KeyError, TypeError, ZeroDivisionError):
            result['passed'] = False
            result['reason'] = '计算涨幅失败'
            print(f"❌ 计算涨幅失败")
            return result
        limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10

        if pct < limit_pct - 0.5:
            result['passed'] = False
            result['reason'] = f'未涨停: 涨幅{pct:.2f}%'
            print(f"❌ 未涨停: 涨幅{pct:.2f}% (需要>={limit_pct-0.5}%)")
            return result
        elif pct > limit_pct + 1:
            result['passed'] = False
            result['reason'] = f'涨幅异常: 涨幅{pct:.2f}%'
            print(f"❌ 涨幅异常: 涨幅{pct:.2f}% (不能>{limit_pct+1}%)")
            return result
        else:
            print(f"✅ 涨停: 涨幅{pct:.2f}%")

        # 检查4: 连板数
        board_count = self._count_boards(df_day, limit_pct)
        if board_count not in [2, 3]:
            result['passed'] = False
            result['reason'] = f'连板数{board_count}板 (需2-3板)'
            print(f"❌ 连板数不符合: {board_count}板 (需要2或3板)")
            return result
        else:
            print(f"✅ 连板数符合: {board_count}板")

        # 检查5: 封板强度
        seal_strength = today['close'] / today['high'] if today['high'] > 0 else 0
        if seal_strength < 0.95:
            result['passed'] = False
            result['reason'] = f'封板强度{seal_strength:.1%}'
            print(f"❌ 封板强度不足: {seal_strength:.1%} (需要>=95%)")
            return result
        else:
            print(f"✅ 封板强度: {seal_strength:.1%}")

        # 检查6: 市值
        estimated_cap = today['amount'] / 0.05 / 100000000
        if estimated_cap < 10 or estimated_cap > 500:
            result['passed'] = False
            result['reason'] = f'市值{estimated_cap:.1f}亿'
            print(f"❌ 市值不符合: {estimated_cap:.1f}亿 (需要10-500亿)")
            return result
        else:
            print(f"✅ 估算市值: {estimated_cap:.1f}亿")

        # 检查7: 真实市值
        real_caps = self.realtime_quote.get_float_market_cap_batch([code], n_workers=1)
        if code in real_caps:
            real_cap = real_caps[code]
            if real_cap < 15 or real_cap > 200:
                result['passed'] = False
                result['reason'] = f'真实市值{real_cap:.1f}亿 (需15-200亿)'
                print(f"❌ 真实市值不符合: {real_cap:.1f}亿 (需要15-200亿)")
                return result
            else:
                print(f"✅ 真实市值: {real_cap:.1f}亿")
        else:
            print(f"⚠️ 无法获取真实市值，使用估算值")

        # 检查8: 市场环境
        sentiment_score, sentiment_phase = self._calc_sentiment(date)
        market_env = self._get_market_environment(sentiment_score)
        print(f"\n市场情绪: {sentiment_score:.1f}分, 阶段: {sentiment_phase}, 环境: {market_env}")

        market_config = self.MARKET_ENV_CONFIG.get(market_env, {})
        if not market_config.get('participate', True):
            result['passed'] = False
            result['reason'] = f'市场环境{market_env}'
            print(f"❌ 市场环境筛选: {market_env} 不参与")
            return result
        else:
            print(f"✅ 市场环境: {market_env} 可参与")

        # 检查9: 情绪阶段
        phase_config = self.SENTIMENT_PHASE_CONFIG.get(sentiment_phase, {})
        if not phase_config.get('participate', True):
            result['passed'] = False
            result['reason'] = f'情绪阶段{sentiment_phase}'
            print(f"❌ 情绪阶段筛选: {sentiment_phase} 不参与")
            return result
        else:
            print(f"✅ 情绪阶段: {sentiment_phase} 可参与")

        # 检查10: 次日开盘涨幅
        date_idx = self.trade_dates.index(date) if date in self.trade_dates else -1
        if date_idx < 0 or date_idx + 1 >= len(self.trade_dates):
            result['passed'] = False
            result['reason'] = '无次日交易数据'
            print(f"❌ 无次日交易数据")
            return result

        trade_date = self.trade_dates[date_idx + 1]
        df_trade = self._filter_df_by_date(df, trade_date)
        if len(df_trade) < 1 or df_trade.index[-1] != trade_date:
            result['passed'] = False
            result['reason'] = '次日数据不完整'
            print(f"❌ 次日数据不完整")
            return result

        try:
            trade_open = df_trade.iloc[-1]['open']
            trade_low = df_trade.iloc[-1]['low']
            prev_close = df_trade.iloc[-2]['close']
            open_gap_pct = (trade_open - prev_close) / prev_close * 100
        except (IndexError, KeyError, TypeError, ValueError, AttributeError, ZeroDivisionError):
            result['passed'] = False
            result['reason'] = '次日数据访问错误'
            print(f"❌ 次日数据访问错误")
            return result

        print(f"\n次日({trade_date.strftime('%Y-%m-%d')})数据:")
        print(f"  开盘价: {trade_open:.2f}, 最低价: {trade_low:.2f}")
        print(f"  开盘涨跌: {open_gap_pct:+.2f}%")

        # 检查11: 开盘涨幅场景
        open_gap_scene = self._get_open_gap_scene(open_gap_pct)
        gap_config = self.OPEN_GAP_CONFIG.get(open_gap_scene, {})
        print(f"  开盘场景: {open_gap_scene}")

        if not gap_config.get('participate', True):
            result['passed'] = False
            result['reason'] = f'开盘场景{open_gap_scene}'
            print(f"❌ 开盘涨幅筛选: {open_gap_scene} 不参与")
            return result
        else:
            print(f"✅ 开盘场景: {open_gap_scene} 可参与")

        # 检查12: 低吸买入验证
        target_buy_gap = gap_config.get('target_buy_gap', 0.0)
        target_buy_price = round(close * (1 + target_buy_gap / 100), 2)

        print(f"\n买入验证:")
        print(f"  目标买入价: {target_buy_price:.2f} (目标涨幅{target_buy_gap}%)")
        print(f"  最低价: {trade_low:.2f}")

        if target_buy_gap < 0:
            if trade_low <= target_buy_price:
                print(f"✅ 低吸成功: 最低价{trade_low:.2f} <= 目标价{target_buy_price:.2f}")
            else:
                result['passed'] = False
                result['reason'] = f'低吸失败: 最低价{trade_low:.2f}>目标价{target_buy_price:.2f}'
                print(f"❌ 低吸失败: 最低价{trade_low:.2f} > 目标价{target_buy_price:.2f}")
                return result

        # 如果所有检查都通过
        if result['passed']:
            result['reason'] = '符合所有筛选条件，应该被选中'
            print(f"\n✅ 符合所有筛选条件，应该被选中!")

        print(f"{'='*60}\n")
        return result


def run_v2_analysis(
    start_date: str,
    end_date: str,
    market: str = "sha",
    capital: float = 10000,
    output_dir: str = None,
):
    """
    运行V2策略分析

    Args:
        start_date: 开始日期 (YYYYMMDD)
        end_date: 结束日期 (YYYYMMDD)
        market: 市场类型 (sha/sza/all)
        capital: 每只股票初始资金
        output_dir: 输出目录（可选）
    """
    analyzer = V2Analyzer(initial_capital=capital)
    results, summary = analyzer.analyze_period(start_date, end_date, market)
    analyzer.print_report(results, summary)
    analyzer.generate_markdown_report(results, summary)

    return results, summary
