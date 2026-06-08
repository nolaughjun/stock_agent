"""
配置文件 - 系统参数设置
"""
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

import yaml


@dataclass
class TradingConfig:
    """交易配置"""
    buy_amount: float = 10000.0  # 每次买入金额（元）
    buy_interval_minutes: int = 30  # 买入间隔（分钟）
    sell_days: int = 5  # 持有天数
    sell_interval_minutes: int = 30  # 卖出间隔（分钟）
    min_annual_return: float = 0.30  # 最小年化收益目标（30%）


@dataclass
class DataConfig:
    """数据配置"""
    # 通达信数据路径（需要用户配置）
    tdx_data_path: Optional[str] = None
    # 同花顺数据路径
    ths_data_path: Optional[str] = None
    # 使用在线数据源（如akshare）
    use_online_data: bool = True
    # 数据缓存路径
    cache_path: str = "data/stock_cache"


@dataclass
class StrategyConfig:
    """策略配置"""
    # 买入策略参数
    ma_short: int = 5  # 短期均线
    ma_mid: int = 10  # 中期均线
    ma_long: int = 20  # 长期均线
    volume_ratio: float = 1.5  # 量比阈值
    price_change_threshold: float = 0.03  # 价格变化阈值


@dataclass
class Config:
    """主配置"""
    trading: TradingConfig = None
    data: DataConfig = None
    strategy: StrategyConfig = None

    def __post_init__(self):
        if self.trading is None:
            self.trading = TradingConfig()
        if self.data is None:
            self.data = DataConfig()
        if self.strategy is None:
            self.strategy = StrategyConfig()


def load_config_from_yaml(config_path: str = None) -> Config:
    """从YAML文件加载配置"""
    if config_path is None:
        config_path = Path(__file__).parent / "config.yaml"

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            yaml_config = yaml.safe_load(f)
    except Exception:
        return Config()

    if not yaml_config:
        return Config()

    # 解析配置
    data_cfg = yaml_config.get('data', {})
    trading_cfg = yaml_config.get('trading', {})

    return Config(
        trading=TradingConfig(
            buy_amount=trading_cfg.get('buy_amount', 10000.0),
            buy_interval_minutes=trading_cfg.get('buy_interval_minutes', 30),
            sell_days=trading_cfg.get('sell_days', 5),
            sell_interval_minutes=trading_cfg.get('sell_interval_minutes', 30),
            min_annual_return=trading_cfg.get('min_annual_return', 0.30),
        ),
        data=DataConfig(
            tdx_data_path=data_cfg.get('tdx_data_path'),
            ths_data_path=data_cfg.get('ths_data_path'),
            use_online_data=data_cfg.get('use_online_data', True),
            cache_path=data_cfg.get('cache_path', 'data/stock_cache'),
        ),
        strategy=StrategyConfig(),
    )


# 全局配置实例
config = load_config_from_yaml()
