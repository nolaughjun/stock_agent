"""
训练龙头策略风险模型 - 并发优化版

流程：
1. 并发运行回测收集历史交易数据
2. 并发提取风险特征（并发加载股票历史数据）
3. 并发训练多个模型并选择最优
4. 保存模型供回测和选股使用
"""
import sys
from pathlib import Path
# 将src目录加入路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
import joblib
import pickle
import time
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from dataclasses import dataclass
import warnings
warnings.filterwarnings('ignore')

try:
    from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
    from sklearn.model_selection import train_test_split, cross_val_score, GridSearchCV
    from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score, roc_curve
    from sklearn.preprocessing import StandardScaler
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    print("请安装sklearn: pip install scikit-learn")


@dataclass
class FeatureResult:
    """特征提取结果"""
    features: Dict
    risk_factors: List[str]
    label: int
    code: str


def extract_risk_features(trade: Dict, df_history: pd.DataFrame = None) -> Tuple[Dict, List[str]]:
    """
    提取龙头策略风险特征

    返回特征字典和风险因子列表
    """
    features = {}
    risk_factors = []

    code = trade.get('code', '')
    board_count = trade.get('board_count', 2)
    float_cap = trade.get('float_cap', 50)
    sentiment_score = trade.get('sentiment_score', 50)
    seal_strength = trade.get('seal_strength', 1.0)

    limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10

    # ========== 连板相关特征 ==========
    features['board_count'] = board_count
    features['is_board_2'] = 1 if board_count == 2 else 0
    features['is_board_3'] = 1 if board_count == 3 else 0

    # ========== 价格特征 ==========
    gain_5d = 0
    gain_10d = 0
    gain_from_low = 0

    if df_history is not None and len(df_history) >= 6:
        close = df_history['close'].iloc[-1]

        # 5日涨幅
        if len(df_history) >= 6:
            price_5d = df_history['close'].iloc[-6]
            gain_5d = (close - price_5d) / price_5d * 100
            if gain_5d > 50:
                risk_factors.append(f'5日涨幅{gain_5d:.0f}%')
            elif gain_5d > 30:
                risk_factors.append(f'5日涨幅{gain_5d:.0f}%')

        # 10日涨幅
        if len(df_history) >= 11:
            price_10d = df_history['close'].iloc[-11]
            gain_10d = (close - price_10d) / price_10d * 100

        # 从低点涨幅
        if len(df_history) >= 20:
            low_20d = df_history['low'].iloc[-20:].min()
            gain_from_low = (close - low_20d) / low_20d * 100

    features['gain_5d'] = gain_5d
    features['gain_10d'] = gain_10d
    features['gain_from_low'] = gain_from_low

    # ========== 断板重启特征 ==========
    has_break = 0
    break_days = 0

    if df_history is not None and len(df_history) >= 10:
        recent_limits = []
        for i in range(-1, -min(15, len(df_history)), -1):
            prev_idx = i - 1
            if abs(prev_idx) > len(df_history):
                break
            t = df_history.iloc[i]
            y = df_history.iloc[prev_idx]
            p = (t['close'] - y['close']) / y['close'] * 100
            is_limit = p >= limit_pct - 0.5
            recent_limits.append(is_limit)

        # 检测断板重启
        if len(recent_limits) >= 3:
            current_streak = 0
            for is_limit in recent_limits:
                if is_limit:
                    current_streak += 1
                else:
                    break

            if current_streak < len(recent_limits):
                for i in range(current_streak + 1, len(recent_limits)):
                    if recent_limits[i]:
                        has_break = 1
                        break_days = i - current_streak
                        risk_factors.append('断板重启')
                        break

    features['has_break'] = has_break
    features['break_days'] = break_days

    # ========== 成交量特征 ==========
    amount_ratio = 1
    amount_ma5_ratio = 1

    if df_history is not None and len(df_history) >= 6:
        amount = df_history['amount']

        # 今日/昨日成交额比
        today_amount = amount.iloc[-1]
        yesterday_amount = amount.iloc[-2]
        if yesterday_amount > 0:
            amount_ratio = today_amount / yesterday_amount

        # 今日/5日均量比
        ma5_amount = amount.iloc[-6:-1].mean()
        if ma5_amount > 0:
            amount_ma5_ratio = today_amount / ma5_amount

        if amount_ma5_ratio > 2:
            risk_factors.append('成交额放大')

    features['amount_ratio'] = amount_ratio
    features['amount_ma5_ratio'] = amount_ma5_ratio

    # ========== 一字板特征 ==========
    yizi_count = 0

    if df_history is not None and len(df_history) >= board_count:
        for i in range(-1, -min(board_count + 1, len(df_history)), -1):
            t = df_history.iloc[i]
            if t['open'] == t['close'] == t['high'] == t['low']:
                yizi_count += 1

        if yizi_count >= 2:
            risk_factors.append(f'一字板{yizi_count}个')

    features['yizi_count'] = yizi_count

    # ========== 市值特征 ==========
    features['float_cap'] = float_cap
    features['is_small_cap'] = 1 if float_cap < 30 else 0

    if float_cap < 30:
        risk_factors.append(f'小市值{float_cap:.0f}亿')

    # ========== 市场情绪特征 ==========
    features['sentiment_score'] = sentiment_score

    # 情绪阶段编码
    if sentiment_score < 30:
        sentiment_phase = 0  # 冰点
    elif sentiment_score < 50:
        sentiment_phase = 1  # 启动
    elif sentiment_score < 70:
        sentiment_phase = 2  # 发酵
    elif sentiment_score < 85:
        sentiment_phase = 3  # 高潮
    else:
        sentiment_phase = 4  # 退潮
    features['sentiment_phase'] = sentiment_phase

    # ========== 封板强度特征 ==========
    features['seal_strength'] = seal_strength

    # ========== 波动率特征 ==========
    volatility = 0
    if df_history is not None and len(df_history) >= 10:
        close = df_history['close']
        returns = close.pct_change().dropna()
        if len(returns) >= 5:
            volatility = returns.tail(10).std() * 100

    features['volatility'] = volatility

    return features, risk_factors


def process_single_trade(args: Tuple) -> Optional[FeatureResult]:
    """
    处理单条交易记录（用于并发）

    Args:
        args: (trade, loader_instance_or_none, use_loader)

    Returns:
        FeatureResult 或 None
    """
    trade, use_loader = args

    try:
        code = trade.get('code', '')
        trade_date = trade.get('trade_date', trade.get('buy_date', ''))

        # 获取历史数据
        df_history = None

        if use_loader and trade_date:
            # 在子进程中创建新的DataLoader
            from app.stock_agent.data.data_loader import DataLoader
            loader = DataLoader()

            start_date = (pd.Timestamp(trade_date) - timedelta(days=30)).strftime('%Y%m%d')
            end_date = pd.Timestamp(trade_date).strftime('%Y%m%d')
            df_history = loader.get_stock_history(code, start_date, end_date)
        else:
            df_history = trade.get('df_history', None)

        # 提取特征
        features, risk_factors = extract_risk_features(trade, df_history)

        # 标签：亏损=1, 盈利=0
        profit = trade.get('profit', trade.get('profit_pct', 0))
        label = 1 if profit < 0 else 0

        return FeatureResult(
            features=features,
            risk_factors=risk_factors,
            label=label,
            code=code
        )
    except Exception as e:
        return None


def train_risk_model_parallel(
    trades: List[Dict],
    output_path: str = "outputs/leader_risk_model.joblib",
    n_workers: int = 8,
    use_process: bool = False
) -> Dict:
    """
    并发训练风险模型

    Args:
        trades: 交易记录列表
        output_path: 模型保存路径
        n_workers: 并发工作数
        use_process: 是否使用进程池（更大数据量时推荐）

    Returns:
        训练结果统计
    """
    if not SKLEARN_AVAILABLE:
        return {'error': 'sklearn not available'}

    total_start = time.time()
    print(f"\n{'='*60}")
    print(f"并发训练风险模型")
    print(f"{'='*60}")
    print(f"样本数: {len(trades)}")
    print(f"并发数: {n_workers}")
    print(f"模式: {'进程池' if use_process else '线程池'}")

    # ========== 阶段1：并发提取特征 ==========
    print(f"\n[阶段1] 并发提取特征...")
    stage1_start = time.time()

    # 准备参数
    args_list = [(trade, True) for trade in trades]

    # 选择并发方式
    ExecutorClass = ProcessPoolExecutor if use_process else ThreadPoolExecutor

    results = []
    feature_names = None

    with ExecutorClass(max_workers=n_workers) as executor:
        futures = {executor.submit(process_single_trade, args): i for i, args in enumerate(args_list)}

        completed = 0
        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                results.append(result)
                if feature_names is None:
                    feature_names = list(result.features.keys())

            completed += 1
            if completed % 500 == 0:
                print(f"  进度: {completed}/{len(trades)} ({completed/len(trades)*100:.1f}%)")

    stage1_time = time.time() - stage1_start
    print(f"  特征提取完成: {len(results)} 条, 耗时 {stage1_time:.1f}秒")

    if not results:
        return {'error': 'no valid features extracted'}

    # ========== 阶段2：构建特征矩阵 ==========
    print(f"\n[阶段2] 构建特征矩阵...")
    stage2_start = time.time()

    X = []
    y = []

    for r in results:
        X.append([r.features.get(k, 0) for k in feature_names])
        y.append(r.label)

    X = np.array(X)
    y = np.array(y)

    stage2_time = time.time() - stage2_start
    print(f"  特征矩阵: {X.shape}")
    print(f"  特征列表: {feature_names}")
    print(f"  总样本: {len(y)}")
    print(f"  亏损样本: {sum(y)} ({sum(y)/len(y)*100:.1f}%)")
    print(f"  盈利样本: {len(y)-sum(y)} ({(len(y)-sum(y))/len(y)*100:.1f}%)")
    print(f"  耗时: {stage2_time:.1f}秒")

    # ========== 阶段3：并发训练多个模型 ==========
    print(f"\n[阶段3] 并发训练模型...")
    stage3_start = time.time()

    # 划分训练集和测试集
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    # 标准化
    scaler = StandardScaler()
    scaler.fit(X_train)
    X_train_scaled = scaler.transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    # 定义多个模型配置
    model_configs = [
        {
            'name': 'GB_n100_d5',
            'model': GradientBoostingClassifier(
                n_estimators=100, max_depth=5, learning_rate=0.1,
                min_samples_split=20, min_samples_leaf=10, random_state=42
            )
        },
        {
            'name': 'GB_n150_d4',
            'model': GradientBoostingClassifier(
                n_estimators=150, max_depth=4, learning_rate=0.08,
                min_samples_split=20, min_samples_leaf=10, random_state=42
            )
        },
        {
            'name': 'GB_n80_d6',
            'model': GradientBoostingClassifier(
                n_estimators=80, max_depth=6, learning_rate=0.12,
                min_samples_split=15, min_samples_leaf=8, random_state=42
            )
        },
        {
            'name': 'RF_n200',
            'model': RandomForestClassifier(
                n_estimators=200, max_depth=8, min_samples_split=20,
                min_samples_leaf=10, random_state=42, n_jobs=-1
            )
        },
    ]

    def train_single_model(config):
        """训练单个模型"""
        model = config['model']
        model.fit(X_train_scaled, y_train)

        train_score = model.score(X_train_scaled, y_train)
        test_score = model.score(X_test_scaled, y_test)

        y_prob = model.predict_proba(X_test_scaled)[:, 1]
        auc = roc_auc_score(y_test, y_prob) if len(np.unique(y_test)) > 1 else 0

        return {
            'name': config['name'],
            'model': model,
            'train_score': train_score,
            'test_score': test_score,
            'auc': auc
        }

    # 并发训练模型
    trained_models = []
    with ThreadPoolExecutor(max_workers=len(model_configs)) as executor:
        futures = [executor.submit(train_single_model, config) for config in model_configs]

        for future in as_completed(futures):
            result = future.result()
            trained_models.append(result)
            print(f"  {result['name']}: test_score={result['test_score']:.4f}, AUC={result['auc']:.4f}")

    # 选择最佳模型（按AUC）
    best_model_info = max(trained_models, key=lambda x: x['auc'])
    best_model = best_model_info['model']

    stage3_time = time.time() - stage3_start
    print(f"\n  最佳模型: {best_model_info['name']}")
    print(f"  训练集准确率: {best_model_info['train_score']:.4f}")
    print(f"  测试集准确率: {best_model_info['test_score']:.4f}")
    print(f"  AUC: {best_model_info['auc']:.4f}")
    print(f"  耗时: {stage3_time:.1f}秒")

    # ========== 阶段4：评估和保存 ==========
    print(f"\n[阶段4] 评估并保存模型...")
    stage4_start = time.time()

    y_pred = best_model.predict(X_test_scaled)
    y_prob = best_model.predict_proba(X_test_scaled)[:, 1]

    report = classification_report(y_test, y_pred, output_dict=True)

    # 特征重要性
    feature_importance = dict(zip(feature_names, best_model.feature_importances_))
    sorted_importance = sorted(feature_importance.items(), key=lambda x: x[1], reverse=True)

    print("\n特征重要性 Top 10:")
    for i, (name, importance) in enumerate(sorted_importance[:10], 1):
        print(f"  {i}. {name}: {importance:.4f}")

    # 保存模型
    model_data = {
        'model': best_model,
        'scaler': scaler,
        'features': feature_names,
        'training_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'model_type': f'leader_risk_{best_model_info["name"]}',
        'accuracy': best_model_info['test_score'],
        'auc_score': best_model_info['auc'],
        'precision': report['1']['precision'] if '1' in report else 0,
        'recall': report['1']['recall'] if '1' in report else 0,
        'f1_score': report['1']['f1-score'] if '1' in report else 0,
        'samples_count': len(trades),
        'feature_importance': sorted_importance,
        'all_models': {m['name']: {'test_score': m['test_score'], 'auc': m['auc']} for m in trained_models},
    }

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model_data, output_path)

    stage4_time = time.time() - stage4_start
    total_time = time.time() - total_start

    print(f"\n{'='*60}")
    print(f"训练完成!")
    print(f"{'='*60}")
    print(f"模型已保存到: {output_path}")
    print(f"\n耗时统计:")
    print(f"  阶段1(特征提取): {stage1_time:.1f}秒")
    print(f"  阶段2(构建矩阵): {stage2_time:.1f}秒")
    print(f"  阶段3(模型训练): {stage3_time:.1f}秒")
    print(f"  阶段4(评估保存): {stage4_time:.1f}秒")
    print(f"  总耗时: {total_time:.1f}秒")

    return {
        'train_score': best_model_info['train_score'],
        'test_score': best_model_info['test_score'],
        'auc': best_model_info['auc'],
        'classification_report': report,
        'feature_importance': sorted_importance,
        'total_samples': len(trades),
        'loss_samples': sum(y),
        'win_samples': len(y) - sum(y),
        'total_time': total_time,
    }


def train_risk_model(trades: List[Dict], output_path: str = "outputs/leader_risk_model.joblib") -> Dict:
    """
    训练风险模型（兼容旧接口，使用并发版本）
    """
    return train_risk_model_parallel(
        trades=trades,
        output_path=output_path,
        n_workers=8,
        use_process=False
    )


def run_parallel_backtest(
    start_date: str = "20210501",
    end_date: str = "20260519",
    markets: List[str] = ["sha"],
    n_workers: int = 4
) -> List[Dict]:
    """
    并发运行多个市场的回测

    Args:
        start_date: 开始日期
        end_date: 结束日期
        markets: 市场列表
        n_workers: 并发数

    Returns:
        所有交易记录
    """
    from app.stock_agent.strategies.leader_backtest import OptimizedLeaderBacktester

    print(f"\n{'='*60}")
    print(f"并发回测多个市场")
    print(f"{'='*60}")
    print(f"市场: {markets}")
    print(f"日期范围: {start_date} - {end_date}")

    all_trades = []

    def run_single_backtest(market: str) -> List[Dict]:
        """运行单个市场的回测"""
        backtester = OptimizedLeaderBacktester(
            initial_capital=100000,
            base_position=0.15,
            max_positions=3,
            stop_loss_pct=-6.0,
            trailing_stop_pct=3.0,
            max_hold_days=5,
            use_ml_risk=False,
        )

        result = backtester.run_backtest(
            start_date=start_date,
            end_date=end_date,
            market=market
        )

        return result.trade_records

    start_time = time.time()

    with ThreadPoolExecutor(max_workers=min(n_workers, len(markets))) as executor:
        futures = {executor.submit(run_single_backtest, market): market for market in markets}

        for future in as_completed(futures):
            market = futures[future]
            try:
                trades = future.result()
                all_trades.extend(trades)
                print(f"  {market}: {len(trades)} 笔交易")
            except Exception as e:
                print(f"  {market}: 回测失败 - {e}")

    elapsed = time.time() - start_time
    print(f"\n并发回测完成: {len(all_trades)} 笔交易, 耗时 {elapsed:.1f}秒")

    return all_trades


def main():
    """主函数：并发训练风险模型"""
    print("=" * 60)
    print("龙头策略风险模型训练 - 并发优化版")
    print("=" * 60)

    total_start = time.time()

    # ========== 步骤1：并发回测收集交易数据 ==========
    print("\n[步骤1] 并发回测收集交易数据...")
    step1_start = time.time()

    # 可以并发多个市场（龙头策略不包含创业板）
    all_trades = run_parallel_backtest(
        start_date="20210501",
        end_date="20260519",
        markets=["sha"],  # 可以添加 "sza" 等
        n_workers=4
    )

    step1_time = time.time() - step1_start
    print(f"  步骤1耗时: {step1_time:.1f}秒")

    if not all_trades:
        print("没有交易记录，无法训练模型")
        return

    # ========== 步骤2：并发训练风险模型 ==========
    print("\n[步骤2] 并发训练风险模型...")
    step2_start = time.time()

    train_result = train_risk_model_parallel(
        trades=all_trades,
        output_path=str(Path(__file__).parent / "leader_risk_model.joblib"),
        n_workers=8,
        use_process=False  # Windows下建议使用线程池
    )

    step2_time = time.time() - step2_start
    print(f"  步骤2耗时: {step2_time:.1f}秒")

    # ========== 输出最终结果 ==========
    total_time = time.time() - total_start

    print("\n" + "=" * 60)
    print("训练结果汇总")
    print("=" * 60)
    print(f"总样本数: {train_result.get('total_samples', 0)}")
    print(f"盈利样本: {train_result.get('win_samples', 0)}")
    print(f"亏损样本: {train_result.get('loss_samples', 0)}")
    print(f"测试集准确率: {train_result.get('test_score', 0):.4f}")
    print(f"AUC: {train_result.get('auc', 0):.4f}")

    print("\n特征重要性排序:")
    for name, importance in train_result.get('feature_importance', []):
        print(f"  {name}: {importance:.4f}")

    print(f"\n总耗时: {total_time:.1f}秒")


if __name__ == "__main__":
    main()
