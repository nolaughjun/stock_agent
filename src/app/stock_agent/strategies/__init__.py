"""
选股策略模块
"""
from .base_strategy import BaseStrategy, Signal, SignalType
try:
    from .advanced_ml_strategy import AdvancedMLStrategy, TradeManager
except ImportError:
    AdvancedMLStrategy = None
    TradeManager = None

__all__ = [
    'BaseStrategy', 'Signal', 'SignalType',
    'AdvancedMLStrategy', 'TradeManager'
]
