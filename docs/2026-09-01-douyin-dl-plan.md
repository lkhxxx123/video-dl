# 抖音无水印下载器 douyin_dl.py 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个单文件 Python CLI：粘贴抖音分享口令/链接，下载该视频的无水印 mp4。

**Architecture:** 4 步流水线（正则提取链接 → 重定向拿 aweme_id → iesdouyin 分享页 `window._ROUTER_DATA` 解析 `play_addr` 并把 `playwm` 换 `play` → 流式下载）。纯逻辑全部收敛在模块级函数，内置 `--selftest` 断言框架做 TDD；网络层不可离线测试，靠端到端验收。

**Tech Stack:** Python 3.10（本机 `python` 命令）、requests（唯一三方依赖）

**Spec:** `d:\templet\app\douyin-dl\docs\2026-09-01-douyin-downloader-design.md`

## Global Constraints

- 本机 Python 一律用 `python` 命令（`python3` 是坏 stub，禁止使用）
- 三方依赖仅 `requests`，不得引入其他包
- `d:\templet` 不是 git 仓库：**所有任务无 commit 步骤**，验证以命令输出为准
- 脚本是单文件：`d:\templet\app\douyin-dl\douyin_dl.py`
- 默认输出目录 = 脚本所在目录 `downloads/`（`-o` 可覆盖）
- 退出码：0 成功 / 1 失败；控制台输出 UTF-8
- 所有网络请求：iPhone UA、10s 超时、自动重试 2 次
- 每个任务的测试都追加到 `douyin_dl.py` 内的 `test_*` 函数，用 `--selftest` 运行

---

### Task 1: 脚本骨架 + selftest 框架 + 链接提取

**Files:**
- Create: `d:\templet\app\douyin-dl\douyin_dl.py`

**Interfaces:**
- Produces: `extract_share_url(text: str) -> str | None`；selftest 约定（模块级 `test_*` 函数自动收集）；常量 `IPHONE_UA`；`python douyin_dl.py --selftest` 入口

- [ ] **Step 1: 环境检查**

Run: `python -c "import requests; print(requests.__version__)"`
Expected: 打印版本号（如 `2.32.x`）。若报 `ModuleNotFoundError`，先执行 `python -m pip install requests` 再复查。

- [ ] **Step 2: 写脚本骨架 + 失败测试**

创建 `d:\templet\app\douyin-dl\douyin_dl.py`，内容如下（测试函数先于实现——`extract_share_url` 此时未定义，测试必须 FAIL）：

```python
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""douyin_dl.py — 抖音单条无水印视频下载器
仅限个人离线保存；请尊重创作者版权，勿去水印二次上传。
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
TIMEOUT = 10
RETRIES = 2


class ParseError(Exception):
    """解析失败（链接无效 / 页面结构变更 / 触发风控等）。"""


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
        except Exception as e:  # noqa: BLE001 - selftest 要抓住一切
            failed += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"selftest: {len(tests) - failed}/{len(tests)} 项通过")
    return failed == 0


# ---------- tests: 链接提取 ----------

def test_extract_share_url_from_token_text():
    text = ("8.83 KJj:/ 复制打开抖音，看看【某人】的作品 "
            "https://v.douyin.com/iAbCdEf/ 哈哈哈")
    assert extract_share_url(text) == "https://v.douyin.com/iAbCdEf/"


def test_extract_share_url_bare_link():
    assert extract_share_url("https://v.douyin.com/iAbCdEf") == \
        "https://v.douyin.com/iAbCdEf"


def test_extract_share_url_web_link():
    text = "看这个 https://www.douyin.com/video/7345678901234567890 好看"
    assert extract_share_url(text) == \
        "https://www.douyin.com/video/7345678901234567890"


def test_extract_share_url_none():
    assert extract_share_url("这段文字里没有链接") is None


def test_extract_share_url_first_match():
    text = "https://v.douyin.com/aaa/ 和 https://v.douyin.com/bbb/"
    assert extract_share_url(text) == "https://v.douyin.com/aaa/"
```

- [ ] **Step 3: 跑 selftest 确认失败**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py --selftest`
Expected: 打印 usage 报错（`--selftest` 分支还没接）或直接退出——本任务先把 `main` 一起接上即可看到 5 项 `FAIL ... NameError: name 'extract_share_url' is not defined`。为让命令能跑，把下面的 `main` 同时写入文件末尾：

```python
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="抖音单条无水印视频下载器（仅限个人保存）")
    parser.add_argument("text", nargs="?", help="含抖音分享链接/口令的任意文本")
    parser.add_argument("-o", "--out-dir", default=None,
                        help="输出目录（默认: 脚本目录/downloads）")
    parser.add_argument("--selftest", action="store_true",
                        help="运行内置自测（不联网）")
    args = parser.parse_args(argv)
    if args.selftest:
        sys.exit(0 if run_selftests() else 1)
    raise SystemExit("run() 尚未实现（Task 5 接入）")


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
```

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py --selftest`
Expected: `FAIL test_extract_share_url_*: NameError ...` 共 5 项 FAIL

- [ ] **Step 4: 最小实现 extract_share_url**

在 `ParseError` 类之后、selftest 区之前插入：

```python
# ---------- 链接提取 ----------

SHARE_URL_RE = re.compile(
    r"https?://v\.douyin\.com/[\w-]+/?"
    r"|https?://www\.douyin\.com/video/\d+",
    re.IGNORECASE,
)


def extract_share_url(text: str):
    """从任意粘贴文本中提取第一个抖音视频链接，找不到返回 None。"""
    m = SHARE_URL_RE.search(text)
    return m.group(0) if m else None
```

- [ ] **Step 5: 跑 selftest 确认通过**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py --selftest`
Expected: `5/5 项通过`，退出码 0

---

### Task 2: aweme_id 提取

**Files:**
- Modify: `d:\templet\app\douyin-dl\douyin_dl.py`

**Interfaces:**
- Consumes: selftest 框架（Task 1）
- Produces: `extract_aweme_id(url: str) -> str | None`（纯数字 ID 或 None）

- [ ] **Step 1: 写失败测试**

追加到测试区：

```python
# ---------- tests: aweme_id 提取 ----------

def test_extract_aweme_id_share_video():
    url = ("https://www.iesdouyin.com/share/video/7345678901234567890/"
           "?region=&mid=123")
    assert extract_aweme_id(url) == "7345678901234567890"


def test_extract_aweme_id_note_and_plain():
    assert extract_aweme_id(
        "https://www.douyin.com/video/7300000000000000000") == \
        "7300000000000000000"
    assert extract_aweme_id(
        "https://www.iesdouyin.com/share/note/7311111111111111111") == \
        "7311111111111111111"


def test_extract_aweme_id_none():
    assert extract_aweme_id("https://www.douyin.com/user/abc") is None
```

- [ ] **Step 2: 跑 selftest 确认失败**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py --selftest`
Expected: 新增 3 项 FAIL（NameError），原 5 项仍 PASS（共 `5/8 项通过`）

- [ ] **Step 3: 实现**

追加到链接提取区：

```python
AWEME_ID_RE = re.compile(r"/(?:video|note)/(\d{5,})")


def extract_aweme_id(url: str):
    """从 URL 中提取纯数字视频 ID，找不到返回 None。"""
    m = AWEME_ID_RE.search(url)
    return m.group(1) if m else None
```

- [ ] **Step 4: 跑 selftest 确认通过**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py --selftest`
Expected: `8/8 项通过`

---

### Task 3: `_ROUTER_DATA` 解析（无水印地址 + 元信息 + 图集检测）

**Files:**
- Modify: `d:\templet\app\douyin-dl\douyin_dl.py`

**Interfaces:**
- Consumes: `ParseError`（Task 1）、selftest 框架
- Produces:
  - `extract_router_data(html: str) -> dict`（失败 raise `ParseError`）
  - `find_item(router_data: dict) -> dict`（找不到 raise `ParseError`）
  - `parse_item(item: dict) -> dict`，返回
    `{"no_wm_url": str, "title": str, "author": str, "is_image_set": bool}`
    （无 `play_addr` raise `ParseError`）

- [ ] **Step 1: 写失败测试**

追加到测试区（含共用夹具函数 `_sample_router_html`）：

```python
# ---------- tests: _ROUTER_DATA 解析 ----------

def _sample_router_html(key="video_(abc123)/page", item_overrides=None):
    item = {
        "desc": "示例 标题",
        "author": {"nickname": "作者A"},
        "video": {"play_addr": {"url_list": [
            "https://www.iesdouyin.com/aweme/v1/playwm/?video_id=999&r=1080p"
        ]}},
    }
    if item_overrides:
        item.update(item_overrides)
    data = {"loaderData": {key: {"videoInfoRes": {"item_list": [item]}}}}
    return "<script>window._ROUTER_DATA = " + json.dumps(
        data, ensure_ascii=False) + ";</script>"


def test_parse_pipeline_playwm_to_play():
    html = _sample_router_html(key="video_(xyz999)/page")
    info = parse_item(find_item(extract_router_data(html)))
    assert info["no_wm_url"] == \
        "https://www.iesdouyin.com/aweme/v1/play/?video_id=999&r=1080p"
    assert "playwm" not in info["no_wm_url"]
    assert info["title"] == "示例 标题"
    assert info["author"] == "作者A"
    assert info["is_image_set"] is False


def test_parse_detects_image_set():
    html = _sample_router_html(item_overrides={"images": [{"url": "x"}]})
    info = parse_item(find_item(extract_router_data(html)))
    assert info["is_image_set"] is True


def test_router_data_missing_raises():
    try:
        extract_router_data("<html>没有数据</html>")
        assert False, "应当抛 ParseError"
    except ParseError:
        pass


def test_find_item_no_match_raises():
    try:
        find_item({"loaderData": {"foo": {"bar": 1}}})
        assert False, "应当抛 ParseError"
    except ParseError:
        pass


def test_parse_item_no_play_addr_raises():
    try:
        parse_item({"desc": "x", "author": {}})
        assert False, "应当抛 ParseError"
    except ParseError:
        pass
```

- [ ] **Step 2: 跑 selftest 确认失败**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py --selftest`
Expected: 新增 5 项 FAIL（NameError），`8/13 项通过`

- [ ] **Step 3: 实现**

追加（放在 Task 2 的 aweme_id 实现之后）：

```python
# ---------- 分享页 _ROUTER_DATA 解析 ----------

ROUTER_DATA_RE = re.compile(
    r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)


def extract_router_data(html: str) -> dict:
    """从分享页 HTML 提取 window._ROUTER_DATA JSON。"""
    m = ROUTER_DATA_RE.search(html)
    if not m:
        raise ParseError(
            "页面中没有 _ROUTER_DATA：分享页结构可能已变更，或触发风控，请稍后重试")
    try:
        return json.loads(m.group(1).strip().rstrip(";"))
    except json.JSONDecodeError as e:
        raise ParseError(f"_ROUTER_DATA JSON 解析失败: {e}") from e


def find_item(router_data: dict) -> dict:
    """遍历 loaderData 找含 videoInfoRes 的分支（不依赖字面 key 名）。"""
    for value in router_data.get("loaderData", {}).values():
        if not isinstance(value, dict):
            continue
        info = value.get("videoInfoRes")
        if isinstance(info, dict) and info.get("item_list"):
            return info["item_list"][0]
    raise ParseError("loaderData 中找不到 videoInfoRes/item_list")


def parse_item(item: dict) -> dict:
    """从 item 提取无水印地址与元信息。"""
    url_list = (item.get("video", {}).get("play_addr", {})
                .get("url_list") or [])
    if not url_list:
        raise ParseError("item 中没有 video.play_addr.url_list")
    return {
        "no_wm_url": url_list[0].replace("playwm", "play"),
        "title": item.get("desc") or "",
        "author": (item.get("author") or {}).get("nickname", ""),
        "is_image_set": bool(item.get("images")),
    }
```

- [ ] **Step 4: 跑 selftest 确认通过**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py --selftest`
Expected: `13/13 项通过`

---

### Task 4: 文件名清洗

**Files:**
- Modify: `d:\templet\app\douyin-dl\douyin_dl.py`

**Interfaces:**
- Consumes: selftest 框架
- Produces: `build_filename(title: str, aweme_id: str) -> str`

- [ ] **Step 1: 写失败测试**

```python
# ---------- tests: 文件名清洗 ----------

def test_build_filename_cleans_and_truncates():
    title = '好"视频:/<标题>|续\n第二行' + "长" * 80
    name = build_filename(title, "7300000000000000000")
    for ch in '\\/:*?"<>|\r\n':
        assert ch not in name, f"非法字符 {ch!r} 残留"
    assert len(name) < 80, "应当截断标题"
    assert name.endswith("_7300000000000000000.mp4")


def test_build_filename_keeps_normal_title():
    assert build_filename("普通的标题", "123") == "普通的标题_123.mp4"


def test_build_filename_fallback_id_only():
    assert build_filename("", "1234567890123456789") == \
        "1234567890123456789.mp4"
    assert build_filename("   ", "1234567890123456789") == \
        "1234567890123456789.mp4"
```

- [ ] **Step 2: 跑 selftest 确认失败**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py --selftest`
Expected: 新增 3 项 FAIL，`13/16 项通过`

- [ ] **Step 3: 实现**

```python
# ---------- 文件名 ----------

INVALID_FN_RE = re.compile(r'[\\/:*?"<>|\r\n]')


def build_filename(title: str, aweme_id: str) -> str:
    """标题清洗(非法字符→空格)+截断50字；空标题回退纯 ID。"""
    clean = INVALID_FN_RE.sub(" ", title).strip()[:50].strip()
    return f"{clean}_{aweme_id}.mp4" if clean else f"{aweme_id}.mp4"
```

- [ ] **Step 4: 跑 selftest 确认通过**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py --selftest`
Expected: `16/16 项通过`

---

### Task 5: 网络层 + 编排 + CLI 完整接入 + 端到端验收

**Files:**
- Modify: `d:\templet\app\douyin-dl\douyin_dl.py`（替换 Task 1 的临时代码）

**Interfaces:**
- Consumes: Task 1-4 的全部函数；`IPHONE_UA`、`TIMEOUT`、`RETRIES`、`ParseError`
- Produces:
  - `http_get_with_retry(session, url, stream=False) -> requests.Response`（重试 2 次后仍失败 raise 最后一个异常）
  - `download_video(session, url, dest: Path) -> None`（<1KB 视为异常并删除半成品）
  - `run(text: str, out_dir: Path) -> Path`（完整流水线，返回保存路径）
  - 完整 `main(argv=None)`（`text` 缺省时交互式 `input()`；错误统一 stderr + exit 1）

- [ ] **Step 1: 实现网络层与编排（本任务无可离线测试的逻辑，靠端到端验收）**

在文件名实现之后追加：

```python
# ---------- 网络层 ----------

def http_get_with_retry(session, url, stream=False):
    """GET，10s 超时，自动重试 RETRIES 次；最终失败抛最后一个异常。"""
    last_exc = None
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT, stream=stream)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc


def download_video(session, url, dest: Path) -> None:
    """流式下载到 dest；小于 1KB 视为异常并清理半成品。"""
    r = http_get_with_retry(session, url, stream=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
    finally:
        r.close()
    if total < 1024:
        dest.unlink(missing_ok=True)
        raise ParseError(f"下载内容异常（仅 {total} 字节），链接可能已失效")


# ---------- 编排 ----------

def run(text: str, out_dir: Path) -> Path:
    url = extract_share_url(text)
    if not url:
        raise ParseError("没有识别到抖音链接，请粘贴完整分享口令")
    with requests.Session() as s:
        s.headers["User-Agent"] = IPHONE_UA
        if "v.douyin.com" in url:
            final_url = http_get_with_retry(s, url).url  # 跟随 302
            aweme_id = extract_aweme_id(final_url)
        else:
            aweme_id = extract_aweme_id(url)
        if not aweme_id:
            raise ParseError("拿不到视频 ID：视频可能已删除或为私密内容")
        page = http_get_with_retry(
            s, f"https://www.iesdouyin.com/share/video/{aweme_id}").text
        info = parse_item(find_item(extract_router_data(page)))
        if info["is_image_set"]:
            raise ParseError("这是图集（图文）作品，本脚本仅支持视频")
        dest = out_dir / build_filename(info["title"], aweme_id)
        if dest.exists():
            print(f"已存在，跳过: {dest}")
            return dest
        print(f"标题: {info['title'] or '(无)'}")
        print(f"作者: {info['author'] or '(未知)'}")
        print("下载中…")
        download_video(s, info["no_wm_url"], dest)
    print(f"已保存: {dest}")
    return dest
```

- [ ] **Step 2: 替换 main 为最终版**

用下面内容**整体替换** Task 1 的临时代码（`def main` 到文件末尾的 `if __name__` 保留不动，只换 `main` 函数体）：

```python
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="抖音单条无水印视频下载器（仅限个人保存）")
    parser.add_argument("text", nargs="?", help="含抖音分享链接/口令的任意文本")
    parser.add_argument("-o", "--out-dir", default=None,
                        help="输出目录（默认: 脚本目录/downloads）")
    parser.add_argument("--selftest", action="store_true",
                        help="运行内置自测（不联网）")
    args = parser.parse_args(argv)
    if args.selftest:
        sys.exit(0 if run_selftests() else 1)
    text = args.text if args.text is not None else \
        input("粘贴抖音分享口令/链接: ").strip()
    out_dir = Path(args.out_dir) if args.out_dir else \
        Path(__file__).resolve().parent / "downloads"
    try:
        run(text, out_dir)
    except (ParseError, requests.RequestException, OSError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 3: 全量自测**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py --selftest`
Expected: `16/16 项通过`，退出码 0

- [ ] **Step 4: 错误路径冒烟（不联网成功路径）**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py "没有链接的文本"; echo "exit=$?"`
Expected: stderr 打印 `错误: 没有识别到抖音链接…`，`exit=1`

- [ ] **Step 5: 端到端验收（需用户提供一条真实分享口令）**

Run: `cd d:/templet/app/douyin-dl && python douyin_dl.py "<用户提供的口令>"`
Expected: 打印标题/作者/下载中/已保存；`downloads/` 下出现 mp4，`> 1MB`；用播放器目视确认**画面四角无抖音水印、可完整播放**。

再 Run 一次同样命令
Expected: `已存在，跳过: …`，文件不被重复下载。

---

## Self-Review 记录

1. **Spec 覆盖**：两种链接提取(T1)、ID 提取(T2)、ROUTER_DATA 模糊匹配+playwm→play(T3)、图集检测(T3/T5)、文件名清洗回退(T4)、超时重试(T5)、幂等跳过(T5)、CLI/交互/-o/--selftest(T1/T5)、UTF-8 与退出码(T1/T5)、错误信息表(T5 Step 4/5)、验收标准(T5 Step 3/5)——全部有对应任务。
2. **占位符扫描**：无 TBD/TODO；所有代码步骤给出完整代码。
3. **命名一致性**：`extract_share_url` / `extract_aweme_id` / `extract_router_data` / `find_item` / `parse_item` / `build_filename` / `http_get_with_retry` / `download_video` / `run` / `main` / `run_selftests` / `ParseError` 在各任务间引用一致。
