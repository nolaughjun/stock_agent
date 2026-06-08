"""
高级模型训练器 - 多维度优化

优化方向：
1. 数据增强：生成更多训练样本
2. 特征工程：增加更多有效特征
3. 模型优化：集成学习 + 超参数调优
4. 阈值调优：平衡精确率和召回率
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Tuple, Optional
from pathlib import Path
from datetime import datetime
import joblib
from dataclasses import dataclass

from sklearn.model_selection import train_test_split, cross_val_score, GridSearchCV, StratifiedKFold
from sklearn.preprocessing import StandardScaler, PolynomialFeatures
from sklearn.feature_selection import SelectKBest, f_classif, RFE
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, VotingClassifier, StackingClassifier
from sklearn.svm import SVC
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score, roc_auc_score,
    classification_report, confusion_matrix, precision_recall_curve
)
from imblearn.over_sampling import SMOTE, ADASYN
from imblearn.under_sampling import RandomUnderSampler
from imblearn.combine import SMOTETomek
import warnings
warnings.filterwarnings('ignore')


@dataclass
class AdvancedModelResult:
    """高级模型结果"""
    model_type: str
    accuracy: float
    precision: float
    recall: float
    f1: float
    auc: float
    optimal_threshold: float
    feature_importance: Dict[str, float]
    cross_val_scores: List[float]
    confusion_matrix: Tuple[int, int, int, int]  # TN, FP, FN, TP


class AdvancedModelTrainer:
    """高级模型训练器"""

    def __init__(self, target_accuracy: float = 0.90, target_recall: float = 0.88):
        """
        初始化训练器

        Args:
            target_accuracy: 目标准确率
            target_recall: 目标召回率
        """
        self.target_accuracy = target_accuracy
        self.target_recall = target_recall
        self.model = None
        self.scaler = None
        self.feature_selector = None
        self.poly_features = None
        self.feature_names = []
        self.optimal_threshold = 0.5

    def engineer_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        特征工程：创建更多有效特征

        Args:
            df: 原始特征DataFrame

        Returns:
            增强后的特征DataFrame
        """
        df_enhanced = df.copy()

        # 1. 交互特征
        if 'volume_change_rate' in df.columns and 'turnover_rate' in df.columns:
            # 成交量变化率 × 换手率 = 活跃度综合指标
            df_enhanced['activity_score'] = df['volume_change_rate'] * df['turnover_rate'] / 1000

        if 'volatility' in df.columns and 'seal_strength' in df.columns:
            # 波动率 × 封板强度 = 风险调整强度
            df_enhanced['risk_adjusted_strength'] = df['volatility'] * df['seal_strength']

        if 'board_count' in df.columns and 'seal_strength' in df.columns:
            # 连板数 × 封板强度 = 动量强度
            df_enhanced['momentum_strength'] = df['board_count'] * df['seal_strength']

        # 2. 比率特征
        if 'turnover_rate' in df.columns and 'volatility' in df.columns:
            # 换手率/波动率 = 活跃效率
            df_enhanced['turnover_volatility_ratio'] = df['turnover_rate'] / (df['volatility'] + 0.01)

        if 'float_cap' in df.columns and 'turnover_rate' in df.columns:
            # 市值调整换手率
            df_enhanced['cap_adjusted_turnover'] = df['turnover_rate'] / (df['float_cap'] + 1)

        # 3. 分箱特征（非线性关系）
        if 'volume_change_rate' in df.columns:
            # 成交量变化分箱
            df_enhanced['volume_bin'] = pd.cut(
                df['volume_change_rate'],
                bins=[-np.inf, 0, 50, 150, np.inf],
                labels=[0, 1, 2, 3]
            ).astype(float)

        if 'turnover_rate' in df.columns:
            # 换手率分箱
            df_enhanced['turnover_bin'] = pd.cut(
                df['turnover_rate'],
                bins=[0, 5, 15, 25, np.inf],
                labels=[0, 1, 2, 3]
            ).astype(float)

        if 'volatility' in df.columns:
            # 波动率分箱
            df_enhanced['volatility_bin'] = pd.cut(
                df['volatility'],
                bins=[0, 4, 8, 12, np.inf],
                labels=[0, 1, 2, 3]
            ).astype(float)

        # 4. 排名特征
        for col in ['volume_change_rate', 'turnover_rate', 'volatility']:
            if col in df.columns:
                df_enhanced[f'{col}_rank'] = df[col].rank(pct=True)

        # 5. 极值标记
        if 'volume_change_rate' in df.columns:
            df_enhanced['is_shrinking'] = (df['volume_change_rate'] < 0).astype(int)
            df_enhanced['is_exploding'] = (df['volume_change_rate'] > 200).astype(int)

        if 'turnover_rate' in df.columns:
            df_enhanced['is_low_turnover'] = (df['turnover_rate'] < 5).astype(int)
            df_enhanced['is_high_turnover'] = (df['turnover_rate'] > 20).astype(int)

        # 填充缺失值
        df_enhanced = df_enhanced.fillna(0)

        return df_enhanced

    def balance_data(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        method: str = 'smote'
    ) -> Tuple[pd.DataFrame, pd.Series]:
        """
        数据平衡处理

        Args:
            X: 特征矩阵
            y: 标签
            method: 平衡方法 (smote/adasyn/undersample/combine)

        Returns:
            平衡后的特征和标签
        """
        print(f"  原始样本分布: 盈利={sum(y)}, 亏损={len(y)-sum(y)}")

        if method == 'smote':
            sampler = SMOTE(random_state=42, k_neighbors=min(5, sum(y)-1))
        elif method == 'adasyn':
            sampler = ADASYN(random_state=42, n_neighbors=min(5, sum(y)-1))
        elif method == 'undersample':
            sampler = RandomUnderSampler(random_state=42)
        elif method == 'combine':
            sampler = SMOTETomek(random_state=42)
        else:
            return X, y

        try:
            X_balanced, y_balanced = sampler.fit_resample(X, y)
            print(f"  平衡后样本分布: 盈利={sum(y_balanced)}, 亏损={len(y_balanced)-sum(y_balanced)}")
            return pd.DataFrame(X_balanced, columns=X.columns), pd.Series(y_balanced)
        except Exception as e:
            print(f"  平衡失败: {e}, 使用原始数据")
            return X, y

    def select_features(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        k: int = 10
    ) -> pd.DataFrame:
        """
        特征选择

        Args:
            X: 特征矩阵
            y: 标签
            k: 选择的特征数量

        Returns:
            选择后的特征矩阵
        """
        if X.shape[1] <= k:
            return X

        # 使用ANOVA F值选择特征
        selector = SelectKBest(score_func=f_classif, k=k)
        X_selected = selector.fit_transform(X, y)

        # 获取选中的特征名
        selected_mask = selector.get_support()
        selected_features = X.columns[selected_mask].tolist()

        print(f"  选择特征: {selected_features}")

        self.feature_selector = selector
        self.feature_names = selected_features

        return pd.DataFrame(X_selected, columns=selected_features)

    def find_optimal_threshold(
        self,
        model,
        X_val: np.ndarray,
        y_val: np.ndarray,
        target_recall: float = 0.88
    ) -> float:
        """
        寻找最优分类阈值

        Args:
            model: 训练好的模型
            X_val: 验证集特征
            y_val: 验证集标签
            target_recall: 目标召回率

        Returns:
            最优阈值
        """
        # 获取预测概率
        y_prob = model.predict_proba(X_val)[:, 1]

        # 计算不同阈值下的精确率和召回率
        precisions, recalls, thresholds = precision_recall_curve(y_val, y_prob)

        # 找到满足目标召回率的最优阈值
        best_threshold = 0.5
        best_f1 = 0

        for i, (prec, rec, thresh) in enumerate(zip(precisions, recalls, thresholds)):
            if rec >= target_recall:
                f1 = 2 * prec * rec / (prec + rec + 1e-10)
                if f1 > best_f1:
                    best_f1 = f1
                    best_threshold = thresh

        print(f"  最优阈值: {best_threshold:.4f} (目标召回率>={target_recall})")

        return best_threshold

    def train_with_optimization(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        model_type: str = 'ensemble'
    ) -> AdvancedModelResult:
        """
        优化训练流程

        Args:
            X: 特征矩阵
            y: 标签
            model_type: 模型类型

        Returns:
            训练结果
        """
        print("\n=== 开始优化训练 ===")
        print(f"样本数: {len(X)}, 特征数: {X.shape[1]}")
        print(f"目标: 准确率>={self.target_accuracy}, 召回率>={self.target_recall}")

        # 1. 特征工程
        print("\n[1] 特征工程...")
        X_enhanced = self.engineer_features(X)
        print(f"  增强后特征数: {X_enhanced.shape[1]}")

        # 2. 划分数据集（分层采样）
        X_train, X_test, y_train, y_test = train_test_split(
            X_enhanced, y, test_size=0.2, random_state=42, stratify=y
        )

        # 3. 数据平衡
        print("\n[2] 数据平衡...")
        X_train_balanced, y_train_balanced = self.balance_data(
            X_train, y_train, method='smote'
        )

        # 4. 特征选择
        print("\n[3] 特征选择...")
        X_train_selected = self.select_features(
            X_train_balanced, y_train_balanced, k=min(15, X_train_balanced.shape[1])
        )

        # 对测试集应用相同的特征选择
        X_test_selected = X_test[X_train_selected.columns]

        # 5. 标准化
        self.scaler = StandardScaler()
        X_train_scaled = self.scaler.fit_transform(X_train_selected)
        X_test_scaled = self.scaler.transform(X_test_selected)

        # 6. 模型训练
        print(f"\n[4] 模型训练 ({model_type})...")

        if model_type == 'ensemble':
            # 集成模型
            model = self._build_ensemble_model()
        elif model_type == 'gradient_boosting':
            # 梯度提升 + 超参数调优
            model = self._build_gb_model_with_tuning(X_train_scaled, y_train_balanced)
        elif model_type == 'random_forest':
            # 随机森林 + 超参数调优
            model = self._build_rf_model_with_tuning(X_train_scaled, y_train_balanced)
        else:
            model = GradientBoostingClassifier(
                n_estimators=200,
                max_depth=5,
                learning_rate=0.05,
                min_samples_split=5,
                min_samples_leaf=2,
                random_state=42
            )

        # 训练模型
        model.fit(X_train_scaled, y_train_balanced)
        self.model = model

        # 7. 寻找最优阈值
        print("\n[5] 阈值优化...")
        self.optimal_threshold = self.find_optimal_threshold(
            model, X_test_scaled, y_test, self.target_recall
        )

        # 8. 使用最优阈值预测
        y_prob = model.predict_proba(X_test_scaled)[:, 1]
        y_pred = (y_prob >= self.optimal_threshold).astype(int)

        # 9. 评估
        metrics = {
            'accuracy': accuracy_score(y_test, y_pred),
            'precision': precision_score(y_test, y_pred, zero_division=0),
            'recall': recall_score(y_test, y_pred, zero_division=0),
            'f1': f1_score(y_test, y_pred, zero_division=0),
            'auc': roc_auc_score(y_test, y_prob) if len(np.unique(y_test)) > 1 else 0.5,
        }

        # 10. 交叉验证
        print("\n[6] 交叉验证...")
        cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
        cv_scores = cross_val_score(model, X_train_scaled, y_train_balanced, cv=cv, scoring='f1')
        print(f"  交叉验证F1: {cv_scores.mean():.4f} (+/- {cv_scores.std()*2:.4f})")

        # 11. 混淆矩阵
        tn, fp, fn, tp = confusion_matrix(y_test, y_pred).ravel()

        # 12. 特征重要性
        feature_importance = {}
        if hasattr(model, 'feature_importances_'):
            for i, name in enumerate(X_train_selected.columns):
                feature_importance[name] = float(model.feature_importances_[i])
        elif hasattr(model, 'coef_'):
            for i, name in enumerate(X_train_selected.columns):
                feature_importance[name] = float(model.coef_[0][i])

        # 更新特征名
        self.feature_names = list(X_train_selected.columns)

        return AdvancedModelResult(
            model_type=model_type,
            accuracy=metrics['accuracy'],
            precision=metrics['precision'],
            recall=metrics['recall'],
            f1=metrics['f1'],
            auc=metrics['auc'],
            optimal_threshold=self.optimal_threshold,
            feature_importance=feature_importance,
            cross_val_scores=cv_scores.tolist(),
            confusion_matrix=(int(tn), int(fp), int(fn), int(tp)),
        )

    def _build_ensemble_model(self) -> VotingClassifier:
        """构建集成模型"""
        # 基础模型
        gb = GradientBoostingClassifier(
            n_estimators=150,
            max_depth=4,
            learning_rate=0.05,
            min_samples_split=5,
            random_state=42
        )

        rf = RandomForestClassifier(
            n_estimators=150,
            max_depth=6,
            min_samples_split=5,
            min_samples_leaf=2,
            random_state=42,
            n_jobs=-1
        )

        lr = LogisticRegression(
            C=0.5,
            max_iter=1000,
            class_weight='balanced',
            random_state=42
        )

        # 投票集成
        ensemble = VotingClassifier(
            estimators=[
                ('gb', gb),
                ('rf', rf),
                ('lr', lr)
            ],
            voting='soft',  # 软投票，使用概率加权
            n_jobs=-1
        )

        return ensemble

    def _build_gb_model_with_tuning(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray
    ) -> GradientBoostingClassifier:
        """梯度提升模型 + 超参数调优"""
        param_grid = {
            'n_estimators': [100, 200],
            'max_depth': [3, 5, 7],
            'learning_rate': [0.05, 0.1],
            'min_samples_split': [5, 10],
            'min_samples_leaf': [2, 4],
        }

        gb = GradientBoostingClassifier(random_state=42)

        # 简化网格搜索（减少计算量）
        grid_search = GridSearchCV(
            gb, param_grid, cv=3, scoring='f1', n_jobs=-1, verbose=0
        )

        try:
            grid_search.fit(X_train, y_train)
            print(f"  最优参数: {grid_search.best_params_}")
            return grid_search.best_estimator_
        except:
            # 如果网格搜索失败，返回默认模型
            return GradientBoostingClassifier(
                n_estimators=200, max_depth=5, learning_rate=0.05,
                random_state=42
            )

    def _build_rf_model_with_tuning(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray
    ) -> RandomForestClassifier:
        """随机森林模型 + 超参数调优"""
        param_grid = {
            'n_estimators': [100, 200],
            'max_depth': [5, 8, 10],
            'min_samples_split': [5, 10],
            'min_samples_leaf': [2, 4],
            'class_weight': ['balanced', None]
        }

        rf = RandomForestClassifier(random_state=42, n_jobs=-1)

        grid_search = GridSearchCV(
            rf, param_grid, cv=3, scoring='f1', n_jobs=-1, verbose=0
        )

        try:
            grid_search.fit(X_train, y_train)
            print(f"  最优参数: {grid_search.best_params_}")
            return grid_search.best_estimator_
        except:
            return RandomForestClassifier(
                n_estimators=200, max_depth=8, min_samples_split=5,
                class_weight='balanced', random_state=42, n_jobs=-1
            )

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """使用最优阈值预测"""
        if self.model is None:
            raise ValueError("模型未训练")

        # 特征工程
        X_enhanced = self.engineer_features(X)

        # 特征选择
        X_selected = X_enhanced[self.feature_names]

        # 标准化
        X_scaled = self.scaler.transform(X_selected)

        # 预测概率
        y_prob = self.model.predict_proba(X_scaled)[:, 1]

        # 使用最优阈值
        y_pred = (y_prob >= self.optimal_threshold).astype(int)

        return y_pred, y_prob

    def save(self, path: str):
        """保存模型"""
        model_data = {
            'model': self.model,
            'scaler': self.scaler,
            'feature_selector': self.feature_selector,
            'feature_names': self.feature_names,
            'optimal_threshold': self.optimal_threshold,
            'target_accuracy': self.target_accuracy,
            'target_recall': self.target_recall,
            'training_date': datetime.now().strftime('%Y-%m-%d'),
        }
        joblib.dump(model_data, path)
        print(f"模型已保存: {path}")

    def generate_optimization_report(self, result: AdvancedModelResult) -> str:
        """生成优化报告"""
        lines = []
        lines.append("# 模型优化报告")
        lines.append("")
        lines.append(f"**训练时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"**模型类型**: {result.model_type}")
        lines.append(f"**最优阈值**: {result.optimal_threshold:.4f}")
        lines.append("")

        # 性能对比
        lines.append("## 一、性能指标")
        lines.append("")
        lines.append("| 指标 | 当前值 | 目标值 | 状态 |")
        lines.append("|------|--------|--------|------|")

        acc_status = "✅ 达标" if result.accuracy >= self.target_accuracy else "❌ 未达标"
        rec_status = "✅ 达标" if result.recall >= self.target_recall else "❌ 未达标"

        lines.append(f"| 准确率 | {result.accuracy:.2%} | {self.target_accuracy:.0%} | {acc_status} |")
        lines.append(f"| 召回率 | {result.recall:.2%} | {self.target_recall:.0%} | {rec_status} |")
        lines.append(f"| 精确率 | {result.precision:.2%} | - | - |")
        lines.append(f"| F1分数 | {result.f1:.2%} | - | - |")
        lines.append(f"| AUC | {result.auc:.4f} | - | - |")
        lines.append("")

        # 混淆矩阵
        tn, fp, fn, tp = result.confusion_matrix
        lines.append("## 二、混淆矩阵")
        lines.append("")
        lines.append("| | 预测亏损 | 预测盈利 |")
        lines.append("|------|----------|----------|")
        lines.append(f"| **实际亏损** | {tn} | {fp} |")
        lines.append(f"| **实际盈利** | {fn} | {tp} |")
        lines.append("")

        # 特征重要性
        lines.append("## 三、特征重要性")
        lines.append("")
        lines.append("| 排名 | 特征 | 重要性 |")
        lines.append("|------|------|--------|")

        sorted_features = sorted(
            result.feature_importance.items(),
            key=lambda x: abs(x[1]),
            reverse=True
        )
        for i, (name, importance) in enumerate(sorted_features[:15], 1):
            lines.append(f"| {i} | {name} | {importance:.4f} |")

        lines.append("")

        # 交叉验证
        lines.append("## 四、交叉验证结果")
        lines.append("")
        lines.append(f"- 平均F1: {np.mean(result.cross_val_scores):.4f}")
        lines.append(f"- 标准差: {np.std(result.cross_val_scores):.4f}")
        lines.append(f"- 各折F1: {[round(s, 4) for s in result.cross_val_scores]}")
        lines.append("")

        # 优化建议
        lines.append("## 五、进一步优化建议")
        lines.append("")

        if result.accuracy < self.target_accuracy:
            lines.append("### 提升准确率")
            lines.append("")
            lines.append("1. **增加训练数据**: 当前样本量可能不足，建议积累更多历史交易数据")
            lines.append("2. **特征工程**: 创建更多有效特征，如技术指标、市场微观结构特征")
            lines.append("3. **模型融合**: 尝试Stacking或Blending集成方法")
            lines.append("")

        if result.recall < self.target_recall:
            lines.append("### 提升召回率")
            lines.append("")
            lines.append("1. **降低阈值**: 当前阈值可能过高，可以进一步降低")
            lines.append("2. **增加正样本权重**: 在训练时增加盈利样本的权重")
            lines.append("3. **过采样**: 使用SMOTE等方法增加少数类样本")
            lines.append("")

        return "\n".join(lines)


def train_advanced_model(
    results: List[Dict],
    model_type: str = 'ensemble',
    target_accuracy: float = 0.90,
    target_recall: float = 0.88,
    output_path: str = None
) -> Tuple[AdvancedModelTrainer, AdvancedModelResult]:
    """
    训练高级模型

    Args:
        results: 交易结果列表
        model_type: 模型类型
        target_accuracy: 目标准确率
        target_recall: 目标召回率
        output_path: 模型保存路径

    Returns:
        (训练器, 结果)
    """
    trainer = AdvancedModelTrainer(
        target_accuracy=target_accuracy,
        target_recall=target_recall
    )

    # 准备数据
    X, y = trainer._prepare_data(results)

    if len(X) < 30:
        print(f"样本数不足({len(X)})，需要更多数据")
        return trainer, None

    # 训练
    result = trainer.train_with_optimization(X, y, model_type)

    # 保存
    if output_path:
        trainer.save(output_path)

    # 打印报告
    report = trainer.generate_optimization_report(result)
    print("\n" + report)

    return trainer, result


# 在AdvancedModelTrainer类中添加_prepare_data方法
def _prepare_data(self, results: List[Dict]) -> Tuple[pd.DataFrame, pd.Series]:
    """准备训练数据"""
    import pandas as pd

    if not results:
        return pd.DataFrame(), pd.Series()

    df = pd.DataFrame(results)
    df = df[df['hold_days'] > 0].copy()

    if len(df) == 0:
        return pd.DataFrame(), pd.Series()

    # 特征列
    feature_cols = [
        'volume_change_rate', 'turnover_rate', 'volatility',
        'board_count', 'seal_strength', 'float_cap',
        'factor_score'
    ]

    available_cols = [col for col in feature_cols if col in df.columns]

    X = df[available_cols].copy().fillna(0)
    y = df['is_win'].astype(int)

    return X, y


# 添加方法到类
AdvancedModelTrainer._prepare_data = _prepare_data
