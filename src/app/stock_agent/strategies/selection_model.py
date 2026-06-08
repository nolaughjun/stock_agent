"""
选股模型 - 整合所有因子用于选股评分

将训练好的因子模型集成到选股流程中，提升策略胜率
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
import joblib


@dataclass
class StockScore:
    """股票评分结果"""
    code: str
    name: str
    total_score: float          # 总评分 (0-100)
    win_probability: float      # 胜率预测
    expected_return: float      # 预期收益
    risk_level: str            # 风险等级
    risk_score: int            # 风险评分
    factor_scores: Dict[str, float]  # 各因子得分
    factor_values: Dict[str, float]  # 各因子原始值


class StockSelectionModel:
    """
    选股模型

    整合因子模型和风险模型，生成综合评分

    使用方式：
    1. 训练：使用历史数据训练模型
    2. 预测：对候选股进行评分
    """

    def __init__(self, model_path: str = None):
        """
        初始化选股模型

        Args:
            model_path: 模型文件路径
        """
        self.model = None
        self.scaler = None
        self.feature_names = []
        self.model_path = model_path

        # 因子配置
        self.factor_config = {
            # 新增因子
            'volume_change_rate': {'weight': 0.15, 'optimal_range': (-50, 50)},
            'turnover_rate': {'weight': 0.15, 'optimal_range': (3, 15)},
            'volatility': {'weight': 0.10, 'optimal_range': (3, 8)},
            # 原有因子
            'board_count': {'weight': 0.20, 'optimal_range': (2, 3)},
            'seal_strength': {'weight': 0.15, 'optimal_range': (0.97, 1.0)},
            'float_cap': {'weight': 0.10, 'optimal_range': (20, 100)},
            'sentiment_score': {'weight': 0.15, 'optimal_range': (60, 100)},
        }

        # 尝试加载模型
        if model_path and Path(model_path).exists():
            self.load(model_path)

    def prepare_features(
        self,
        candidate: Dict,
        sentiment_score: float = 50
    ) -> np.ndarray:
        """
        准备特征向量

        Args:
            candidate: 候选股数据
            sentiment_score: 市场情绪分

        Returns:
            特征向量
        """
        features = []

        # 成交量变化率
        volume_change = candidate.get('volume_change_rate', 0) or 0
        features.append(volume_change)

        # 换手率
        turnover = candidate.get('turnover_rate', 0) or 0
        features.append(turnover)

        # 波动率
        volatility = candidate.get('volatility', 0) or 0
        features.append(volatility)

        # 连板数
        board_count = candidate.get('board_count', 2)
        features.append(board_count)

        # 封板强度
        seal_strength = candidate.get('seal_strength', 0.95)
        features.append(seal_strength)

        # 市值
        float_cap = candidate.get('float_cap', 50)
        features.append(float_cap)

        # 情绪分
        features.append(sentiment_score)

        return np.array(features)

    def calculate_factor_score(
        self,
        factor_name: str,
        factor_value: float
    ) -> float:
        """
        计算单个因子得分

        Args:
            factor_name: 因子名称
            factor_value: 因子值

        Returns:
            因子得分 (-10 到 +10)
        """
        if factor_name not in self.factor_config:
            return 0

        config = self.factor_config[factor_name]
        optimal_range = config['optimal_range']

        lower, upper = optimal_range

        if lower <= factor_value <= upper:
            # 在最优区间内，得满分
            return 10
        elif factor_value < lower:
            # 低于下限，按距离扣分
            distance = lower - factor_value
            if factor_name in ['volume_change_rate', 'board_count', 'seal_strength']:
                # 这些因子过低不好
                return max(-10, -distance / lower * 10)
            else:
                return max(-10, -distance / lower * 5)
        else:
            # 高于上限，按距离扣分
            distance = factor_value - upper
            if factor_name in ['volatility', 'turnover_rate']:
                # 这些因子过高不好
                return max(-10, -distance / upper * 10)
            else:
                return max(-10, -distance / upper * 5)

    def score_candidate(
        self,
        candidate: Dict,
        sentiment_score: float = 50,
        use_ml: bool = True
    ) -> StockScore:
        """
        对候选股进行评分

        Args:
            candidate: 候选股数据
            sentiment_score: 市场情绪分
            use_ml: 是否使用ML模型

        Returns:
            StockScore 评分结果
        """
        code = candidate.get('code', '')
        name = candidate.get('name', code)

        # 提取因子值
        factor_values = {
            'volume_change_rate': candidate.get('volume_change_rate', 0) or 0,
            'turnover_rate': candidate.get('turnover_rate', 0) or 0,
            'volatility': candidate.get('volatility', 0) or 0,
            'board_count': candidate.get('board_count', 2),
            'seal_strength': candidate.get('seal_strength', 0.95),
            'float_cap': candidate.get('float_cap', 50),
            'sentiment_score': sentiment_score,
        }

        # 计算各因子得分
        factor_scores = {}
        for factor_name, factor_value in factor_values.items():
            factor_scores[factor_name] = self.calculate_factor_score(factor_name, factor_value)

        # 计算加权总分
        total_score = 0
        for factor_name, score in factor_scores.items():
            weight = self.factor_config.get(factor_name, {}).get('weight', 0.1)
            total_score += score * weight

        # 归一化到 0-100
        total_score = max(0, min(100, (total_score + 10) * 5))

        # 使用ML模型预测胜率
        win_probability = 0.5
        if use_ml and self.model is not None:
            try:
                X = self.prepare_features(candidate, sentiment_score).reshape(1, -1)
                if self.scaler is not None:
                    try:
                        X_scaled = self.scaler.transform(X)
                    except:
                        X_scaled = X
                else:
                    X_scaled = X

                if hasattr(self.model, 'predict_proba'):
                    win_probability = self.model.predict_proba(X_scaled)[0, 1]
                else:
                    win_probability = self.model.predict(X_scaled)[0]
            except Exception:
                pass

        # 计算风险评分
        risk_score = candidate.get('risk_score', 20)
        risk_level = candidate.get('risk_level', '低风险')

        # 计算预期收益（简化估算）
        # 预期收益 = 胜率 * 平均盈利 - (1-胜率) * 平均亏损
        avg_win = 8  # 平均盈利8%
        avg_loss = 6  # 平均亏损6%
        expected_return = win_probability * avg_win - (1 - win_probability) * avg_loss

        return StockScore(
            code=code,
            name=name,
            total_score=round(total_score, 2),
            win_probability=round(win_probability, 4),
            expected_return=round(expected_return, 2),
            risk_level=risk_level,
            risk_score=risk_score,
            factor_scores=factor_scores,
            factor_values=factor_values,
        )

    def rank_candidates(
        self,
        candidates: List[Dict],
        sentiment_score: float = 50,
        use_ml: bool = True
    ) -> List[StockScore]:
        """
        对候选股进行排序

        Args:
            candidates: 候选股列表
            sentiment_score: 市场情绪分
            use_ml: 是否使用ML模型

        Returns:
            排序后的评分列表
        """
        scores = []

        for candidate in candidates:
            score = self.score_candidate(candidate, sentiment_score, use_ml)
            scores.append(score)

        # 按总评分排序
        scores.sort(key=lambda x: x.total_score, reverse=True)

        return scores

    def save(self, path: str = None):
        """保存模型"""
        path = path or self.model_path
        if not path:
            return

        Path(path).parent.mkdir(parents=True, exist_ok=True)

        model_data = {
            'model': self.model,
            'scaler': self.scaler,
            'feature_names': self.feature_names,
            'factor_config': self.factor_config,
            'training_date': datetime.now().strftime('%Y-%m-%d'),
        }

        joblib.dump(model_data, path)
        print(f"选股模型已保存: {path}")

    def load(self, path: str = None) -> bool:
        """加载模型"""
        path = path or self.model_path
        if not path or not Path(path).exists():
            return False

        try:
            model_data = joblib.load(path)

            self.model = model_data.get('model')
            self.scaler = model_data.get('scaler')
            self.feature_names = model_data.get('feature_names', [])
            self.factor_config = model_data.get('factor_config', self.factor_config)

            print(f"选股模型加载成功: {path}")
            if model_data.get('training_date'):
                print(f"  训练日期: {model_data.get('training_date')}")

            return True
        except Exception as e:
            print(f"选股模型加载失败: {e}")
            return False

    def generate_score_report(self, scores: List[StockScore], top_n: int = 10) -> str:
        """生成评分报告"""
        lines = []
        lines.append("# 选股评分报告")
        lines.append("")
        lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**候选股数量**: {len(scores)}")
        lines.append("")

        lines.append("## 一、评分排名（Top 10）")
        lines.append("")
        lines.append("| 排名 | 代码 | 名称 | 总评分 | 胜率 | 预期收益 | 风险等级 |")
        lines.append("|------|------|------|--------|------|----------|----------|")

        for i, score in enumerate(scores[:top_n], 1):
            lines.append(f"| {i} | {score.code} | {score.name} | {score.total_score:.1f} | {score.win_probability:.1%} | {score.expected_return:+.2f}% | {score.risk_level} |")

        lines.append("")

        lines.append("## 二、因子评分详情")
        lines.append("")

        for score in scores[:5]:
            lines.append(f"### {score.code} {score.name}")
            lines.append("")
            lines.append("| 因子 | 原始值 | 得分 |")
            lines.append("|------|--------|------|")

            for factor_name, value in score.factor_values.items():
                factor_score = score.factor_scores.get(factor_name, 0)
                lines.append(f"| {factor_name} | {value:.2f} | {factor_score:+.1f} |")

            lines.append("")

        return "\n".join(lines)


def integrate_model_into_selection(
    candidates: List[Dict],
    sentiment_score: float = 50,
    model_path: str = None
) -> List[Dict]:
    """
    将模型集成到选股流程中

    Args:
        candidates: 候选股列表
        sentiment_score: 市场情绪分
        model_path: 模型路径

    Returns:
        带有评分的候选股列表
    """
    # 初始化模型
    model = StockSelectionModel(model_path)

    # 评分
    scores = model.rank_candidates(candidates, sentiment_score, use_ml=True)

    # 将评分结果合并到候选股
    for i, candidate in enumerate(candidates):
        # 找到对应的评分
        for score in scores:
            if score.code == candidate.get('code'):
                candidate['ml_score'] = score.total_score
                candidate['win_probability'] = score.win_probability
                candidate['expected_return'] = score.expected_return
                candidate['factor_scores'] = score.factor_scores
                break

    # 按ML评分重新排序
    candidates.sort(key=lambda x: x.get('ml_score', 0), reverse=True)

    return candidates
