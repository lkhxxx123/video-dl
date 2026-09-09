#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""platforms.douyin.dl — 抖音单条无水印视频下载
链接提取 / 分享页 _ROUTER_DATA 解析 / 浏览器兜底路线 / 下载编排。
仅限个人离线保存；请尊重创作者版权，勿去水印二次上传。

（原 douyin_dl.py 的抖音专属部分，HTTP 引擎/命名/清单已在 core）
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

from core import bootstrap
from core import paths
from core.reporting import log, urgent
from core import selftest as _st
from core.download import (IPHONE_UA, ParseError, WEB_HEADERS, download_video,
                           http_get_with_retry)
from core.naming import build_filename, record_manifest
from .config import DETAIL_SUBSTR

BROWSER_PROFILE_DIR = paths.PROFILE_DIR  # 与合集模式共用主登录态


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


# ---------- 浏览器兜底路线（分享页被风控时使用） ----------

def _capture_detail(resp, got):
    if DETAIL_SUBSTR in resp.url:
        try:
            got.append(resp.json())
        except Exception:
            pass


def _resolve_detail_in_page(page, aweme_id: str) -> dict:
    """在给定页面上打开视频页拦 detail 接口。

    兜底核心——可复用流式模式的 worker 页（同进程已有 sync_playwright
    在跑时不能再嵌套开一个，会报 "Sync API inside the asyncio loop"）。
    结束后跳回 about:blank：视频页会自动播放，挂在后台吃内存/带宽，
    通宵跑会累积成浏览器崩溃（实测 Connection closed 断连）。
    """
    got = []

    def _on_resp(r):
        _capture_detail(r, got)

    try:
        page.on("response", _on_resp)
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
        # 监听器必须摘除：兜底被频繁调用时(分享页风控期)不移除会无限
        # 叠加，每个都攥着响应数据 → 内存/CPU 复合增长 → 浏览器崩溃
        try:
            page.remove_listener("response", _on_resp)
        except Exception:
            pass
        try:
            page.goto("about:blank", timeout=10000)  # 卸载视频页
        except Exception:
            pass


def resolve_via_browser(aweme_id: str, page=None) -> dict:
    """无头登录浏览器打开视频页，拦截 detail 接口拿无水印直链。

    返回 {"urls": [直链...], "title": str, "author": str}。
    page: 复用已打开页面（流式 worker 页），或返回页的工厂函数——
    后台闲置页签会被 Chromium 丢弃(Tab Discard)，工厂在用时检查
    is_closed 并重开；None 自开登录浏览器（需先 --login）。
    """
    if page is not None:
        pg = page() if callable(page) else page
        return _resolve_detail_in_page(pg, aweme_id)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ParseError(
            "分享页被风控且 playwright 未安装，无法走兜底: "
            "pip install playwright && playwright install chromium")
    if not BROWSER_PROFILE_DIR.exists():
        raise ParseError("分享页被风控且无浏览器登录态，"
                         "请先运行: python run.py login")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR), headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"])
        try:
            pg = context.pages[0] if context.pages else context.new_page()
            return _resolve_detail_in_page(pg, aweme_id)
        finally:
            context.close()


def _via_browser(s, aweme_id: str, out_dir: Path, name_prefix: str,
                 page=None) -> Path:
    """浏览器兜底下载：拦 detail 接口拿 web 无水印直链再下。

    用于：分享页被风控拿不到数据；分享页直链失效(如 404，付费/受限内容)。
    page: 页或页工厂（流式 worker 页），避免嵌套 sync_playwright 崩溃。
    """
    log("  (切换浏览器兜底路线…)", flush=True)
    fb = resolve_via_browser(aweme_id, page=page)
    dest = out_dir / (name_prefix + build_filename(fb["title"], aweme_id,
                                                    fb.get("author", "")))
    if dest.exists():
        log(f"已存在，跳过: {dest}")
        record_manifest(out_dir, dest.name, fb["title"], fb["author"],
                        fb.get("mix_name", ""), aweme_id)
        return dest
    log(f"标题: {fb['title'] or '(无)'}")
    log(f"作者: {fb['author'] or '(未知)'}")
    log("下载中…")
    last_exc = None
    for i, u in enumerate(fb["urls"], 1):
        try:
            download_video(s, u, dest, headers=WEB_HEADERS)
            break
        except Exception as e:  # noqa: BLE001 - 逐个直链尝试
            log(f"  (直链{i}失败: {type(e).__name__}: {e})", flush=True)
            last_exc = e
    else:
        raise last_exc or ParseError("兜底路线下载失败")
    record_manifest(out_dir, dest.name, fb["title"], fb["author"],
                    fb.get("mix_name", ""), aweme_id)
    log(f"已保存: {dest}")
    return dest


# ---------- 编排 ----------

def run(text: str, out_dir: Path, name_prefix: str = "",
        fallback_page=None) -> Path:
    """下载视频；name_prefix 用于剧集集数前缀（如 '07_'）保证排序。

    fallback_page: 已打开的浏览器页或页工厂（流式模式的 worker 页），
    供兜底路线复用——避免在已运行的 sync_playwright 里嵌套再开一个
    而崩溃；页工厂能在页签被 Chromium 丢弃时自动重开。
    """
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
            # 分享页被风控 → 浏览器兜底路线
            log("  (分享页被风控)", flush=True)
            return _via_browser(s, aweme_id, out_dir, name_prefix,
                                page=fallback_page)
        info = parse_item(item)
        dest = out_dir / (name_prefix +
                          build_filename(info["title"], aweme_id,
                                         info.get("author", "")))
        if dest.exists():
            log(f"已存在，跳过: {dest}")
            record_manifest(out_dir, dest.name, info["title"],
                            info["author"], info.get("mix_name", ""),
                            aweme_id)
            return dest
        log(f"标题: {info['title'] or '(无)'}")
        log(f"作者: {info['author'] or '(未知)'}")
        log("下载中…")
        try:
            download_video(s, info["no_wm_url"], dest)
        except Exception as e:  # noqa: BLE001 - 直链失效(如404)也走兜底
            log(f"  (分享页直链失败: {type(e).__name__}，尝试浏览器兜底)",
                  flush=True)
            return _via_browser(s, aweme_id, out_dir, name_prefix,
                                page=fallback_page)
    record_manifest(out_dir, dest.name, info["title"], info["author"],
                    info.get("mix_name", ""), aweme_id)
    log(f"已保存: {dest}")
    return dest


# ---------- selftest ----------

def run_selftests():
    return _st.run_selftests(globals())


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


# ---------- tests: 其他 ----------

def test_parse_item_mix_name():
    item = {"desc": "t", "author": {"nickname": "a",
                                    "mix_info": {"mix_name": "合集Q"}},
            "video": {"play_addr": {"url_list": ["https://x/playwm/1"]}}}
    assert parse_item(item)["mix_name"] == "合集Q"
    top = {"desc": "t2", "mix_info": {"mix_name": "顶层合集"},
           "video": {"play_addr": {"url_list": ["https://y/playwm/2"]}}}
    assert parse_item(top)["mix_name"] == "顶层合集"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="抖音单条无水印视频下载器（仅限个人保存）")
    parser.add_argument("text", nargs="?", help="含抖音分享链接/口令的任意文本")
    parser.add_argument("-o", "--out-dir", default=None,
                        help="输出目录（默认: 应用根/downloads）")
    parser.add_argument("--selftest", action="store_true",
                        help="运行内置自测（不联网）")
    args = parser.parse_args(argv)
    if args.selftest:
        sys.exit(0 if run_selftests() else 1)
    text = args.text if args.text is not None else \
        input("粘贴抖音分享口令/链接: ").strip()
    out_dir = Path(args.out_dir) if args.out_dir else paths.DOWNLOADS_DIR
    try:
        run(text, out_dir)
    except (ParseError, requests.RequestException, OSError) as e:
        log(f"错误: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    bootstrap.setup_stdio()
    main()
