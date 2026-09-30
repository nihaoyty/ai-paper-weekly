import sys
from pathlib import Path

# 让 pytest 在仓库根目录找到业务模块
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))