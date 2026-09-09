#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.selftest — 内置自测框架（原 7 份重复实现合一）。

约定保持不变：模块内 test_* 命名的无参函数即测试；各模块 --selftest
只跑本模块；测试必须与被测函数放在同一模块（globals() 收集）。
"""


def collect_selftests(g: dict):
    """收集模块 globals() 里的 test_* 函数，按名排序（与原实现一致）。"""
    return sorted((name, fn) for name, fn in g.items()
                  if name.startswith("test_") and callable(fn))


def run_selftests(g: dict) -> bool:
    """跑一遍并打印 PASS/FAIL；全部通过返回 True。"""
    failed = 0
    tests = collect_selftests(g)
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
        except Exception as e:  # noqa: BLE001 - selftest 要抓住一切
            failed += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"selftest: {len(tests) - failed}/{len(tests)} 项通过")
    return failed == 0
