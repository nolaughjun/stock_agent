"""
基础策略类
定义策略接口和通用方法
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

import pandas as pd
import numpy as np


class SignalType(Enum):
    """信号类型"""
    BUY = "buy"      # 买入信号
    SELL = "sell"    # 卖出信号
    HOLD = "hold"    # 持有/观望
    NONE = "none"    # 无信号


@dataclass
class Signal:
    """交易信号"""
    type: SignalType
    date: datetime
    price: float
    confidence: float = 1.0  # 信号强度 (0-1)
    reason: str = ""         # 信号原因


class BaseStrategy(ABC):
    """基础策略类"""

    def __init__(self, name: str = "BaseStrategy"):
        """初始化策略"""
        self.name = name
        self.params = {}

    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> list[Signal]:
        """
        生成交易信号

        Args:
            data: 股票数据DataFrame

        Returns:
            信号列表
        """
        pass

    def find_buy_signals(self, data: pd.DataFrame) -> list[Signal]:
        """
        查找所有买入信号（买一点）

        Args:
            data: 股票数据DataFrame

        Returns:
            买入信号列表
        """
        signals = self.generate_signals(data)
        return [s for s in signals if s.type == SignalType.BUY]

    @staticmethod
    def calculate_ma(data: pd.DataFrame, period: int, column: str = 'close') -> pd.Series:
        """计算移动平均线"""
        return data[column].rolling(window=period).mean()

    @staticmethod
    def calculate_ema(data: pd.DataFrame, period: int, column: str = 'close') -> pd.Series:
        """计算指数移动平均线"""
        return data[column].ewm(span=period, adjust=False).mean()

    @staticmethod
    def calculate_macd(
        data: pd.DataFrame,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        column: str = 'close'
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        计算MACD指标

        Returns:
            (DIF, DEA, MACD柱)
        """
        ema_fast = data[column].ewm(span=fast, adjust=False).mean()
        ema_slow = data[column].ewm(span=slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=signal, adjust=False).mean()
        macd = (dif - dea) * 2
        return dif, dea, macd

    @staticmethod
    def calculate_kdj(
        data: pd.DataFrame,
        n: int = 9,
        m1: int = 3,
        m2: int = 3
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        计算KDJ指标

        Returns:
            (K, D, J)
        """
        low_n = data['low'].rolling(window=n).min()
        high_n = data['high'].rolling(window=n).max()

        rsv = (data['close'] - low_n) / (high_n - low_n + 1e-10) * 100

        k = rsv.ewm(com=m1 - 1, adjust=False).mean()
        d = k.ewm(com=m2 - 1, adjust=False).mean()
        j = 3 * k - 2 * d

        return k, d, j

    @staticmethod
    def calculate_rsi(data: pd.DataFrame, period: int = 14, column: str = 'close') -> pd.Series:
        """计算RSI指标"""
        delta = data[column].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()

        rs = gain / (loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi

    @staticmethod
    def calculate_bollinger_bands(
        data: pd.DataFrame,
        period: int = 20,
        std_dev: float = 2.0,
        column: str = 'close'
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        计算布林带

        Returns:
            (上轨, 中轨, 下轨)
        """
        middle = data[column].rolling(window=period).mean()
        std = data[column].rolling(window=period).std()
        upper = middle + std_dev * std
        lower = middle - std_dev * std
        return upper, middle, lower

    @staticmethod
    def calculate_volume_ratio(data: pd.DataFrame, period: int = 5) -> pd.Series:
        """计算量比"""
        avg_volume = data['volume'].rolling(window=period).mean()
        return data['volume'] / (avg_volume + 1e-10)

    def validate_data(self, data: pd.DataFrame) -> bool:
        """验证数据是否满足策略要求"""
        required_columns = ['open', 'high', 'low', 'close', 'volume']
        return all(col in data.columns for col in required_columns)

    def set_params(self, **kwargs):
        """设置策略参数"""
        self.params.update(kwargs)

    def get_params(self) -> dict:
        """获取策略参数"""
        return self.params.copy()
