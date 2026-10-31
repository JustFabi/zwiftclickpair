"""Minimal pytest-style runner (pytest not installed on this machine)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import traceback

import tests.test_obc_protocol as t

fails = 0
for name, fn in sorted(vars(t).items()):
    if not (name.startswith("test_") and callable(fn)):
        continue
    try:
        fn()
        print(f"  ok    {name}")
    except AssertionError:
        fails += 1
        print(f"  FAIL  {name} (assertion)")
    except Exception as exc:
        fails += 1
        print(f"  FAIL  {name}: {exc!r}")
print(f"failures: {fails}")
sys.exit(1 if fails else 0)
