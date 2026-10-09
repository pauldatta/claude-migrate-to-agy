#!/usr/bin/env python3
"""Run the test suite without pytest (standard library only).

Calls every test_* function in tests/test_*.py, giving each a fresh temp
directory when it asks for `tmp_path`. CI uses pytest; this exists for
machines where pytest isn't installed.
"""
import importlib.util
import inspect
import sys
import tempfile
import traceback
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here))
failed = passed = 0
for file in sorted(here.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(file.stem, file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, fn in inspect.getmembers(mod, inspect.isfunction):
        if not name.startswith("test_") or fn.__module__ != mod.__name__:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            kwargs = {"tmp_path": Path(tmp)} if "tmp_path" in inspect.signature(fn).parameters else {}
            try:
                fn(**kwargs)
                passed += 1
                print(f"PASS {file.name}::{name}")
            except Exception:  # report every failure, keep going
                failed += 1
                print(f"FAIL {file.name}::{name}")
                traceback.print_exc()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
