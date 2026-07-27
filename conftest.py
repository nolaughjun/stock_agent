"""根级 pytest conftest

确保 `src` 命名空间包可导入 (项目为 src/ layout, 无 pyproject.toml)。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
