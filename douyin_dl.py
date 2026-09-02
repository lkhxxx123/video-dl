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
SCRIPT_DIR = Path(__file__).resolve().parent
BROWSER_PROFILE_DIR = SCRIPT_DIR / ".browser-profile"
DETAIL_SUBSTR = "/aweme/v1/web/aweme/detail/"
# web CDN 直链需要的请求头（实测 2026-09：无 Referer 会 403）
WEB_HEADERS = {
    "Referer": "https://www.douyin.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
}


class ParseError(Exception):
    """解析失败（链接无效 / 页面结构变更 / 触发风控等）。"""


# ---------- 链接提取 ----------

SHARE_URL_RE = re.compile(
    r"https?://v\.douyin\.com/[A-Za-z0-9_-]+/?"
    r"|https?://www\.douyin\.com/video/\d+"
    r"|https?://(?:www\.)?douyin\.com/[\w./?=%&+~-]*[?&]modal_id=\d+"
    r"[\w./?=%&+~-]*",
    re.IGNORECASE,
)


def extract_share_url(text: str):
    """从任意粘贴文本中提取第一个抖音视频链接，找不到返回 None。"""
    m = SHARE_URL_RE.search(text)
    return m.group(0) if m else None


AWEME_ID_RE = re.compile(r"/(?:video|note)/(\d{5,})")
MODAL_ID_RE = re.compile(r"[?&]modal_id=(\d{5,})")


def extract_aweme_id(url: str):
    """从 URL 中提取纯数字视频 ID（路径 /video|note/{id} 或参数 modal_id），
    找不到返回 None。"""
    m = AWEME_ID_RE.search(url)
    if m:
        return m.group(1)
    m = MODAL_ID_RE.search(url)
    return m.group(1) if m else None


# ---------- 分享页 _ROUTER_DATA 解析 ----------

ROUTER_DATA_RE = re.compile(
    r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)


def extract_router_data(html: str) -> dict:
    """从分享页 HTML 提取 window._ROUTER_DATA JSON。"""
    m = ROUTER_DATA_RE.search(html)
    if not m:
        raise ParseError(
            "页面中没有 _ROUTER_DATA：分享页结构可能已变更，或触发风控，"
            "请稍后重试")
    try:
        return json.loads(m.group(1).strip().rstrip(";"))
    except json.JSONDecodeError as e:
        raise ParseError(
            f"_ROUTER_DATA JSON 解析失败: {e}"
            "（分享页结构可能已变更或触发风控，请稍后重试）") from e


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
    """从 item 提取无水印地址与元信息。

    有 play_addr 即视为视频（实测部分真实视频 item 也带 images 字段）；
    无 play_addr 且含 images 才判为图集。
    """
    url_list = (item.get("video", {}).get("play_addr", {})
                .get("url_list") or [])
    if url_list:
        mix = (item.get("mix_info") or
               (item.get("author") or {}).get("mix_info") or {})
        return {
            "no_wm_url": url_list[0].replace("playwm", "play"),
            "title": item.get("desc") or "",
            "author": (item.get("author") or {}).get("nickname", ""),
            "mix_name": mix.get("mix_name") or "",
        }
    if item.get("images"):
        raise ParseError("这是图集（图文）作品，本脚本仅支持视频")
    raise ParseError("item 中没有 video.play_addr.url_list")


def record_manifest(out_dir: Path, filename: str, title: str, author: str,
                    mix_name: str = "", vid: str = "") -> None:
    """登记视频元信息（作者/短剧名/标题）到 视频清单.json + .csv。

    按文件名去重，重复调用不产生重复行；CSV 用 UTF-8-BOM，Excel 直接可开。
    """
    mf = out_dir / "视频清单.json"
    data = {}
    if mf.exists():
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if filename in data:
        return
    data[filename] = {"time": time.strftime("%Y-%m-%d %H:%M"),
                      "author": author or "", "mix": mix_name or "",
                      "title": title or "", "vid": vid or ""}
    mf.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                  encoding="utf-8")
    with open(out_dir / "视频清单.csv", "w", encoding="utf-8-sig",
              newline="") as f:
        f.write("下载时间,作者,短剧名,标题,文件名,视频ID\n")
        for fn, m in data.items():
            row = [m["time"], m["author"], m["mix"], m["title"], fn,
                   m["vid"]]
            f.write(",".join('"' + str(c).replace('"', '""') + '"'
                             for c in row) + "\n")


# ---------- 文件名 ----------

INVALID_FN_RE = re.compile(r'[\\/:*?"<>|\r\n]')


def build_filename(title: str, aweme_id: str) -> str:
    """标题清洗(非法字符→空格)+截断50字；空标题回退纯 ID。"""
    clean = INVALID_FN_RE.sub(" ", title).strip()[:50].strip()
    return f"{clean}_{aweme_id}.mp4" if clean else f"{aweme_id}.mp4"


# ---------- 网络层 ----------

def http_get_with_retry(session, url, stream=False, headers=None):
    """GET，10s 超时，自动重试 RETRIES 次；最终失败抛最后一个异常。"""
    last_exc = None
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT, stream=stream,
                            headers=headers)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc


def download_video(session, url, dest: Path, headers=None) -> None:
    """流式下载到 dest；任何异常/过小内容都清理半成品后再抛错。"""
    r = http_get_with_retry(session, url, stream=True, headers=headers)
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
    except Exception:
        dest.unlink(missing_ok=True)  # 中途失败清半成品，避免幂等误判
        raise
    finally:
        r.close()
    if total < 1024:
        dest.unlink(missing_ok=True)
        raise ParseError(f"下载内容异常（仅 {total} 字节），链接可能已失效")


# ---------- 浏览器兜底路线（分享页被风控时使用） ----------

def _capture_detail(resp, got):
    if DETAIL_SUBSTR in resp.url:
        try:
            got.append(resp.json())
        except Exception:
            pass


def resolve_via_browser(aweme_id: str) -> dict:
    """无头登录浏览器打开视频页，拦截 detail 接口拿无水印直链。

    返回 {"urls": [直链...], "title": str, "author": str}。
    需已通过 `python douyin_search.py --login` 建立登录态。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ParseError(
            "分享页被风控且 playwright 未安装，无法走兜底: "
            "pip install playwright && playwright install chromium")
    if not BROWSER_PROFILE_DIR.exists():
        raise ParseError("分享页被风控且无浏览器登录态，"
                         "请先运行: python douyin_search.py --login")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR), headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        try:
            page = context.pages[0] if context.pages else context.new_page()
            got = []
            page.on("response", lambda r: _capture_detail(r, got))
            page.goto(f"https://www.douyin.com/video/{aweme_id}",
                      timeout=30000)
            deadline = time.time() + 20
            while time.time() < deadline:
                if got:
                    break
                page.wait_for_timeout(1500)
            if not got:
                raise ParseError("浏览器兜底: 未拦截到 detail 接口"
                                 "（可能需先 --login 或触发验证）")
            data = got[0].get("aweme_detail") or got[0]
            urls = [u for u in ((data.get("video") or {}).get("play_addr")
                                or {}).get("url_list") or []
                    if isinstance(u, str) and u.startswith("http")]
            if not urls:
                raise ParseError("浏览器兜底: detail 无 play_addr")
            mix = (data.get("mix_info") or
                   (data.get("author") or {}).get("mix_info") or {})
            author = data.get("author") or {}
            return {"urls": urls,
                    "title": data.get("desc") or "",
                    "author": author.get("nickname", ""),
                    "sec_uid": author.get("sec_uid") or "",
                    "mix_id": mix.get("mix_id"),
                    "mix_name": mix.get("mix_name") or "",
                    "episode_count": mix.get("episode_count")}
        finally:
            context.close()


# ---------- 编排 ----------

def run(text: str, out_dir: Path, name_prefix: str = "") -> Path:
    """下载视频；name_prefix 用于剧集集数前缀（如 '07_'）保证排序。"""
    url = extract_share_url(text)
    if not url:
        raise ParseError("没有识别到抖音链接，请粘贴完整分享口令")
    with requests.Session() as s:
        s.headers["User-Agent"] = IPHONE_UA
        if "v.douyin.com" in url.lower():
            final_url = http_get_with_retry(s, url).url  # 跟随 302
            aweme_id = extract_aweme_id(final_url)
        else:
            aweme_id = extract_aweme_id(url)
        if not aweme_id:
            raise ParseError("拿不到视频 ID：视频可能已删除或为私密内容")
        share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}"
        # 实测(2026-09)：首次请求常只种 ttwid cookie 返回壳页面，且带随机性
        # （连续快速请求可能连续壳页），需多次带间隔重试直到拿到数据
        item = None
        for _ in range(5):
            page = http_get_with_retry(s, share_url).text
            try:
                item = find_item(extract_router_data(page))
                break
            except ParseError:
                time.sleep(0.8)
        if item is None:
            # 分享页被风控 → 浏览器兜底路线（拦 detail 接口拿无水印直链）
            print("  (分享页被风控，切换浏览器兜底路线…)", flush=True)
            fb = resolve_via_browser(aweme_id)
            dest = out_dir / (name_prefix +
                              build_filename(fb["title"], aweme_id))
            if dest.exists():
                print(f"已存在，跳过: {dest}")
                record_manifest(out_dir, dest.name, fb["title"],
                                fb["author"], fb.get("mix_name", ""),
                                aweme_id)
                return dest
            print(f"标题: {fb['title'] or '(无)'}")
            print(f"作者: {fb['author'] or '(未知)'}")
            print("下载中…")
            last_exc = None
            for i, u in enumerate(fb["urls"], 1):
                try:
                    download_video(s, u, dest, headers=WEB_HEADERS)
                    break
                except Exception as e:  # noqa: BLE001 - 逐个直链尝试
                    print(f"  (直链{i}失败: {type(e).__name__}: {e})",
                          flush=True)
                    last_exc = e
            else:
                raise last_exc or ParseError("兜底路线下载失败")
            record_manifest(out_dir, dest.name, fb["title"], fb["author"],
                            fb.get("mix_name", ""), aweme_id)
            print(f"已保存: {dest}")
            return dest
        info = parse_item(item)
        dest = out_dir / (name_prefix +
                          build_filename(info["title"], aweme_id))
        if dest.exists():
            print(f"已存在，跳过: {dest}")
            record_manifest(out_dir, dest.name, info["title"],
                            info["author"], info.get("mix_name", ""),
                            aweme_id)
            return dest
        print(f"标题: {info['title'] or '(无)'}")
        print(f"作者: {info['author'] or '(未知)'}")
        print("下载中…")
        download_video(s, info["no_wm_url"], dest)
    record_manifest(out_dir, dest.name, info["title"], info["author"],
                    info.get("mix_name", ""), aweme_id)
    print(f"已保存: {dest}")
    return dest


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


def test_extract_share_url_stops_at_cjk():
    assert extract_share_url("https://v.douyin.com/iAbCdEf哈哈哈哈") == \
        "https://v.douyin.com/iAbCdEf"


def test_extract_share_url_modal_id():
    url = ("https://www.douyin.com/user/self/search/AI"
           "?aid=d452eeaf-8cb5-4c53-9dba-03fc2b25e9b1"
           "&modal_id=7673851043746221352&type=general")
    assert extract_share_url(url) == url


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


def test_extract_aweme_id_modal_id_param():
    url = ("https://www.douyin.com/user/self/search/AI?aid=x"
           "&modal_id=7673851043746221352&type=general")
    assert extract_aweme_id(url) == "7673851043746221352"


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


def test_parse_item_with_images_and_play_addr_is_video():
    # 实测(2026-09)：真实视频 item 也可能带 images 字段；有 play_addr 即视频
    html = _sample_router_html(item_overrides={"images": [{"url": "x"}]})
    info = parse_item(find_item(extract_router_data(html)))
    assert info["no_wm_url"] == \
        "https://www.iesdouyin.com/aweme/v1/play/?video_id=999&r=1080p"


def test_parse_item_image_set_without_play_addr_raises_image_set_error():
    try:
        parse_item({"desc": "x", "author": {}, "images": [{"url": "y"}]})
        assert False, "应当抛图集 ParseError"
    except ParseError as e:
        assert "图集" in str(e)


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


# ---------- tests: 文件名清洗 ----------

def test_parse_item_mix_name():
    item = {"desc": "t", "author": {"nickname": "a",
                                    "mix_info": {"mix_name": "合集Q"}},
            "video": {"play_addr": {"url_list": ["https://x/playwm/1"]}}}
    assert parse_item(item)["mix_name"] == "合集Q"
    top = {"desc": "t2", "mix_info": {"mix_name": "顶层合集"},
           "video": {"play_addr": {"url_list": ["https://y/playwm/2"]}}}
    assert parse_item(top)["mix_name"] == "顶层合集"


def test_record_manifest_no_dup_and_csv():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        record_manifest(d, "a.mp4", "标题A", "作者X", "剧Z", "1" * 19)
        record_manifest(d, "a.mp4", "重复不写", "重复", "", "1" * 19)
        record_manifest(d, "b.mp4", '含"引号"的标题', "作者Y", "", "2" * 19)
        j = json.loads((d / "视频清单.json").read_text(encoding="utf-8"))
        assert len(j) == 2 and j["a.mp4"]["author"] == "作者X"
        csv_text = (d / "视频清单.csv").read_text(encoding="utf-8-sig")
        assert "作者X" in csv_text and "剧Z" in csv_text
        assert "重复不写" not in csv_text
        assert csv_text.count("\n") == 3  # 表头 + 2 行

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


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
