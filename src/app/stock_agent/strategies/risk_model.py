"""
机器学习风险评分模型

使用历史交易数据训练模型，预测每笔交易的风险等级
"""
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import pickle

try:
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.model_selection import train_test_split, cross_val_score
    from sklearn.metrics import classification_report, confusion_matrix, roc_auc_score
    from sklearn.preprocessing import StandardScaler
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False


@dataclass
class RiskFeatures:
    """风险特征"""
    # 连板相关
    board_count: int           # 连板数
    is_board_2: int           # 是否2板
    is_board_3: int           # 是否3板

    # 价格相关
    gain_5d: float            # 5日涨幅
    gain_10d: float           # 10日涨幅
    gain_from_low: float      # 从近期低点涨幅

    # 成交相关
    amount_ratio: float       # 成交额/前一日比例
    amount_ma5_ratio: float   # 成交额/5日均量比例

    # 封板相关
    seal_strength: float      # 封板强度
    yizi_count: int          # 连续一字板数

    # 市值相关
    float_cap: float         # 流通市值
    is_small_cap: int        # 是否小盘股(<30亿)

    # 市场情绪
    sentiment_score: float   # 市场情绪分
    sentiment_phase: str     # 情绪阶段

    # 断板重启
    has_break: int           # 是否断板重启
    break_days: int          # 断板天数


class RiskModel:
    """
    机器学习风险评分模型

    输出：
    - 风险评分：0-100分
    - 风险等级：低风险/中风险/高风险
    - 风险因子贡献度
    """

    def __init__(self, model_path: str = None):
        self.model = None
        self.scaler = None
        self.feature_names = []
        self.model_path = model_path or "outputs/risk_model.pkl"

        if SKLEARN_AVAILABLE:
            self.model = GradientBoostingClassifier(
                n_estimators=100,
                max_depth=5,
                learning_rate=0.1,
                random_state=42
            )
            self.scaler = StandardScaler()

    def extract_features(self, trade: Dict, df_history: pd.DataFrame = None) -> RiskFeatures:
        """从交易记录和股票历史数据提取风险特征"""
        code = trade.get('code', '')

        # 基础特征
        board_count = trade.get('board_count', 2)
        float_cap = trade.get('float_cap', 50)
        sentiment_score = trade.get('sentiment_score', 50)

        # 计算涨幅相关特征
        gain_5d = 0
        gain_10d = 0
        gain_from_low = 0
        amount_ratio = 1
        amount_ma5_ratio = 1
        seal_strength = trade.get('seal_strength', 1.0)
        yizi_count = 0
        has_break = 0
        break_days = 0

        if df_history is not None and len(df_history) >= 10:
            close = df_history.iloc[-1]['close']

            # 5日涨幅
            if len(df_history) >= 6:
                price_5d = df_history.iloc[-6]['close']
                gain_5d = (close - price_5d) / price_5d * 100

            # 10日涨幅
            if len(df_history) >= 11:
                price_10d = df_history.iloc[-11]['close']
                gain_10d = (close - price_10d) / price_10d * 100

            # 从近期低点涨幅
            low_20d = df_history.iloc[-20:]['low'].min() if len(df_history) >= 20 else df_history['low'].min()
            gain_from_low = (close - low_20d) / low_20d * 100

            # 成交额比例
            if len(df_history) >= 2:
                today_amount = df_history.iloc[-1]['amount']
                yesterday_amount = df_history.iloc[-2]['amount']
                if yesterday_amount > 0:
                    amount_ratio = today_amount / yesterday_amount

            # 成交额/5日均量
            if len(df_history) >= 6:
                today_amount = df_history.iloc[-1]['amount']
                ma5_amount = df_history.iloc[-6:-1]['amount'].mean()
                if ma5_amount > 0:
                    amount_ma5_ratio = today_amount / ma5_amount

            # 一字板统计
            limit_pct = 20 if code.startswith('30') or code.startswith('68') else 10
            yizi_count = 0
            for i in range(-1, -min(board_count + 1, len(df_history)), -1):
                t = df_history.iloc[i]
                if t['open'] == t['close'] == t['high'] == t['low']:
                    yizi_count += 1

            # 检测断板重启
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
                            # 计算断板天数
                            break_days = i - current_streak
                            break

        return RiskFeatures(
            board_count=board_count,
            is_board_2=1 if board_count == 2 else 0,
            is_board_3=1 if board_count == 3 else 0,
            gain_5d=gain_5d,
            gain_10d=gain_10d,
            gain_from_low=gain_from_low,
            amount_ratio=amount_ratio,
            amount_ma5_ratio=amount_ma5_ratio,
            seal_strength=seal_strength,
            yizi_count=yizi_count,
            float_cap=float_cap,
            is_small_cap=1 if float_cap < 30 else 0,
            sentiment_score=sentiment_score,
            sentiment_phase=trade.get('sentiment_phase', '发酵'),
            has_break=has_break,
            break_days=break_days
        )

    def features_to_array(self, features: RiskFeatures) -> np.ndarray:
        """将特征转换为数组"""
        phase_map = {'冰点': 0, '启动': 1, '发酵': 2, '高潮': 3, '退潮': 4}

        return np.array([
            features.board_count,
            features.is_board_2,
            features.is_board_3,
            features.gain_5d,
            features.gain_10d,
            features.gain_from_low,
            features.amount_ratio,
            features.amount_ma5_ratio,
            features.seal_strength,
            features.yizi_count,
            features.float_cap,
            features.is_small_cap,
            features.sentiment_score,
            phase_map.get(features.sentiment_phase, 2),
            features.has_break,
            features.break_days,
        ])

    def train(self, trades: List[Dict], df_history_map: Dict[str, pd.DataFrame] = None) -> Dict:
        """
        训练风险模型

        Args:
            trades: 历史交易记录列表
            df_history_map: 股票代码到历史数据的映射

        Returns:
            训练结果统计
        """
        if not SKLEARN_AVAILABLE:
            return {'error': 'sklearn not available'}

        X = []
        y = []  # 1=亏损, 0=盈利

        for trade in trades:
            code = trade.get('code', '')
            df_history = df_history_map.get(code) if df_history_map else None

            features = self.extract_features(trade, df_history)
            X.append(self.features_to_array(features))

            # 标签：亏损=1, 盈利=0
            profit = trade.get('profit_pct', trade.get('profit', 0))
            y.append(1 if profit < 0 else 0)

        X = np.array(X)
        y = np.array(y)

        self.feature_names = [
            'board_count', 'is_board_2', 'is_board_3',
            'gain_5d', 'gain_10d', 'gain_from_low',
            'amount_ratio', 'amount_ma5_ratio', 'seal_strength',
            'yizi_count', 'float_cap', 'is_small_cap',
            'sentiment_score', 'sentiment_phase', 'has_break', 'break_days'
        ]

        # 划分训练集和测试集
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )

        # 标准化
        self.scaler.fit(X_train)
        X_train = self.scaler.transform(X_train)
        X_test = self.scaler.transform(X_test)

        # 训练模型
        self.model.fit(X_train, y_train)

        # 评估
        train_score = self.model.score(X_train, y_train)
        test_score = self.model.score(X_test, y_test)

        y_pred = self.model.predict(X_test)
        y_prob = self.model.predict_proba(X_test)[:, 1]

        report = classification_report(y_test, y_pred, output_dict=True)
        auc = roc_auc_score(y_test, y_prob) if len(np.unique(y_test)) > 1 else 0

        # 特征重要性
        feature_importance = dict(zip(self.feature_names, self.model.feature_importances_))
        sorted_importance = sorted(feature_importance.items(), key=lambda x: x[1], reverse=True)

        return {
            'train_score': train_score,
            'test_score': test_score,
            'auc': auc,
            'classification_report': report,
            'feature_importance': sorted_importance,
            'total_samples': len(trades),
            'loss_samples': sum(y),
            'win_samples': len(y) - sum(y),
        }

    def predict_risk(self, trade: Dict, df_history: pd.DataFrame = None) -> Dict:
        """
        预测交易风险

        Returns:
            risk_score: 0-100分
            risk_level: 低风险/中风险/高风险
            loss_probability: 亏损概率
            risk_factors: 风险因子列表
        """
        # 检查模型和scaler是否已训练
        if not SKLEARN_AVAILABLE or self.model is None:
            return self._rule_based_risk(trade, df_history)

        # 检查scaler是否已fit
        try:
            check_is_fitted = getattr(self.scaler, 'n_features_in_', None)
            if check_is_fitted is None:
                return self._rule_based_risk(trade, df_history)
        except:
            return self._rule_based_risk(trade, df_history)

        # 提取龙头策略专用特征
        features = self.extract_features(trade, df_history)
        X = self.features_to_array(features).reshape(1, -1)

        try:
            X_scaled = self.scaler.transform(X)

            # 预测亏损概率
            loss_prob = self.model.predict_proba(X_scaled)[0, 1]

            # 转换为风险评分 (0-100)
            risk_score = int(loss_prob * 100)

            # 确定风险等级
            if risk_score < 20:
                risk_level = '低风险'
            elif risk_score < 40:
                risk_level = '中风险'
            else:
                risk_level = '高风险'

            # 提取主要风险因子
            risk_factors = self._get_risk_factors(features)

            return {
                'risk_score': risk_score,
                'risk_level': risk_level,
                'loss_probability': loss_prob,
                'risk_factors': risk_factors,
            }
        except Exception as e:
            return self._rule_based_risk(trade, df_history)

    def _rule_based_risk(self, trade: Dict, df_history: pd.DataFrame = None) -> Dict:
        """规则评分作为后备方案"""
        risk_score = 0
        risk_factors = []

        board_count = trade.get('board_count', 2)
        float_cap = trade.get('float_cap', 50)

        # 断板重启
        if df_history is not None:
            features = self.extract_features(trade, df_history)
            if features.has_break:
                risk_score += 25
                risk_factors.append('断板重启')

            # 5日涨幅
            if features.gain_5d > 50:
                risk_score += 25
                risk_factors.append(f'5日涨幅{features.gain_5d:.0f}%')
            elif features.gain_5d > 30:
                risk_score += 15
                risk_factors.append(f'5日涨幅{features.gain_5d:.0f}%')

            # 一字板
            if features.yizi_count >= 2:
                risk_score += 10
                risk_factors.append(f'一字板{features.yizi_count}个')

            # 成交额放大
            if features.amount_ma5_ratio > 2:
                risk_score += 10
                risk_factors.append('成交额放大')

        # 小市值
        if float_cap < 30:
            risk_score += 10
            risk_factors.append(f'小市值{float_cap:.0f}亿')

        # 3板
        if board_count == 3:
            risk_score += 5
            risk_factors.append('3板')

        if risk_score < 20:
            risk_level = '低风险'
        elif risk_score < 40:
            risk_level = '中风险'
        else:
            risk_level = '高风险'

        return {
            'risk_score': risk_score,
            'risk_level': risk_level,
            'loss_probability': risk_score / 100,
            'risk_factors': risk_factors,
        }

    def _get_risk_factors(self, features: RiskFeatures) -> List[str]:
        """提取主要风险因子"""
        factors = []

        if features.has_break:
            factors.append('断板重启')
        if features.gain_5d > 50:
            factors.append(f'5日涨幅{features.gain_5d:.0f}%')
        elif features.gain_5d > 30:
            factors.append(f'5日涨幅{features.gain_5d:.0f}%')
        if features.yizi_count >= 2:
            factors.append(f'一字板{features.yizi_count}个')
        if features.amount_ma5_ratio > 2:
            factors.append('成交额放大')
        if features.is_small_cap:
            factors.append(f'小市值{features.float_cap:.0f}亿')
        if features.board_count == 3:
            factors.append('3板')

        return factors

    def save(self, path: str = None):
        """保存模型"""
        path = path or self.model_path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        with open(path, 'wb') as f:
            pickle.dump({
                'model': self.model,
                'scaler': self.scaler,
                'feature_names': self.feature_names,
            }, f)

    def load(self, path: str = None):
        """加载模型"""
        path = path or self.model_path

        if not Path(path).exists():
            return False

        # 根据文件扩展名选择加载方式
        if path.endswith('.joblib'):
            import joblib
            data = joblib.load(path)

            # 处理字典格式的模型
            if isinstance(data, dict):
                self.model = data.get('model')
                self.scaler = data.get('scaler')
                self.feature_names = data.get('features', [])
                self.model_info = {
                    'training_date': data.get('training_date'),
                    'accuracy': data.get('accuracy'),
                    'auc_score': data.get('auc_score'),
                    'samples_count': data.get('samples_count'),
                }
                print(f"[信息] 加载ML模型成功: accuracy={data.get('accuracy', 0):.2%}, AUC={data.get('auc_score', 0):.3f}")
            elif hasattr(data, 'predict_proba'):
                self.model = data
                if self.scaler is None and SKLEARN_AVAILABLE:
                    self.scaler = StandardScaler()
            return True
        else:
            with open(path, 'rb') as f:
                data = pickle.load(f)
                self.model = data['model']
                self.scaler = data['scaler']
                self.feature_names = data['feature_names']

        return True


def generate_risk_report(train_result: Dict, trades_by_risk: Dict) -> str:
    """生成风险模型分析报告"""
    lines = []

    lines.append("=" * 60)
    lines.append("机器学习风险评分模型报告")
    lines.append("=" * 60)
    lines.append("")

    # 一、训练数据统计
    lines.append("## 一、训练数据统计")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总样本数 | {train_result.get('total_samples', 0)} |")
    lines.append(f"| 盈利样本 | {train_result.get('win_samples', 0)} |")
    lines.append(f"| 亏损样本 | {train_result.get('loss_samples', 0)} |")
    lines.append(f"| 亏损比例 | {train_result.get('loss_samples', 0) / max(train_result.get('total_samples', 1), 1) * 100:.1f}% |")
    lines.append("")

    # 二、模型性能
    lines.append("## 二、模型性能")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 训练集准确率 | {train_result.get('train_score', 0):.2%} |")
    lines.append(f"| 测试集准确率 | {train_result.get('test_score', 0):.2%} |")
    lines.append(f"| AUC | {train_result.get('auc', 0):.3f} |")
    lines.append("")

    # 三、特征重要性
    lines.append("## 三、特征重要性（Top 10）")
    lines.append("")
    lines.append("| 排名 | 特征 | 重要性 |")
    lines.append("|------|------|--------|")

    importance = train_result.get('feature_importance', [])
    for i, (name, score) in enumerate(importance[:10], 1):
        lines.append(f"| {i} | {name} | {score:.4f} |")

    lines.append("")

    # 四、各风险等级表现
    if trades_by_risk:
        lines.append("## 四、各风险等级实际表现")
        lines.append("")
        lines.append("| 风险等级 | 交易次数 | 胜率 | 平均收益 | 总收益 |")
        lines.append("|----------|----------|------|----------|--------|")

        for risk in ['低风险', '中风险', '高风险']:
            if risk in trades_by_risk:
                stats = trades_by_risk[risk]
                if stats['count'] > 0:
                    wr = stats['wins'] / stats['count'] * 100
                    avg = stats['profit'] / stats['count']
                    lines.append(f"| **{risk}** | {stats['count']} | {wr:.1f}% | {avg:+.2f}% | {stats['profit']:+.2f}% |")

        lines.append("")

    # 五、模型说明
    lines.append("## 五、模型说明")
    lines.append("")
    lines.append("**模型类型**：梯度提升树 (Gradient Boosting)")
    lines.append("")
    lines.append("**风险评分**：基于模型预测的亏损概率转换为0-100分")
    lines.append("")
    lines.append("**风险等级划分**：")
    lines.append("- 低风险：0-29分（亏损概率<30%）")
    lines.append("- 中风险：30-49分（亏损概率30%-50%）")
    lines.append("- 高风险：50-100分（亏损概率>=50%）")
    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)
