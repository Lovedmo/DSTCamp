"""测试脚本的公共入口：按定义顺序运行模块内全部 ``test_*`` 函数（不使用 pytest/unittest）。"""

import atexit
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 设置与缓存一律放进临时目录，绝不读写真实 %APPDATA%/DSTCamp（否则结果会随本机的 Steam 通道等设置变化）。
# run_all.py 已为每个子进程指定好时不再替换；单独运行某个测试文件时在这里兜底。
if not os.environ.get("DSTCAMP_TEST_APPDATA"):
    _settings_tmp = tempfile.mkdtemp(prefix="dstcamp_test_")
    atexit.register(shutil.rmtree, _settings_tmp, True)
    os.environ["APPDATA"] = os.environ["DSTCAMP_TEST_APPDATA"] = _settings_tmp


def run(namespace: dict) -> None:
    tests = [value for name, value in namespace.items() if name.startswith("test_") and callable(value)]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL: {test.__name__}")
            traceback.print_exc()
    print(f"通过 {len(tests) - failed}/{len(tests)}")
    raise SystemExit(1 if failed else 0)
