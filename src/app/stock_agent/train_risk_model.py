"""
训练机器学习风险评分模型
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
from datetime import datetime
from app.stock_agent.data.data_loader import DataLoader, MarketType
from app.stock_agent.strategies.leader_backtest import OptimizedLeaderBacktester
from app.stock_agent.strategies.risk_model import RiskModel, generate_risk_report


def train_risk_model(
    start_date: str = "20210101",
    end_date: str = "20260519",
    market: str = "sha",
    output_dir: str = None
):
    """
    训练风险模型

    步骤：
    1. 运行回测获取历史交易数据
    2. 加载股票历史数据用于特征提取
    3. 训练风险模型
    4. 验证模型效果
    5. 保存模型和报告
    """
    # 默认输出到 stock_agent/outputs 目录
    if output_dir is None:
        output_dir = Path(__file__).parent / "outputs"

    print("=" * 60)
    print("机器学习风险模型训练")
    print("=" * 60)
    print(f"训练数据区间: {start_date} - {end_date}")
    print(f"市场: {market}")
    print(f"输出目录: {output_dir}")
    print()

    # 1. 运行回测获取交易数据
    print("[步骤1] 运行回测获取历史交易数据...")

    backtester = OptimizedLeaderBacktester(
        initial_capital=100000,
        base_position=0.15,
        max_positions=3,
        stop_loss_pct=-6.0,
        trailing_stop_pct=3.0,
        max_hold_days=5
    )

    result = backtester.run_backtest(start_date, end_date, market)

    trades = result.trade_records
    print(f"  获取交易记录: {len(trades)} 笔")

    if len(trades) < 100:
        print("  [警告] 交易样本不足，建议扩大回测区间")
        return None

    # 2. 加载股票历史数据
    print("\n[步骤2] 加载股票历史数据...")

    loader = DataLoader()

    # 获取所有涉及的股票代码
    codes = list(set(t['code'] for t in trades))
    print(f"  涉及股票: {len(codes)} 只")

    # 加载历史数据
    df_history_map = {}
    data_start = (pd.Timestamp(start_date) - pd.Timedelta(days=30)).strftime("%Y%m%d")

    for i, code in enumerate(codes):
        if (i + 1) % 100 == 0:
            print(f"  加载进度: {i+1}/{len(codes)}")

        try:
            df = loader.get_stock_history(code, data_start, end_date)
            if df is not None and len(df) > 0:
                df_history_map[code] = df
        except:
            pass

    print(f"  成功加载: {len(df_history_map)} 只")

    # 3. 训练风险模型
    print("\n[步骤3] 训练风险模型...")

    risk_model = RiskModel()
    train_result = risk_model.train(trades, df_history_map)

    print(f"  训练样本: {train_result.get('total_samples', 0)}")
    print(f"  亏损样本: {train_result.get('loss_samples', 0)}")
    print(f"  盈利样本: {train_result.get('win_samples', 0)}")
    print(f"  训练准确率: {train_result.get('train_score', 0):.2%}")
    print(f"  测试准确率: {train_result.get('test_score', 0):.2%}")
    print(f"  AUC: {train_result.get('auc', 0):.3f}")

    # 4. 计算各风险等级的实际表现
    print("\n[步骤4] 验证风险等级表现...")

    trades_by_risk = {'低风险': {'count': 0, 'profit': 0, 'wins': 0},
                      '中风险': {'count': 0, 'profit': 0, 'wins': 0},
                      '高风险': {'count': 0, 'profit': 0, 'wins': 0}}

    for trade in trades:
        code = trade.get('code', '')
        df_history = df_history_map.get(code)

        risk_pred = risk_model.predict_risk(trade, df_history)
        risk_level = risk_pred['risk_level']

        if risk_level in trades_by_risk:
            trades_by_risk[risk_level]['count'] += 1
            profit = trade.get('profit_pct', trade.get('profit', 0))
            trades_by_risk[risk_level]['profit'] += profit
            if profit > 0:
                trades_by_risk[risk_level]['wins'] += 1

    print("\n  各风险等级实际表现:")
    print("  | 风险等级 | 交易次数 | 胜率 | 平均收益 |")
    print("  |----------|----------|------|----------|")

    for risk in ['低风险', '中风险', '高风险']:
        stats = trades_by_risk[risk]
        if stats['count'] > 0:
            wr = stats['wins'] / stats['count'] * 100
            avg = stats['profit'] / stats['count']
            print(f"  | {risk} | {stats['count']} | {wr:.1f}% | {avg:+.2f}% |")

    # 5. 保存模型和报告
    print("\n[步骤5] 保存模型和报告...")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # 保存模型
    model_file = output_path / "risk_model.pkl"
    risk_model.save(str(model_file))
    print(f"  模型已保存: {model_file}")

    # 生成报告
    report = generate_risk_report(train_result, trades_by_risk)
    report_file = output_path / f"risk_model_report_{start_date}_{end_date}.md"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"  报告已保存: {report_file}")

    # 输出特征重要性
    print("\n[特征重要性 Top 10]")
    importance = train_result.get('feature_importance', [])
    for i, (name, score) in enumerate(importance[:10], 1):
        print(f"  {i}. {name}: {score:.4f}")

    print("\n" + "=" * 60)
    print("训练完成！")
    print("=" * 60)

    return risk_model, train_result, trades_by_risk


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="训练风险模型")
    parser.add_argument("--start", default="20210101", help="开始日期")
    parser.add_argument("--end", default="20260519", help="结束日期")
    parser.add_argument("--market", default="sha", help="市场")
    parser.add_argument("--output", default="outputs", help="输出目录")

    args = parser.parse_args()

    train_risk_model(
        start_date=args.start,
        end_date=args.end,
        market=args.market,
        output_dir=args.output
    )
