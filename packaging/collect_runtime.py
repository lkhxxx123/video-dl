#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""packaging.collect_runtime — 按 playwright browsers.json 精确拷贝浏览器。

只拷当前 playwright 版本实际需要的 chromium / chromium-headless-shell /
winldd（ms-playwright 目录常有多版本残留和其他工具的浏览器，整目录拷
会把发行包撑到 2GB+）。

用法: python packaging/collect_runtime.py <ms-playwright目录> <目标runtime/browsers>
"""
import json
import shutil
import sys
from pathlib import Path

import playwright

KEEP = {"chromium", "chromium-headless-shell", "winldd"}


def main(src_dir: str, dst_dir: str) -> int:
    pj = (Path(playwright.__file__).parent / "driver" / "package"
          / "browsers.json")
    browsers = json.loads(pj.read_text(encoding="utf-8"))["browsers"]
    src, dst = Path(src_dir), Path(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for b in browsers:
        if b["name"] not in KEEP:
            continue
        folder = f"{b['name'].replace('-', '_')}-{b['revision']}"
        s = src / folder
        d = dst / folder
        if not s.exists():
            print(f"WARN: {folder} not found in {src} (install chromium first)")
            continue
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(s, d)
        print(f"copy {folder}")
        copied += 1
    if not copied:
        print("ERROR: nothing copied — run 'python -m playwright install chromium' first")
        return 1
    print(f"done: {copied} browser components -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
