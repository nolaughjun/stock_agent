"""检查my_gb模型的特征"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import joblib

# 加载模型
model_path = "strategies/my_gb.joblib"
data = joblib.load(model_path)

print("模型数据类型:", type(data))
print()

if isinstance(data, dict):
    print("字典键:", list(data.keys()))
    print()

    # 打印详细信息
    for k, v in data.items():
        print(f"{k}:")

        if k == 'features':
            print(f"  {v}")
        elif k == 'model':
            print(f"  类型: {type(v)}")
            if hasattr(v, 'feature_names_in_'):
                print(f"  特征名: {v.feature_names_in_}")
        elif k == 'scaler':
            print(f"  类型: {type(v)}")
            if hasattr(v, 'feature_names_in_'):
                print(f"  特征名: {v.feature_names_in_}")
        else:
            print(f"  {v}")
        print()
