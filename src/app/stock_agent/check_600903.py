"""查看600903详细信息"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.stock_agent.data.data_loader import DataLoader
import pandas as pd

loader = DataLoader()

# 获取近期数据
df = loader.get_stock_history('600903', '20260501', '20260519')
print('600903 近期数据：')
print(df.tail(15).to_string())
print()

# 检查是否连续涨停
if df is not None and len(df) >= 2:
    df_recent = df[df.index <= '20260519']
    print(f'\n数据条数: {len(df_recent)}')

    limit_pct = 10
    for i in range(-1, max(-len(df_recent), -10), -1):
        t = df_recent.iloc[i]
        prev_idx = i - 1
        if abs(prev_idx) > len(df_recent):
            break
        y = df_recent.iloc[prev_idx]
        p = (t['close'] - y['close']) / y['close'] * 100
        date_str = t.name.strftime('%Y-%m-%d') if hasattr(t.name, 'strftime') else str(t.name)

        # 判断是否涨停
        is_limit = '涨停' if p >= limit_pct - 0.5 else ''

        # 判断是否一字板（开盘=收盘=最高=最低）
        is_yizi = '一字板' if (t['open'] == t['close'] == t['high'] == t['low']) else ''

        print(f'{date_str}: 收盘={t["close"]:.2f}, 涨幅={p:.2f}%, 成交额={t["amount"]/100000000:.2f}亿 {is_limit} {is_yizi}')
