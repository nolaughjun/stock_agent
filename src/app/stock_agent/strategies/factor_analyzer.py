"""
因子有效性分析工具

分析各因子与收益的相关性，验证因子预测能力
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Tuple
from pathlib import Path
import matplotlib.pyplot as plt
from datetime import datetime


class FactorAnalyzer:
    """因子分析器"""

    def __init__(self):
        self.factor_stats = {}

    def analyze_factor_effectiveness(
        self,
        results: List[Dict],
        output_path: str = None
    ) -> Dict:
        """
        分析因子有效性

        Args:
            results: 交易结果列表，包含因子值和收益
            output_path: 输出路径

        Returns:
            因子有效性统计
        """
        if not results:
            return {}

        # 转换为DataFrame
        df = pd.DataFrame(results)

        # 只分析已完成的交易
        df = df[df['hold_days'] > 0]

        if len(df) == 0:
            return {}

        # 因子列表
        factors = ['volume_change_rate', 'turnover_rate', 'volatility', 'factor_score']

        stats = {}

        for factor in factors:
            if factor not in df.columns:
                continue

            factor_values = df[factor].dropna()
            if len(factor_values) == 0:
                continue

            # 1. 相关性分析
            correlation = df[[factor, 'profit_pct']].corr().iloc[0, 1]

            # 2. 分组分析（按因子值分成5组，看各组平均收益）
            df['factor_group'] = pd.qcut(df[factor], q=5, labels=['Q1', 'Q2', 'Q3', 'Q4', 'Q5'], duplicates='drop')
            group_stats = df.groupby('factor_group').agg({
                'profit_pct': ['mean', 'std', 'count'],
                'is_win': 'mean'  # 胜率
            }).round(4)

            # 3. IC值（信息系数）
            # IC = 因子值与未来收益的相关系数
            ic = correlation

            # 4. 因子单调性
            group_means = df.groupby('factor_group')['profit_pct'].mean()
            monotonicity = self._calc_monotonicity(group_means.values)

            stats[factor] = {
                'correlation': round(correlation, 4),
                'ic': round(ic, 4),
                'monotonicity': round(monotonicity, 4),
                'group_stats': group_stats.to_dict(),
                'mean': round(factor_values.mean(), 4),
                'std': round(factor_values.std(), 4),
                'min': round(factor_values.min(), 4),
                'max': round(factor_values.max(), 4),
            }

        self.factor_stats = stats

        # 生成报告
        report = self._generate_factor_report(stats)

        if output_path:
            import os
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(report)
            print(f"因子分析报告已保存: {output_path}")

        return stats

    def _calc_monotonicity(self, values: np.ndarray) -> float:
        """
        计算单调性

        单调性 = 相邻组收益方向一致的比例
        """
        if len(values) < 2:
            return 0

        diffs = np.diff(values)
        positive = np.sum(diffs > 0)
        negative = np.sum(diffs < 0)

        total = len(diffs)
        monotonicity = max(positive, negative) / total

        return monotonicity

    def _generate_factor_report(self, stats: Dict) -> str:
        """生成因子分析报告"""
        lines = []
        lines.append("# 因子有效性分析报告")
        lines.append("")
        lines.append(f"**分析时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        lines.append("## 一、因子有效性汇总")
        lines.append("")
        lines.append("| 因子 | 相关系数 | IC值 | 单调性 | 平均值 | 标准差 |")
        lines.append("|------|----------|------|--------|--------|--------|")

        for factor, s in stats.items():
            effectiveness = "✅ 有效" if abs(s['ic']) > 0.02 and s['monotonicity'] > 0.6 else "⚠️ 待优化"
            lines.append(f"| {factor} | {s['correlation']:.4f} | {s['ic']:.4f} | {s['monotonicity']:.4f} | {s['mean']:.2f} | {s['std']:.2f} | {effectiveness} |")

        lines.append("")
        lines.append("**判断标准**：")
        lines.append("- IC > 0.02 且 单调性 > 0.6：因子有效")
        lines.append("- IC < 0.02：因子预测能力弱")
        lines.append("- 单调性 < 0.6：因子与收益关系不单调，需优化阈值")
        lines.append("")

        lines.append("## 二、因子分组分析")
        lines.append("")
        lines.append("按因子值分成5组（Q1最低，Q5最高），分析各组收益特征：")
        lines.append("")

        for factor, s in stats.items():
            lines.append(f"### {factor}")
            lines.append("")
            lines.append("| 分组 | 平均收益% | 收益标准差 | 样本数 | 胜率% |")
            lines.append("|------|----------|------------|--------|-------|")

            group_stats = s.get('group_stats', {})
            profit_means = group_stats.get(('profit_pct', 'mean'), {})
            profit_stds = group_stats.get(('profit_pct', 'std'), {})
            counts = group_stats.get(('profit_pct', 'count'), {})
            win_rates = group_stats.get(('is_win', 'mean'), {})

            for group in ['Q1', 'Q2', 'Q3', 'Q4', 'Q5']:
                mean = profit_means.get(group, 0) * 100 if group in profit_means else 0
                std = profit_stds.get(group, 0) * 100 if group in profit_stds else 0
                count = counts.get(group, 0) if group in counts else 0
                win_rate = win_rates.get(group, 0) * 100 if group in win_rates else 0

                lines.append(f"| {group} | {mean:+.2f} | {std:.2f} | {int(count)} | {win_rate:.1f} |")

            lines.append("")

        lines.append("## 三、因子优化建议")
        lines.append("")

        for factor, s in stats.items():
            lines.append(f"### {factor}")
            lines.append("")

            if s['ic'] < 0.02:
                lines.append("- ⚠️ IC值较低，建议调整计算方式或阈值")
            elif s['monotonicity'] < 0.6:
                lines.append("- ⚠️ 单调性不足，建议重新设计分组阈值")
            else:
                lines.append("- ✅ 因子有效，可用于模型训练")

            # 基于分组分析给出阈值建议
            group_stats = s.get('group_stats', {})
            profit_means = group_stats.get(('profit_pct', 'mean'), {})

            if profit_means:
                # 找到收益最高的组
                best_group = max(profit_means.keys(), key=lambda k: profit_means[k])
                lines.append(f"- 💡 收益最高组：{best_group}，建议重点关注该区间的股票")
            lines.append("")

        lines.append("## 四、下一步行动")
        lines.append("")
        lines.append("1. **数据收集**：积累更多历史交易数据（建议1000+样本）")
        lines.append("2. **因子优化**：根据分组分析调整阈值")
        lines.append("3. **模型训练**：使用优化后的因子训练ML模型")
        lines.append("4. **回测验证**：验证模型在历史数据上的表现")
        lines.append("")

        return "\n".join(lines)


def run_factor_analysis(results: List[Dict], output_dir: str = None) -> Dict:
    """
    运行因子分析

    Args:
        results: 交易结果列表
        output_dir: 输出目录

    Returns:
        因子统计
    """
    analyzer = FactorAnalyzer()

    output_path = None
    if output_dir:
        output_path = Path(output_dir) / "factor_analysis_report.md"

    return analyzer.analyze_factor_effectiveness(results, output_path)
