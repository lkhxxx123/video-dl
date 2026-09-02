# douyin_search.py（关键词搜索+批量下载）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python douyin_search.py "关键词" --limit N`：Playwright 真浏览器搜索抖音，逐条复用 douyin_dl 管线批量下载无水印 mp4。

**Architecture:** 新单文件 `douyin_search.py` 单向依赖 `douyin_dl.py`（零改动）。浏览器层拦截搜索 XHR 响应收集 aweme_id（签名由浏览器原生算），下载编排循环调 `douyin_dl.run()`。纯逻辑（响应解析/登录判定/目录名清洗）内置 `--selftest` 做 TDD；浏览器层靠端到端验收。

**Tech Stack:** Python 3.10（命令一律 `python`）、requests、playwright(+Chromium)

**Spec:** `d:\templet\app\douyin-dl\docs\2026-09-01-douyin-search-design.md`

## Global Constraints

- Python 命令一律 `python`（本机 `python3` 是坏 stub）
- 依赖：现有 requests + 新增 playwright；不得引入其他包
- 非 git 仓库：**所有任务无 commit 步骤**，验证以命令输出为准
- 文件：`d:\templet\app\douyin-dl\douyin_search.py`；登录态目录 `.browser-profile/`（敏感勿外传）
- `--selftest` 不联网、不开浏览器（playwright 顶层 try-import，未装也能跑自测）
- 退出码：0 成功（含部分跳过）/ 1 致命错误；输出 UTF-8
- 批量下载仅限个人保存，勿二次上传（脚本头注释注明）

---

### Task 1: 骨架 + selftest 框架 + 搜索响应解析

**Files:**
- Create: `d:\templet\app\douyin-dl\douyin_search.py`

**Interfaces:**
- Produces: `parse_search_response(payload: dict, seen: set) -> list[tuple[str, str]]`
  （返回新增的 `(aweme_id, title)`，原地更新 `seen` 去重；无 aweme_info/aweme_id 的
  广告条目丢弃）；selftest 约定（模块级 `test_*` 自动收集）；常量 `SCRIPT_DIR`、
  `PROFILE_DIR`、`DOWNLOADS_DIR`、`SEARCH_URL_PREFIX`、`LOGIN_TIMEOUT`、
  `SCROLL_WAIT`、`MAX_IDLE_SCROLLS`；`class SearchError(Exception)`

- [ ] **Step 1: 写骨架 + 失败测试**

创建 `douyin_search.py`：

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""douyin_search.py — 抖音关键词搜索 + 批量无水印下载
依赖 douyin_dl.py（下载）与 playwright（搜索）。
仅限个人离线保存；请尊重创作者版权，勿二次上传。
"""
import argparse
import json
import random
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import douyin_dl

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILE_DIR = SCRIPT_DIR / ".browser-profile"
DOWNLOADS_DIR = SCRIPT_DIR / "downloads"
SEARCH_URL_PREFIX = "aweme/v1/web/search/item/"
LOGIN_TIMEOUT = 120
SCROLL_WAIT = 1.5
MAX_IDLE_SCROLLS = 3


class SearchError(Exception):
    """搜索/登录流程失败。"""


# ---------- selftest ----------

def _collect_selftests():
    return sorted(
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    )


def run_selftests():
    tests = _collect_selftests()
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"selftest: {len(tests) - failed}/{len(tests)} 项通过")
    return failed == 0


# ---------- tests: 搜索响应解析 ----------

def test_parse_search_response_extracts_dedup_skips_ads():
    payload = {"data": [
        {"aweme_info": {"aweme_id": "111", "desc": "标题A"}},
        {"aweme_info": {"aweme_id": "111", "desc": "标题A重复"}},   # 重复 ID
        {"log_pb": {"impr_id": "ad"}},                                # 广告，无 aweme_info
        {"aweme_info": {"aweme_id": "222", "desc": ""}},               # 空标题
    ]}
    seen = set()
    assert parse_search_response(payload, seen) == [("111", "标题A"), ("222", "")]
    assert seen == {"111", "222"}
    # 再次传入同 payload：全部去重，返回空
    assert parse_search_response(payload, seen) == []


def test_parse_search_response_empty_or_broken():
    assert parse_search_response({}, set()) == []
    assert parse_search_response({"data": None}, set()) == []


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="抖音关键词搜索+批量无水印下载（仅限个人保存）")
    parser.add_argument("keyword", nargs="?", help="搜索关键词")
    parser.add_argument("--limit", type=int, default=10,
                        help="下载条数（默认 10）")
    parser.add_argument("--login", action="store_true",
                        help="只扫码登录不搜索")
    parser.add_argument("--selftest", action="store_true",
                        help="运行内置自测（不联网、不开浏览器）")
    args = parser.parse_args(argv)
    if args.selftest:
        sys.exit(0 if run_selftests() else 1)
    raise SystemExit("搜索流程尚未实现（Task 4 接入）")


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
```

- [ ] **Step 2: 跑 selftest 确认失败**

Run: `cd d:/templet/app/douyin-dl && python douyin_search.py --selftest`
Expected: 2 项 FAIL（NameError: parse_search_response）

- [ ] **Step 3: 实现 parse_search_response**

插入到 SearchError 之后、selftest 区之前：

```python
# ---------- 纯逻辑：搜索响应解析 ----------

def parse_search_response(payload: dict, seen: set):
    """从一条搜索接口响应提取新增 (aweme_id, title)；去重，丢弃广告条目。"""
    results = []
    for entry in payload.get("data") or []:
        info = entry.get("aweme_info") or {}
        aweme_id = info.get("aweme_id")
        if not aweme_id or aweme_id in seen:
            continue
        seen.add(aweme_id)
        results.append((aweme_id, info.get("desc") or ""))
    return results
```

- [ ] **Step 4: 跑 selftest 确认通过**

Run: `cd d:/templet/app/douyin-dl && python douyin_search.py --selftest`
Expected: `2/2 项通过`，退出码 0

---

### Task 2: 登录判定 + 目录名清洗（纯逻辑）

**Files:**
- Modify: `d:\templet\app\douyin-dl\douyin_search.py`

**Interfaces:**
- Consumes: selftest 框架（Task 1）
- Produces: `has_login(cookies) -> bool`（cookies 为 playwright
  `context.cookies()` 形态：`[{"name":..., "value":...}, ...]`）；
  `safe_dir_name(name: str) -> str`（Windows 非法字符→空格、strip、截 50）

- [ ] **Step 1: 写失败测试**

```python
# ---------- tests: 登录判定与目录名 ----------

def test_has_login_true_only_with_sessionid_value():
    assert has_login([{"name": "sessionid", "value": "abc"}]) is True
    assert has_login([{"name": "sessionid", "value": ""}]) is False
    assert has_login([{"name": "ttwid", "value": "x"}]) is False
    assert has_login([]) is False


def test_safe_dir_name():
    assert safe_dir_name("AI 短剧") == "AI 短剧"
    assert safe_dir_name('a/b:c*d?"e<f>g|h') == "a b c d   e f g h"
    assert safe_dir_name("  " ) == ""
    assert len(safe_dir_name("长" * 80)) == 50
```

- [ ] **Step 2: 跑 selftest 确认失败**

Run: `cd d:/templet/app/douyin-dl && python douyin_search.py --selftest`
Expected: 新增 2 项 FAIL，`2/4 项通过`

- [ ] **Step 3: 实现**

```python
def has_login(cookies) -> bool:
    """playwright context.cookies() 中存在非空 sessionid 即视为已登录。"""
    return any(c.get("name") == "sessionid" and c.get("value")
               for c in cookies)


def safe_dir_name(name: str) -> str:
    """清洗为合法目录名：非法字符→空格、strip、截 50 字符。"""
    return douyin_dl.INVALID_FN_RE.sub(" ", name).strip()[:50]
```

- [ ] **Step 4: 跑 selftest 确认通过**

Run: `cd d:/templet/app/douyin-dl && python douyin_search.py --selftest`
Expected: `4/4 项通过`

---

### Task 3: 浏览器层（打开/登录/拦截收集）

**Files:**
- Modify: `d:\templet\app\douyin-dl\douyin_search.py`

**Interfaces:**
- Consumes: `has_login`、`parse_search_response`、常量、`PLAYWRIGHT_OK`、`SearchError`
- Produces:
  - `open_browser()` —— contextmanager，yield playwright BrowserContext
    （playwright 未装→SearchError 含安装命令；Chromium 未下载→SearchError）
  - `ensure_login(context, page) -> None`（已登录直接返回；否则提示扫码并
    轮询 sessionid，超时 `LOGIN_TIMEOUT` 抛 SearchError）
  - `login_only() -> None`（`--login` 用：开浏览器→ensure_login→关）
  - `collect_ids(keyword: str, limit: int) -> list[tuple[str, str]]`
    （打开搜索页，拦截响应收集 (aweme_id, title)，收够 limit 或连续
    MAX_IDLE_SCROLLS 次滚动无新增即停；一条都没拿到抛 SearchError）

**说明**：本任务全部为浏览器交互，无离线可测逻辑，靠 Task 5 端到端验收；
本任务验证 = selftest 复跑全绿 + 模块可导入。

- [ ] **Step 1: 实现（追加到 safe_dir_name 之后）**

```python
# ---------- 浏览器层 ----------

@contextmanager
def open_browser():
    if not PLAYWRIGHT_OK:
        raise SearchError(
            "playwright 未安装。先执行: "
            "pip install playwright && playwright install chromium")
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=False,
                viewport={"width": 1280, "height": 900})
            try:
                yield context
            finally:
                context.close()
    except SearchError:
        raise
    except Exception as e:  # playwright.Error 等
        if "Executable doesn't exist" in str(e):
            raise SearchError(
                "Chromium 未下载。先执行: playwright install chromium")
        raise SearchError(f"浏览器错误: {e}")


def _first_page(context):
    return context.pages[0] if context.pages else context.new_page()


def ensure_login(context, page) -> None:
    if has_login(context.cookies()):
        return
    print("未检测到登录态：请在打开的浏览器窗口中扫码登录抖音…")
    page.goto("https://www.douyin.com/", timeout=30000)
    deadline = time.time() + LOGIN_TIMEOUT
    while time.time() < deadline:
        if has_login(context.cookies()):
            print("登录成功。")
            return
        page.wait_for_timeout(2000)
    raise SearchError(f"扫码超时（{LOGIN_TIMEOUT}s），请重跑 --login")


def login_only() -> None:
    with open_browser() as context:
        ensure_login(context, _first_page(context))


def collect_ids(keyword: str, limit: int):
    with open_browser() as context:
        page = _first_page(context)
        ensure_login(context, page)
        url = (f"https://www.douyin.com/search/{quote(keyword)}"
               "?type=video")
        page.goto(url, timeout=30000)
        seen, items = set(), []

        def on_response(resp):
            if SEARCH_URL_PREFIX not in resp.url:
                return
            try:
                fresh = parse_search_response(resp.json(), seen)
            except Exception:
                return  # 非 JSON / 请求失败，忽略
            items.extend(fresh)

        page.on("response", on_response)
        try:
            idle = 0
            while len(items) < limit and idle < MAX_IDLE_SCROLLS:
                before = len(items)
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(int(SCROLL_WAIT * 1000))
                idle = 0 if len(items) > before else idle + 1
        finally:
            page.remove_listener("response", on_response)
        if not items:
            raise SearchError(
                "滚动数轮仍未拦截到搜索响应：页面可能改版或触发风控")
        return items[:limit]
```

- [ ] **Step 2: 验证可导入 + selftest 全绿**

Run: `cd d:/templet/app/douyin-dl && python -c "import douyin_search; print('import ok')" && python douyin_search.py --selftest 2>&1 | tail -1`
Expected: `import ok` 且 `4/4 项通过`

---

### Task 4: 下载编排 + CLI 完整接入

**Files:**
- Modify: `d:\templet\app\douyin-dl\douyin_search.py`

**Interfaces:**
- Consumes: `collect_ids`、`login_only`、`safe_dir_name`、`DOWNLOADS_DIR`、
  `douyin_dl.run`、`douyin_dl.ParseError`、`SearchError`
- Produces:
  - `download_all(items, out_dir: Path) -> tuple[int, int, int]`
    （逐条调 `douyin_dl.run(f"https://www.douyin.com/video/{id}", out_dir)`；
    ParseError→跳过；其他异常→失败；条间隔随机 1~2s；打印汇总；返回
    (成功, 跳过, 失败)）
  - 完整 `main(argv=None)`（keyword 缺失→`parser.error`；--selftest/--login
    分支；SearchError→stderr+exit 1）

- [ ] **Step 1: 实现 download_all（追加到 collect_ids 之后）**

```python
# ---------- 下载编排 ----------

def download_all(items, out_dir: Path):
    ok = skipped = failed = 0
    total = len(items)
    for i, (aweme_id, title) in enumerate(items, 1):
        print(f"\n[{i}/{total}] {title[:30] or aweme_id}")
        try:
            douyin_dl.run(
                f"https://www.douyin.com/video/{aweme_id}", out_dir)
            ok += 1
        except douyin_dl.ParseError as e:
            print(f"  跳过: {e}")
            skipped += 1
        except Exception as e:  # noqa: BLE001 - 单条失败不中断批次
            print(f"  失败: {e}")
            failed += 1
        if i < total:
            time.sleep(random.uniform(1, 2))
    print(f"\n汇总: 成功 {ok} / 跳过 {skipped} / 失败 {failed}")
    return ok, skipped, failed
```

- [ ] **Step 2: 替换 main 为最终版**

用下面内容**整体替换** Task 1 的临时代码（`if __name__` 块保持不动）：

```python
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="抖音关键词搜索+批量无水印下载（仅限个人保存）")
    parser.add_argument("keyword", nargs="?", help="搜索关键词")
    parser.add_argument("--limit", type=int, default=10,
                        help="下载条数（默认 10）")
    parser.add_argument("--login", action="store_true",
                        help="只扫码登录不搜索")
    parser.add_argument("--selftest", action="store_true",
                        help="运行内置自测（不联网、不开浏览器）")
    args = parser.parse_args(argv)
    if args.selftest:
        sys.exit(0 if run_selftests() else 1)
    try:
        if args.login:
            login_only()
            return
        if not args.keyword:
            parser.error("请提供搜索关键词")
        out_dir = DOWNLOADS_DIR / (safe_dir_name(args.keyword) or "搜索结果")
        out_dir.mkdir(parents=True, exist_ok=True)
        items = collect_ids(args.keyword, args.limit)
        print(f"搜索到 {len(items)} 条（目标 {args.limit}）")
        if len(items) < args.limit:
            print("提示：结果不足 limit，下载已拿到的条目")
        download_all(items, out_dir)
    except SearchError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 3: 全量自测 + CLI 冒烟**

Run: `cd d:/templet/app/douyin-dl && python douyin_search.py --selftest 2>&1 | tail -1 && python douyin_search.py 2>&1 | tail -1; echo "exit=$?"`
Expected: `4/4 项通过`；无关键词报 `error: 请提供搜索关键词`，exit=2

---

### Task 5: 端到端验收（需用户扫码一次）

**Files:**
- 无代码改动（如验收发现 bug，修复后复跑本任务）

- [ ] **Step 1: 安装依赖**

Run: `python -m pip install playwright && python -m playwright install chromium`
Expected: 均成功退出 0

- [ ] **Step 2: 扫码登录**

Run: `cd d:/templet/app/douyin-dl && python douyin_search.py --login`
Expected: 弹出浏览器→用户扫码→打印"登录成功。"→自动退出；`.browser-profile/` 生成

- [ ] **Step 3: 真实搜索下载**

Run: `cd d:/templet/app/douyin-dl && python douyin_search.py "AI 短剧" --limit 5`
Expected: 打印"搜索到 N 条（目标 5）"；逐条下载；汇总 成功≥3 / 跳过 / 失败；
`downloads/AI 短剧/` 下出现 ≥3 个 mp4（各 >1MB，抽查魔数 ftyp）

- [ ] **Step 4: 幂等复跑**

再 Run 一次 Step 3 命令
Expected: 每条"已存在，跳过"，无新增文件

- [ ] **Step 5: 用户目视确认无水印 + 文档同步**

用户播放确认；将设计文档错误表中"30s 未拦截到搜索响应"措辞同步为
"滚动数轮仍未拦截到搜索响应"（与实现一致），并在验收记录段落追加结果。

---

## Self-Review 记录

1. **Spec 覆盖**：CLI 四形态(T1/T4)、扫码登录+120s 超时(T3)、拦截解析去重丢广告(T1/T3)、
   滚动停止条件(T3)、单条跳过+汇总(T4)、downloads/{关键词}(T2/T4)、playwright/Chromium
   未装提示(T3)、结果不足提示(T4)、幂等(T5)、验收标准(T5) —— 全覆盖。
2. **占位符扫描**：无 TBD/TODO；Task 3 含一处已明示并更正的笔误说明（seen/items 类型），
   以更正行为准。
3. **类型/命名一致**：`parse_search_response` / `has_login` / `safe_dir_name` /
   `open_browser` / `_first_page` / `ensure_login` / `login_only` / `collect_ids` /
   `download_all` / `main` / `run_selftests` / `SearchError` / `PLAYWRIGHT_OK` /
   常量名在各任务间一致；`douyin_dl.run(text, out_dir)` 签名与现有实现一致。
