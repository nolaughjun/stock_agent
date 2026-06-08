"""
数据加载器
统一的数据加载接口，支持通达信本地数据

支持市场：
- 创业板 (Gem): 代码以300开头
- 沪A (ShA): 代码以600、601、603、605开头
- 深A (SzA): 代码以000、001、002、003开头
"""
from datetime import datetime
from typing import Optional, List
from enum import Enum
import os
import struct

import pandas as pd
import numpy as np


class MarketType(Enum):
    """市场类型"""
    GEM = "gem"      # 创业板
    SHA = "sha"      # 沪A
    SZA = "sza"      # 深A
    ALL = "all"      # 全部


class TdxReader:
    """通达信本地数据读取器"""

    # 市场代码前缀映射
    MARKET_PREFIX = {
        MarketType.GEM: ['30'],           # 创业板: 300xxx
        MarketType.SHA: ['60'],           # 沪A: 600xxx, 601xxx, 603xxx, 605xxx
        MarketType.SZA: ['00', '001', '002', '003'],  # 深A: 000xxx, 001xxx, 002xxx, 003xxx
    }

    def __init__(self, tdx_path: str = None):
        """
        初始化通达信读取器

        Args:
            tdx_path: 通达信安装路径
        """
        self.tdx_path = tdx_path or self._find_tdx_path()
        self.vipdoc_path = None
        if self.tdx_path:
            self.vipdoc_path = os.path.join(self.tdx_path, 'vipdoc')
        # 缓存流通股本数据
        self._float_shares_cache = None

    def _find_tdx_path(self) -> Optional[str]:
        """自动查找通达信安装路径"""
        common_paths = [
            r'E:\new_tdx',
            r'C:\new_tdx',
            r'D:\new_tdx',
            r'C:\tdx',
            r'D:\tdx',
            r'C:\Program Files\new_tdx',
            r'C:\Program Files (x86)\new_tdx',
        ]
        for path in common_paths:
            if os.path.exists(path):
                return path
        return None

    def get_market_by_code(self, code: str) -> str:
        """
        根据股票代码确定市场

        Args:
            code: 股票代码

        Returns:
            市场标识: 'sh' 或 'sz'
        """
        if code.startswith('6'):
            return 'sh'
        else:
            return 'sz'

    def get_market_type_by_code(self, code: str) -> MarketType:
        """
        根据股票代码确定市场类型

        Args:
            code: 股票代码

        Returns:
            MarketType 枚举值
        """
        if code.startswith('30'):
            return MarketType.GEM
        elif code.startswith('60'):
            return MarketType.SHA
        elif code.startswith('00'):
            return MarketType.SZA
        else:
            return MarketType.ALL

    def get_stock_history(self, code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        读取股票历史数据

        Args:
            code: 股票代码
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)

        Returns:
            DataFrame，包含open, high, low, close, volume等列
        """
        if not self.vipdoc_path:
            return pd.DataFrame()

        # 确定市场
        market = self.get_market_by_code(code)

        # 构建文件路径
        day_file = os.path.join(self.vipdoc_path, market, 'lday', f'{market}{code}.day')

        if not os.path.exists(day_file):
            return pd.DataFrame()

        # 读取日线数据
        try:
            data = []
            with open(day_file, 'rb') as f:
                while True:
                    chunk = f.read(32)
                    if len(chunk) < 32:
                        break

                    # 解析数据
                    date_int, open_price, high_price, low_price, close_price, \
                    amount, volume, reserved = struct.unpack('IIIIIfII', chunk)

                    date_str = str(date_int)
                    date = datetime.strptime(date_str, '%Y%m%d')

                    data.append({
                        'date': date,
                        'open': open_price / 100.0,
                        'high': high_price / 100.0,
                        'low': low_price / 100.0,
                        'close': close_price / 100.0,
                        'volume': volume,
                        'amount': amount,
                    })

            df = pd.DataFrame(data)
            df.set_index('date', inplace=True)
            df.sort_index(inplace=True)

            # 日期过滤
            if start_date:
                start_dt = pd.Timestamp(start_date)
                df = df[df.index >= start_dt]
            if end_date:
                end_dt = pd.Timestamp(end_date)
                df = df[df.index <= end_dt]

            return df

        except Exception as e:
            return pd.DataFrame()

    def get_stock_listing_date(self, code: str) -> Optional[datetime]:
        """
        根据通达信本地数据推断股票上市日期

        注意：这是基于本地数据的推断，可能不准确。
        对于代码重用的情况（如301xxx），可能返回错误的日期。

        Args:
            code: 股票代码

        Returns:
            推断的上市日期，如果无法确定则返回None
        """
        if not self.vipdoc_path:
            return None

        # 读取不带日期过滤的完整数据
        market = self.get_market_by_code(code)
        day_file = os.path.join(self.vipdoc_path, market, 'lday', f'{market}{code}.day')

        if not os.path.exists(day_file):
            return None

        try:
            with open(day_file, 'rb') as f:
                # 只读取第一条记录
                chunk = f.read(32)
                if len(chunk) < 32:
                    return None

                date_int = struct.unpack('I', chunk[:4])[0]
                date_str = str(date_int)
                return datetime.strptime(date_str, '%Y%m%d')
        except:
            return None

    def get_float_shares(self, code: str) -> Optional[float]:
        """
        获取股票流通股本（万股）

        从通达信 base.dbf 文件读取流通A股数量

        Args:
            code: 股票代码

        Returns:
            流通股本（万股），失败返回None
        """
        if not self.tdx_path:
            return None

        base_dbf = os.path.join(self.tdx_path, 'T0002', 'hq_cache', 'base.dbf')
        if not os.path.exists(base_dbf):
            return None

        try:
            with open(base_dbf, 'rb') as f:
                data = f.read()

            # 解析DBF头部
            header_size = int.from_bytes(data[8:10], 'little')
            record_size = int.from_bytes(data[10:12], 'little')

            # 搜索股票代码（格式：SC(1字节) + GPDM(6字节)）
            # 例如：'1' + '600110' = '1600110'
            search = ('1' + code).encode('gbk')
            pos = data.find(search)
            if pos < 0:
                return None

            # 计算记录起始位置
            data_start = header_size + 1
            record_num = (pos - data_start) // record_size
            record_start = data_start + record_num * record_size

            # 读取记录
            record = data[record_start:record_start + record_size]

            # 解析字段
            # 字段偏移量：SC(1) + GPDM(6) + GXRQ(8) + ZGB(14) + GJG(14) + FQRFRG(14) + FRG(14) + BG(14) + HG(14) + LTAG(14)
            # LTAG (流通A股) 的偏移量 = 1 + 6 + 8 + 14*6 = 99
            ltag_offset = 1 + 6 + 8 + 14 * 6
            ltag_len = 14

            ltag_str = record[ltag_offset:ltag_offset + ltag_len].decode('gbk', errors='ignore').strip()

            if ltag_str:
                return float(ltag_str)
            return None
        except:
            return None

    def _load_all_float_shares(self) -> dict:
        """
        一次性加载所有股票的流通股本数据（内部方法）

        Returns:
            {股票代码: 流通股本(万股)} 的映射
        """
        if self._float_shares_cache is not None:
            return self._float_shares_cache

        results = {}

        if not self.tdx_path:
            self._float_shares_cache = results
            return results

        base_dbf = os.path.join(self.tdx_path, 'T0002', 'hq_cache', 'base.dbf')
        if not os.path.exists(base_dbf):
            self._float_shares_cache = results
            return results

        try:
            with open(base_dbf, 'rb') as f:
                data = f.read()

            # 解析DBF头部
            header_size = int.from_bytes(data[8:10], 'little')
            record_size = int.from_bytes(data[10:12], 'little')
            record_count = int.from_bytes(data[4:8], 'little')
            data_start = header_size + 1

            # LTAG字段偏移量 = 1 + 6 + 8 + 14*6 = 99
            ltag_offset = 1 + 6 + 8 + 14 * 6
            ltag_len = 14

            # 遍历所有记录
            for i in range(record_count):
                record_start = data_start + i * record_size
                if record_start + ltag_offset + ltag_len > len(data):
                    break

                # 读取股票代码（SC + GPDM）
                code_bytes = data[record_start + 1:record_start + 7]
                try:
                    code = code_bytes.decode('gbk', errors='ignore').strip()
                    if not code or len(code) != 6:
                        continue

                    # 读取LTAG
                    ltag_bytes = data[record_start + ltag_offset:record_start + ltag_offset + ltag_len]
                    ltag_str = ltag_bytes.decode('gbk', errors='ignore').strip()
                    if ltag_str:
                        results[code] = float(ltag_str)
                except:
                    pass

        except:
            pass

        self._float_shares_cache = results
        return results

    def get_float_shares_batch(self, codes: list) -> dict:
        """
        批量获取股票流通股本（使用缓存优化）

        Args:
            codes: 股票代码列表

        Returns:
            {股票代码: 流通股本(万股)} 的映射
        """
        all_shares = self._load_all_float_shares()
        return {code: all_shares.get(code) for code in codes if code in all_shares}

    def is_stock_data_valid(self, code: str, data_start_date: str, min_listing_days: int = 60) -> tuple:
        """
        校验股票数据是否有效（防止历史遗留数据问题）

        检查规则：
        1. 301xxx代码的股票必须在2020-08-24之后上市（注册制改革）
        2. 股票数据的起始日期不能早于合理的上市日期
        3. 对于301xxx股票，额外检查数据起始日期是否异常

        注意：由于通达信本地数据可能存在代码重用问题，对于301xxx股票，
        建议用户核实实际上市日期。

        Args:
            code: 股票代码
            data_start_date: 数据使用的开始日期
            min_listing_days: 最小上市天数要求

        Returns:
            (is_valid, reason, warning) 元组，其中warning用于提示需要核实的信息
        """
        from datetime import datetime

        listing_date = self.get_stock_listing_date(code)
        if listing_date is None:
            return False, "无法获取上市日期", ""

        data_start = datetime.strptime(data_start_date, '%Y%m%d')
        warning = ""

        # 规则1: 301xxx代码必须在2020-08-24之后上市（注册制改革后发行）
        if code.startswith('301'):
            gem_reform_date = datetime(2020, 8, 24)
            if listing_date < gem_reform_date:
                return False, f"301股票数据起始日期{listing_date.strftime('%Y-%m-%d')}早于注册制改革日期2020-08-24，可能是历史遗留数据", ""

            # 规则1.1: 301xxx股票数据起始日期异常检测
            # 301xxx股票从2020年8月开始发行，但大量发行是在2021年后
            # 如果通达信数据显示的起始日期在2023年之前，可能存在代码重用问题
            # 因为很多301xxx股票是2023-2024年才上市的
            if listing_date < datetime(2023, 1, 1):
                warning = f"警告: 301股票数据显示起始日期为{listing_date.strftime('%Y-%m-%d')}，请核实实际上市日期"

        # 规则2: 检查上市天数
        days_since_listing = (data_start - listing_date).days
        if days_since_listing < min_listing_days:
            return False, f"上市天数不足（{days_since_listing}天 < {min_listing_days}天）", ""

        return True, f"有效（上市日期: {listing_date.strftime('%Y-%m-%d')}）", warning

    def get_stock_list(self, market_type: MarketType = MarketType.GEM) -> pd.DataFrame:
        """
        获取指定市场的股票列表

        Args:
            market_type: 市场类型 (GEM/SHA/SZA/ALL)

        Returns:
            DataFrame，包含code, name, market_type列
        """
        if not self.vipdoc_path:
            return pd.DataFrame(columns=['code', 'name', 'market_type'])

        stocks = []

        # 确定要扫描的市场（龙头策略不包含创业板）
        if market_type == MarketType.ALL:
            markets_to_scan = [MarketType.SHA, MarketType.SZA]  # 排除创业板
        else:
            markets_to_scan = [market_type]

        for mt in markets_to_scan:
            prefixes = self.MARKET_PREFIX.get(mt, [])

            for market in ['sz', 'sh']:
                lday_path = os.path.join(self.vipdoc_path, market, 'lday')
                if not os.path.exists(lday_path):
                    continue

                for filename in os.listdir(lday_path):
                    if not filename.endswith('.day'):
                        continue

                    # 提取股票代码
                    code = filename.replace(f'{market}', '').replace('.day', '')

                    # 检查是否符合该市场的前缀
                    for prefix in prefixes:
                        if code.startswith(prefix):
                            stocks.append({
                                'code': code,
                                'name': code,
                                'market_type': mt.value
                            })
                            break

        return pd.DataFrame(stocks)

    # 保留旧方法以兼容现有代码
    def get_gem_stock_list(self) -> pd.DataFrame:
        """获取创业板股票列表（兼容旧接口）"""
        return self.get_stock_list(MarketType.GEM)


class DataLoader:
    """数据加载器"""

    def __init__(self, tdx_path: str = None):
        """
        初始化数据加载器

        Args:
            tdx_path: 通达信安装路径
        """
        self.tdx_reader = TdxReader(tdx_path)

    def get_stock_history(self, code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取股票历史数据

        Args:
            code: 股票代码
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)

        Returns:
            DataFrame，包含open, high, low, close, volume等列
        """
        return self.tdx_reader.get_stock_history(code, start_date, end_date)

    def get_stock_list(self, market_type: MarketType = MarketType.GEM) -> pd.DataFrame:
        """
        获取指定市场的股票列表

        Args:
            market_type: 市场类型 (GEM/SHA/SZA/ALL)

        Returns:
            DataFrame，包含code, name, market_type列
        """
        return self.tdx_reader.get_stock_list(market_type)

    def get_stock_listing_date(self, code: str):
        """
        根据通达信本地数据推断股票上市日期

        Args:
            code: 股票代码

        Returns:
            推断的上市日期
        """
        return self.tdx_reader.get_stock_listing_date(code)

    def get_float_shares(self, code: str):
        """
        获取股票流通股本（万股）

        Args:
            code: 股票代码

        Returns:
            流通股本（万股），失败返回None
        """
        return self.tdx_reader.get_float_shares(code)

    def get_float_shares_batch(self, codes: list) -> dict:
        """
        批量获取股票流通股本

        Args:
            codes: 股票代码列表

        Returns:
            {股票代码: 流通股本(万股)} 的映射
        """
        return self.tdx_reader.get_float_shares_batch(codes)

    def is_stock_data_valid(self, code: str, data_start_date: str, min_listing_days: int = 60) -> tuple:
        """
        校验股票数据是否有效

        Args:
            code: 股票代码
            data_start_date: 数据使用的开始日期
            min_listing_days: 最小上市天数要求

        Returns:
            (is_valid, reason) 元组
        """
        return self.tdx_reader.is_stock_data_valid(code, data_start_date, min_listing_days)

    def get_gem_stock_list(self) -> pd.DataFrame:
        """获取创业板股票列表（兼容旧接口）"""
        return self.tdx_reader.get_stock_list(MarketType.GEM)

    def get_sha_stock_list(self) -> pd.DataFrame:
        """获取沪A股票列表"""
        return self.tdx_reader.get_stock_list(MarketType.SHA)

    def get_sza_stock_list(self) -> pd.DataFrame:
        """获取深A股票列表"""
        return self.tdx_reader.get_stock_list(MarketType.SZA)

    def get_all_stock_list(self) -> pd.DataFrame:
        """获取全部A股股票列表（创业板+沪A+深A）"""
        return self.tdx_reader.get_stock_list(MarketType.ALL)
