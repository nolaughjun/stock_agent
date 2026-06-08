"""
ML策略选股系统 - 主程序

功能：
1. ML策略回测 (ml)
2. 每日选股扫描 (ml-scan)
3. 龙头情绪策略回测 (leader-backtest)
4. 龙头情绪选股 (leader-scan)

使用方法：
    python main.py ml --start 20200101 --end 20241231 --threshold 0.45
    python main.py ml-scan --date 20240429 --threshold 0.45
    python main.py leader-backtest --start 20200101 --end 20241231 --market sha
    python main.py leader-scan --market sha
"""

import argparse
import os
import sys
import time
import re
import shutil
import joblib
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 统一输出目录
OUTPUT_DIR = Path(__file__).parent / "outputs"

import pandas as pd
import numpy as np

from stock_agent.data.data_loader import DataLoader, MarketType

# 导入ML风险模型
try:
    from stock_agent.strategies.risk_model import RiskModel
except ImportError:
    RiskModel = None


# 市场名称映射
MARKET_NAMES = {
    MarketType.GEM: "创业板",
    MarketType.SHA: "沪A",
    MarketType.SZA: "深A",
    MarketType.ALL: "全部A股",
}


def format_limit_up_list_for_csv(limit_up_list: list) -> list:
    """
    格式化涨停股列表用于CSV输出,将百分比数据保留2位小数

    Args:
        limit_up_list: 原始涨停股列表

    Returns:
        格式化后的涨停股列表
    """
    formatted_list = []
    for item in limit_up_list:
        formatted = item.copy()

        # 百分比和价格字段列表(保留2位小数)
        pct_fields = ['pct', 'seal', 'close', 'high', 'low', 'amount', 'float_mv']
        int_fields = ['board', 'risk_score']

        for field in pct_fields:
            if field in formatted and formatted[field] is not None:
                try:
                    formatted[field] = round(float(formatted[field]), 2)
                except (ValueError, TypeError):
                    pass

        for field in int_fields:
            if field in formatted and formatted[field] is not None:
                try:
                    formatted[field] = int(formatted[field])
                except (ValueError, TypeError):
                    pass

        # 格式化买入/卖出策略中的价格
        if 'sell_strategy' in formatted:
            sell = formatted['sell_strategy']
            if isinstance(sell, dict):
                formatted['sell_strategy'] = {
                    k: round(v, 2) if isinstance(v, (int, float)) and k in ['stop_loss_price', 'stop_loss_pct', 'profit_5pct', 'profit_10pct', 'profit_15pct', 'trailing_trigger', 'trailing_pct', 'max_hold_days']
                    else v for k, v in sell.items()
                }

        if 'buy_strategy' in formatted:
            buy = formatted['buy_strategy']
            if isinstance(buy, dict):
                formatted['buy_strategy'] = {
                    k: round(v, 2) if isinstance(v, (int, float)) and k in ['total_position', 'batch1_price', 'batch1_ratio', 'batch2_price', 'batch2_ratio', 'batch3_price', 'batch3_ratio', 'avg_buy_price', 'next_day_reference']
                    else v for k, v in buy.items()
                }

        formatted_list.append(formatted)

    return formatted_list


def get_output_path(date_str: str, market: str, report_type: str, ext: str = "md",
                    end_date: str = None, topic: str = None) -> Path:
    """
    根据日期和市场获取统一的输出路径

    Args:
        date_str: 开始日期字符串 (YYYYMMDD)
        market: 市场类型 (sha/sza/all)
        report_type: 报告类型 (如 leader_scan, analysis 等)
        ext: 文件扩展名 (默认md)
        end_date: 结束日期字符串 (YYYYMMDD),用于时间范围报告
        topic: 主题子目录名 (如 leader_scan, analysis, stock_advice 等)

    Returns:
        输出文件路径
    """
    from datetime import datetime

    # 计算日期范围
    if end_date:
        start_dt = datetime.strptime(date_str, '%Y%m%d')
        end_dt = datetime.strptime(end_date, '%Y%m%d')
        days_diff = (end_dt - start_dt).days + 1
        date_range = f"{date_str}_{end_date}"
    else:
        days_diff = 1
        date_range = date_str

    # 根据日期范围确定目录
    if days_diff <= 1:
        output_dir = OUTPUT_DIR / "daily"
    elif days_diff <= 7:
        output_dir = OUTPUT_DIR / "weekly"
    elif days_diff <= 31:
        output_dir = OUTPUT_DIR / "monthly"
    else:
        output_dir = OUTPUT_DIR / "yearly"

    # 添加主题子目录
    if topic:
        output_dir = output_dir / topic

    # 确保目录存在
    output_dir.mkdir(parents=True, exist_ok=True)

    # 生成文件名
    filename = f"{report_type}_{market}_{date_range}.{ext}"
    return output_dir / filename


def run_ml_scan(
    scan_date: str = None,
    ml_threshold: float = 0.45,
    min_gap_days: int = 5,
    max_gap_days: int = 30,
    n_workers: int = 8,
    output_dir: str = "outputs",
    market: str = "sha",
    push_wechat: bool = True,
    use_model: str = None,
    min_listing_days: int = 365,
):
    """
    ML策略每日选股扫描

    Args:
        scan_date: 扫描日期(格式：YYYYMMDD),默认为今天
        ml_threshold: ML信号质量阈值
        min_gap_days: 最小信号间隔天数
        max_gap_days: 最大信号间隔天数
        n_workers: 并行线程数
        output_dir: 输出目录
        market: 市场类型 (sha/sza/all)
        push_wechat: 是否推送到微信(默认True)
        use_model: 指定使用的模型名称(不含.joblib后缀)
        min_listing_days: 最小上市天数要求(防止新股数据不足)
    """
    # 解析市场类型(龙头策略不包含创业板)
    market_map = {
        "sha": MarketType.SHA,
        "sza": MarketType.SZA,
        "all": MarketType.ALL,
    }
    market_type = market_map.get(market.lower(), MarketType.SHA)
    market_name = MARKET_NAMES.get(market_type, "沪A")

    # 确定扫描日期
    if scan_date:
        target_date = pd.Timestamp(scan_date)
    else:
        target_date = pd.Timestamp(datetime.now().strftime("%Y-%m-%d"))

    print("=" * 80)
    print("ML策略每日选股扫描")
    print("=" * 80)
    print(f"扫描日期: {target_date.strftime('%Y-%m-%d')}")
    print(f"市场: {market_name}")
    print(f"ML阈值: {ml_threshold}")
    print(f"信号间隔: {min_gap_days}-{max_gap_days}天")
    print("=" * 80)

    # 加载ML模型
    strategies_dir = Path(__file__).parent / "strategies"
    if use_model:
        model_path = strategies_dir / f"{use_model}.joblib"
    else:
        model_path = strategies_dir / "signal_model.joblib"

    if not model_path.exists():
        print(f"错误: 找不到ML模型文件: {model_path}")
        if use_model:
            print(f"提示: 使用 'python main.py ml-list' 查看所有可用模型")
        return None

    try:
        model_data = joblib.load(model_path)
        model = model_data.get('model')
        scaler = model_data.get('scaler')
        feature_names = model_data.get('features')
        model_type = model_data.get('model_type', 'unknown')
        print(f"ML模型加载成功: {model_path.name} (类型: {model_type})")
    except Exception as e:
        print(f"错误: 加载ML模型失败 - {e}")
        return None

    # 加载数据
    loader = DataLoader()

    print(f"\n正在获取{market_name}股票列表...")
    stocks = loader.get_stock_list(market_type)
    code_col = 'code' if 'code' in stocks.columns else stocks.columns[0]
    stock_codes = [str(row[code_col]) for _, row in stocks.iterrows()]
    print(f"{market_name}股票数量: {len(stock_codes)}")

    # 计算数据加载范围
    data_start = (target_date - timedelta(days=180)).strftime("%Y%m%d")
    data_end = target_date.strftime("%Y%m%d")

    print(f"\n正在加载股票数据({data_start} - {data_end})...")
    start_time = time.time()
    all_data = {}
    invalid_stocks = []  # 记录无效股票
    warning_stocks = []  # 记录有警告的股票

    def load_stock(code):
        try:
            # 先校验股票数据是否有效
            is_valid, reason, warning = loader.is_stock_data_valid(code, data_start, min_listing_days)
            if not is_valid:
                return ('invalid', code, reason)

            df = loader.get_stock_history(code, data_start, data_end)
            if not df.empty and len(df) >= 120:
                return ('valid', code, df, warning)
        except Exception as e:
            pass
        return None

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(load_stock, code): code for code in stock_codes}
        for future in as_completed(futures):
            result = future.result()
            if result:
                status = result[0]
                code = result[1]
                if status == 'valid':
                    all_data[code] = result[2]
                    if result[3]:  # 有警告
                        warning_stocks.append((code, result[3]))
                elif status == 'invalid':
                    invalid_stocks.append((code, result[2]))

    print(f"  加载完成: {len(all_data)} 只股票, 耗时 {time.time()-start_time:.1f}秒")
    if invalid_stocks:
        print(f"  过滤无效股票: {len(invalid_stocks)} 只")
        # 显示前5个无效股票的原因
        for code, reason in invalid_stocks[:5]:
            print(f"    - {code}: {reason}")
        if len(invalid_stocks) > 5:
            print(f"    ... 还有 {len(invalid_stocks) - 5} 只")
    if warning_stocks:
        print(f"  注意: {len(warning_stocks)} 只股票数据需核实")
        for code, warning in warning_stocks[:5]:
            print(f"    - {code}: {warning}")

    # 扫描信号
    print("\n正在扫描信号...")

    selected_stocks = []

    for stock_code, df in all_data.items():
        try:
            # 过滤数据：只使用扫描日期及之前的数据
            df_filtered = df[df.index <= target_date]

            if len(df_filtered) < 120:
                continue

            latest_date = df_filtered.index[-1]

            # 如果最新数据日期与扫描日期相差超过3天,跳过
            days_diff = (target_date - latest_date).days
            if days_diff > 3:
                continue

            # 初始化策略(静默模式)
            strategy = AdvancedMLStrategy(
                ml_threshold=ml_threshold,
                min_gap_days=min_gap_days,
                max_gap_days=max_gap_days,
                silent=True,
            )

            # 获取买入信号
            signals = strategy.find_buy_signals(df_filtered)

            for signal in signals:
                # 只保留扫描日期当天或前1天的信号
                days_since_signal = (target_date - signal.date).days
                if days_since_signal < 0 or days_since_signal > 1:
                    continue

                # 获取股票信息
                loc = df_filtered.index.get_loc(signal.date)
                close = df_filtered['close'].iloc[loc]
                atr = signal.atr_value
                stop_loss = signal.stop_loss_price

                # 计算止损比例
                stop_loss_pct = (close - stop_loss) / close * 100

                selected_stocks.append({
                    'stock_code': stock_code,
                    'signal_date': signal.date.strftime('%Y-%m-%d'),
                    'strategy1_date': signal.strategy1_date.strftime('%Y-%m-%d'),
                    'strategy2_date': signal.strategy2_date.strftime('%Y-%m-%d'),
                    'gap_days': signal.gap_days,
                    'close_price': round(close, 2),
                    'stop_loss_price': round(stop_loss, 2),
                    'stop_loss_pct': round(stop_loss_pct, 2),
                    'atr_value': round(atr, 2),
                    'ml_score': round(signal.ml_score, 4),
                    'ml_level': get_ml_level(signal.ml_score),
                    'days_since_signal': days_since_signal,
                    'latest_date': latest_date.strftime('%Y-%m-%d'),
                })

        except Exception as e:
            continue

    # 排序结果
    if selected_stocks:
        df_result = pd.DataFrame(selected_stocks)
        df_result = df_result.sort_values('ml_score', ascending=False)

        # 输出结果
        print("\n" + "=" * 80)
        print("选股结果")
        print("=" * 80)

        print(f"\n共选出 {len(df_result)} 只股票:\n")

        # 检查是否有301xxx股票需要特别提示
        stocks_301 = [row['stock_code'] for _, row in df_result.iterrows() if str(row['stock_code']).startswith('301')]
        if stocks_301:
            print(f"[注意] 以下301股票请核实实际上市日期: {', '.join(stocks_301)}")
            print("       通达信本地数据可能存在代码重用问题\n")

        print(f"{'序号':<4} {'代码':<8} {'信号日期':<12} {'收盘价':<8} {'止损价':<8} {'止损%':<8} {'ML分数':<8} {'等级':<6}")
        print("-" * 70)

        for i, (idx, row) in enumerate(df_result.iterrows(), 1):
            print(f"{i:<4} {row['stock_code']:<8} {row['signal_date']:<12} "
                  f"{row['close_price']:<8} {row['stop_loss_price']:<8} "
                  f"{row['stop_loss_pct']:<8.2f}% {row['ml_score']:<8.4f} {row['ml_level']:<6}")

        # 保存结果
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        result_file = output_path / f"daily_scan_{target_date.strftime('%Y%m%d')}.csv"
        df_result.to_csv(result_file, index=False, encoding='utf-8-sig')
        print(f"\n结果已保存: {result_file}")

        # 操作建议
        print("\n" + "=" * 80)
        print("次日操作建议")
        print("=" * 80)

        for i, (idx, row) in enumerate(df_result.head(5).iterrows(), 1):
            print(f"\n{i}. {row['stock_code']}")
            print(f"   买入价: 次日开盘价")
            print(f"   止损价: {row['stop_loss_price']:.2f} (距离收盘价{row['stop_loss_pct']:.2f}%)")
            print(f"   止盈目标: +5%(卖30%), +10%(卖30%), +15%(卖40%)")
            print(f"   ML分数: {row['ml_score']:.4f} ({row['ml_level']})")

    else:
        print("\n未找到符合条件的股票")

    # 推送到微信公众号
    if push_wechat and selected_stocks:
        print("\n" + "=" * 80)
        print("发布到微信公众号")
        print("=" * 80)

        try:
            from stock_agent.notifiers.mp_publisher import MPPublisher, MPPublishConfig

            config = MPPublishConfig.from_file()
            publisher = MPPublisher(config)

            result = publisher.publish_article(
                stocks=selected_stocks,
                scan_date=target_date.strftime("%Y-%m-%d"),
                market=market_name,
            )

            if result.get('success'):
                print("发布成功!")
                if result.get('media_id'):
                    print(f"  草稿ID: {result.get('media_id')}")
                print("  请登录公众号后台手动发布")
            else:
                print(f"发布失败: {result.get('message', '未知错误')}")
                if result.get('tip'):
                    print(f"  提示: {result.get('tip')}")
        except Exception as e:
            print(f"发布异常: {e}")
            print("提示: 请先配置公众号发布,创建 mp_publish_config.json 文件")

    return selected_stocks


def run_leader_sentiment_scan(
    scan_date: str = None,
    market: str = 'sha',
    min_seal: float = 0.95,
    output_dir: str = "outputs",
    push: bool = True,
    use_ml_risk: bool = True,
):
    """
    龙头情绪选股扫描

    Args:
        scan_date: 扫描日期(格式：YYYYMMDD),默认为最新交易日
        market: 市场类型 (sha/sza/all)
        min_seal: 最小封板强度
        output_dir: 输出目录
        push: 是否推送到微信(默认True)
        use_ml_risk: 是否使用ML风险模型(默认True)
    """
    from datetime import datetime, timedelta
    import pandas as pd
    from stock_agent.data.data_loader import DataLoader, MarketType
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time

    # 加载ML风险模型
    risk_model = None
    if use_ml_risk and RiskModel is not None:
        # 优先使用龙头策略专用风险模型
        model_path = Path(__file__).parent / "strategies" / "leader_risk_model.joblib"
        risk_model = RiskModel(model_path=str(model_path))
        if not risk_model.load():
            print("[提示] 未找到已训练的风险模型,将使用规则评分")
            risk_model = RiskModel()  # 使用规则评分作为后备

    # 解析市场类型(龙头策略不包含创业板)
    market_map = {
        "sha": MarketType.SHA,
        "sza": MarketType.SZA,
        "all": MarketType.ALL,
    }
    market_type = market_map.get(market.lower(), MarketType.SHA)
    market_name = MARKET_NAMES.get(market_type, "沪A")

    loader = DataLoader()
    today = datetime.now()

    # 确定扫描日期
    if scan_date:
        target_date = pd.Timestamp(scan_date)
    else:
        target_date = pd.Timestamp(today.strftime("%Y-%m-%d"))

    print("=" * 80)
    print("龙头情绪选股")
    print("=" * 80)
    print(f"选股日期: {target_date.strftime('%Y-%m-%d')}")
    print(f"今天日期: {today.strftime('%Y-%m-%d')}")
    print(f"市场: {market_name}")
    print("=" * 80)

    # ========== 数据有效性验证 ==========
    print("\n[步骤1] 验证数据有效性...")

    # 检查几只代表性股票的最新数据日期
    test_codes = ['600519', '601318', '000001'] if market in ['sha', 'all'] else ['300750', '300059']
    latest_dates = []

    for code in test_codes:
        try:
            df = loader.get_stock_history(code,
                (target_date - timedelta(days=10)).strftime("%Y%m%d"),
                target_date.strftime("%Y%m%d"))
            if df is not None and len(df) > 0:
                if not isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index)
                latest_dates.append(df.index[-1])
        except:
            pass

    if not latest_dates:
        print("\n" + "=" * 80)
        print("【数据错误】无法获取任何股票数据")
        print("=" * 80)
        print("\n可能原因：")
        print("  1. 通达信未安装或路径配置错误")
        print("  2. 本地数据文件损坏")
        print("\n请检查通达信安装路径：E:\\new_tdx")
        return None

    # 获取数据最新日期
    data_latest_date = max(latest_dates)
    data_lag_days = (target_date - data_latest_date).days

    print(f"  数据最新日期: {data_latest_date.strftime('%Y-%m-%d')}")
    print(f"  数据滞后天数: {data_lag_days} 天")

    # 判断数据是否有效
    if data_lag_days > 3:
        print("\n" + "=" * 80)
        print("【警告】数据已过期,请先更新数据")
        print("=" * 80)
        print(f"\n当前数据最新日期: {data_latest_date.strftime('%Y-%m-%d')}")
        print(f"目标扫描日期: {target_date.strftime('%Y-%m-%d')}")
        print(f"数据滞后: {data_lag_days} 个交易日")
        print("\n请按以下步骤更新数据：")
        print("  1. 打开通达信软件")
        print("  2. 点击菜单【系统】->【盘后数据下载】")
        print("  3. 选择下载日线数据")
        print("  4. 下载完成后重新运行此命令")
        print("\n更新完成后,运行命令：")
        print(f"  python main.py leader-scan --market {market}")
        return None

    # 如果数据略有滞后,提示但继续执行
    if data_lag_days > 0:
        print(f"\n  [提示] 数据滞后{data_lag_days}天,将使用{data_latest_date.strftime('%Y-%m-%d')}的数据")
        actual_date = data_latest_date.strftime("%Y%m%d")
    else:
        actual_date = target_date.strftime("%Y%m%d")

    # 获取股票列表
    print(f"\n[步骤2] 获取{market_name}股票列表...")
    stocks = loader.get_stock_list(market_type)
    code_col = 'code' if 'code' in stocks.columns else stocks.columns[0]
    stock_codes = [str(row[code_col]).zfill(6) for _, row in stocks.iterrows()]
    print(f"  {market_name}股票数量: {len(stock_codes)}")

    # 计算数据加载范围(需要更多历史数据来准确计算连板)
    data_start = (pd.Timestamp(actual_date) - timedelta(days=30)).strftime("%Y%m%d")

    print(f"\n[步骤3] 扫描涨停股...")
    start_time = time.time()

    limit_up_list = []
    up, down, limit_up_cnt, limit_down_cnt, high_board = 0, 0, 0, 0, 0

    def process_stock(code):
        try:
            df = loader.get_stock_history(code, data_start, actual_date)
            if df is None or len(df) < 2:
                return None

            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)

            df_recent = df[df.index <= actual_date]
            if len(df_recent) < 2:
                return None

            today_data = df_recent.iloc[-1]
            yesterday = df_recent.iloc[-2]

            pct = (today_data['close'] - yesterday['close']) / yesterday['close'] * 100

            # 涨跌幅限制
            limit_pct = 20 if code.startswith('68') or code.startswith('30') else 10

            result = {
                'up': 1 if pct > 0 else 0,
                'down': 1 if pct <= 0 else 0,
                'limit_up': 1 if pct >= limit_pct - 0.5 else 0,
                'limit_down': 1 if pct <= -limit_pct + 0.5 else 0,
            }

            # 判断涨停
            if pct >= limit_pct - 0.5:
                # 计算连板(从今天开始往前数连续涨停天数)
                board = 0
                for j in range(-1, -min(15, len(df_recent)), -1):
                    t = df_recent.iloc[j]
                    # 前一天的索引
                    prev_idx = j - 1
                    # 检查前一天数据是否存在
                    if abs(prev_idx) > len(df_recent):
                        break
                    y = df_recent.iloc[prev_idx]
                    p = (t['close'] - y['close']) / y['close'] * 100
                    if p >= limit_pct - 0.5:
                        board += 1
                    else:
                        break

                if board >= 3:
                    result['high_board'] = 1
                else:
                    result['high_board'] = 0

                seal = today_data['close'] / today_data['high'] if today_data['high'] > 0 else 0

                # 计算市值和换手率(用于买入筛选)
                close = today_data['close']
                volume = today_data['volume']
                amount = today_data['amount']

                # 市值先设为None,后续通过API获取真实市值
                # 不再使用估算方法,因为误差太大
                float_mv = None  # 待后续通过API获取

                # 换手率：通达信数据中可能没有此字段,暂时设为0
                # 后续可通过其他数据源补充
                turnover_rate = 0

                result['stock'] = {
                    'code': code,
                    'close': close,
                    'pct': pct,
                    'board': board,
                    'seal': seal,
                    'amount': amount / 100000000,
                    'float_mv': float_mv,  # 待获取真实市值
                    'turnover_rate': turnover_rate,
                    'high': today_data['high'],
                    'low': today_data['low'],
                }
            else:
                result['high_board'] = 0
                result['stock'] = None

            return result

        except:
            return None

    # 并行处理
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(process_stock, code): code for code in stock_codes}
        for i, future in enumerate(as_completed(futures)):
            if (i + 1) % 300 == 0:
                print(f"  进度: {i+1}/{len(stock_codes)}")

            result = future.result()
            if result:
                up += result['up']
                down += result['down']
                limit_up_cnt += result['limit_up']
                limit_down_cnt += result['limit_down']
                high_board += result['high_board']
                if result['stock']:
                    limit_up_list.append(result['stock'])

    print(f"  完成: 耗时 {time.time()-start_time:.1f}秒")

    # ========== 获取真实流通市值和股票名称 ==========
    if limit_up_list:
        print(f"\n[步骤4] 获取真实流通市值和股票名称...")
        from stock_agent.data.realtime_quote import RealtimeQuote

        quote = RealtimeQuote()
        limit_codes = [s['code'] for s in limit_up_list]

        # 批量获取市值
        market_caps = quote.get_float_market_cap_batch(limit_codes, n_workers=8)

        # 批量获取股票名称(使用腾讯API)
        stock_names = {}
        try:
            import requests
            # 批量获取股票名称(腾讯API支持批量查询)
            market_codes = []
            for code in limit_codes:
                if code.startswith('6'):
                    market_codes.append(f'sh{code}')
                else:
                    market_codes.append(f'sz{code}')

            # 每次查询100个
            batch_size = 100
            for i in range(0, len(market_codes), batch_size):
                batch = market_codes[i:i+batch_size]
                url = f"http://qt.gtimg.cn/q={','.join(batch)}"
                try:
                    response = requests.get(url, headers={'Referer': 'http://finance.qq.com'}, timeout=10)
                    response.encoding = 'gbk'
                    data = response.text

                    # 解析每个股票
                    for line in data.split(';'):
                        if not line.strip():
                            continue
                        match = re.search(r'v_(sh|sz)(\d+)=\"(.*)\"', line)
                        if match:
                            code = match.group(2)
                            items = match.group(3).split('~')
                            if len(items) >= 2:
                                stock_names[code] = items[1]
                except Exception as e:
                    pass
        except Exception as e:
            print(f"  [提示] 获取股票名称失败: {e}")

        # 更新市值数据和股票名称
        for s in limit_up_list:
            code = s['code']
            if code in market_caps:
                s['float_mv'] = market_caps[code]
            else:
                # 如果API获取失败,使用成交额估算(假设3%换手率,更保守)
                s['float_mv'] = s['amount'] / 0.03

            # 更新股票名称
            if code in stock_names:
                s['name'] = stock_names[code]
            else:
                s['name'] = code  # 如果获取失败,使用代码

        success_count = len([s for s in limit_up_list if s['float_mv'] and s['float_mv'] > 0])
        name_count = len([s for s in limit_up_list if s.get('name') and s['name'] != s['code']])
        print(f"  获取市值成功: {success_count}/{len(limit_up_list)}只")
        print(f"  获取名称成功: {name_count}/{len(limit_up_list)}只")

    # 计算情绪分
    total = up + down
    breadth = up / total if total > 0 else 0.5
    limit_total = limit_up_cnt + limit_down_cnt
    limit_score = (limit_up_cnt - limit_down_cnt) / limit_total if limit_total > 0 else 0
    limit_score = (limit_score + 1) / 2
    board_bonus = min(high_board / 50, 0.2) if limit_up_cnt > 0 else 0
    sentiment = (breadth * 0.4 + limit_score * 0.4 + 0.2 + board_bonus) * 100
    sentiment = min(100, max(0, sentiment))

    if sentiment < 30:
        phase = "冰点"
    elif sentiment < 50:
        phase = "启动"
    elif sentiment < 70:
        phase = "发酵"
    elif sentiment < 85:
        phase = "高潮"
    else:
        phase = "退潮"

    # ========== 市场环境判断(基于回测优化逻辑)==========
    if sentiment >= 70:
        market_env = "牛市"
    elif sentiment >= 50:
        market_env = "震荡市"
    else:
        market_env = "熊市"

    # 输出市场环境信息
    print(f"\n【市场环境判断】")
    print(f"  情绪分: {sentiment:.1f} → {market_env}")
    print(f"  情绪阶段: {phase}")

    # 如果是震荡市或熊市,给出明确提示
    if market_env in ["震荡市", "熊市"]:
        print(f"\n{'='*60}")
        print(f"【重要提示】当前为{market_env},建议空仓观望！")
        print(f"{'='*60}")
        print(f"\n回测数据表明：")
        if market_env == "震荡市":
            print(f"  - 震荡市平均胜率仅 20-47%,亏损严重")
            print(f"  - 建议等待情绪分 >= 70(牛市)再参与")
        else:
            print(f"  - 熊市平均胜率仅 14-40%,亏损严重")
            print(f"  - 建议等待情绪分 >= 50(震荡市)或 >= 70(牛市)再参与")
        print(f"\n当前情绪分 {sentiment:.1f},距离牛市门槛还差 {70 - sentiment:.1f} 分")
        print(f"{'='*60}")

        # 仍然继续选股,但标记为谨慎参与
        print(f"\n仍将继续选股,请谨慎参与！\n")

    # 按连板分类
    board_groups = {}
    for s in limit_up_list:
        b = s['board']
        if b not in board_groups:
            board_groups[b] = []
        board_groups[b].append(s)

    # ========== 买入条件筛选 ==========
    # 条件：连板(2板优先)、封板强度>95%、情绪>=40、市值15-150亿、换手率3%-20%
    def check_buy_conditions(s, sentiment_score):
        """检查是否满足买入条件"""
        reasons = []

        # 连板筛选：只允许2板和3板
        # 4板以上属于高位板,风险较大,不参与
        board = s['board']
        if board == 2:
            pass  # 2板：核心参与
        elif board == 3:
            pass  # 3板：适度参与
        elif board >= 4:
            reasons.append(f"连板数{board}>=4板,属于高位板,风险较大")
            return False, reasons
        else:
            reasons.append(f"连板数{board}不符合(需2或3板)")
            return False, reasons

        # 封板强度 > 95%
        if s['seal'] < 0.95:
            reasons.append(f"封板强度{s['seal']:.1%}不足95%")

        # 市值筛选：15-200亿(使用真实市值)
        float_mv = s.get('float_mv')
        if float_mv is None or float_mv <= 0:
            reasons.append("无法获取市值数据")
        elif float_mv < 15:
            reasons.append(f"市值{float_mv:.1f}亿<15亿")
        elif float_mv > 200:
            reasons.append(f"市值{float_mv:.1f}亿>200亿")

        # 换手率：3%-20%(暂时跳过,通达信数据无此字段)
        # if s['turnover_rate'] > 0:
        #     if s['turnover_rate'] < 3:
        #         reasons.append(f"换手率{s['turnover_rate']:.1f}%<3%")
        #     elif s['turnover_rate'] > 20:
        #         reasons.append(f"换手率{s['turnover_rate']:.1f}%>20%")

        return len(reasons) == 0, reasons

    def calculate_risk_score(s, df_history, risk_model=None, sentiment_score=50):
        """
        计算风险评估分数(0-100分,分数越高风险越大)

        优先使用ML风险模型,如果没有则使用规则评分

        风险因子：
        1. 断板重启：+25分(高风险)
        2. 涨幅过大：近5日涨幅>30% +15分,>50% +25分
        3. 一字板过多：连续一字板>=2 +10分
        4. 成交额异常：成交额突然放大2倍以上 +10分
        5. 市值偏小：<30亿 +10分(容易被操纵)
        6. 连板数：3板比2板风险高 +5分
        """
        # 如果有ML模型,优先使用
        if risk_model is not None:
            try:
                trade_info = {
                    'code': s['code'],
                    'board_count': s['board'],
                    'float_cap': s['float_mv'],
                    'sentiment_score': sentiment_score,
                    'seal_strength': s.get('seal', 1.0),
                }
                result = risk_model.predict_risk(trade_info, df_history)
                return result['risk_score'], result['risk_factors'], result['risk_level']
            except Exception as e:
                pass  # 如果ML预测失败,回退到规则评分

        # 规则评分(后备方案)
        risk_score = 0
        risk_factors = []

        code = s['code']
        board = s['board']
        close = s['close']
        float_mv = s['float_mv']

        # 1. 检查断板重启
        if df_history is not None and len(df_history) >= 10:
            limit_pct = 20 if code.startswith('68') or code.startswith('30') else 10
            recent_limits = []

            for i in range(-1, -min(10, len(df_history)), -1):
                t = df_history.iloc[i]
                prev_idx = i - 1
                if abs(prev_idx) > len(df_history):
                    break
                y = df_history.iloc[prev_idx]
                p = (t['close'] - y['close']) / y['close'] * 100
                is_limit = p >= limit_pct - 0.5
                recent_limits.append(is_limit)

            # 检测断板：在最近的涨停中是否有断开
            # 例如：[True, True, False, True, True] 表示断板重启
            if len(recent_limits) >= 3:
                # 找到最近的连续涨停
                current_streak = 0
                for is_limit in recent_limits:
                    if is_limit:
                        current_streak += 1
                    else:
                        break

                # 检查是否有断板历史(在当前连板之前有涨停记录)
                if current_streak < len(recent_limits):
                    has_break = False
                    for i in range(current_streak + 1, len(recent_limits)):
                        if recent_limits[i]:
                            has_break = True
                            break
                    if has_break and current_streak >= 2:
                        risk_score += 25
                        risk_factors.append(f"断板重启(当前{current_streak}板)")

        # 2. 检查涨幅过大
        if df_history is not None and len(df_history) >= 5:
            # 找到5个交易日前的收盘价
            idx_5d = -6 if len(df_history) >= 6 else -len(df_history)
            price_5d = df_history.iloc[idx_5d]['close']
            gain_5d = (close - price_5d) / price_5d * 100

            if gain_5d > 50:
                risk_score += 25
                risk_factors.append(f"5日涨幅{gain_5d:.1f}%>50%")
            elif gain_5d > 30:
                risk_score += 15
                risk_factors.append(f"5日涨幅{gain_5d:.1f}%>30%")

        # 3. 检查一字板过多
        if df_history is not None and len(df_history) >= 2:
            yizi_count = 0
            for i in range(-1, -min(board + 1, len(df_history)), -1):
                t = df_history.iloc[i]
                if t['open'] == t['close'] == t['high'] == t['low']:
                    yizi_count += 1

            if yizi_count >= 2:
                risk_score += 10
                risk_factors.append(f"连续一字板{yizi_count}个")

        # 4. 检查成交额异常(近期最大成交额是否异常放大)
        if df_history is not None and len(df_history) >= 5:
            amounts = [df_history.iloc[i]['amount'] for i in range(-1, -min(5, len(df_history)), -1)]
            avg_amount = sum(amounts[1:]) / len(amounts[1:]) if len(amounts) > 1 else amounts[0]
            if avg_amount > 0 and amounts[0] > avg_amount * 2:
                risk_score += 10
                risk_factors.append("成交额异常放大")

        # 5. 市值偏小
        if float_mv < 30:
            risk_score += 10
            risk_factors.append(f"市值{float_mv:.1f}亿<30亿")

        # 6. 连板数风险
        if board == 3:
            risk_score += 5
            risk_factors.append("3板风险略高于2板")

        # 确定风险等级
        if risk_score < 20:
            risk_level = '低风险'
        elif risk_score < 40:
            risk_level = '中风险'
        else:
            risk_level = '高风险'

        return risk_score, risk_factors, risk_level

    def calculate_sell_strategy(s):
        """计算卖出策略"""
        close = s['close']
        high = s['high']

        # 止损价：亏损6%
        stop_loss_price = round(close * 0.94, 2)
        stop_loss_pct = 6.0

        # 分批止盈价位
        profit_5pct = round(close * 1.05, 2)    # +5% 卖30%
        profit_10pct = round(close * 1.10, 2)   # +10% 卖30%
        profit_15pct = round(close * 1.15, 2)   # +15% 卖40%

        # 移动止盈触发价
        trailing_trigger = round(close * 1.05, 2)  # 盈利5%后启动
        trailing_pct = 3.0  # 回撤3%止盈

        return {
            'stop_loss_price': stop_loss_price,
            'stop_loss_pct': stop_loss_pct,
            'profit_5pct': profit_5pct,
            'profit_10pct': profit_10pct,
            'profit_15pct': profit_15pct,
            'trailing_trigger': trailing_trigger,
            'trailing_pct': trailing_pct,
            'max_hold_days': 5,
        }

    def calculate_buy_strategy(s, sentiment_score):
        """
        计算买入策略(分批建仓)- 针对次日操作

        策略逻辑：
        - 今日收盘价 = 涨停价
        - 次日建仓价位基于今日收盘价计算
        - 分3批建仓,降低风险
        """
        close = s['close']  # 今日收盘价(涨停价)

        # 次日参考价 = 今日收盘价
        # 涨停股次日通常会有溢价开盘
        next_day_reference = close

        # 根据情绪阶段确定总仓位
        if sentiment_score >= 85:  # 退潮期
            total_position = 0.8
            phase_desc = "退潮期最佳"
        elif sentiment_score >= 70:  # 高潮期
            total_position = 0.6
            phase_desc = "高潮期谨慎"
        elif sentiment_score >= 50:  # 发酵期
            total_position = 0.6
            phase_desc = "发酵期积极"
        elif sentiment_score >= 40:  # 启动期
            total_position = 0.4
            phase_desc = "启动期轻仓"
        else:
            total_position = 0
            phase_desc = "冰点期观望"

        # 分批建仓价位(基于今日收盘价,针对次日操作)
        # 涨停股次日预期开盘价通常在 -2% 到 +5% 之间
        batch1_price = round(close * 0.98, 2)  # 低开/平开附近买入第一批
        batch2_price = round(close * 1.01, 2)  # 突破+1%买入第二批
        batch3_price = round(close * 1.03, 2)  # 突破+3%买入第三批

        # 建仓比例
        batch1_ratio = 0.4  # 第一批40%
        batch2_ratio = 0.4  # 第二批40%
        batch3_ratio = 0.2  # 第三批20%

        return {
            'total_position': total_position,
            'phase_desc': phase_desc,
            'batch1_price': batch1_price,
            'batch1_ratio': batch1_ratio,
            'batch1_condition': '竞价/开盘价附近',
            'batch2_price': batch2_price,
            'batch2_ratio': batch2_ratio,
            'batch2_condition': '突破+1%确认强势',
            'batch3_price': batch3_price,
            'batch3_ratio': batch3_ratio,
            'batch3_condition': '突破+3%加仓',
            'avg_buy_price': round((batch1_price * batch1_ratio + batch2_price * batch2_ratio + batch3_price * batch3_ratio), 2),
            'next_day_reference': next_day_reference,  # 次日参考价
        }

    # 筛选候选股(应用买入条件)
    two_boards = []
    three_boards = []
    high_boards_list = []

    for s in limit_up_list:
        if s['seal'] < min_seal:
            continue

        # 获取历史数据用于风险计算
        code = s['code']
        try:
            df_history = loader.get_stock_history(code, data_start, actual_date)
        except:
            df_history = None

        # 计算风险评分(使用ML模型或规则评分)
        risk_score, risk_factors, risk_level = calculate_risk_score(s, df_history, risk_model, sentiment)
        s['risk_score'] = risk_score
        s['risk_factors'] = risk_factors
        s['risk_level'] = risk_level

        # 计算卖出策略
        sell_strategy = calculate_sell_strategy(s)
        s['sell_strategy'] = sell_strategy

        # 计算买入策略
        buy_strategy = calculate_buy_strategy(s, sentiment)
        s['buy_strategy'] = buy_strategy

        # 检查买入条件
        is_qualified, reasons = check_buy_conditions(s, sentiment)

        if s['board'] == 2:
            s['buy_qualified'] = is_qualified
            s['reject_reasons'] = reasons if not is_qualified else []
            two_boards.append(s)
        elif s['board'] == 3:
            s['buy_qualified'] = is_qualified
            s['reject_reasons'] = reasons if not is_qualified else []
            three_boards.append(s)
        elif s['board'] >= 4:
            s['buy_qualified'] = is_qualified
            s['reject_reasons'] = reasons if not is_qualified else []
            high_boards_list.append(s)
    high_boards_list = [s for s in limit_up_list if s['board'] >= 4 and s['seal'] >= min_seal]

    # ========== 去重处理 ==========
    # 按股票代码去重,保留第一次出现的记录
    seen_codes = set()
    unique_two_boards = []
    unique_three_boards = []
    unique_high_boards = []

    for s in two_boards:
        code = s.get('code', '')
        if code not in seen_codes:
            seen_codes.add(code)
            unique_two_boards.append(s)

    for s in three_boards:
        code = s.get('code', '')
        if code not in seen_codes:
            seen_codes.add(code)
            unique_three_boards.append(s)

    for s in high_boards_list:
        code = s.get('code', '')
        if code not in seen_codes:
            seen_codes.add(code)
            unique_high_boards.append(s)

    two_boards = unique_two_boards
    three_boards = unique_three_boards
    high_boards_list = unique_high_boards

    # ========== 生成报告 ==========
    report_date = pd.Timestamp(actual_date).strftime('%Y%m%d')
    report_file = get_output_path(report_date, market, "leader_sentiment_report", topic="leader_sentiment")

    # 统计符合条件的候选股
    qualified_2b = [s for s in two_boards if s.get('buy_qualified', False)]
    qualified_3b = [s for s in three_boards if s.get('buy_qualified', False)]
    qualified_high = [s for s in high_boards_list if s.get('buy_qualified', False)]

    # ========== 推送内容(精简版：市场情绪、候选股清单、操作建议)==========
    push_lines = []
    push_lines.append(f"# 龙头情绪选股")
    push_lines.append("")
    push_lines.append(f"**选股日期**: {pd.Timestamp(actual_date).strftime('%Y-%m-%d')} | **市场**: {market_name}")
    push_lines.append("")

    # 一、市场情绪分析
    push_lines.append(f"## 一、市场情绪分析")
    push_lines.append("")
    push_lines.append(f"| 指标 | 数值 |")
    push_lines.append(f"|------|------|")
    push_lines.append(f"| 情绪分 | **{sentiment:.1f} 分** |")
    push_lines.append(f"| 情绪阶段 | **{phase}** |")
    push_lines.append(f"| 涨停家数 | {limit_up_cnt} |")
    push_lines.append(f"| 跌停家数 | {limit_down_cnt} |")
    push_lines.append(f"| 3连板以上 | {high_board} |")
    push_lines.append("")

    # 二、候选股清单
    push_lines.append(f"## 二、候选股清单")
    push_lines.append("")

    # 2板候选(按风险评分排序,低风险优先)
    push_lines.append(f"### 【2板候选】核心关注(符合条件{len(qualified_2b)}/{len(two_boards)}只)")
    push_lines.append("")
    if qualified_2b:
        # 按风险评分排序
        sorted_2b = sorted(qualified_2b, key=lambda x: x.get('risk_score', 0))
        push_lines.append(f"| 优先级 | 代码 | 名称 | 收盘价 | 市值 | 风险等级 | 风险因素 | 止损价 |")
        push_lines.append(f"|:------:|:----:|:----:|:------:|:----:|:--------:|:--------:|:------:|")
        for i, s in enumerate(sorted_2b[:10], 1):
            sell = s['sell_strategy']
            risk_level = s.get('risk_level', '低风险')
            risk_factors = s.get('risk_factors', [])
            risk_str = risk_factors[0] if risk_factors else '-'
            stop_loss = sell['stop_loss_price'] if sell else s['close'] * 0.94
            name = s.get('name', s['code'])
            # 计算优先级：低风险⭐⭐⭐,中风险⭐⭐,高风险⭐
            if risk_level == '低风险':
                priority = '⭐⭐⭐'
            elif risk_level == '中风险':
                priority = '⭐⭐'
            else:
                priority = '⭐'
            push_lines.append(f"| {priority} | **{s['code']}** | {name} | {s['close']:.2f} | {s['float_mv']:.1f}亿 | {risk_level} | {risk_str} | {stop_loss:.2f} |")
    else:
        push_lines.append("*无符合买入条件的2板股票*")
        # 显示不符合原因
        if two_boards:
            push_lines.append("")
            push_lines.append("*筛选结果(未通过原因)：*")
            for s in sorted(two_boards, key=lambda x: x['amount'], reverse=True)[:5]:
                reasons = s.get('reject_reasons', [])
                push_lines.append(f"- {s['code']}: {', '.join(reasons)}")
    push_lines.append("")

    # 3板候选
    push_lines.append(f"### 【3板候选】适度参与(符合条件{len(qualified_3b)}/{len(three_boards)}只)")
    push_lines.append("")
    if qualified_3b:
        push_lines.append(f"| 优先级 | 代码 | 名称 | 收盘价 | 市值 | 风险等级 | 风险因素 | 止损价 |")
        push_lines.append(f"|:------:|:----:|:----:|:------:|:----:|:--------:|:--------:|:------:|")
        for i, s in enumerate(sorted(qualified_3b, key=lambda x: x.get('risk_score', 0))[:5], 1):
            risk_level = s.get('risk_level', '中风险')
            risk_factors = s.get('risk_factors', [])
            risk_str = risk_factors[0] if risk_factors else '-'
            sell = s.get('sell_strategy', {})
            stop_loss = sell.get('stop_loss_price', s['close'] * 0.94)
            name = s.get('name', s['code'])
            # 3板风险较高,优先级降低一级
            if risk_level == '低风险':
                priority = '⭐⭐'
            elif risk_level == '中风险':
                priority = '⭐⭐'
            else:
                priority = '⭐'
            push_lines.append(f"| {priority} | **{s['code']}** | {name} | {s['close']:.2f} | {s['float_mv']:.1f}亿 | {risk_level} | {risk_str} | {stop_loss:.2f} |")
        push_lines.append("")
        push_lines.append("*风险提示：3板处于加速期,注意确认连板数据准确性,高位接力风险较大*")
    else:
        push_lines.append("*无符合买入条件的3板股票*")
    push_lines.append("")

    # 高位板
    push_lines.append(f"### 【高位板】谨慎参与(符合条件{len(qualified_high)}/{len(high_boards_list)}只)")
    push_lines.append("")
    if qualified_high:
        push_lines.append(f"| 优先级 | 代码 | 名称 | 连板 | 收盘价 | 市值 | 风险等级 |")
        push_lines.append(f"|:------:|:----:|:----:|:----:|:------:|:----:|:--------:|")
        for i, s in enumerate(sorted(qualified_high, key=lambda x: x.get('risk_score', 0))[:5], 1):
            risk_level = s.get('risk_level', '高风险')
            name = s.get('name', s['code'])
            # 高位板风险最大,优先级最低
            priority = '⭐'
            push_lines.append(f"| {priority} | {s['code']} | {name} | {s['board']}板 | {s['close']:.2f} | {s['float_mv']:.1f}亿 | {risk_level} |")
    else:
        push_lines.append("*无符合条件的高位板股票*")
    push_lines.append("")

    # 三、操作建议
    push_lines.append(f"## 三、操作建议")
    push_lines.append("")

    if sentiment < 40:
        push_lines.append(f"**情绪分{sentiment:.1f}分(冰点期)**：建议空仓观望")
        push_lines.append(f"- 市场情绪低迷,风险较大")
        push_lines.append(f"- 等待情绪修复后再参与")
    elif sentiment < 50:
        push_lines.append(f"**情绪分{sentiment:.1f}分(启动期)**：建议轻仓试错")
        push_lines.append(f"- 建议仓位：30%-50%")
        push_lines.append(f"- 严格止损：亏损>=6%止损")
    elif sentiment < 70:
        push_lines.append(f"**情绪分{sentiment:.1f}分(发酵期)**：可积极参与")
        push_lines.append(f"- 建议仓位：60%-80%")
    elif sentiment < 85:
        push_lines.append(f"**情绪分{sentiment:.1f}分(高潮期)**：控制仓位参与")
        push_lines.append(f"- 建议仓位：50%-70%")
        push_lines.append(f"- 注意风险控制")
    else:
        push_lines.append(f"**情绪分{sentiment:.1f}分(退潮期)**：最佳参与时机")
        push_lines.append(f"- 十年平均胜率69.0%")
        push_lines.append(f"- 建议仓位：80%-100%")

    # 建仓策略说明
    push_lines.append("")
    push_lines.append(f"**建仓策略(分批买入,针对次日操作)**：")
    push_lines.append(f"- 第一批(40%)：竞价/开盘价附近入场")
    push_lines.append(f"- 第二批(40%)：突破+1%确认强势加仓")
    push_lines.append(f"- 第三批(20%)：突破+3%继续加仓")

    # 卖出策略说明
    push_lines.append("")
    push_lines.append(f"**卖出策略**：")
    push_lines.append(f"- 止损：亏损6%止损")
    push_lines.append(f"- 分批止盈：+5%卖30%,+10%卖30%,+15%卖40%")
    push_lines.append(f"- 移动止盈：盈利5%后回撤3%止盈")
    push_lines.append(f"- 持仓期限：最多5天")

    push_content = "\n".join(push_lines)

    # ========== 完整报告(保存到文件)==========
    report_lines = []
    report_lines.append(f"# 龙头情绪选股报告")
    report_lines.append("")
    report_lines.append(f"## 报告信息")
    report_lines.append("")
    report_lines.append(f"| 项目 | 内容 |")
    report_lines.append(f"|------|------|")
    report_lines.append(f"| 选股日期 | {pd.Timestamp(actual_date).strftime('%Y-%m-%d')} |")
    report_lines.append(f"| 生成时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |")
    report_lines.append(f"| 市场 | {market_name} |")
    report_lines.append(f"| 数据来源 | 通达信本地数据 |")
    report_lines.append("")

    # 一、市场情绪分析
    report_lines.append(f"## 一、市场情绪分析")
    report_lines.append("")
    report_lines.append(f"| 指标 | 数值 |")
    report_lines.append(f"|------|------|")
    report_lines.append(f"| 情绪分 | **{sentiment:.1f} 分** |")
    report_lines.append(f"| 情绪阶段 | **{phase}** |")
    report_lines.append(f"| 上涨家数 | {up} |")
    report_lines.append(f"| 下跌家数 | {down} |")
    report_lines.append(f"| 涨停家数 | {limit_up_cnt} |")
    report_lines.append(f"| 跌停家数 | {limit_down_cnt} |")
    report_lines.append(f"| 3连板以上 | {high_board} |")
    report_lines.append("")

    # 情绪阶段说明
    report_lines.append(f"### 情绪阶段操作建议")
    report_lines.append("")
    if sentiment >= 85:
        report_lines.append(f"**退潮期(情绪分{sentiment:.1f})**：最佳参与时机,十年平均胜率69.0%")
    elif sentiment >= 70:
        report_lines.append(f"**高潮期(情绪分{sentiment:.1f})**：控制仓位参与,注意风险")
    elif sentiment >= 50:
        report_lines.append(f"**发酵期(情绪分{sentiment:.1f})**：可积极参与,建议仓位60%-80%")
    elif sentiment >= 30:
        report_lines.append(f"**启动期(情绪分{sentiment:.1f})**：谨慎参与,建议轻仓试错")
    else:
        report_lines.append(f"**冰点期(情绪分{sentiment:.1f})**：建议空仓观望")
    report_lines.append("")

    # 二、涨停股按连板分类
    report_lines.append(f"## 二、涨停股按连板分类")
    report_lines.append("")

    for b in sorted(board_groups.keys(), reverse=True):
        report_lines.append(f"### {b}板股票 ({len(board_groups[b])}只)")
        report_lines.append("")
        report_lines.append(f"| 代码 | 收盘价 | 涨幅 | 封板强度 | 成交额 | 市值 | 换手率 |")
        report_lines.append(f"|------|--------|------|----------|--------|------|--------|")
        for s in sorted(board_groups[b], key=lambda x: x['seal'], reverse=True)[:10]:
            report_lines.append(f"| {s['code']} | {s['close']:.2f} | {s['pct']:.1f}% | {s['seal']:.1%} | {s['amount']:.2f}亿 | {s.get('float_mv', 0):.1f}亿 | {s.get('turnover_rate', 0):.1f}% |")
        report_lines.append("")

    # 三、明日候选股清单(含卖出策略)
    report_lines.append(f"## 三、明日候选股清单")
    report_lines.append("")

    # 2板候选(按风险评分排序)
    report_lines.append(f"### 【2板候选】核心关注(符合条件{len(qualified_2b)}/{len(two_boards)}只)")
    report_lines.append("")
    if qualified_2b:
        # 按风险评分排序,低风险优先
        sorted_2b = sorted(qualified_2b, key=lambda x: x.get('risk_score', 0))
        report_lines.append(f"| 优先级 | 代码 | 名称 | 收盘价 | 市值 | 风险等级 | 风险因素 |")
        report_lines.append(f"|:------:|:----:|:----:|:------:|:----:|:--------:|:--------:|")
        for i, s in enumerate(sorted_2b[:10], 1):
            risk_level = s.get('risk_level', '低风险')
            risk_factors = s.get('risk_factors', [])
            risk_str = risk_factors[0] if risk_factors else '-'
            name = s.get('name', s['code'])
            # 计算优先级：低风险⭐⭐⭐,中风险⭐⭐,高风险⭐
            if risk_level == '低风险':
                priority = '⭐⭐⭐'
            elif risk_level == '中风险':
                priority = '⭐⭐'
            else:
                priority = '⭐'
            report_lines.append(f"| {priority} | **{s['code']}** | {name} | {s['close']:.2f} | {s['float_mv']:.1f}亿 | {risk_level} | {risk_str} |")
        # 风险评分详情
        report_lines.append("")
        report_lines.append("**风险评分详情：**")
        for s in sorted_2b[:10]:
            risk_score = s.get('risk_score', 0)
            risk_factors = s.get('risk_factors', [])
            if risk_factors:
                report_lines.append(f"- {s['code']}: 风险分{risk_score}分,{', '.join(risk_factors)}")
    if len(two_boards) > len(qualified_2b):
        report_lines.append("")
        report_lines.append(f"*未通过筛选的股票：*")
        for s in sorted([x for x in two_boards if not x.get('buy_qualified', False)], key=lambda x: x['amount'], reverse=True)[:5]:
            reasons = s.get('reject_reasons', [])
            report_lines.append(f"- {s['code']}: {', '.join(reasons)}")
    if not two_boards:
        report_lines.append("*无2板股票*")
    report_lines.append("")

    # 3板候选
    report_lines.append(f"### 【3板候选】适度参与(符合条件{len(qualified_3b)}/{len(three_boards)}只)")
    report_lines.append("")
    if qualified_3b:
        sorted_3b = sorted(qualified_3b, key=lambda x: x.get('risk_score', 0))
        report_lines.append(f"| 优先级 | 代码 | 名称 | 收盘价 | 市值 | 风险等级 | 风险因素 |")
        report_lines.append(f"|:------:|:----:|:----:|:------:|:----:|:--------:|:--------:|")
        for i, s in enumerate(sorted_3b[:5], 1):
            risk_level = s.get('risk_level', '中风险')
            risk_factors = s.get('risk_factors', [])
            risk_str = risk_factors[0] if risk_factors else '-'
            name = s.get('name', s['code'])
            # 3板风险较高,优先级降低一级
            if risk_level == '低风险':
                priority = '⭐⭐'
            elif risk_level == '中风险':
                priority = '⭐⭐'
            else:
                priority = '⭐'
            report_lines.append(f"| {priority} | **{s['code']}** | {name} | {s['close']:.2f} | {s['float_mv']:.1f}亿 | {risk_level} | {risk_str} |")
        report_lines.append("")
        report_lines.append("*风险提示：3板处于加速期,注意确认连板数据准确性,高位接力风险较大*")
    if not three_boards:
        report_lines.append("*无3板股票*")
    report_lines.append("")

    # 高位板
    report_lines.append(f"### 【高位板】谨慎参与(符合条件{len(qualified_high)}/{len(high_boards_list)}只)")
    report_lines.append("")
    if qualified_high:
        sorted_high = sorted(qualified_high, key=lambda x: x.get('risk_score', 0))
        report_lines.append(f"| 优先级 | 代码 | 名称 | 连板 | 收盘价 | 市值 | 风险等级 |")
        report_lines.append(f"|:------:|:----:|:----:|:----:|:------:|:----:|:--------:|")
        for i, s in enumerate(sorted_high[:5], 1):
            risk_level = s.get('risk_level', '高风险')
            name = s.get('name', s['code'])
            # 高位板风险最大,优先级最低
            priority = '⭐'
            report_lines.append(f"| {priority} | {s['code']} | {name} | {s['board']}板 | {s['close']:.2f} | {s['float_mv']:.1f}亿 | {risk_level} |")
    if not high_boards_list:
        report_lines.append("*无高位板股票*")
    report_lines.append("")

    # 四、卖出策略详情
    report_lines.append(f"## 四、卖出策略详情")
    report_lines.append("")
    report_lines.append(f"### 买入条件")
    report_lines.append(f"")
    report_lines.append(f"| 条件 | 要求 |")
    report_lines.append(f"|------|------|")
    report_lines.append(f"| 连板数 | 2板优先,其次3板 |")
    report_lines.append(f"| 封板强度 | > 95% |")
    report_lines.append(f"| 情绪分 | >= 40分 |")
    report_lines.append(f"| 流通市值 | 15-200亿 |")
    report_lines.append(f"| 换手率 | 3%-20% |")
    report_lines.append("")

    report_lines.append(f"### 卖出条件")
    report_lines.append(f"")
    report_lines.append(f"| 类型 | 条件 |")
    report_lines.append(f"|------|------|")
    report_lines.append(f"| 硬止损 | 亏损超过6%止损 |")
    report_lines.append(f"| 移动止盈 | 盈利超过5%后回撤3%止盈 |")
    report_lines.append(f"| 分批止盈 | +5%卖30%,+10%卖30%,+15%卖40% |")
    report_lines.append(f"| 到期卖出 | 持仓超过5天强制卖出 |")
    report_lines.append("")

    # 候选股建仓和卖出策略
    if qualified_2b or qualified_3b or qualified_high:
        report_lines.append(f"### 候选股交易策略(次日操作)")
        report_lines.append("")

        all_qualified = sorted(qualified_2b + qualified_3b + qualified_high, key=lambda x: x['amount'], reverse=True)[:10]

        for s in all_qualified:
            buy = s['buy_strategy']
            sell = s['sell_strategy']
            name = s.get('name', s['code'])
            report_lines.append(f"#### {s['code']} {name}({s['board']}板,今日收盘价{s['close']:.2f})")
            report_lines.append(f"")
            report_lines.append(f"**建仓策略**(总仓位{buy['total_position']*100:.0f}%,{buy['phase_desc']})")
            report_lines.append(f"")
            report_lines.append(f"| 批次 | 价位 | 比例 | 条件 |")
            report_lines.append(f"|:----:|:----:|:----:|:----:|")
            report_lines.append(f"| 第一批 | {buy['batch1_price']:.2f} | {buy['batch1_ratio']*100:.0f}% | {buy['batch1_condition']} |")
            report_lines.append(f"| 第二批 | {buy['batch2_price']:.2f} | {buy['batch2_ratio']*100:.0f}% | {buy['batch2_condition']} |")
            report_lines.append(f"| 第三批 | {buy['batch3_price']:.2f} | {buy['batch3_ratio']*100:.0f}% | {buy['batch3_condition']} |")
            report_lines.append(f"| 预估均价 | {buy['avg_buy_price']:.2f} | - | 分批建仓后均价 |")
            report_lines.append(f"")
            report_lines.append(f"**卖出策略**")
            report_lines.append(f"")
            report_lines.append(f"| 策略 | 价位 | 说明 |")
            report_lines.append(f"|:----:|:----:|:----:|")
            report_lines.append(f"| 止损价 | {sell['stop_loss_price']:.2f} | 亏损{sell['stop_loss_pct']:.0f}% |")
            report_lines.append(f"| 止盈1 | {sell['profit_5pct']:.2f} | +5%卖30% |")
            report_lines.append(f"| 止盈2 | {sell['profit_10pct']:.2f} | +10%卖30% |")
            report_lines.append(f"| 止盈3 | {sell['profit_15pct']:.2f} | +15%卖40% |")
            report_lines.append(f"| 移动止盈触发 | {sell['trailing_trigger']:.2f} | 盈利5%后启动 |")
            report_lines.append(f"| 最大持仓 | {sell['max_hold_days']}天 | 到期强制卖出 |")
            report_lines.append("")

    # 五、明日操作建议
    report_lines.append(f"## 五、明日操作建议")
    report_lines.append("")

    if sentiment < 40:
        report_lines.append(f"### [风险提示]")
        report_lines.append(f"")
        report_lines.append(f"情绪分{sentiment:.1f}分(<{40}分),**建议空仓观望**")
        report_lines.append(f"")
        report_lines.append(f"- 市场情绪低迷,风险较大")
        report_lines.append(f"- 等待情绪修复后再参与")
    elif sentiment < 50:
        report_lines.append(f"### [谨慎参与]")
        report_lines.append(f"")
        report_lines.append(f"情绪分{sentiment:.1f}分,**建议轻仓试错**")
        report_lines.append(f"")
        report_lines.append(f"- 建议仓位：30%-50%")
        report_lines.append(f"- 严格止损：亏损>=6%止损")
    else:
        report_lines.append(f"### [正常参与]")
        report_lines.append(f"")
        report_lines.append(f"情绪分{sentiment:.1f}分,**可正常参与**")
        report_lines.append(f"")
        if sentiment >= 85:
            report_lines.append(f"- **退潮期最佳**：十年平均胜率69.0%")
            report_lines.append(f"- 建议仓位：80%-100%")
        elif sentiment >= 70:
            report_lines.append(f"- 高潮期注意风险控制")
            report_lines.append(f"- 建议仓位：50%-70%")
        else:
            report_lines.append(f"- 发酵期可积极参与")
            report_lines.append(f"- 建议仓位：60%-80%")

        if qualified_2b:
            report_lines.append(f"")
            report_lines.append(f"**明日重点**：")
            top_2b = sorted(qualified_2b, key=lambda x: x['amount'], reverse=True)[0]
            sell = top_2b['sell_strategy']
            report_lines.append(f"- {top_2b['code']}(市值{top_2b['float_mv']:.1f}亿,止损价{sell['stop_loss_price']:.2f})")
        if qualified_3b:
            report_lines.append(f"")
            report_lines.append(f"**适度参与**：")
            top_3b = sorted(qualified_3b, key=lambda x: x['amount'], reverse=True)[0]
            report_lines.append(f"- {top_3b['code']} 已3板,关注能否走出4板")

    # 五、早盘操作时间表
    report_lines.append("")
    report_lines.append(f"## 五、早盘操作时间表")
    report_lines.append("")
    report_lines.append(f"| 时间 | 操作 |")
    report_lines.append(f"|------|------|")
    report_lines.append(f"| 9:15-9:25 | 观察候选股竞价情况 |")
    report_lines.append(f"| 9:25 | 重新计算情绪分,确认是否交易 |")
    report_lines.append(f"| 9:30-9:45 | 观察开盘走势,确认建仓 |")
    report_lines.append(f"| 9:45-10:30 | 执行买入 |")
    report_lines.append("")

    report_lines.append(f"---")
    report_lines.append(f"")
    report_lines.append(f"*报告生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

    report_content = "\n".join(report_lines)

    # 保存报告文件
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report_content)

    # 同时保存CSV
    if limit_up_list:
        formatted_list = format_limit_up_list_for_csv(limit_up_list)
        df_result = pd.DataFrame(formatted_list)
        csv_file = get_output_path(report_date, market, "leader_scan", "csv", topic="leader_scan")
        df_result.to_csv(csv_file, index=False, encoding='utf-8-sig')

    # 输出到控制台(使用推送内容,处理编码问题)
    print("\n" + "=" * 80)
    try:
        print(push_content)
    except UnicodeEncodeError:
        # Windows控制台编码问题,替换星号为文字
        safe_content = push_content.replace('⭐⭐⭐', '[高优先级]').replace('⭐⭐', '[中优先级]').replace('⭐', '[低优先级]')
        print(safe_content)
    print("=" * 80)
    print(f"\n完整报告已保存: {report_file}")

    # 发布到微信公众号(只有2板和3板候选股符合条件)
    if push and (qualified_2b or qualified_3b):
        print("\n" + "=" * 80)
        print("发布到微信公众号")
        print("=" * 80)
        try:
            from stock_agent.notifiers.mp_publisher import MPPublisher, MPPublishConfig

            config = MPPublishConfig.from_file()
            publisher = MPPublisher(config)

            result = publisher.publish_leader_article(
                scan_date=pd.Timestamp(actual_date).strftime('%Y-%m-%d'),
                market=market_name,
                sentiment_score=sentiment,
                sentiment_phase=phase,
                two_board_candidates=qualified_2b,
                three_board_candidates=qualified_3b,
                high_board_candidates=[],  # 高位板不符合买入条件,不推送
                limit_up_count=limit_up_cnt,
                limit_down_count=limit_down_cnt,
                up_count=up,
                down_count=down,
                high_board_count=high_board,
            )

            if result.get('success'):
                print("发布成功!")
                if result.get('media_id'):
                    print(f"  草稿ID: {result.get('media_id')}")
                print("  请登录公众号后台手动发布")
            else:
                print(f"发布失败: {result.get('message', '未知错误')}")
                if result.get('tip'):
                    print(f"  提示: {result.get('tip')}")
        except Exception as e:
            print(f"推送异常: {e}")
    elif push:
        print("\n无符合条件的候选股,跳过推送")

    return {
        'report_file': str(report_file),
        'push_content': push_content,
        'sentiment_score': sentiment,
        'sentiment_phase': phase,
        'two_board_count': len(two_boards),
        'three_board_count': len(three_boards),
        'qualified_count': len(qualified_2b) + len(qualified_3b) + len(qualified_high),
    }


def run_wechat_config(
    method: str = None,
    wework_webhook: str = None,
    wework_add_webhook: str = None,
    wework_remove_webhook: str = None,
    wework_list: bool = False,
    wxpusher_token: str = None,
    wxpusher_uid: str = None,
    wxpusher_add_uid: str = None,
    wxpusher_remove_uid: str = None,
    wxpusher_list: bool = False,
    mp_appid: str = None,
    mp_secret: str = None,
    mp_template_id: str = None,
    mp_add_openid: str = None,
    mp_remove_openid: str = None,
    mp_list: bool = False,
    test: bool = False,
):
    """
    配置微信推送

    Args:
        method: 推送方式 (mp/wework/wxpusher)
        wework_webhook: 企业微信Webhook(设置主机器人)
        wework_add_webhook: 添加企业微信机器人
        wework_remove_webhook: 删除企业微信机器人
        wework_list: 列出所有企业微信机器人
        wxpusher_token: WxPusher Token
        wxpusher_uid: WxPusher用户ID(设置主用户)
        wxpusher_add_uid: 添加WxPusher用户
        wxpusher_remove_uid: 删除WxPusher用户
        wxpusher_list: 列出所有WxPusher用户
        mp_appid: 公众号AppID
        mp_secret: 公众号AppSecret
        mp_template_id: 公众号模板消息ID
        mp_add_openid: 添加公众号用户OpenID
        mp_remove_openid: 删除公众号用户OpenID
        mp_list: 列出所有公众号用户
        test: 是否测试推送
    """
    from stock_agent.notifiers.wechat_notifier import WechatConfig, WechatNotifier

    config_path = Path(__file__).parent / "wechat_config.json"

    # 如果只是测试
    if test:
        config = WechatConfig.from_file(str(config_path))
        notifier = WechatNotifier(config)
        notifier.test_connection()
        return

    # 加载现有配置
    existing_config = WechatConfig.from_file(str(config_path))

    # 设置推送方式
    if method:
        existing_config.method = method

    # 企业微信配置
    if wework_webhook:
        existing_config.wework_webhook = wework_webhook
    if wework_add_webhook:
        if wework_add_webhook not in existing_config.wework_webhooks:
            existing_config.wework_webhooks.append(wework_add_webhook)
    if wework_remove_webhook:
        if wework_remove_webhook in existing_config.wework_webhooks:
            existing_config.wework_webhooks.remove(wework_remove_webhook)
        if existing_config.wework_webhook == wework_remove_webhook:
            existing_config.wework_webhook = ""

    # WxPusher配置
    if wxpusher_token:
        existing_config.wxpusher_token = wxpusher_token
    if wxpusher_uid:
        existing_config.wxpusher_uids = [wxpusher_uid]
    if wxpusher_add_uid:
        if wxpusher_add_uid not in existing_config.wxpusher_uids:
            existing_config.wxpusher_uids.append(wxpusher_add_uid)
    if wxpusher_remove_uid:
        if wxpusher_remove_uid in existing_config.wxpusher_uids:
            existing_config.wxpusher_uids.remove(wxpusher_remove_uid)

    # 公众号配置
    if mp_appid:
        existing_config.mp_appid = mp_appid
    if mp_secret:
        existing_config.mp_secret = mp_secret
    if mp_template_id:
        existing_config.mp_template_id = mp_template_id
    if mp_add_openid:
        if mp_add_openid not in existing_config.mp_openids:
            existing_config.mp_openids.append(mp_add_openid)
    if mp_remove_openid:
        if mp_remove_openid in existing_config.mp_openids:
            existing_config.mp_openids.remove(mp_remove_openid)

    existing_config.save(str(config_path))

    # 显示配置信息
    print("=" * 80)
    print("微信推送配置")
    print("=" * 80)
    print(f"\n配置文件: {config_path}")
    print(f"推送方式: {existing_config.method}")
    print(f"启用状态: {'已启用' if existing_config.enabled else '已禁用'}")

    if existing_config.method == "wework" or wework_list:
        all_hooks = existing_config.get_all_webhooks()
        print(f"\n企业微信机器人 ({len(all_hooks)}个):")
        if existing_config.wework_webhook:
            print(f"  [主机器人] {existing_config.wework_webhook[-20:]}")
        for i, hook in enumerate(existing_config.wework_webhooks, 1):
            print(f"  机器人{i}: {hook[-20:]}")

    if existing_config.method == "wxpusher" or wxpusher_list:
        print(f"\nWxPusher用户 ({len(existing_config.wxpusher_uids)}人):")
        for i, uid in enumerate(existing_config.wxpusher_uids, 1):
            print(f"  用户{i}: {uid}")

    if existing_config.method == "mp" or mp_list:
        print(f"\n公众号配置:")
        print(f"  AppID: {'已配置' if existing_config.mp_appid else '未配置'}")
        print(f"  Secret: {'已配置' if existing_config.mp_secret else '未配置'}")
        print(f"  模板ID: {'已配置' if existing_config.mp_template_id else '未配置'}")
        print(f"  用户数: {len(existing_config.mp_openids)}人")
        for i, openid in enumerate(existing_config.mp_openids, 1):
            print(f"    用户{i}: {openid[:8]}****")

    print("\n配置已保存!")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="ML策略选股系统")
    parser.add_argument("--workers", type=int, default=8, help="并行线程数(默认8)")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # 优化版龙头策略回测 (V2 - 次日开盘买入)
    leader_bt_parser = subparsers.add_parser("leader-backtest", help="优化版龙头策略回测(V2-次日开盘买入)")
    leader_bt_parser.add_argument("--start", default=None, help="开始日期 (YYYYMMDD),默认3年前")
    leader_bt_parser.add_argument("--end", default=None, help="结束日期 (YYYYMMDD),默认今天")
    leader_bt_parser.add_argument("--market", default="sha", choices=["sha", "sza", "all"], help="市场类型")
    leader_bt_parser.add_argument("--output", default=None, help="输出目录")

    # 龙头策略回测 V3 (四层筛选深度优化版)
    leader_bt_v3_parser = subparsers.add_parser("leader-backtest-v3", help="龙头策略回测V3(四层筛选深度优化版)")
    leader_bt_v3_parser.add_argument("--start", default=None, help="开始日期 (YYYYMMDD),默认3年前")
    leader_bt_v3_parser.add_argument("--end", default=None, help="结束日期 (YYYYMMDD),默认今天")
    leader_bt_v3_parser.add_argument("--market", default="sha", choices=["sha", "sza", "all"], help="市场类型")
    leader_bt_v3_parser.add_argument("--output", default=None, help="输出目录")
    leader_bt_v3_parser.add_argument("--no-zero-board-filter", action="store_true", help="禁用0板过滤")
    leader_bt_v3_parser.add_argument("--no-sentiment-filter", action="store_true", help="禁用情绪阶段筛选")
    leader_bt_v3_parser.add_argument("--no-open-gap-filter", action="store_true", help="禁用开盘涨幅筛选")
    leader_bt_v3_parser.add_argument("--no-market-env-filter", action="store_true", help="禁用市场环境判断")

    # 龙头风险模型训练
    risk_train_parser = subparsers.add_parser("train-risk", help="训练龙头策略风险模型(并发优化版)")
    risk_train_parser.add_argument("--start", default="20210501", help="开始日期 (YYYYMMDD),默认5年前")
    risk_train_parser.add_argument("--end", default=None, help="结束日期 (YYYYMMDD),默认今天")
    risk_train_parser.add_argument("--market", default="sha", choices=["sha", "sza", "all"], help="市场类型")
    risk_train_parser.add_argument("--output", default="outputs/leader_risk_model.joblib", help="模型保存路径")
    risk_train_parser.add_argument("--workers", type=int, default=8, help="并发工作数(默认8)")

    # 龙头情绪选股
    leader_scan_parser = subparsers.add_parser("leader-scan", help="龙头情绪选股")
    leader_scan_parser.add_argument("--date", default=None, help="选股日期 (YYYYMMDD),默认今天")
    leader_scan_parser.add_argument("--market", default="sha", choices=["sha", "sza", "all"], help="市场类型")
    leader_scan_parser.add_argument("--min-seal", type=float, default=0.95, help="最小封板强度 (默认0.95)")
    leader_scan_parser.add_argument("--output", default=None, help="输出目录")
    leader_scan_parser.add_argument("--no-push", action="store_true", help="不推送选股结果到微信")

    # 龙头情绪选股策略分析报告(选股 + 大模型分析)
    leader_report_parser = subparsers.add_parser("leader-report", help="龙头情绪选股策略分析报告(选股+大模型分析)")
    leader_report_parser.add_argument("--date", default=None, help="选股日期 (YYYYMMDD),默认今天")
    leader_report_parser.add_argument("--market", default="all", choices=["sha", "sza", "all"], help="市场类型 (默认全部A股)")
    leader_report_parser.add_argument("--min-seal", type=float, default=0.95, help="最小封板强度 (默认0.95)")
    leader_report_parser.add_argument("--output", default=None, help="输出目录")
    leader_report_parser.add_argument("--no-llm", action="store_true", help="不使用大模型生成分析")
    leader_report_parser.add_argument("--push", action="store_true", help="推送报告到微信")

    # 微信推送配置
    wechat_parser = subparsers.add_parser("wechat-config", help="配置微信推送")
    wechat_parser.add_argument("--method", choices=["mp", "wework", "wxpusher"], help="推送方式: mp(公众号)/wework/wxpusher")

    # 企业微信参数
    wechat_parser.add_argument("--webhook", help="企业微信Webhook地址(设置主机器人)")
    wechat_parser.add_argument("--add-webhook", help="添加企业微信机器人")
    wechat_parser.add_argument("--remove-webhook", help="删除企业微信机器人")
    wechat_parser.add_argument("--list-webhooks", action="store_true", help="列出所有企业微信机器人")

    # WxPusher参数
    wechat_parser.add_argument("--token", help="WxPusher Token")
    wechat_parser.add_argument("--uid", help="WxPusher用户ID")
    wechat_parser.add_argument("--add-uid", help="添加WxPusher用户")
    wechat_parser.add_argument("--remove-uid", help="删除WxPusher用户")
    wechat_parser.add_argument("--list-uids", action="store_true", help="列出所有WxPusher用户")

    # 微信公众号参数
    wechat_parser.add_argument("--mp-appid", help="公众号AppID")
    wechat_parser.add_argument("--mp-secret", help="公众号AppSecret")
    wechat_parser.add_argument("--mp-template", help="公众号模板消息ID")

    # 股票顾问命令(支持两种模式：个股模式 / 选股+大模型分析模式)
    advisor_parser = subparsers.add_parser("advisor", help="股票顾问 - 龙头情绪选股策略分析报告(选股+大模型分析)")
    advisor_parser.add_argument("codes", nargs="*", help="股票代码列表(个股模式),不指定则进入选股模式")
    advisor_parser.add_argument("--market", default="all", choices=["sha", "sza", "all"], help="市场类型(选股模式,默认全部A股)")
    advisor_parser.add_argument("--date", default=None, help="选股日期 (YYYYMMDD),默认今天")
    advisor_parser.add_argument("--no-llm", action="store_true", help="不使用大模型生成分析")
    advisor_parser.add_argument("--output", default=None, help="输出目录")
    advisor_parser.add_argument("--push", action="store_true", help="推送报告到微信")

    # 市场情绪命令
    sentiment_parser = subparsers.add_parser("sentiment", help="实时市场情绪分析")
    sentiment_parser.add_argument("--market", default="all", choices=["sha", "sza", "all"], help="市场类型")
    sentiment_parser.add_argument("--watch", action="store_true", help="持续监控模式")
    sentiment_parser.add_argument("--interval", type=int, default=60, help="监控刷新间隔(秒),默认60秒")

    # V2策略分析命令
    v2_parser = subparsers.add_parser("analysis", help="选股收益分析")
    v2_parser.add_argument("--start", required=True, help="开始日期 (YYYYMMDD)")
    v2_parser.add_argument("--end", required=True, help="结束日期 (YYYYMMDD)")
    v2_parser.add_argument("--market", default="sha", choices=["sha", "sza", "all"], help="市场类型 (默认沪A)")
    v2_parser.add_argument("--capital", type=float, default=10000, help="每只股票初始资金 (默认10000)")
    v2_parser.add_argument("--output", default=None, help="输出目录")

    # V4策略分析命令(动态目标买入价优化版)
    v4_parser = subparsers.add_parser("analysis-v4", help="选股收益分析V4(动态目标买入价优化版)")
    v4_parser.add_argument("--start", required=True, help="开始日期 (YYYYMMDD)")
    v4_parser.add_argument("--end", required=True, help="结束日期 (YYYYMMDD)")
    v4_parser.add_argument("--market", default="sha", choices=["sha", "sza", "all"], help="市场类型 (默认沪A)")
    v4_parser.add_argument("--capital", type=float, default=10000, help="每只股票初始资金 (默认10000)")
    v4_parser.add_argument("--output", default=None, help="输出目录")

    # V5策略分析命令(动态止损+盈亏比+多因子+组合优化版)
    v5_parser = subparsers.add_parser("analysis-v5", help="选股收益分析V5(动态止损+盈亏比+多因子+组合优化版)")
    v5_parser.add_argument("--start", required=True, help="开始日期 (YYYYMMDD)")
    v5_parser.add_argument("--end", required=True, help="结束日期 (YYYYMMDD)")
    v5_parser.add_argument("--market", default="sha", choices=["sha", "sza", "all"], help="市场类型 (默认沪A)")
    v5_parser.add_argument("--capital", type=float, default=10000, help="每只股票初始资金 (默认10000)")
    v5_parser.add_argument("--output", default=None, help="输出目录")

    # 开盘涨幅场景分析命令
    open_gap_parser = subparsers.add_parser("open-gap", help="开盘涨幅场景分析(基于V2策略)")
    open_gap_parser.add_argument("--start", default="20260101", help="开始日期 (YYYYMMDD),默认20260101")
    open_gap_parser.add_argument("--end", default=None, help="结束日期 (YYYYMMDD),默认今天")
    open_gap_parser.add_argument("--market", default="sha", choices=["sha", "sza", "all"], help="市场类型 (默认沪A)")
    open_gap_parser.add_argument("--output", default=None, help="输出目录")

    # 因子分析命令
    factor_parser = subparsers.add_parser("factor-analysis", help="因子有效性分析")
    factor_parser.add_argument("--start", required=True, help="开始日期 (YYYYMMDD)")
    factor_parser.add_argument("--end", required=True, help="结束日期 (YYYYMMDD)")
    factor_parser.add_argument("--market", default="all", choices=["sha", "sza", "all"], help="市场类型 (默认全部A股)")
    factor_parser.add_argument("--output", default=None, help="输出目录")

    # 因子模型训练命令
    train_parser = subparsers.add_parser("train-factor-model", help="训练因子模型")
    train_parser.add_argument("--start", required=True, help="开始日期 (YYYYMMDD)")
    train_parser.add_argument("--end", required=True, help="结束日期 (YYYYMMDD)")
    train_parser.add_argument("--market", default="all", choices=["sha", "sza", "all"], help="市场类型 (默认全部A股)")
    train_parser.add_argument("--model", default="logistic", choices=["logistic", "random_forest", "gradient_boosting"], help="模型类型")
    train_parser.add_argument("--output", default=None, help="模型保存路径")
    train_parser.add_argument("--batch", action="store_true", help="使用批量训练模式(适用于大数据量)")
    train_parser.add_argument("--batch-months", type=int, default=6, help="批量训练每批次的月数(默认6个月)")
    train_parser.add_argument("--val-start", default=None, help="验证集开始日期 (YYYYMMDD), 不指定则不验证")
    train_parser.add_argument("--val-end", default=None, help="验证集结束日期 (YYYYMMDD)")

    # 高级模型训练命令
    adv_train_parser = subparsers.add_parser("train-advanced-model", help="训练高级优化模型(目标准确率90%%, 召回率88%%)")
    adv_train_parser.add_argument("--start", required=True, help="开始日期 (YYYYMMDD)")
    adv_train_parser.add_argument("--end", required=True, help="结束日期 (YYYYMMDD)")
    adv_train_parser.add_argument("--market", default="all", choices=["sha", "sza", "all"], help="市场类型 (默认全部A股)")
    adv_train_parser.add_argument("--model", default="ensemble", choices=["ensemble", "gradient_boosting", "random_forest"], help="模型类型")
    adv_train_parser.add_argument("--target-accuracy", type=float, default=0.90, help="目标准确率 (默认0.90)")
    adv_train_parser.add_argument("--target-recall", type=float, default=0.88, help="目标召回率 (默认0.88)")
    adv_train_parser.add_argument("--output", default=None, help="模型保存路径")

    wechat_parser.add_argument("--mp-add-openid", help="添加公众号用户OpenID")
    wechat_parser.add_argument("--mp-remove-openid", help="删除公众号用户OpenID")
    wechat_parser.add_argument("--mp-list", action="store_true", help="列出所有公众号用户")

    wechat_parser.add_argument("--test", action="store_true", help="测试推送")

    args = parser.parse_args()

    if args.command == "wechat-config":
        run_wechat_config(
            method=args.method,
            wework_webhook=args.webhook,
            wework_add_webhook=args.add_webhook,
            wework_remove_webhook=args.remove_webhook,
            wework_list=args.list_webhooks,
            wxpusher_token=args.token,
            wxpusher_uid=args.uid,
            wxpusher_add_uid=args.add_uid,
            wxpusher_remove_uid=args.remove_uid,
            wxpusher_list=args.list_uids,
            mp_appid=args.mp_appid,
            mp_secret=args.mp_secret,
            mp_template_id=args.mp_template,
            mp_add_openid=args.mp_add_openid,
            mp_remove_openid=args.mp_remove_openid,
            mp_list=args.mp_list,
            test=args.test,
        )

    elif args.command == "leader-backtest":
        from stock_agent.strategies.leader_backtest import run_optimized_backtest
        run_optimized_backtest(
            start_date=args.start,
            end_date=args.end,
            market=args.market,
            output_dir=args.output or str(OUTPUT_DIR),
        )

    elif args.command == "leader-backtest-v3":
        from stock_agent.strategies.leader_backtest_v3 import run_backtest_v3
        run_backtest_v3(
            start_date=args.start,
            end_date=args.end,
            market=args.market,
            output_dir=args.output,
            enable_zero_board_filter=not args.no_zero_board_filter,
            enable_sentiment_filter=not args.no_sentiment_filter,
            enable_open_gap_filter=not args.no_open_gap_filter,
            enable_market_env_filter=not args.no_market_env_filter,
        )

    elif args.command == "train-risk":
        from stock_agent.strategies.train_risk_model import train_risk_model_parallel, run_parallel_backtest
        from datetime import datetime, timedelta

        end_date = args.end or datetime.now().strftime("%Y%m%d")
        start_date = args.start
        n_workers = args.workers

        print("=" * 60)
        print("龙头策略风险模型训练 - 并发优化版")
        print("=" * 60)
        print(f"回测区间: {start_date} - {end_date}")
        print(f"市场: {args.market}")
        print(f"并发数: {n_workers}")

        # 1. 并发回测收集交易数据
        all_trades = run_parallel_backtest(
            start_date=start_date,
            end_date=end_date,
            markets=[args.market],
            n_workers=n_workers
        )

        if not all_trades:
            print("没有交易记录,无法训练模型")
            return

        # 2. 并发训练风险模型
        train_result = train_risk_model_parallel(
            trades=all_trades,
            output_path=args.output,
            n_workers=n_workers,
            use_process=False
        )

        print("\n" + "=" * 60)
        print("训练完成")
        print(f"模型已保存到: {args.output}")

    elif args.command == "leader-scan":
        run_leader_sentiment_scan(
            scan_date=args.date,
            market=args.market,
            min_seal=args.min_seal,
            output_dir=args.output or str(OUTPUT_DIR),
            push=not args.no_push,
        )

    elif args.command == "sentiment":
        from stock_agent.data.realtime_quote import MarketSentimentCalculator, monitor_sentiment

        if args.watch:
            # 监控模式
            monitor_sentiment(interval=args.interval, market=args.market)
        else:
            # 单次计算
            calc = MarketSentimentCalculator()
            sentiment = calc.calculate_sentiment(args.market)

            if sentiment:
                print(calc.format_sentiment(sentiment))

    elif args.command == "advisor":
        from stock_agent.advisor.stock_advisor import run_stock_advisor
        run_stock_advisor(
            codes=args.codes if args.codes else None,
            market=args.market,
            scan_date=args.date,
            use_llm=not args.no_llm,
            output_dir=args.output or str(OUTPUT_DIR),
            push=args.push,
        )

    elif args.command == "analysis":
        from stock_agent.strategies.v2_analysis import run_v2_analysis
        run_v2_analysis(
            start_date=args.start,
            end_date=args.end,
            market=args.market,
            capital=args.capital,
            output_dir=args.output,
        )

    elif args.command == "analysis-v4":
        from stock_agent.strategies.v4_analysis import run_v4_analysis
        run_v4_analysis(
            start_date=args.start,
            end_date=args.end,
            market=args.market,
            capital=args.capital,
            output_dir=args.output,
        )

    elif args.command == "analysis-v5":
        from stock_agent.strategies.v5_analysis import run_v5_analysis
        run_v5_analysis(
            start_date=args.start,
            end_date=args.end,
            market=args.market,
            capital=args.capital,
            output_dir=args.output,
        )

    elif args.command == "open-gap":
        from stock_agent.strategies.open_gap_analysis import run_open_gap_analysis
        run_open_gap_analysis(
            start_date=args.start,
            end_date=args.end,
            market=args.market,
            output_dir=args.output,
        )

    elif args.command == "factor-analysis":
        from stock_agent.strategies.v2_analysis import V2Analyzer
        from stock_agent.strategies.factor_analyzer import run_factor_analysis

        print("=" * 60)
        print("因子有效性分析")
        print("=" * 60)
        print(f"分析区间: {args.start} - {args.end}")
        print(f"市场: {args.market}")

        # 运行策略获取交易数据
        analyzer = V2Analyzer(initial_capital=10000)
        results, summary = analyzer.analyze_period(args.start, args.end, args.market)

        # 转换结果格式
        results_dict = []
        for r in results:
            results_dict.append({
                'code': r.code,
                'volume_change_rate': r.volume_change_rate,
                'turnover_rate': r.turnover_rate,
                'volatility': r.volatility,
                'factor_score': r.factor_score,
                'profit_pct': r.profit_pct,
                'is_win': r.is_win,
                'hold_days': r.hold_days,
            })

        # 运行因子分析
        output_dir = args.output or str(OUTPUT_DIR / "factor_analysis")
        run_factor_analysis(results_dict, output_dir)

    elif args.command == "train-factor-model":
        from stock_agent.strategies.v2_analysis import V2Analyzer
        from stock_agent.strategies.factor_model_trainer import train_factor_model, FactorModelTrainer

        print("=" * 60)
        print("因子模型训练")
        print("=" * 60)
        print(f"训练区间: {args.start} - {args.end}")
        print(f"市场: {args.market}")
        print(f"模型类型: {args.model}")
        if args.batch:
            print(f"训练模式: 批量训练 (每批次 {args.batch_months} 个月)")
        if args.val_start:
            print(f"验证区间: {args.val_start} - {args.val_end or '今天'}")

        # 运行策略获取训练数据
        analyzer = V2Analyzer(initial_capital=10000)

        if args.batch:
            # 批量训练模式
            results, summary = analyzer.analyze_period_batch(
                args.start, args.end, args.market,
                batch_months=args.batch_months
            )
        else:
            # 普通训练模式
            results, summary = analyzer.analyze_period(args.start, args.end, args.market)

        # 转换结果格式
        results_dict = []
        for r in results:
            results_dict.append({
                'code': r.code,
                'volume_change_rate': r.volume_change_rate,
                'turnover_rate': r.turnover_rate,
                'volatility': r.volatility,
                'factor_score': r.factor_score,
                'seal_strength': getattr(r, 'seal_strength', 0),
                'float_cap': getattr(r, 'float_cap', 0),
                'board_count': 2,  # 默认值
                'profit_pct': r.profit_pct,
                'is_win': r.is_win,
                'hold_days': r.hold_days,
            })

        # 训练因子模型
        model_path = args.output or str(OUTPUT_DIR / "factor_model.joblib")
        report_path = str(OUTPUT_DIR / "factor_model_report.md")

        trainer, result = train_factor_model(
            results_dict,
            model_type=args.model,
            output_path=model_path,
            report_path=report_path
        )

        if result:
            print(f"\n模型性能: AUC={result.auc:.4f}, 准确率={result.accuracy:.4f}")

            # 验证集评估
            if args.val_start:
                print("\n" + "=" * 60)
                print("验证集评估")
                print("=" * 60)

                # 清理训练数据，重新加载验证数据
                analyzer_val = V2Analyzer(initial_capital=10000)
                val_end = args.val_end or datetime.now().strftime('%Y%m%d')

                val_results, val_summary = analyzer_val.analyze_period(
                    args.val_start, val_end, args.market
                )

                # 转换验证集结果
                val_results_dict = []
                for r in val_results:
                    val_results_dict.append({
                        'code': r.code,
                        'volume_change_rate': r.volume_change_rate,
                        'turnover_rate': r.turnover_rate,
                        'volatility': r.volatility,
                        'factor_score': r.factor_score,
                        'seal_strength': getattr(r, 'seal_strength', 0),
                        'float_cap': getattr(r, 'float_cap', 0),
                        'board_count': 2,
                        'profit_pct': r.profit_pct,
                        'is_win': r.is_win,
                        'hold_days': r.hold_days,
                    })

                # 评估模型
                val_metrics = trainer.evaluate(val_results_dict)

                if val_metrics:
                    print(f"\n验证集样本数: {val_metrics['sample_count']}")
                    print(f"验证集胜率: {val_metrics['win_rate']:.2%}")
                    print(f"验证集准确率: {val_metrics['accuracy']:.4f}")
                    print(f"验证集AUC: {val_metrics['auc']:.4f}")

                    # 生成验证报告
                    val_report = trainer.generate_validation_report(
                        result, val_metrics,
                        f"{args.start} - {args.end}",
                        f"{args.val_start} - {val_end}"
                    )

                    val_report_path = str(OUTPUT_DIR / "factor_model_validation_report.md")
                    with open(val_report_path, 'w', encoding='utf-8') as f:
                        f.write(val_report)
                    print(f"\n验证报告已保存: {val_report_path}")

            # 同时训练选股模型
            print("\n正在训练选股模型...")
            from stock_agent.strategies.selection_model import StockSelectionModel

            selection_model = StockSelectionModel()
            selection_model.model = trainer.model
            selection_model.scaler = trainer.scaler
            selection_model.feature_names = trainer.feature_names

            selection_model_path = str(OUTPUT_DIR / "selection_model.joblib")
            selection_model.save(selection_model_path)

            print(f"\n选股模型已保存: {selection_model_path}")
            print("\n使用方式:")
            print("  1. 模型会自动加载到选股流程中")
            print("  2. 重新运行 analysis 命令即可使用新模型评分")

    elif args.command == "train-advanced-model":
        from stock_agent.strategies.v2_analysis import V2Analyzer
        from stock_agent.strategies.advanced_model_trainer import train_advanced_model

        print("=" * 60)
        print("高级模型优化训练")
        print("=" * 60)
        print(f"训练区间: {args.start} - {args.end}")
        print(f"市场: {args.market}")
        print(f"模型类型: {args.model}")
        print(f"目标准确率: {args.target_accuracy:.0%}")
        print(f"目标召回率: {args.target_recall:.0%}")

        # 运行策略获取训练数据
        analyzer = V2Analyzer(initial_capital=10000)
        results, summary = analyzer.analyze_period(args.start, args.end, args.market)

        # 转换结果格式
        results_dict = []
        for r in results:
            results_dict.append({
                'code': r.code,
                'volume_change_rate': r.volume_change_rate,
                'turnover_rate': r.turnover_rate,
                'volatility': r.volatility,
                'factor_score': r.factor_score,
                'seal_strength': getattr(r, 'seal_strength', 0),
                'float_cap': getattr(r, 'float_cap', 0),
                'board_count': 2,
                'profit_pct': r.profit_pct,
                'is_win': r.is_win,
                'hold_days': r.hold_days,
            })

        # 训练高级模型
        model_path = args.output or str(OUTPUT_DIR / "advanced_model.joblib")

        trainer, result = train_advanced_model(
            results_dict,
            model_type=args.model,
            target_accuracy=args.target_accuracy,
            target_recall=args.target_recall,
            output_path=model_path
        )

        if result:
            print(f"\n最终性能: 准确率={result.accuracy:.2%}, 召回率={result.recall:.2%}")

            if result.accuracy >= args.target_accuracy and result.recall >= args.target_recall:
                print("\n🎉 已达到目标！")
            else:
                print("\n建议：")
                if result.accuracy < args.target_accuracy:
                    print("  - 增加训练数据(当前样本可能不足)")
                    print("  - 尝试不同的模型类型")
                if result.recall < args.target_recall:
                    print("  - 增加更多正样本(盈利交易)")
                    print("  - 使用更强的过采样方法")

    else:
        parser.print_help()
        print("\n命令示例:")
        print("  python main.py ml --start 20200101 --end 20241231 --threshold 0.45 --market sha")
        print("  python main.py ml --start 20200101 --end 20241231 --market all")
        print("  python main.py ml-scan                              # 扫描并推送(默认)")
        print("  python main.py ml-scan --market all                 # 扫描全部A股并推送")
        print("  python main.py ml-scan --no-push                    # 扫描但不推送")
        print("  python main.py ml-scan --use-model my_xgb           # 使用指定模型扫描")
        print("  python main.py ml-train --start 20200101 --market sha")
        print("  python main.py ml-train --model xgb --save-as my_xgb  # 训练并命名保存")
        print("  python main.py ml-tune --model gb                   # 超参数调优")
        print("  python main.py ml-tune --model gb --quick           # 快速调优")
        print("  python main.py ml-tune --model gb --save            # 调优并保存最优模型")
        print("  python main.py ml-list                              # 列出所有模型")
        print("  python main.py ml-use my_xgb                        # 切换默认模型")
        print("  python main.py ml-compare --models gb,rf,xgb        # 对比模型")
        print("  python main.py ml-eval")
        print("  python main.py ml-info")
        print("  # 龙头策略")
        print("  python main.py leader-backtest --start 20200101 --end 20241231 --market sha")
        print("  python main.py leader-backtest-v3 --start 20250101 --end 20251231 --market sha  # V3四层筛选版")
        print("  python main.py leader-scan --market sha                    # 龙头情绪选股")
        print("  python main.py leader-scan --date 20240515 --market all    # 指定日期和市场")
        print("  python main.py leader-scan --no-push                       # 不推送")
        print("  # 实时行情和分析")
        print("  python main.py analyze 603206                             # 分析单只股票")
        print("  python main.py analyze 603206 --watch                     # 持续监控")
        print("  python main.py quote 603206 603989                        # 查询实时行情")
        print("  python main.py limit-up                                   # 扫描涨停股")
        print("  python main.py sentiment                                  # 市场情绪分析")
        print("  # 股票顾问(龙头情绪选股策略分析报告)")
        print("  python main.py advisor                                # 选股模式：选股+大模型分析报告")
        print("  python main.py advisor --market sha --date 20260528  # 指定市场和日期")
        print("  python main.py advisor --no-llm                       # 不使用大模型")
        print("  python main.py advisor 605288 603938 603316           # 个股模式：分析指定股票")
        print("  python main.py advisor --push                         # 选股模式并推送到微信")
        print("  # 选股收益分析")
        print("  python main.py analysis --start 20260501 --end 20260531 --market all  # V3策略分析")
        print("  python main.py analysis-v4 --start 20260501 --end 20260531 --market all  # V4动态目标优化版")
        print("  python main.py analysis-v5 --start 20260501 --end 20260531 --market all  # V5动态止损+盈亏比优化版")
        print("  # 开盘涨幅场景分析")
        print("  python main.py open-gap --start 20260101 --end 20261231 --market sha")
        print("  # 因子分析")
        print("  python main.py factor-analysis --start 20250101 --end 20261231 --market all")
        print("  # 因子模型训练")
        print("  python main.py train-factor-model --start 20250101 --end 20261231 --model logistic")
        print("  python main.py train-factor-model --start 20250101 --end 20261231 --model random_forest")
        print("  # 批量训练模式(适用于大数据量,如6年数据)")
        print("  python main.py train-factor-model --start 20200101 --end 20260605 --model logistic --batch")
        print("  python main.py train-factor-model --start 20200101 --end 20260605 --model logistic --batch --batch-months 3")
        print("  # 带验证集训练(推荐)")
        print("  python main.py train-factor-model --start 20200101 --end 20241231 --val-start 20250101 --val-end 20260605 --model random_forest --batch")
        print("  # 高级模型训练(目标准确率90%%,召回率88%%)")
        print("  python main.py train-advanced-model --start 20240101 --end 20261231 --model ensemble")
        print("  python main.py train-advanced-model --start 20240101 --end 20261231 --target-accuracy 0.90 --target-recall 0.88")
        print("  # 微信公众号配置")
        print("  python main.py wechat-config --method mp --mp-appid YOUR_APPID --mp-secret YOUR_SECRET")
        print("  python main.py wechat-config --test                 # 测试推送")


if __name__ == "__main__":
    main()
