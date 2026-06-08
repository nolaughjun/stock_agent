"""
实时行情数据获取模块

支持的数据源：
1. 新浪财经 - 免费，稳定，实时性好
2. 东方财富 - 免费，数据全面
3. 腾讯财经 - 免费，备用

功能：
- 获取实时行情
- 获取分时数据
- 获取五档买卖盘
- 计算实时市场情绪
- 扫描涨停股/跌停股
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import re
import json
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from dataclasses import dataclass


def create_session_with_retry() -> requests.Session:
    """创建带重试机制的Session"""
    session = requests.Session()

    # 配置重试策略
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


@dataclass
class MarketSentiment:
    """市场情绪数据结构"""
    score: float                    # 情绪分数 0-100
    phase: str                      # 情绪阶段
    up_count: int                   # 上涨家数
    down_count: int                 # 下跌家数
    limit_up_count: int             # 涨停家数
    limit_down_count: int           # 跌停家数
    high_board_count: int           # 3连板以上家数
    breadth: float                  # 涨跌比
    limit_score: float              # 涨停跌停比
    timestamp: str                  # 更新时间
    top_gainers: List[Dict]         # 涨幅榜前10
    top_losers: List[Dict]          # 跌幅榜前10
    limit_up_stocks: List[Dict]     # 涨停股列表
    high_board_stocks: List[Dict]   # 高位连板股


class RealtimeQuote:
    """实时行情获取类"""

    def __init__(self):
        self.session = create_session_with_retry()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        })
        # 禁用代理
        self.session.trust_env = False

    def _get_market_code(self, code: str) -> str:
        """根据股票代码判断市场"""
        if code.startswith('6'):
            return f'sh{code}'
        elif code.startswith('0') or code.startswith('3'):
            return f'sz{code}'
        elif code.startswith('68'):
            return f'sh{code}'
        else:
            return f'sh{code}'

    def get_quote_sina(self, code: str) -> Dict:
        """
        新浪财经实时行情

        Args:
            code: 股票代码，如 '603206'

        Returns:
            实时行情字典
        """
        full_code = self._get_market_code(code)
        url = f'http://hq.sinajs.cn/list={full_code}'

        try:
            headers = {
                'Referer': 'http://finance.sina.com.cn',
            }
            response = self.session.get(url, headers=headers, timeout=5)
            response.encoding = 'gbk'

            data = response.text
            match = re.search(r'=\"(.*)\"', data)

            if match:
                items = match.group(1).split(',')
                if len(items) >= 33:
                    price = float(items[3])
                    prev_close = float(items[2])

                    return {
                        'code': code,
                        'name': items[0],
                        'open': float(items[1]),
                        'prev_close': prev_close,
                        'price': price,
                        'high': float(items[4]),
                        'low': float(items[5]),
                        'bid': float(items[6]),
                        'ask': float(items[7]),
                        'volume': int(items[8]),
                        'amount': float(items[9]),
                        'bid1_vol': int(items[10]),   # 买一量
                        'bid1': float(items[11]),      # 买一价
                        'bid2_vol': int(items[12]),
                        'bid2': float(items[13]),
                        'bid3_vol': int(items[14]),
                        'bid3': float(items[15]),
                        'bid4_vol': int(items[16]),
                        'bid4': float(items[17]),
                        'bid5_vol': int(items[18]),
                        'bid5': float(items[19]),
                        'ask1_vol': int(items[20]),    # 卖一量
                        'ask1': float(items[21]),       # 卖一价
                        'ask2_vol': int(items[22]),
                        'ask2': float(items[23]),
                        'ask3_vol': int(items[24]),
                        'ask3': float(items[25]),
                        'ask4_vol': int(items[26]),
                        'ask4': float(items[27]),
                        'ask5_vol': int(items[28]),
                        'ask5': float(items[29]),
                        'date': items[30],
                        'time': items[31],
                        'change': price - prev_close,
                        'change_pct': (price - prev_close) / prev_close * 100 if prev_close > 0 else 0,
                        'status': 'trading' if price > 0 else 'stopped',
                    }
        except Exception as e:
            return {'code': code, 'error': str(e)}

        return {'code': code, 'error': 'no data'}

    def get_quotes_batch(self, codes: List[str], n_workers: int = 8) -> Dict[str, Dict]:
        """
        批量获取实时行情（并发）

        Args:
            codes: 股票代码列表
            n_workers: 并发数

        Returns:
            股票代码 -> 行情字典 的映射
        """
        results = {}

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(self.get_quote_sina, code): code for code in codes}

            for future in as_completed(futures):
                code = futures[future]
                result = future.result()
                if 'error' not in result:
                    results[code] = result

        return results

    def get_minute_data_sina(self, code: str) -> pd.DataFrame:
        """
        获取分时数据（新浪）

        Args:
            code: 股票代码

        Returns:
            分时数据DataFrame
        """
        full_code = self._get_market_code(code)
        url = f'http://hq.sinajs.cn/list={full_code}_minutes'

        try:
            headers = {'Referer': 'http://finance.sina.com.cn'}
            response = self.session.get(url, headers=headers, timeout=5)
            response.encoding = 'gbk'

            # 解析分时数据
            data = response.text
            match = re.search(r'=\"(.*)\"', data)

            if match:
                content = match.group(1)
                lines = content.strip().split('\n')

                records = []
                for line in lines:
                    parts = line.split(',')
                    if len(parts) >= 3:
                        try:
                            records.append({
                                'time': parts[0],
                                'price': float(parts[1]),
                                'volume': int(parts[2]),
                                'amount': float(parts[3]) if len(parts) > 3 else 0,
                            })
                        except:
                            pass

                if records:
                    df = pd.DataFrame(records)
                    return df
        except Exception as e:
            pass

        return pd.DataFrame()

    def get_minute_data_eastmoney(self, code: str) -> pd.DataFrame:
        """
        获取分时数据（东方财富）

        Args:
            code: 股票代码

        Returns:
            分时数据DataFrame
        """
        # 判断市场
        if code.startswith('6'):
            secid = f'1.{code}'
        else:
            secid = f'0.{code}'

        url = f'http://push2his.eastmoney.com/api/qt/stock/trends2/get'
        params = {
            'secid': secid,
            'fields1': 'f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13',
            'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58',
            'iscr': '0',
        }

        try:
            response = self.session.get(url, params=params, timeout=5)
            data = response.json()

            if data.get('data') and data['data'].get('trends'):
                records = []
                for item in data['data']['trends']:
                    parts = item.split(',')
                    if len(parts) >= 8:
                        records.append({
                            'time': parts[0],
                            'open': float(parts[1]),
                            'close': float(parts[2]),
                            'high': float(parts[3]),
                            'low': float(parts[4]),
                            'volume': int(parts[5]),
                            'amount': float(parts[6]),
                            'avg_price': float(parts[7]) if len(parts) > 7 else 0,
                        })

                if records:
                    return pd.DataFrame(records)
        except Exception as e:
            pass

        return pd.DataFrame()

    def get_limit_up_stocks(self) -> List[Dict]:
        """
        获取当日涨停股列表

        Returns:
            涨停股列表
        """
        # 方案1：东方财富API
        url = 'http://push2.eastmoney.com/api/qt/clist/get'
        params = {
            'pn': 1,
            'pz': 100,
            'po': 1,
            'np': 1,
            'fltt': 2,
            'invt': 2,
            'fid': 'f3',  # 按涨幅排序
            'fs': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23',  # A股
            'fields': 'f1,f2,f3,f4,f5,f6,f7,f12,f14,f15,f16,f17,f18',
        }

        try:
            response = self.session.get(url, params=params, timeout=15)
            data = response.json()

            if data.get('data') and data['data'].get('diff'):
                results = []
                for item in data['data']['diff']:
                    pct = float(item.get('f3', 0))
                    if pct >= 9.5:  # 涨停
                        results.append({
                            'code': item.get('f12', ''),
                            'name': item.get('f14', ''),
                            'price': float(item.get('f2', 0)) / 100 if item.get('f2') else 0,
                            'change_pct': pct,
                            'volume': item.get('f5', 0),
                            'amount': float(item.get('f6', 0)) / 100000000 if item.get('f6') else 0,  # 亿
                            'high': float(item.get('f15', 0)) / 100 if item.get('f15') else 0,
                            'low': float(item.get('f16', 0)) / 100 if item.get('f16') else 0,
                            'open': float(item.get('f17', 0)) / 100 if item.get('f17') else 0,
                            'prev_close': float(item.get('f18', 0)) / 100 if item.get('f18') else 0,
                        })
                return results
        except Exception as e:
            print(f"东方财富API获取涨停股失败: {e}")

        # 方案2：使用新浪财经API（备用）
        try:
            # 新浪涨停榜
            url = 'http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData'
            params = {
                'page': 1,
                'num': 100,
                'sort': 'changepercent',
                'asc': 0,
                'node': 'hs_a',  # A股
                'symbol': '',
                '_s_r_a': 'page',
            }

            response = self.session.get(url, params=params, timeout=15)
            data = response.json()

            if data:
                results = []
                for item in data:
                    pct = float(item.get('changepercent', 0))
                    if pct >= 9.5:  # 涨停
                        results.append({
                            'code': item.get('code', ''),
                            'name': item.get('name', ''),
                            'price': float(item.get('trade', 0)),
                            'change_pct': pct,
                            'volume': int(float(item.get('volume', 0))),
                            'amount': float(item.get('amount', 0)) / 100000000 if item.get('amount') else 0,
                            'high': float(item.get('high', 0)),
                            'low': float(item.get('low', 0)),
                            'open': float(item.get('open', 0)),
                            'prev_close': float(item.get('settlement', 0)),
                        })
                return results
        except Exception as e:
            print(f"新浪财经API获取涨停股失败: {e}")

        return []

    def get_float_market_cap(self, code: str) -> Optional[float]:
        """
        获取流通市值（亿元）

        使用腾讯财经API获取真实流通市值

        Args:
            code: 股票代码

        Returns:
            流通市值（亿元），失败返回None
        """
        # 确定市场代码
        if code.startswith('6'):
            market = 'sh'
        else:
            market = 'sz'

        url = f'http://qt.gtimg.cn/q={market}{code}'

        try:
            headers = {
                'Referer': 'http://finance.qq.com',
            }
            response = self.session.get(url, headers=headers, timeout=5)
            response.encoding = 'gbk'
            data = response.text

            # 解析数据
            match = re.search(r'=\"(.*)\"', data)
            if match:
                items = match.group(1).split('~')
                # 腾讯行情数据字段：
                # items[44]: 流通市值（亿）
                if len(items) >= 45:
                    float_mv = items[44]
                    if float_mv and float_mv != '-':
                        return float(float_mv)
        except Exception as e:
            pass

        return None

    def get_float_market_cap_batch(self, codes: List[str], n_workers: int = 8) -> Dict[str, float]:
        """
        批量获取流通市值

        Args:
            codes: 股票代码列表
            n_workers: 并发数

        Returns:
            股票代码 -> 流通市值(亿元) 的映射
        """
        results = {}

        with ThreadPoolExecutor(max_workers=n_workers) as executor:
            futures = {executor.submit(self.get_float_market_cap, code): code for code in codes}

            for future in as_completed(futures):
                code = futures[future]
                result = future.result()
                if result is not None:
                    results[code] = result

        return results

    def get_board_count(self, code: str) -> int:
        """
        获取连板数

        Args:
            code: 股票代码

        Returns:
            连板数
        """
        # 需要获取近期日K数据来计算
        # 这里简化处理，仅返回当前涨停状态
        quote = self.get_quote_sina(code)
        if 'error' not in quote:
            if quote.get('change_pct', 0) >= 9.5:
                return 1  # 至少1板
        return 0

    def format_quote(self, quote: Dict) -> str:
        """格式化行情显示"""
        if 'error' in quote:
            return f"获取失败: {quote['error']}"

        lines = []
        lines.append(f"{'='*50}")
        lines.append(f"【{quote['name']}】{quote['code']}")
        lines.append(f"{'='*50}")

        # 价格信息
        change_str = f"{quote['change']:+.2f} ({quote['change_pct']:+.2f}%)"
        lines.append(f"当前价: {quote['price']:.2f} 元  涨跌: {change_str}")
        lines.append(f"开盘: {quote['open']:.2f}  最高: {quote['high']:.2f}  最低: {quote['low']:.2f}")
        lines.append(f"昨收: {quote['prev_close']:.2f}")

        # 成交信息
        lines.append(f"成交量: {quote['volume']/10000:.0f}万手  成交额: {quote['amount']/100000000:.2f}亿")

        # 五档
        lines.append(f"\n五档买卖:")
        lines.append(f"卖5: {quote.get('ask5', 0):.2f} ({quote.get('ask5_vol', 0)})")
        lines.append(f"卖4: {quote.get('ask4', 0):.2f} ({quote.get('ask4_vol', 0)})")
        lines.append(f"卖3: {quote.get('ask3', 0):.2f} ({quote.get('ask3_vol', 0)})")
        lines.append(f"卖2: {quote.get('ask2', 0):.2f} ({quote.get('ask2_vol', 0)})")
        lines.append(f"卖1: {quote.get('ask1', 0):.2f} ({quote.get('ask1_vol', 0)})")
        lines.append(f"-" * 30)
        lines.append(f"买1: {quote.get('bid1', 0):.2f} ({quote.get('bid1_vol', 0)})")
        lines.append(f"买2: {quote.get('bid2', 0):.2f} ({quote.get('bid2_vol', 0)})")
        lines.append(f"买3: {quote.get('bid3', 0):.2f} ({quote.get('bid3_vol', 0)})")
        lines.append(f"买4: {quote.get('bid4', 0):.2f} ({quote.get('bid4_vol', 0)})")
        lines.append(f"买5: {quote.get('bid5', 0):.2f} ({quote.get('bid5_vol', 0)})")
        lines.append(f"{'='*50}")

        return '\n'.join(lines)


def monitor_stocks(codes: List[str], interval: int = 3):
    """
    实时监控股票

    Args:
        codes: 股票代码列表
        interval: 刷新间隔（秒）
    """
    quote = RealtimeQuote()

    print(f"开始监控股票: {', '.join(codes)}")
    print(f"刷新间隔: {interval}秒")
    print("按 Ctrl+C 停止")
    print()

    try:
        while True:
            print(f"\n更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print("-" * 60)

            # 批量获取
            quotes = quote.get_quotes_batch(codes)

            for code in codes:
                if code in quotes:
                    q = quotes[code]
                    change_str = f"{q['change']:+.2f} ({q['change_pct']:+.2f}%)"
                    print(f"{q['name']:8} {code}: {q['price']:7.2f} 元  {change_str:15}  成交额: {q['amount']/100000000:.2f}亿")
                else:
                    print(f"{code}: 获取失败")

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n监控已停止")


class MarketSentimentCalculator:
    """实时市场情绪计算器"""

    def __init__(self):
        self.session = create_session_with_retry()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Referer': 'http://quote.eastmoney.com/',
        })
        # 禁用代理
        self.session.trust_env = False

    def get_market_overview(self) -> Dict:
        """
        获取市场概况统计（东方财富）

        Returns:
            市场概况数据
        """
        url = 'http://push2.eastmoney.com/api/qt/stock/get'

        # 获取上证指数
        params = {
            'secid': '1.000001',
            'fields': 'f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f55,f57,f58,f60,f170,f171',
        }

        try:
            response = self.session.get(url, params=params, timeout=10)
            data = response.json()

            result = {}
            if data.get('data'):
                d = data['data']
                result = {
                    'index_name': '上证指数',
                    'index_price': d.get('f43', 0) / 100 if d.get('f43') else 0,
                    'index_change': d.get('f170', 0) / 100 if d.get('f170') else 0,
                    'index_change_pct': d.get('f170', 0) / d.get('f43', 1) * 100 if d.get('f43') else 0,
                    'volume': d.get('f47', 0),  # 成交量
                    'amount': d.get('f48', 0),  # 成交额
                }

            # 获取涨跌统计
            url2 = 'http://push2.eastmoney.com/api/qt/clist/get'
            params2 = {
                'pn': 1,
                'pz': 20,
                'fs': 'm:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23',
                'fields': 'f3,f12,f14',
            }

            return result

        except Exception as e:
            print(f"获取市场概况失败: {e}")
            return {}

    def get_all_stocks_quote(self, market: str = 'all') -> List[Dict]:
        """
        获取全市场股票行情（东方财富）

        Args:
            market: 市场类型 'sha'/'sza'/'all'

        Returns:
            股票行情列表
        """
        # 东方财富全市场行情接口
        url = 'http://push2.eastmoney.com/api/qt/clist/get'

        # 市场筛选
        if market == 'sha':
            fs_list = ['m:1+t:2,m:1+t:23']  # 沪A
        elif market == 'sza':
            fs_list = ['m:0+t:6,m:0+t:80']  # 深A
        else:
            fs_list = ['m:0+t:6,m:0+t:80', 'm:1+t:2,m:1+t:23']  # 深A + 沪A

        all_results = []
        max_retries = 3
        eastmoney_failed = False

        for fs in fs_list:
            page = 1
            page_size = 500

            while True:
                params = {
                    'pn': page,
                    'pz': page_size,
                    'po': 1,
                    'np': 1,
                    'fltt': 2,
                    'invt': 2,
                    'fid': 'f3',  # 按涨幅排序
                    'fs': fs,
                    'fields': 'f1,f2,f3,f4,f5,f6,f7,f12,f14,f15,f16,f17,f18',
                }

                # 重试机制
                retry_failed = False
                for retry in range(max_retries):
                    try:
                        response = self.session.get(url, params=params, timeout=15)
                        data = response.json()

                        if data.get('data') and data['data'].get('diff'):
                            items = data['data']['diff']
                            total = data['data'].get('total', 0)

                            for item in items:
                                try:
                                    all_results.append({
                                        'code': item.get('f12', ''),
                                        'name': item.get('f14', ''),
                                        'price': float(item.get('f2', 0)) / 100 if item.get('f2') else 0,
                                        'change_pct': float(item.get('f3', 0)) if item.get('f3') else 0,
                                        'volume': item.get('f5', 0),
                                        'amount': float(item.get('f6', 0)) / 100000000 if item.get('f6') else 0,
                                        'amplitude': float(item.get('f7', 0)) if item.get('f7') else 0,
                                        'high': float(item.get('f15', 0)) / 100 if item.get('f15') else 0,
                                        'low': float(item.get('f16', 0)) / 100 if item.get('f16') else 0,
                                        'open': float(item.get('f17', 0)) / 100 if item.get('f17') else 0,
                                        'prev_close': float(item.get('f18', 0)) / 100 if item.get('f18') else 0,
                                    })
                                except:
                                    pass

                            # 检查是否还有更多数据
                            if len(items) < page_size or len(all_results) >= total:
                                break
                            page += 1
                        else:
                            break

                        # 成功则跳出重试循环
                        break

                    except Exception as e:
                        if retry < max_retries - 1:
                            time.sleep(1)  # 等待后重试
                            continue
                        else:
                            print(f"[提示] 东方财富API连接失败，切换到新浪财经数据源")
                            eastmoney_failed = True
                            retry_failed = True
                            break

                if retry_failed:
                    break

        # 如果东方财富失败，使用新浪财经备用数据源
        if eastmoney_failed or not all_results:
            return self._get_all_stocks_quote_sina(market)

        return all_results

    def _get_all_stocks_quote_sina(self, market: str = 'all') -> List[Dict]:
        """
        使用新浪财经API获取全市场股票行情（备用数据源）

        Args:
            market: 市场类型 'sha'/'sza'/'all'

        Returns:
            股票行情列表
        """
        all_results = []

        # 新浪财经行情接口
        url = 'http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData'

        # 确定市场节点
        if market == 'sha':
            nodes = ['sh_a']  # 沪A
        elif market == 'sza':
            nodes = ['sz_a']  # 深A
        else:
            nodes = ['sh_a', 'sz_a']  # 沪A + 深A

        for node in nodes:
            page = 1
            page_size = 500

            while True:
                params = {
                    'page': page,
                    'num': page_size,
                    'sort': 'changepercent',
                    'asc': 0,
                    'node': node,
                    'symbol': '',
                    '_s_r_a': 'page',
                }

                try:
                    response = self.session.get(url, params=params, timeout=15)
                    data = response.json()

                    if data and isinstance(data, list):
                        for item in data:
                            try:
                                change_pct = float(item.get('changepercent', 0))
                                all_results.append({
                                    'code': item.get('code', ''),
                                    'name': item.get('name', ''),
                                    'price': float(item.get('trade', 0)),
                                    'change_pct': change_pct,
                                    'volume': int(float(item.get('volume', 0))),
                                    'amount': float(item.get('amount', 0)) / 100000000 if item.get('amount') else 0,
                                    'amplitude': float(item.get('turnover', 0)) if item.get('turnover') else 0,
                                    'high': float(item.get('high', 0)),
                                    'low': float(item.get('low', 0)),
                                    'open': float(item.get('open', 0)),
                                    'prev_close': float(item.get('settlement', 0)),
                                })
                            except:
                                pass

                        # 检查是否还有更多数据
                        if len(data) < page_size:
                            break
                        page += 1
                    else:
                        break

                except Exception as e:
                    print(f"新浪财经API获取行情数据失败: {e}")
                    break

        return all_results

    def get_market_sentiment_fast(self) -> Dict:
        """
        快速获取市场情绪

        Returns:
            市场情绪数据字典
        """
        result = {
            'up_count': 0,
            'down_count': 0,
            'limit_up_count': 0,
            'limit_down_count': 0,
            'sentiment_score': 50,
            'phase': '中性',
        }

        try:
            # 使用新浪财经A股排行接口
            # 分别获取涨幅榜和跌幅榜来统计
            url = 'http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData'

            # 获取涨幅榜（前100）
            params_up = {
                'page': 1,
                'num': 100,
                'sort': 'changepercent',
                'asc': 0,  # 降序
                'node': 'hs_a',
                'symbol': '',
                '_s_r_a': 'page',
            }

            response_up = self.session.get(url, params=params_up, timeout=10)
            data_up = response_up.json()

            # 获取跌幅榜（前100）
            params_down = params_up.copy()
            params_down['asc'] = 1  # 升序（跌幅榜）
            response_down = self.session.get(url, params=params_down, timeout=10)
            data_down = response_down.json()

            # 统计
            up_count = 0
            down_count = 0
            limit_up = 0
            limit_down = 0

            # 涨幅榜统计
            if data_up and isinstance(data_up, list):
                for item in data_up:
                    pct = float(item.get('changepercent', 0)) if item.get('changepercent') else 0
                    if pct > 0:
                        up_count += 1
                    if pct >= 9.5:
                        limit_up += 1

            # 跌幅榜统计
            if data_down and isinstance(data_down, list):
                for item in data_down:
                    pct = float(item.get('changepercent', 0)) if item.get('changepercent') else 0
                    if pct < 0:
                        down_count += 1
                    if pct <= -9.5:
                        limit_down += 1

            # 涨停跌停数量
            result['limit_up_count'] = limit_up
            result['limit_down_count'] = limit_down

            # 用涨幅榜和跌幅榜的比例估算市场涨跌比
            # A股总数约5000只，涨幅榜前100和跌幅榜前100反映市场两端
            # 假设市场涨跌分布均匀，用样本比例估算
            total_sample = up_count + down_count  # 应该接近200
            if total_sample > 0:
                up_ratio = up_count / total_sample
                down_ratio = down_count / total_sample
            else:
                up_ratio = 0.5
                down_ratio = 0.5

            # 估算全市场涨跌家数（A股约5500只）
            total_stocks = 5500
            result['up_count'] = int(total_stocks * up_ratio)
            result['down_count'] = int(total_stocks * down_ratio)

            # 计算情绪分数
            total_count = result['up_count'] + result['down_count']
            if total_count > 0:
                breadth = result['up_count'] / total_count
            else:
                breadth = 0.5

            limit_total = result['limit_up_count'] + result['limit_down_count']
            if limit_total > 0:
                limit_score = (result['limit_up_count'] - result['limit_down_count']) / limit_total
                limit_score = (limit_score + 1) / 2
            else:
                limit_score = 0.5

            # 综合得分
            score = (breadth * 0.5 + limit_score * 0.3 + 0.2) * 100
            score = min(100, max(0, score))

            result['sentiment_score'] = round(score, 1)

            # 判断阶段
            if score < 30:
                result['phase'] = '冰点'
            elif score < 50:
                result['phase'] = '启动'
            elif score < 70:
                result['phase'] = '发酵'
            elif score < 85:
                result['phase'] = '高潮'
            else:
                result['phase'] = '退潮'

            return result

        except Exception as e:
            print(f"快速获取市场情绪失败: {e}")
            return result

    def calculate_sentiment(self, market: str = 'all') -> MarketSentiment:
        """
        计算实时市场情绪

        Args:
            market: 市场类型

        Returns:
            MarketSentiment对象
        """
        print("正在获取全市场行情数据...")
        stocks = self.get_all_stocks_quote(market)

        if not stocks:
            return None

        print(f"获取到 {len(stocks)} 只股票数据")

        # 统计涨跌
        up_count = sum(1 for s in stocks if s['change_pct'] > 0)
        down_count = sum(1 for s in stocks if s['change_pct'] <= 0)

        # 统计涨停跌停
        limit_up_count = sum(1 for s in stocks if s['change_pct'] >= 9.5)
        limit_down_count = sum(1 for s in stocks if s['change_pct'] <= -9.5)

        # 获取涨停股
        limit_up_stocks = [s for s in stocks if s['change_pct'] >= 9.5]

        # 计算连板（需要历史数据，这里简化处理，用涨幅>=19%估算2板+）
        high_board_stocks = [s for s in stocks if s['change_pct'] >= 19]
        high_board_count = len(high_board_stocks)

        # 涨幅榜前10
        sorted_by_gain = sorted(stocks, key=lambda x: x['change_pct'], reverse=True)
        top_gainers = sorted_by_gain[:10]

        # 跌幅榜前10
        sorted_by_loss = sorted(stocks, key=lambda x: x['change_pct'])
        top_losers = sorted_by_loss[:10]

        # 计算情绪分数
        total = up_count + down_count
        breadth = up_count / total if total > 0 else 0.5

        limit_total = limit_up_count + limit_down_count
        limit_score = (limit_up_count - limit_down_count) / limit_total if limit_total > 0 else 0
        limit_score = (limit_score + 1) / 2

        # 高位连板加分
        board_bonus = min(high_board_count / 50, 0.2) if limit_up_count > 0 else 0

        # 综合得分
        score = (breadth * 0.4 + limit_score * 0.4 + 0.2 + board_bonus) * 100
        score = min(100, max(0, score))

        # 判断情绪阶段
        if score < 30:
            phase = "冰点"
        elif score < 50:
            phase = "启动"
        elif score < 70:
            phase = "发酵"
        elif score < 85:
            phase = "高潮"
        else:
            phase = "退潮"

        return MarketSentiment(
            score=round(score, 1),
            phase=phase,
            up_count=up_count,
            down_count=down_count,
            limit_up_count=limit_up_count,
            limit_down_count=limit_down_count,
            high_board_count=high_board_count,
            breadth=round(breadth * 100, 1),
            limit_score=round(limit_score * 100, 1),
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            top_gainers=top_gainers,
            top_losers=top_losers,
            limit_up_stocks=limit_up_stocks,
            high_board_stocks=high_board_stocks,
        )

    def format_sentiment(self, sentiment: MarketSentiment) -> str:
        """格式化情绪报告"""
        lines = []
        lines.append("=" * 70)
        lines.append("实时市场情绪分析")
        lines.append("=" * 70)
        lines.append(f"更新时间: {sentiment.timestamp}")
        lines.append("")

        # 情绪分数
        lines.append(f"【情绪分数】{sentiment.score} 分 - {sentiment.phase}")
        lines.append("")

        # 市场概况
        lines.append("【市场概况】")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 上涨家数 | {sentiment.up_count} |")
        lines.append(f"| 下跌家数 | {sentiment.down_count} |")
        lines.append(f"| 涨停家数 | {sentiment.limit_up_count} |")
        lines.append(f"| 跌停家数 | {sentiment.limit_down_count} |")
        lines.append(f"| 3连板以上 | {sentiment.high_board_count} |")
        lines.append(f"| 涨跌比 | {sentiment.breadth}% |")
        lines.append("")

        # 涨幅榜
        lines.append("【涨幅榜 Top 10】")
        lines.append(f"| 股票 | 代码 | 涨幅 | 现价 |")
        lines.append(f"|------|------|------|------|")
        for s in sentiment.top_gainers:
            lines.append(f"| {s['name']} | {s['code']} | {s['change_pct']:+.2f}% | {s['price']:.2f} |")
        lines.append("")

        # 跌幅榜
        lines.append("【跌幅榜 Top 10】")
        lines.append(f"| 股票 | 代码 | 跌幅 | 现价 |")
        lines.append(f"|------|------|------|------|")
        for s in sentiment.top_losers:
            lines.append(f"| {s['name']} | {s['code']} | {s['change_pct']:+.2f}% | {s['price']:.2f} |")
        lines.append("")

        # 涨停股
        if sentiment.limit_up_stocks:
            lines.append(f"【涨停股】共 {len(sentiment.limit_up_stocks)} 只")
            lines.append(f"| 股票 | 代码 | 涨幅 | 成交额(亿) |")
            lines.append(f"|------|------|------|----------|")
            for s in sentiment.limit_up_stocks[:20]:
                lines.append(f"| {s['name']} | {s['code']} | {s['change_pct']:+.2f}% | {s['amount']:.2f} |")
            if len(sentiment.limit_up_stocks) > 20:
                lines.append(f"| ... | 还有 {len(sentiment.limit_up_stocks) - 20} 只 | | |")
            lines.append("")

        # 操作建议
        lines.append("【操作建议】")
        if sentiment.phase == "冰点":
            lines.append("- 情绪冰点，观望为主，不参与")
        elif sentiment.phase == "启动":
            lines.append("- 情绪启动，可轻仓试错，关注首板/2板")
        elif sentiment.phase == "发酵":
            lines.append("- 情绪发酵，积极参与，聚焦龙头")
        elif sentiment.phase == "高潮":
            lines.append("- 情绪高潮，谨慎参与，注意风险")
        else:
            lines.append("- 情绪退潮，减仓观望，控制风险")

        lines.append("")
        lines.append("=" * 70)

        return '\n'.join(lines)


def monitor_sentiment(interval: int = 60, market: str = 'all'):
    """
    实时监控市场情绪

    Args:
        interval: 刷新间隔（秒）
        market: 市场类型
    """
    calc = MarketSentimentCalculator()

    print(f"开始监控市场情绪")
    print(f"刷新间隔: {interval}秒")
    print(f"市场: {market}")
    print("按 Ctrl+C 停止")
    print()

    try:
        while True:
            sentiment = calc.calculate_sentiment(market)
            if sentiment:
                print(calc.format_sentiment(sentiment))

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n监控已停止")


@dataclass
class StockAnalysis:
    """股票分析结果"""
    code: str
    name: str
    # 实时行情
    price: float
    change: float
    change_pct: float
    open: float
    high: float
    low: float
    prev_close: float
    volume: float            # 万手
    amount: float            # 亿
    amplitude: float         # 振幅%
    turnover_rate: float     # 换手率%
    # 五档
    bid1: float
    bid1_vol: int
    ask1: float
    ask1_vol: int
    # 技术指标
    limit_price: float       # 涨停价
    limit_down_price: float  # 跌停价
    distance_limit: float    # 距涨停%
    distance_limit_down: float # 距跌停%
    # 连板分析
    board_count: int         # 连板数
    is_limit: bool           # 是否涨停
    has_break: bool          # 是否断板重启
    # 风险评估
    risk_level: str          # 风险等级
    risk_score: int          # 风险分数
    risk_factors: List[str]  # 风险因素
    # 操作建议
    suggestion: str          # 操作建议
    stop_loss: float         # 止损价
    stop_profit: float       # 止盈价
    # 更新时间
    timestamp: str


class StockRealtimeAnalyzer:
    """单只股票实时分析器"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        })
        self.quote = RealtimeQuote()

    def _get_market_code(self, code: str) -> str:
        """根据股票代码判断市场"""
        if code.startswith('6'):
            return f'sh{code}'
        elif code.startswith('0') or code.startswith('3'):
            return f'sz{code}'
        elif code.startswith('68'):
            return f'sh{code}'
        else:
            return f'sh{code}'

    def get_history_data(self, code: str, days: int = 30) -> pd.DataFrame:
        """
        获取历史数据（从通达信本地数据）

        Args:
            code: 股票代码
            days: 天数

        Returns:
            历史数据DataFrame
        """
        try:
            from app.stock_agent.data.data_loader import DataLoader
            loader = DataLoader()
            from datetime import datetime, timedelta
            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
            df = loader.get_stock_history(code, start_date, end_date)
            return df
        except:
            return None

    def calculate_board_count(self, df: pd.DataFrame, limit_pct: float = 10) -> int:
        """
        计算连板数

        Args:
            df: 历史数据
            limit_pct: 涨停阈值

        Returns:
            连板数
        """
        if df is None or len(df) < 2:
            return 0

        df['pct'] = df['close'].pct_change() * 100

        board_count = 0
        for i in range(-1, -min(15, len(df)), -1):
            if df.iloc[i]['pct'] >= limit_pct - 0.5:
                board_count += 1
            else:
                break

        return board_count

    def detect_break_restart(self, df: pd.DataFrame, limit_pct: float = 10) -> Tuple[bool, int]:
        """
        检测断板重启

        Returns:
            (是否断板重启, 断板天数)
        """
        if df is None or len(df) < 10:
            return False, 0

        df['pct'] = df['close'].pct_change() * 100

        # 获取最近涨停情况
        recent_limits = []
        for i in range(-1, -min(15, len(df)), -1):
            pct = df.iloc[i]['pct']
            is_limit = pct >= limit_pct - 0.5
            recent_limits.append(is_limit)

        if len(recent_limits) < 3:
            return False, 0

        # 找当前连板数
        current_streak = 0
        for is_limit in recent_limits:
            if is_limit:
                current_streak += 1
            else:
                break

        # 检查是否有断板重启
        if current_streak < len(recent_limits):
            for i in range(current_streak + 1, len(recent_limits)):
                if recent_limits[i]:
                    return True, i - current_streak

        return False, 0

    def calculate_risk(self, quote: Dict, df: pd.DataFrame) -> Tuple[int, str, List[str]]:
        """
        计算风险评分

        Returns:
            (风险分数, 风险等级, 风险因素列表)
        """
        risk_score = 0
        risk_factors = []

        # 1. 断板重启
        has_break, break_days = self.detect_break_restart(df)
        if has_break:
            risk_score += 25
            risk_factors.append('断板重启')

        # 2. 5日涨幅
        if df is not None and len(df) >= 6:
            gain_5d = (df.iloc[-1]['close'] - df.iloc[-6]['close']) / df.iloc[-6]['close'] * 100
            if gain_5d > 50:
                risk_score += 25
                risk_factors.append(f'5日涨幅{gain_5d:.0f}%')
            elif gain_5d > 30:
                risk_score += 15
                risk_factors.append(f'5日涨幅{gain_5d:.0f}%')

        # 3. 一字板
        if df is not None and len(df) >= 2:
            yizi_count = 0
            board_count = self.calculate_board_count(df)
            for i in range(-1, -min(board_count + 1, len(df)), -1):
                t = df.iloc[i]
                if t['open'] == t['close'] == t['high'] == t['low']:
                    yizi_count += 1
            if yizi_count >= 2:
                risk_score += 10
                risk_factors.append(f'一字板{yizi_count}个')

        # 4. 成交额放大
        if df is not None and len(df) >= 6:
            today_amount = df.iloc[-1]['amount']
            avg_amount = df.iloc[-6:-1]['amount'].mean()
            if avg_amount > 0 and today_amount > avg_amount * 2:
                risk_score += 10
                risk_factors.append('成交额放大')

        # 5. 高位板风险
        board_count = self.calculate_board_count(df)
        if board_count >= 4:
            risk_score += 20
            risk_factors.append(f'{board_count}板高位风险')
        elif board_count == 3:
            risk_score += 5
            risk_factors.append('3板风险')

        # 确定风险等级
        if risk_score < 20:
            risk_level = '低风险'
        elif risk_score < 40:
            risk_level = '中风险'
        else:
            risk_level = '高风险'

        return risk_score, risk_level, risk_factors

    def analyze(self, code: str) -> StockAnalysis:
        """
        实时分析单只股票

        Args:
            code: 股票代码

        Returns:
            StockAnalysis对象
        """
        # 获取实时行情
        quote_data = self.quote.get_quote_sina(code)
        if 'error' in quote_data:
            return None

        # 获取历史数据
        df = self.get_history_data(code, days=30)

        # 计算涨停价和跌停价
        prev_close = quote_data['prev_close']
        if code.startswith('30') or code.startswith('68'):
            limit_pct = 20
        else:
            limit_pct = 10

        limit_price = round(prev_close * (1 + limit_pct / 100), 2)
        limit_down_price = round(prev_close * (1 - limit_pct / 100), 2)

        # 计算距离涨跌停
        price = quote_data['price']
        distance_limit = (limit_price - price) / price * 100 if price > 0 else 0
        distance_limit_down = (price - limit_down_price) / price * 100 if price > 0 else 0

        # 计算连板数
        board_count = self.calculate_board_count(df, limit_pct)

        # 是否涨停
        is_limit = quote_data['change_pct'] >= limit_pct - 0.5

        # 断板重启
        has_break, break_days = self.detect_break_restart(df, limit_pct)

        # 计算风险
        risk_score, risk_level, risk_factors = self.calculate_risk(quote_data, df)

        # 换手率估算
        amount_yi = quote_data['amount'] / 100000000
        # 假设成交额/换手率 = 流通市值
        # 简化估算：假设5%换手率对应流通市值 = 成交额/0.05
        estimated_mv = amount_yi / 0.05 if amount_yi > 0 else 50
        turnover_rate = amount_yi / estimated_mv * 100 if estimated_mv > 0 else 0

        # 操作建议
        suggestion = self._generate_suggestion(
            quote_data, is_limit, board_count, risk_level, distance_limit
        )

        # 止损止盈
        stop_loss = round(price * 0.94, 2)  # -6%
        stop_profit = round(price * 1.10, 2)  # +10%

        return StockAnalysis(
            code=code,
            name=quote_data['name'],
            price=price,
            change=quote_data['change'],
            change_pct=quote_data['change_pct'],
            open=quote_data['open'],
            high=quote_data['high'],
            low=quote_data['low'],
            prev_close=prev_close,
            volume=quote_data['volume'] / 10000,
            amount=amount_yi,
            amplitude=(quote_data['high'] - quote_data['low']) / prev_close * 100 if prev_close > 0 else 0,
            turnover_rate=turnover_rate,
            bid1=quote_data.get('bid1', 0),
            bid1_vol=quote_data.get('bid1_vol', 0),
            ask1=quote_data.get('ask1', 0),
            ask1_vol=quote_data.get('ask1_vol', 0),
            limit_price=limit_price,
            limit_down_price=limit_down_price,
            distance_limit=distance_limit,
            distance_limit_down=distance_limit_down,
            board_count=board_count,
            is_limit=is_limit,
            has_break=has_break,
            risk_level=risk_level,
            risk_score=risk_score,
            risk_factors=risk_factors,
            suggestion=suggestion,
            stop_loss=stop_loss,
            stop_profit=stop_profit,
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )

    def _generate_suggestion(self, quote: Dict, is_limit: bool, board_count: int,
                            risk_level: str, distance_limit: float) -> str:
        """生成操作建议"""
        suggestions = []

        change_pct = quote['change_pct']

        # 涨停状态
        if is_limit:
            if board_count >= 4:
                suggestions.append("涨停但连板数过高，不建议追入")
            elif board_count == 3:
                suggestions.append("3板涨停，可观察封单情况")
            elif board_count == 2:
                suggestions.append("2板涨停，关注封单强度")
            else:
                suggestions.append("首板涨停，关注明日走势")
        # 上涨
        elif change_pct > 5:
            if distance_limit < 2:
                suggestions.append(f"距涨停仅{distance_limit:.2f}%，可观察封板情况")
            else:
                suggestions.append("涨幅较大，注意回调风险")
        # 下跌
        elif change_pct < -5:
            suggestions.append("跌幅较大，不建议参与")
        elif change_pct < 0:
            suggestions.append("下跌走势，观望为主")
        # 震荡
        else:
            suggestions.append("震荡走势，观察为主")

        # 风险提示
        if risk_level == '高风险':
            suggestions.append("[高风险]谨慎参与")
        elif risk_level == '中风险':
            suggestions.append("[中风险]注意风险")

        return "；".join(suggestions)

    def format_analysis(self, analysis: StockAnalysis) -> str:
        """格式化分析报告"""
        lines = []
        lines.append("=" * 70)
        lines.append(f"【{analysis.name}】{analysis.code} 实时分析")
        lines.append("=" * 70)
        lines.append(f"更新时间: {analysis.timestamp}")
        lines.append("")

        # 基本信息
        lines.append("【实时行情】")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 当前价 | {analysis.price:.2f} 元 |")
        lines.append(f"| 涨跌 | {analysis.change:+.2f} 元 ({analysis.change_pct:+.2f}%) |")
        lines.append(f"| 开盘价 | {analysis.open:.2f} 元 |")
        lines.append(f"| 最高价 | {analysis.high:.2f} 元 |")
        lines.append(f"| 最低价 | {analysis.low:.2f} 元 |")
        lines.append(f"| 昨收盘 | {analysis.prev_close:.2f} 元 |")
        lines.append(f"| 振幅 | {analysis.amplitude:.2f}% |")
        lines.append(f"| 成交量 | {analysis.volume:.0f} 万手 |")
        lines.append(f"| 成交额 | {analysis.amount:.2f} 亿 |")
        lines.append("")

        # 涨跌停信息
        lines.append("【涨跌停信息】")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 涨停价 | {analysis.limit_price:.2f} 元 |")
        lines.append(f"| 跌停价 | {analysis.limit_down_price:.2f} 元 |")
        lines.append(f"| 距涨停 | {analysis.distance_limit:.2f}% |")
        lines.append(f"| 距跌停 | {analysis.distance_limit_down:.2f}% |")
        lines.append(f"| 连板数 | {analysis.board_count} 板 |")
        lines.append(f"| 涨停状态 | {'是' if analysis.is_limit else '否'} |")
        lines.append("")

        # 五档买卖
        lines.append("【五档买卖】")
        lines.append(f"卖一: {analysis.ask1:.2f} 元 ({analysis.ask1_vol} 手)")
        lines.append(f"买一: {analysis.bid1:.2f} 元 ({analysis.bid1_vol} 手)")
        lines.append("")

        # 风险评估
        lines.append("【风险评估】")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 风险等级 | {analysis.risk_level} |")
        lines.append(f"| 风险分数 | {analysis.risk_score} 分 |")
        if analysis.risk_factors:
            lines.append(f"| 风险因素 | {', '.join(analysis.risk_factors)} |")
        if analysis.has_break:
            lines.append(f"| 断板重启 | 是 |")
        lines.append("")

        # 操作建议
        lines.append("【操作建议】")
        lines.append(f"{analysis.suggestion}")
        lines.append("")
        lines.append(f"止损价: {analysis.stop_loss:.2f} 元 (-6%)")
        lines.append(f"止盈价: {analysis.stop_profit:.2f} 元 (+10%)")
        lines.append("")

        # 风险提示
        lines.append("【风险提示】")
        if analysis.board_count >= 4:
            lines.append("- 高位板风险大，谨慎参与")
        if analysis.has_break:
            lines.append("- 断板重启后风险累积，注意回调")
        if analysis.amplitude > 10:
            lines.append("- 振幅较大，多空分歧明显")
        if not analysis.is_limit and analysis.change_pct > 5:
            lines.append("- 未封板，注意回落风险")

        lines.append("")
        lines.append("=" * 70)

        return '\n'.join(lines)

    def monitor(self, code: str, interval: int = 5):
        """
        实时监控单只股票

        Args:
            code: 股票代码
            interval: 刷新间隔（秒）
        """
        print(f"开始监控股票: {code}")
        print(f"刷新间隔: {interval}秒")
        print("按 Ctrl+C 停止")
        print()

        try:
            while True:
                analysis = self.analyze(code)
                if analysis:
                    print(self.format_analysis(analysis))
                else:
                    print(f"获取 {code} 数据失败")

                time.sleep(interval)

        except KeyboardInterrupt:
            print("\n监控已停止")


def analyze_stock(code: str) -> StockAnalysis:
    """
    分析单只股票（便捷函数）

    Args:
        code: 股票代码

    Returns:
        StockAnalysis对象
    """
    analyzer = StockRealtimeAnalyzer()
    return analyzer.analyze(code)


def monitor_stock(code: str, interval: int = 5):
    """
    监控单只股票（便捷函数）

    Args:
        code: 股票代码
        interval: 刷新间隔
    """
    analyzer = StockRealtimeAnalyzer()
    analyzer.monitor(code, interval)


if __name__ == "__main__":
    # 测试实时行情
    print("测试获取实时行情:")
    quote = RealtimeQuote()
    data = quote.get_quote_sina('603206')
    print(quote.format_quote(data))

    # 测试单只股票分析
    print("\n测试单只股票分析:")
    analyzer = StockRealtimeAnalyzer()
    analysis = analyzer.analyze('603206')
    if analysis:
        print(analyzer.format_analysis(analysis))
