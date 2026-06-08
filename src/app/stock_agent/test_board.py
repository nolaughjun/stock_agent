"""测试连板计算"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.stock_agent.data.data_loader import DataLoader, MarketType
import pandas as pd

loader = DataLoader()
df = loader.get_stock_history('603779', '20260501', '20260519')
print('603779 威龙股份 近期数据：')
print(df.tail(10).to_string())
print()

# 加载更多数据
df2 = loader.get_stock_history('603779', '20260501', '20260525')
print('\n加载更多数据（到5月25日）：')
print(df2.tail(15).to_string())
print()

print('='*60)
print('修复后的连板计算逻辑：')
print('='*60)

if df2 is not None and len(df2) >= 2:
    df_recent = df2[df2.index <= '20260525']
    print(f'数据条数: {len(df_recent)}')

    limit_pct = 10  # 沪A涨停幅度
    board = 0
    for j in range(-1, -min(15, len(df_recent)), -1):
        t = df_recent.iloc[j]
        prev_idx = j - 1
        if abs(prev_idx) > len(df_recent):
            print(f'j={j}: 前一天数据不存在，停止')
            break
        y = df_recent.iloc[prev_idx]
        p = (t['close'] - y['close']) / y['close'] * 100
        date_str = t.name.strftime('%Y-%m-%d') if hasattr(t.name, 'strftime') else str(t.name)
        if p >= limit_pct - 0.5:
            board += 1
            print(f'j={j}, 日期={date_str}, 收盘={t["close"]:.2f}, 昨收={y["close"]:.2f}, 涨幅={p:.2f}% -> 涨停! board={board}')
        else:
            print(f'j={j}, 日期={date_str}, 涨幅={p:.2f}% -> 非涨停，停止')
            break

    print(f'\n最终连板数: {board}')
