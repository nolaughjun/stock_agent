"""
因子模型训练器

使用机器学习优化因子权重，提升策略胜率
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Tuple, Optional
from pathlib import Path
from datetime import datetime
import joblib
from dataclasses import dataclass

from sklearn.model_selection import train_test_split, cross_val_score
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score
import warnings
warnings.filterwarnings('ignore')


@dataclass
class FactorModelResult:
    """因子模型结果"""
    model_type: str
    accuracy: float
    precision: float
    recall: float
    f1: float
    auc: float
    feature_importance: Dict[str, float]
    optimal_thresholds: Dict[str, Tuple[float, float]]


class FactorModelTrainer:
    """因子模型训练器"""

    # 因子配置
    FACTORS = ['volume_change_rate', 'turnover_rate', 'volatility', 'seal_strength', 'float_cap']

    # 原有因子（用于对比）
    ORIGINAL_FACTORS = ['board_count', 'seal_strength', 'float_cap']

    def __init__(self, model_type: str = 'logistic'):
        """
        初始化训练器

        Args:
            model_type: 模型类型 (logistic/random_forest/gradient_boosting)
        """
        self.model_type = model_type
        self.model = None
        self.scaler = None
        self.feature_names = []

    def prepare_training_data(
        self,
        results: List[Dict],
        target: str = 'is_win'
    ) -> Tuple[pd.DataFrame, pd.Series]:
        """
        准备训练数据

        Args:
            results: 交易结果列表
            target: 目标变量 (is_win / profit_pct > 0)

        Returns:
            (X, y) 特征矩阵和标签
        """
        if not results:
            return pd.DataFrame(), pd.Series()

        df = pd.DataFrame(results)

        # 只使用完成的交易
        df = df[df['hold_days'] > 0].copy()

        if len(df) == 0:
            return pd.DataFrame(), pd.Series()

        # 选择特征列
        feature_cols = []
        for col in self.FACTORS:
            if col in df.columns:
                feature_cols.append(col)

        # 添加原有因子
        for col in self.ORIGINAL_FACTORS:
            if col in df.columns and col not in feature_cols:
                feature_cols.append(col)

        if not feature_cols:
            return pd.DataFrame(), pd.Series()

        # 特征矩阵
        X = df[feature_cols].copy()

        # 处理缺失值
        X = X.fillna(0)

        # 标签
        if target == 'is_win':
            y = df['is_win'].astype(int)
        else:
            y = (df['profit_pct'] > 0).astype(int)

        self.feature_names = feature_cols

        return X, y

    def train(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        test_size: float = 0.2
    ) -> FactorModelResult:
        """
        训练模型

        Args:
            X: 特征矩阵
            y: 标签
            test_size: 测试集比例

        Returns:
            模型结果
        """
        if len(X) < 50:
            print(f"警告：样本数不足（{len(X)}），建议至少100个样本")
            return None

        # 划分训练集和测试集
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=42, stratify=y if len(y.unique()) > 1 else None
        )

        # 标准化
        self.scaler = StandardScaler()
        X_train_scaled = self.scaler.fit_transform(X_train)
        X_test_scaled = self.scaler.transform(X_test)

        # 选择模型
        if self.model_type == 'logistic':
            self.model = LogisticRegression(max_iter=1000, random_state=42)
        elif self.model_type == 'random_forest':
            self.model = RandomForestClassifier(
                n_estimators=100,
                max_depth=5,
                random_state=42,
                n_jobs=-1
            )
        elif self.model_type == 'gradient_boosting':
            self.model = GradientBoostingClassifier(
                n_estimators=100,
                max_depth=3,
                random_state=42
            )
        else:
            self.model = LogisticRegression(max_iter=1000, random_state=42)

        # 训练
        self.model.fit(X_train_scaled, y_train)

        # 预测
        y_pred = self.model.predict(X_test_scaled)
        y_prob = self.model.predict_proba(X_test_scaled)[:, 1] if hasattr(self.model, 'predict_proba') else y_pred

        # 评估
        metrics = {
            'accuracy': accuracy_score(y_test, y_pred),
            'precision': precision_score(y_test, y_pred, zero_division=0),
            'recall': recall_score(y_test, y_pred, zero_division=0),
            'f1': f1_score(y_test, y_pred, zero_division=0),
            'auc': roc_auc_score(y_test, y_prob) if len(y_test.unique()) > 1 else 0.5,
        }

        # 特征重要性
        feature_importance = {}
        if hasattr(self.model, 'coef_'):
            # 线性模型
            for i, name in enumerate(self.feature_names):
                feature_importance[name] = float(self.model.coef_[0][i])
        elif hasattr(self.model, 'feature_importances_'):
            # 树模型
            for i, name in enumerate(self.feature_names):
                feature_importance[name] = float(self.model.feature_importances_[i])

        # 计算最优阈值区间
        optimal_thresholds = self._calc_optimal_thresholds(X, y)

        return FactorModelResult(
            model_type=self.model_type,
            accuracy=metrics['accuracy'],
            precision=metrics['precision'],
            recall=metrics['recall'],
            f1=metrics['f1'],
            auc=metrics['auc'],
            feature_importance=feature_importance,
            optimal_thresholds=optimal_thresholds,
        )

    def _calc_optimal_thresholds(self, X: pd.DataFrame, y: pd.Series) -> Dict[str, Tuple[float, float]]:
        """
        计算因子的最优阈值区间

        通过分析因子与收益的关系，找到收益最高时的因子区间

        Returns:
            {因子名: (下限, 上限)}
        """
        thresholds = {}

        for col in X.columns:
            if col not in self.feature_names:
                continue

            # 将因子分成10份
            try:
                df_temp = pd.DataFrame({
                    'factor': X[col],
                    'win': y
                })

                # 计算每个区间的胜率
                df_temp['group'] = pd.qcut(df_temp['factor'], q=10, labels=False, duplicates='drop')
                group_win_rate = df_temp.groupby('group')['win'].mean()

                if len(group_win_rate) > 0:
                    # 找到胜率最高的区间
                    best_group = group_win_rate.idxmax()

                    # 计算该区间的因子范围
                    group_bounds = df_temp.groupby('group')['factor'].agg(['min', 'max'])

                    if best_group in group_bounds.index:
                        lower = group_bounds.loc[best_group, 'min']
                        upper = group_bounds.loc[best_group, 'max']
                        thresholds[col] = (round(lower, 4), round(upper, 4))
            except Exception:
                pass

        return thresholds

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        预测

        Args:
            X: 特征矩阵

        Returns:
            预测概率
        """
        if self.model is None or self.scaler is None:
            return np.array([])

        # 确保列顺序一致
        X = X.reindex(columns=self.feature_names, fill_value=0)

        X_scaled = self.scaler.transform(X)
        return self.model.predict_proba(X_scaled)[:, 1]

    def save(self, path: str):
        """保存模型"""
        if self.model is None:
            return

        model_data = {
            'model': self.model,
            'scaler': self.scaler,
            'feature_names': self.feature_names,
            'model_type': self.model_type,
        }
        joblib.dump(model_data, path)
        print(f"模型已保存: {path}")

    def load(self, path: str) -> bool:
        """加载模型"""
        try:
            model_data = joblib.load(path)
            self.model = model_data['model']
            self.scaler = model_data['scaler']
            self.feature_names = model_data['feature_names']
            self.model_type = model_data.get('model_type', 'unknown')
            print(f"模型加载成功: {path}")
            return True
        except Exception as e:
            print(f"模型加载失败: {e}")
            return False

    def evaluate(
        self,
        results: List[Dict],
        target: str = 'is_win'
    ) -> Dict:
        """
        在验证集上评估模型

        Args:
            results: 验证集交易结果列表
            target: 目标变量

        Returns:
            评估指标字典
        """
        if self.model is None or self.scaler is None:
            print("错误：模型未训练或未加载")
            return None

        # 准备数据
        X, y = self.prepare_training_data(results, target)

        if len(X) < 10:
            print(f"验证集样本数不足（{len(X)}）")
            return None

        # 标准化
        X_scaled = self.scaler.transform(X)

        # 预测
        y_pred = self.model.predict(X_scaled)
        y_prob = self.model.predict_proba(X_scaled)[:, 1] if hasattr(self.model, 'predict_proba') else y_pred

        # 计算指标
        metrics = {
            'sample_count': len(X),
            'win_rate': y.mean(),
            'accuracy': accuracy_score(y, y_pred),
            'precision': precision_score(y, y_pred, zero_division=0),
            'recall': recall_score(y, y_pred, zero_division=0),
            'f1': f1_score(y, y_pred, zero_division=0),
            'auc': roc_auc_score(y, y_prob) if len(y.unique()) > 1 else 0.5,
        }

        # 计算不同阈值下的表现
        threshold_metrics = {}
        for threshold in [0.4, 0.5, 0.6, 0.7]:
            y_pred_thresh = (y_prob >= threshold).astype(int)
            threshold_metrics[f'thresh_{threshold}'] = {
                'accuracy': accuracy_score(y, y_pred_thresh),
                'precision': precision_score(y, y_pred_thresh, zero_division=0),
                'recall': recall_score(y, y_pred_thresh, zero_division=0),
                'count': (y_prob >= threshold).sum(),
            }

        metrics['threshold_metrics'] = threshold_metrics

        return metrics

    def generate_model_report(self, result: FactorModelResult) -> str:
        """生成模型报告"""
        lines = []
        lines.append("# 因子模型训练报告")
        lines.append("")
        lines.append(f"**训练时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**模型类型**: {result.model_type}")
        lines.append("")

        lines.append("## 一、模型性能")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|------|------|")
        lines.append(f"| 准确率 | {result.accuracy:.4f} |")
        lines.append(f"| 精确率 | {result.precision:.4f} |")
        lines.append(f"| 召回率 | {result.recall:.4f} |")
        lines.append(f"| F1分数 | {result.f1:.4f} |")
        lines.append(f"| AUC | {result.auc:.4f} |")
        lines.append("")

        lines.append("## 二、特征重要性")
        lines.append("")
        lines.append("| 因子 | 重要性/系数 | 方向 |")
        lines.append("|------|------------|------|")

        for factor, importance in sorted(result.feature_importance.items(), key=lambda x: abs(x[1]), reverse=True):
            direction = "正向" if importance > 0 else "负向"
            lines.append(f"| {factor} | {importance:.4f} | {direction} |")

        lines.append("")

        lines.append("## 三、因子最优阈值")
        lines.append("")
        lines.append("| 因子 | 下限 | 上限 | 说明 |")
        lines.append("|------|------|------|------|")

        for factor, (lower, upper) in result.optimal_thresholds.items():
            lines.append(f"| {factor} | {lower:.4f} | {upper:.4f} | 该区间胜率最高 |")

        lines.append("")

        lines.append("## 四、优化建议")
        lines.append("")
        lines.append("### 阈值优化")
        lines.append("")

        for factor, (lower, upper) in result.optimal_thresholds.items():
            lines.append(f"- **{factor}**: 建议阈值区间 [{lower:.2f}, {upper:.2f}]")
            lines.append(f"  - 修改评分规则：当因子值在此区间时加分")

        lines.append("")
        lines.append("### 模型使用")
        lines.append("")
        lines.append("1. 将训练好的模型集成到选股逻辑")
        lines.append("2. 使用 `predict()` 方法预测胜率")
        lines.append("3. 设置阈值筛选高胜率股票")
        lines.append("")

        return "\n".join(lines)

    def generate_validation_report(
        self,
        train_result: FactorModelResult,
        val_metrics: Dict,
        train_period: str,
        val_period: str
    ) -> str:
        """生成验证报告"""
        lines = []
        lines.append("# 因子模型训练与验证报告")
        lines.append("")
        lines.append(f"**训练时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**模型类型**: {train_result.model_type}")
        lines.append(f"**训练区间**: {train_period}")
        lines.append(f"**验证区间**: {val_period}")
        lines.append("")

        lines.append("## 一、训练集性能")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|------|------|")
        lines.append(f"| 准确率 | {train_result.accuracy:.4f} |")
        lines.append(f"| 精确率 | {train_result.precision:.4f} |")
        lines.append(f"| 召回率 | {train_result.recall:.4f} |")
        lines.append(f"| F1分数 | {train_result.f1:.4f} |")
        lines.append(f"| AUC | {train_result.auc:.4f} |")
        lines.append("")

        lines.append("## 二、验证集性能")
        lines.append("")
        lines.append(f"**样本数量**: {val_metrics['sample_count']}")
        lines.append(f"**基准胜率**: {val_metrics['win_rate']:.2%}")
        lines.append("")
        lines.append("| 指标 | 数值 | 说明 |")
        lines.append("|------|------|------|")
        lines.append(f"| 准确率 | {val_metrics['accuracy']:.4f} | {(val_metrics['accuracy'] - val_metrics['win_rate'])*100:+.2f}% vs 基准 |")
        lines.append(f"| 精确率 | {val_metrics['precision']:.4f} | 预测为赢的准确程度 |")
        lines.append(f"| 召回率 | {val_metrics['recall']:.4f} | 实际赢的被找出比例 |")
        lines.append(f"| F1分数 | {val_metrics['f1']:.4f} | 精确率与召回率调和 |")
        lines.append(f"| AUC | {val_metrics['auc']:.4f} | 模型区分能力 |")
        lines.append("")

        lines.append("## 三、不同阈值下的表现")
        lines.append("")
        lines.append("| 阈值 | 数量 | 准确率 | 精确率 | 召回率 |")
        lines.append("|------|------|--------|--------|--------|")

        for thresh, m in val_metrics['threshold_metrics'].items():
            threshold = thresh.replace('thresh_', '')
            lines.append(f"| {threshold} | {m['count']} | {m['accuracy']:.4f} | {m['precision']:.4f} | {m['recall']:.4f} |")

        lines.append("")

        lines.append("## 四、过拟合分析")
        lines.append("")

        acc_diff = val_metrics['accuracy'] - train_result.accuracy
        auc_diff = val_metrics['auc'] - train_result.auc

        if acc_diff > -0.05 and auc_diff > -0.05:
            status = "✅ 模型泛化能力良好"
        elif acc_diff > -0.10 and auc_diff > -0.10:
            status = "⚠️ 轻微过拟合，可接受"
        else:
            status = "❌ 严重过拟合，需要优化"

        lines.append(f"**状态**: {status}")
        lines.append("")
        lines.append("| 指标 | 训练集 | 验证集 | 差异 |")
        lines.append("|------|--------|--------|------|")
        lines.append(f"| 准确率 | {train_result.accuracy:.4f} | {val_metrics['accuracy']:.4f} | {acc_diff:+.4f} |")
        lines.append(f"| AUC | {train_result.auc:.4f} | {val_metrics['auc']:.4f} | {auc_diff:+.4f} |")
        lines.append("")

        lines.append("## 五、特征重要性")
        lines.append("")
        lines.append("| 因子 | 重要性/系数 | 方向 |")
        lines.append("|------|------------|------|")

        for factor, importance in sorted(train_result.feature_importance.items(), key=lambda x: abs(x[1]), reverse=True):
            direction = "正向" if importance > 0 else "负向"
            lines.append(f"| {factor} | {importance:.4f} | {direction} |")

        lines.append("")

        lines.append("## 六、使用建议")
        lines.append("")

        if val_metrics['auc'] > 0.65:
            lines.append("- 模型表现优秀，可用于实盘选股")
            best_thresh = max(val_metrics['threshold_metrics'].items(), key=lambda x: x[1]['precision'])
            lines.append(f"- 建议使用阈值 **{best_thresh[0].replace('thresh_', '')}** 以获得较高精确率")
        elif val_metrics['auc'] > 0.55:
            lines.append("- 模型有一定预测能力，建议谨慎使用")
            lines.append("- 可结合其他选股条件一起筛选")
        else:
            lines.append("- 模型预测能力较弱，建议重新训练或调整因子")

        lines.append("")

        return "\n".join(lines)


def train_factor_model(
    results: List[Dict],
    model_type: str = 'logistic',
    output_path: str = None,
    report_path: str = None
) -> Tuple[FactorModelTrainer, FactorModelResult]:
    """
    训练因子模型

    Args:
        results: 交易结果列表
        model_type: 模型类型
        output_path: 模型保存路径
        report_path: 报告保存路径

    Returns:
        (训练器, 模型结果)
    """
    trainer = FactorModelTrainer(model_type=model_type)

    # 准备数据
    X, y = trainer.prepare_training_data(results)

    if len(X) < 50:
        print(f"样本数不足（{len(X)}），需要更多交易数据")
        return trainer, None

    # 训练
    result = trainer.train(X, y)

    # 保存模型
    if output_path and result:
        trainer.save(output_path)

    # 生成报告
    if result:
        report = trainer.generate_model_report(result)
        print("\n" + report)

        if report_path:
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report)
            print(f"\n报告已保存: {report_path}")

    return trainer, result
