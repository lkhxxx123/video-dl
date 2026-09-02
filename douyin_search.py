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
import series_detect

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
VERIFY_WAIT = 240          # 滑块验证最长等待（实测用户可能不在屏幕前）
SCROLL_WAIT = 1.5
MAX_IDLE_SCROLLS = 3


class SearchError(Exception):
    """搜索/登录流程失败。"""


# 剧集面板接口有两种形态：合集(mix)与系列(series)，实测(2026-09)同一作者
# 只命中其中一种，两种都要拦
EPISODE_URL_SUBSTRS = ("/aweme/v1/web/mix/aweme/", "/aweme/v1/web/series/aweme/")
USER_POST_URL_SUBSTR = "/aweme/v1/web/aweme/post/"


def resp_json(resp):
    """playwright resp.json() 实测(2026-09)对部分抖音接口会抛错，
    统一走 text()+json.loads 兜底；失败返回 None。"""
    try:
        return resp.json()
    except Exception:
        try:
            return json.loads(resp.text())
        except Exception:
            return None


# ---------- 纯逻辑：搜索响应解析 ----------

def parse_search_response(payload: dict, seen: set):
    """从搜索响应提取新增条目 dict；去重，丢弃无 ID 条目。

    实测(2026-09)：条目在顶层 aweme_list（元素即 aweme 对象）；
    兼容旧形态 data[].aweme_info。返回 dict 含筛选/剧集/黑名单所需字段。
    """
    results = []
    entries = list(payload.get("aweme_list") or []) + \
        list(payload.get("data") or [])
    for entry in entries:
        info = entry.get("aweme_info") or entry
        aweme_id = info.get("aweme_id")
        if not aweme_id or aweme_id in seen:
            continue
        seen.add(aweme_id)
        author = info.get("author") or {}
        followers = author.get("follower_count")
        if followers is None:
            followers = author.get("mplatform_followers_count")
        duration_ms = (info.get("video") or {}).get("duration")
        if duration_ms is None:
            duration_ms = info.get("duration")
        mix = info.get("mix_info") or author.get("mix_info") or {}
        results.append({
            "aweme_id": aweme_id,
            "title": info.get("desc") or "",
            "digg": (info.get("statistics") or {}).get("digg_count"),
            "duration_ms": duration_ms,
            "followers": followers,
            "mix_id": mix.get("mix_id"),
            "mix_name": mix.get("mix_name") or "",
            "sig": author.get("signature") or "",
            "nick": author.get("nickname") or "",
            "sec_uid": author.get("sec_uid") or "",
        })
    return results


DEFAULT_BLOCK_KEYWORDS = ["搬运", "侵权", "联系删除", "如有侵权",
                          "仅供欣赏", "仅供学习", "转载", "免责"]


def author_blocked(item: dict, keywords):
    """作者简介/昵称/视频标题命中黑名单关键词（搬运/侵权类账号）。"""
    if not keywords:
        return False, ""
    text = " ".join([item.get("sig", ""), item.get("nick", ""),
                     item.get("title", "")])
    for kw in keywords:
        if kw and kw in text:
            return True, kw
    return False, ""


def parse_mix_response(payload: dict, seen: set):
    """mix/series 接口响应 → 新增集条目 [{aweme_id,title,ep,ct}]。"""
    out = []
    for e in payload.get("aweme_list") or []:
        aid = e.get("aweme_id")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        mix = e.get("mix_info") or {}
        out.append({"aweme_id": aid, "title": e.get("desc") or "",
                    "ep": mix.get("current_episode") or 0,
                    "ct": e.get("create_time") or 0})
    return out


def sort_episodes(items):
    """剧集排序：有官方集数按集数升序；否则按发布时间升序（返回新列表）。"""
    if any(it.get("ep") for it in items):
        return sorted(items, key=lambda x: (x.get("ep") or 0,
                                            x.get("ct") or 0))
    return sorted(items, key=lambda x: x.get("ct") or 0)


def episode_prefix(n: int, total: int) -> str:
    """集数文件名前缀：零填充保证资源管理器按名排序即观看顺序。"""
    width = max(2, len(str(max(total, 1))))
    return f"{n:0{width}d}_"


def looks_continuous(eps):
    """合集标题是否像同一部连续剧集（防"杂物合集"误下整部）。

    判据（满足其一）：≥2 个标题带集数标记；或 ≥60% 标题共享前 6 字前缀。
    返回 (是否连续, 依据说明)。
    """
    titles = [(e.get("title") or "").strip() for e in eps]
    titles = [t.split("#")[0].strip() or t for t in titles]  # 去话题标签
    if len(titles) < 2:
        return False, "集数不足 2"
    hint = sum(1 for t in titles if series_detect.episode_hint(t))
    if hint >= 2:
        return True, f"{hint} 个标题带集数标记"
    prefix = titles[0][:6]
    same = sum(1 for t in titles if t[:6] == prefix)
    if same >= max(2, len(titles) * 0.6):
        return True, f"{same}/{len(titles)} 标题共享前缀「{prefix}」"
    return False, "标题混杂（无集数标记也无共同前缀）"


def is_verify_block(payload: dict) -> bool:
    """识别风控软拦截：HTTP 200 + status_code 0 + search_nil_info 标记。"""
    nil = payload.get("search_nil_info") or {}
    return nil.get("search_nil_type") == "verify_check"


def split_keywords(text: str):
    """按中英文逗号拆分关键词，去空白与空项。"""
    return [k.strip() for k in re.split(r"[,，]", text) if k.strip()]


def passes_filter(item: dict, max_followers=None, max_duration=None,
                  max_likes=None):
    """筛选判定：严格小于才保留；启用的条件遇字段未知即拒绝。"""
    if max_likes is not None:
        if item["digg"] is None:
            return False, "点赞数未知"
        if item["digg"] >= max_likes:
            return False, f"点赞 {item['digg']} >= {max_likes}"
    if max_duration is not None:
        if item["duration_ms"] is None:
            return False, "时长未知"
        if item["duration_ms"] >= max_duration * 1000:
            return False, \
                f"时长 {item['duration_ms'] // 1000}s >= {max_duration}s"
    if max_followers is not None:
        if item["followers"] is None:
            return False, "粉丝数未知"
        if item["followers"] >= max_followers:
            return False, f"粉丝 {item['followers']} >= {max_followers}"
    return True, ""


def has_login(cookies) -> bool:
    """playwright context.cookies() 中存在非空 sessionid 即视为已登录。"""
    return any(c.get("name") == "sessionid" and c.get("value")
               for c in cookies)


def _search_urls(keyword: str, prefer_jingxuan: bool = False):
    """搜索路由优先级：标准 /search/ 为主，精选 /jingxuan/search/ 为兜底。

    实测(2026-09)：标准路由偶发 503（风控/抖动），精选路由仍可用。
    prefer_jingxuan=True 时精选优先（douyin_jingxuan 场景）。
    """
    kw = quote(keyword)
    urls = [f"https://www.douyin.com/search/{kw}?type=video",
            f"https://www.douyin.com/jingxuan/search/{kw}?type=video"]
    return urls[::-1] if prefer_jingxuan else urls


def safe_dir_name(name: str) -> str:
    """清洗为合法目录名：非法字符→空格、strip、截 50 字符。"""
    return douyin_dl.INVALID_FN_RE.sub(" ", name).strip()[:50]


def make_dated_dir(root: Path) -> Path:
    """按日期建目录 downloads/YYYY-MM-DD。

    同一天多次运行共用同一目录（数据叠加）——去重由全局 ID 扫描保证，
    状态文件/视频清单在同日内累积，断点续跑更顺。
    """
    d = root / time.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def existing_ids_under(root: Path) -> set:
    """递归收集 root 下所有 mp4 文件名尾部的视频 ID（跨目录全局去重）。"""
    ids = set()
    if root.is_dir():
        for f in root.rglob("*.mp4"):
            m = re.search(r"_(\d{15,})\.mp4$", f.name)
            if m:
                ids.add(m.group(1))
    return ids


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
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 900})
            # 降低自动化指纹，减少风控验证码概率（实测 2026-09）
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver',"
                " {get: () => undefined})")
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


def _wait_captcha(page, timeout=90) -> None:
    """标题含"验证"时提示用户手动完成验证码并等待放行。"""
    prompted = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "验证" not in page.title():
            return
        if not prompted:
            print(">>> 触发验证码：请在浏览器窗口中手动完成验证 <<<", flush=True)
            prompted = True
        page.wait_for_timeout(2000)
    raise SearchError(f"验证码等待超时（{timeout}s）")


def _collect_page(context, page, keyword, limit, max_followers=None,
                  max_duration=None, max_likes=None, seen=None,
                  block_keywords=None, prefer_jingxuan=False):
    """单个关键词的搜索收集（在已打开的浏览器页签内跳转）。

    成功返回合格列表；验证超时/无数据抛 SearchError（由调用方决定是否继续）。
    """
    if seen is None:
        seen = set()
    kept, raw = [], 0
    state = {"verify": False, "prompted": False, "prompted_at": 0.0,
             "reloaded": False}

    def on_response(resp):
        nonlocal raw
        if SEARCH_URL_PREFIX not in resp.url:
            return
        try:
            payload = resp.json()
        except Exception:
            return  # 非 JSON / 请求失败
        if is_verify_block(payload):
            state["verify"] = True
            return
        state["verify"] = False
        for it in parse_search_response(payload, seen):
            raw += 1
            ok, reason = passes_filter(it, max_followers, max_duration,
                                       max_likes)
            if ok:
                bad, kw = author_blocked(it, block_keywords)
                if bad:
                    print(f"  跳过: {(it['title'] or it['aweme_id'])[:24]}"
                          f"（作者黑名单: 简介含「{kw}」）", flush=True)
                    continue
                kept.append(it)
            else:
                print(f"  跳过: {(it['title'] or it['aweme_id'])[:24]}"
                      f"（{reason}）", flush=True)

    # 关键：监听器必须在 goto 之前挂上——搜索 XHR 在页面加载瞬间发出
    page.on("response", on_response)
    try:
        # 路由探测：标准 /search/ 12s 内无数据（含 503）→ 换 jingxuan 兜底
        route = ""
        for url in _search_urls(keyword, prefer_jingxuan):
            resp = page.goto(url, timeout=30000)
            route = url.split("/")[3] or "(根)"
            status = resp.status if resp else "?"
            landed = ((resp.url if resp else "?").split("/")[3]
                      if resp else "?")
            _wait_captcha(page)
            probe_deadline = time.time() + 12
            while raw == 0 and time.time() < probe_deadline:
                page.wait_for_timeout(1500)
            if raw:
                extra = f"（被跳转到 {landed}）" if landed != route else ""
                print(f"  (路由 {route} 命中{extra})", flush=True)
                break
            print(f"  (路由 {route} 无数据[HTTP {status}]"
                  f"实际落点 {landed}，切换下一条路由…)", flush=True)
        # 长等待：等首条有数据的响应；遇软拦截(verify_check)提示用户滑验证。
        # 提示 90s 后仍无数据则刷新页面重发搜索（验证通过后刷新即可拿到）
        first_deadline = time.time() + VERIFY_WAIT
        while raw == 0 and time.time() < first_deadline:
            if state["verify"] and not state["prompted"]:
                print(">>> 触发滑块验证：请在浏览器窗口中拖动滑块完成拼图 <<<",
                      flush=True)
                state["prompted"] = True
                state["prompted_at"] = time.time()
            if (state["prompted"] and not state["reloaded"]
                    and time.time() - state["prompted_at"] > 90):
                print("  (刷新页面重新触发搜索…)", flush=True)
                page.reload(timeout=30000)
                _wait_captcha(page)
                state["reloaded"] = True
            page.wait_for_timeout(1500)
        idle = 0
        while len(kept) < limit and idle < MAX_IDLE_SCROLLS:
            before = raw
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(int(SCROLL_WAIT * 1000))
            idle = 0 if raw > before else idle + 1
    finally:
        page.remove_listener("response", on_response)
    if raw == 0:
        if state["verify"]:
            raise SearchError(f"[{keyword}] 验证未完成或未通过")
        raise SearchError(f"[{keyword}] 未拦截到搜索响应：可能改版或风控")
    print(f"本词合格 {len(kept)} / 共 {raw} 条")
    return kept[:limit]


def collect_many(keywords, limit, max_followers=None, max_duration=None,
                 max_likes=None, seen=None, block_keywords=None,
                 prefer_jingxuan=False):
    """多关键词聚合：开一次浏览器，逐词收集，全局去重，凑够 limit 即停。

    seen: 额外提供的"已处理 ID 集合"（流水线复用，跳过历史视频）。
    block_keywords: 作者黑名单关键词（过滤搬运/侵权类账号）。
    """
    seen_ids = seen if seen is not None else set()
    merged = []
    with open_browser() as context:
        page = _first_page(context)
        ensure_login(context, page)
        for idx, kw in enumerate(keywords, 1):
            if len(merged) >= limit:
                break
            remaining = limit - len(merged)
            print(f"\n=== 关键词 [{idx}/{len(keywords)}] {kw}"
                  f"（还需 {remaining} 条）===", flush=True)
            try:
                merged.extend(_collect_page(context, page, kw, remaining,
                                            max_followers, max_duration,
                                            max_likes, seen_ids,
                                            block_keywords,
                                            prefer_jingxuan))
            except SearchError as e:
                print(f"  !! {e}，跳到下一个关键词", flush=True)
            print(f"累计合格 {len(merged)}/{limit}")
            if idx < len(keywords) and len(merged) < limit:
                time.sleep(random.uniform(3, 5))  # 词间降温
    return merged[:limit]


def _goto_episode_list(page) -> bool:
    """从视频页进入"剧集列表"整页（或全屏列表），使全部集可滚动翻页。

    实测(2026-09)：视频页面板只预取第一页。策略：
    ① DOM 里找合集/系列链接直接跳转；② 点击"共N集/合集/系列"入口。
    返回是否成功切换（失败则留在原页，靠窗口滚动兜底）。
    """
    href = page.evaluate(
        """() => {
            const pats = [/\\/mix\\//, /collection/i, /\\/series\\//];
            for (const a of document.querySelectorAll('a[href]')) {
                if (pats.some(p => p.test(a.href))) return a.href;
            }
            return null;
        }""")
    if href:
        try:
            page.goto(href, timeout=30000)
            _wait_captcha(page)
            print("  (已跳转剧集列表页：面板链接)", flush=True)
            return True
        except Exception:
            pass
    for pattern in (r"共\s*\d+\s*[集期]", "合集", "系列"):
        try:
            loc = page.get_by_text(re.compile(pattern)).first
            loc.click(timeout=3000)
            page.wait_for_timeout(2500)
            print(f"  (已打开剧集列表：点击「{pattern}」入口)", flush=True)
            return True
        except Exception:
            continue
    print("  (⚠ 未能进入剧集列表页，只有面板第一页)", flush=True)
    return False


def collect_mix(video_id: str):
    """进入剧集列表页收集全部集（合集 mix / 系列 series 双拦截）。

    关键(实测 2026-09)：视频页面板只预取第一页（例：34 集只见 6 集），
    必须进列表页整页滚动才能翻完。以 detail/响应携带的总集数做完成度
    校验，不足时打印 ⚠，不再静默截断。
    返回按集数升序的 [{aweme_id, title, ep}]；非剧集或无数据抛 SearchError。
    """
    with open_browser() as context:
        page = _first_page(context)
        ensure_login(context, page)
        seen, items = set(), []
        state = {"has_more": True, "total": None}

        def on_response(resp):
            url = resp.url
            if any(s in url for s in EPISODE_URL_SUBSTRS):
                payload = resp_json(resp)
                if payload is None:
                    return
                if payload.get("has_more") == 0:
                    state["has_more"] = False
                for key in ("total", "episode_count"):
                    v = payload.get(key)
                    if isinstance(v, int) and v > (state["total"] or 0):
                        state["total"] = v
                try:
                    items.extend(parse_mix_response(payload, seen))
                except Exception:
                    return
            elif "/aweme/v1/web/aweme/detail/" in url:
                payload = resp_json(resp)
                if payload:
                    data = payload.get("aweme_detail") or payload
                    mix = data.get("mix_info") or {}
                    ec = mix.get("episode_count")
                    if isinstance(ec, int):
                        state["total"] = ec

        page.on("response", on_response)
        try:
            page.goto(f"https://www.douyin.com/video/{video_id}",
                      timeout=30000)
            _wait_captcha(page)
            # 等首个剧集接口响应（面板第一页）
            deadline = time.time() + 20
            while time.time() < deadline:
                if items:
                    break
                page.wait_for_timeout(1500)
            # 关键：进入剧集列表整页，让全部集可滚动加载
            if _goto_episode_list(page):
                page.wait_for_timeout(2500)
            # 滚动拉全：JS 滚到底(不依赖焦点/坐标) + 模拟滚轮双保险
            idle = 0
            while idle < 12:
                if state["total"] and len(items) >= state["total"]:
                    break
                before = len(items)
                try:
                    page.evaluate(
                        "window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(1500)
                idle = 0 if len(items) > before else idle + 1
        finally:
            page.remove_listener("response", on_response)
        if not items:
            raise SearchError("未拦截到合集/系列接口（可能不是剧集或触发验证）")
        total = state["total"]
        if total and len(items) < total:
            print(f"  ⚠ 剧集只拿到 {len(items)}/{total} 集（翻页未完成）",
                  flush=True)
        elif not total:
            # 总集数未知时用标题里的最大集号推断，尽量发现截断
            expected = 0
            for it in items:
                expected = max(expected, it.get("ep") or 0,
                               series_detect.episode_hint(
                                   it.get("title") or ""))
            if expected > len(items):
                print(f"  ⚠ 剧集疑似不全：拿到 {len(items)} 集，"
                      f"但标题集数已达第 {expected} 集", flush=True)
        items = sort_episodes(items)
        return items


def parse_post_response(payload: dict, seen: set):
    """作者主页作品接口 → 新增 [{aweme_id, title, create_time}]。"""
    out = []
    for e in payload.get("aweme_list") or []:
        aid = e.get("aweme_id")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        out.append({"aweme_id": aid, "title": e.get("desc") or "",
                    "create_time": e.get("create_time") or 0})
    return out


def collect_user_posts(sec_uid: str, screenshot_to=None, max_scrolls=120):
    """打开作者主页，拦截 /aweme/v1/web/aweme/post/ 收集全部作品。

    返回按发布时间升序 [{aweme_id, title, create_time}]（无时间的排末尾）；
    screenshot_to: 可选 Path，回顶后截主页图（剧集判定的辅助证据）。
    翻页以响应 has_more=0 为准（实测(2026-09)懒加载较慢，需高耐心阈值）。
    """
    with open_browser() as context:
        page = _first_page(context)
        ensure_login(context, page)
        seen, items = set(), []
        state = {"has_more": True}

        def on_response(resp):
            if USER_POST_URL_SUBSTR not in resp.url:
                return
            payload = resp_json(resp)
            if payload is None:
                return
            if payload.get("has_more") == 0:
                state["has_more"] = False
            try:
                items.extend(parse_post_response(payload, seen))
            except Exception:
                return

        page.on("response", on_response)
        try:
            page.goto(f"https://www.douyin.com/user/{sec_uid}",
                      timeout=30000)
            _wait_captcha(page)
            deadline = time.time() + 20
            while time.time() < deadline:
                if items:
                    break
                page.wait_for_timeout(1500)
            # 滚动翻页加载全部作品；has_more=0 停，idle 8 防卡死，封顶防死滚
            idle = scrolls = 0
            while state["has_more"] and idle < 8 and scrolls < max_scrolls:
                before = len(items)
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(2000)
                idle = 0 if len(items) > before else idle + 1
                scrolls += 1
            if screenshot_to:
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(1200)
                    page.screenshot(path=str(screenshot_to))
                except Exception:
                    pass
        finally:
            page.remove_listener("response", on_response)
        if not items:
            raise SearchError("未拦截到主页作品接口（可能触发验证）")
        items.sort(key=lambda x: x["create_time"] or 10 ** 12)
        return items


# ---------- 下载编排 ----------

def download_all(items, out_dir: Path):
    ok, skipped = 0, 0
    failed_items = []
    total = len(items)
    for i, it in enumerate(items, 1):
        aweme_id, title = it["aweme_id"], it["title"]
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
            failed_items.append(it)
        if i < total:
            time.sleep(random.uniform(1, 2))
    # 失败补漏：整轮结束后统一再试一轮（多数是分享页风控，缓一缓能过）
    rescued = 0
    if failed_items:
        print(f"\n--- {len(failed_items)} 条失败，等 10s 后补漏一轮 ---",
              flush=True)
        time.sleep(10)
        for it in failed_items:
            try:
                douyin_dl.run(
                    f"https://www.douyin.com/video/{it['aweme_id']}", out_dir)
                ok += 1
                rescued += 1
            except Exception as e:  # noqa: BLE001
                print(f"  仍失败: {e}")
                time.sleep(2)
    failed = len(failed_items) - rescued
    print(f"\n汇总: 成功 {ok} / 跳过 {skipped} / 失败 {failed}"
          f"（补漏救回 {rescued}）")
    return ok, skipped, failed


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

def test_parse_search_response_empty_or_broken():
    assert parse_search_response({}, set()) == []
    assert parse_search_response({"data": None}, set()) == []


# ---------- tests: 搜索响应解析 ----------

def test_parse_search_response_aweme_list_shape():
    payload = {"status_code": 0, "aweme_list": [
        {"aweme_id": "111", "desc": "标题A",
         "statistics": {"digg_count": 5},
         "video": {"duration": 90000},
         "author": {"follower_count": 200}},
        {"aweme_id": "111", "desc": "重复"},   # 重复 ID 去重
        {"aweme_id": "222", "desc": ""},        # 无统计字段的条目
        {"log_pb": {}},                          # 无 ID 条目丢弃
    ]}
    seen = set()
    got = parse_search_response(payload, seen)
    assert got == [
        {"aweme_id": "111", "title": "标题A", "digg": 5,
         "duration_ms": 90000, "followers": 200,
         "mix_id": None, "mix_name": "", "sig": "", "nick": "",
         "sec_uid": ""},
        {"aweme_id": "222", "title": "", "digg": None,
         "duration_ms": None, "followers": None,
         "mix_id": None, "mix_name": "", "sig": "", "nick": "",
         "sec_uid": ""},
    ]
    assert parse_search_response(payload, seen) == []


def test_parse_search_mix_and_author_fields():
    payload = {"aweme_list": [{
        "aweme_id": "111", "desc": "第1集",
        "mix_info": {"mix_id": 555, "mix_name": "合集A",
                     "episode_count": 3},
        "author": {"signature": "简介x", "nickname": "作者B",
                   "follower_count": 100}}]}
    got = parse_search_response(payload, set())[0]
    assert got["mix_id"] == 555 and got["mix_name"] == "合集A"
    assert got["sig"] == "简介x" and got["nick"] == "作者B"


def test_author_blocked():
    it = {"sig": "每天更新短剧 禁止搬运", "nick": "xx号", "title": "t"}
    ok, kw = author_blocked(it, ["搬运"])
    assert ok is True and kw == "搬运"
    assert author_blocked(it, ["毫无关系"])[0] is False
    assert author_blocked(it, None)[0] is False
    assert author_blocked(it, [])[0] is False
    clean = {"sig": "原创作者", "nick": "正经营", "title": "无水印"}
    assert author_blocked(clean, ["搬运", "侵权"])[0] is False


def test_parse_mix_response():
    payload = {"aweme_list": [
        {"aweme_id": "111", "desc": "第2集",
         "mix_info": {"current_episode": 2}},
        {"aweme_id": "222", "desc": "第1集",
         "mix_info": {"current_episode": 1}},
        {"aweme_id": "111", "desc": "重复"},
        {"aweme_id": "333", "desc": "无集数"},
    ]}
    seen = set()
    got = parse_mix_response(payload, seen)
    assert {(g["aweme_id"], g["ep"]) for g in got} == \
        {("111", 2), ("222", 1), ("333", 0)}
    assert parse_mix_response(payload, seen) == []


def test_looks_continuous():
    eps_ep = [{"title": "《寻龙》第1集"}, {"title": "《寻龙》第2集"},
              {"title": "《寻龙》第3集"}]
    ok, why = looks_continuous(eps_ep)
    assert ok and "集数标记" in why
    eps_pre = [{"title": "盛夏光年故事之上"}, {"title": "盛夏光年故事之下"},
               {"title": "盛夏光年故事番外"}]
    ok2, why2 = looks_continuous(eps_pre)
    assert ok2 is True and "前缀" in why2
    eps_bad = [{"title": "今天吃火锅"}, {"title": "昨天去钓鱼"},
               {"title": "日常vlog记录"}]
    ok3, why3 = looks_continuous(eps_bad)
    assert ok3 is False and "混杂" in why3
    assert looks_continuous([{"title": "唯一"}])[0] is False


def test_sort_episodes_and_prefix():
    # 有官方集数 → 按集数升序
    eps = [{"ep": 3, "ct": 1, "aweme_id": "C"},
           {"ep": 1, "ct": 9, "aweme_id": "A"},
           {"ep": 2, "ct": 5, "aweme_id": "B"}]
    assert [e["aweme_id"] for e in sort_episodes(eps)] == ["A", "B", "C"]
    # 无集数 → 按发布时间升序
    eps2 = [{"ep": 0, "ct": 30, "aweme_id": "Z"},
            {"ep": 0, "ct": 10, "aweme_id": "X"},
            {"ep": 0, "ct": 20, "aweme_id": "Y"}]
    assert [e["aweme_id"] for e in sort_episodes(eps2)] == ["X", "Y", "Z"]
    # 前缀零填充：两位数总量补两位，三位补三位
    assert episode_prefix(7, 34) == "07_"
    assert episode_prefix(7, 120) == "007_"
    assert episode_prefix(34, 34) == "34_"


# ---------- tests: 主页作品与 series 响应 ----------

def test_parse_search_response_carries_sec_uid():
    payload = {"aweme_list": [{
        "aweme_id": "111", "desc": "标题A",
        "author": {"sec_uid": "MS4wABCD", "nickname": "作者B"}}]}
    got = parse_search_response(payload, set())[0]
    assert got["sec_uid"] == "MS4wABCD" and got["nick"] == "作者B"


def test_parse_mix_response_series_shape():
    # series/aweme 接口形态(实测 2026-09)：条目精简、无 mix_info/集数字段，
    # 面板顺序即集序，解析须兼容（ep=0，保持原顺序）
    payload = {"aweme_list": [
        {"aweme_id": "900", "desc": "【黄皮子回忆录】-初见"},
        {"aweme_id": "901", "desc": "【黄皮子回忆录】-约定"},
        {"aweme_id": "902", "desc": "【欧尔佩松回忆录】-卫兵"},
    ], "has_more": 1}
    seen = set()
    got = parse_mix_response(payload, seen)
    assert [g["aweme_id"] for g in got] == ["900", "901", "902"]
    assert all(g["ep"] == 0 for g in got)
    assert parse_mix_response(payload, seen) == []


def test_parse_post_response():
    payload = {"aweme_list": [
        {"aweme_id": "111", "desc": "旧作", "create_time": 1779000001},
        {"aweme_id": "222", "desc": "新作", "create_time": 1779000002},
        {"aweme_id": "111", "desc": "重复"},
        {"aweme_id": "333", "desc": "无时间"},
    ]}
    seen = set()
    got = parse_post_response(payload, seen)
    assert got == [
        {"aweme_id": "111", "title": "旧作", "create_time": 1779000001},
        {"aweme_id": "222", "title": "新作", "create_time": 1779000002},
        {"aweme_id": "333", "title": "无时间", "create_time": 0},
    ]
    assert parse_post_response(payload, seen) == []
    assert parse_post_response({}, set()) == []


def test_parse_search_response_legacy_data_shape():
    # 兼容旧形态 data[].aweme_info；粉丝回退 mplatform_followers_count
    payload = {"data": [{"aweme_info": {
        "aweme_id": "333", "desc": "标题C",
        "statistics": {"digg_count": 7},
        "author": {"mplatform_followers_count": 999}}}]}
    got = parse_search_response(payload, set())
    assert got[0]["aweme_id"] == "333" and got[0]["digg"] == 7
    assert got[0]["followers"] == 999 and got[0]["duration_ms"] is None


def test_parse_search_response_empty_or_broken():
    assert parse_search_response({}, set()) == []
    assert parse_search_response({"data": None, "aweme_list": None}, set()) == []


def test_is_verify_block():
    # 实测(2026-09)：软拦截 = HTTP 200 + status_code 0 + search_nil_info 标记
    blocked = {"status_code": 0, "aweme_list": [],
               "search_nil_info": {"search_nil_type": "verify_check"}}
    assert is_verify_block(blocked) is True
    assert is_verify_block({"status_code": 0, "aweme_list": []}) is False
    assert is_verify_block({}) is False
    assert is_verify_block({"search_nil_info": {"search_nil_type": "other"}}) is False


# ---------- tests: 筛选 ----------

def _item(**kw):
    base = {"aweme_id": "x", "title": "t", "digg": 100,
            "duration_ms": 60000, "followers": 500}
    base.update(kw)
    return base


def test_passes_filter_all_pass():
    ok, why = passes_filter(_item(), max_followers=10000,
                            max_duration=120, max_likes=1000)
    assert ok is True and why == ""


def test_passes_filter_rejects_each_dimension():
    assert passes_filter(_item(followers=10000),
                         max_followers=10000) == (False, "粉丝 10000 >= 10000")
    assert passes_filter(_item(duration_ms=120000),
                         max_duration=120) == (False, "时长 120s >= 120s")
    assert passes_filter(_item(digg=1000),
                         max_likes=1000) == (False, "点赞 1000 >= 1000")


def test_passes_filter_unknown_rejects_only_when_active():
    assert passes_filter(_item(followers=None), max_followers=100)[0] is False
    assert passes_filter(_item(digg=None), max_likes=100)[0] is False
    assert passes_filter(_item(duration_ms=None), max_duration=60)[0] is False
    assert passes_filter(_item(followers=None, digg=None,
                               duration_ms=None))[0] is True


# ---------- tests: 多关键词 ----------

def test_split_keywords():
    assert split_keywords("AI 短剧,AI 动画，ai漫剧") == \
        ["AI 短剧", "AI 动画", "ai漫剧"]
    assert split_keywords(" 单词 ") == ["单词"]
    assert split_keywords("a,,b，") == ["a", "b"]


# ---------- tests: 日期目录与全局去重 ----------

def test_search_urls_fallback_routes():
    urls = _search_urls("AI 短剧")
    assert urls[0] == ("https://www.douyin.com/search/AI%20%E7%9F%AD%E5%89%A7"
                       "?type=video")
    assert urls[1] == ("https://www.douyin.com/jingxuan/search/"
                       "AI%20%E7%9F%AD%E5%89%A7?type=video")
    # 精选优先时顺序反转
    jx = _search_urls("AI 短剧", prefer_jingxuan=True)
    assert jx[0].startswith("https://www.douyin.com/jingxuan/")


# ---------- tests: 日期目录与全局去重 ----------

def test_make_dated_dir_same_day_reused():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        d1 = make_dated_dir(root)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", d1.name), d1.name
        # 同一天多次运行 → 同一目录（叠加），不再 -1/-2
        assert make_dated_dir(root) == d1
        assert len(list(root.iterdir())) == 1


def test_existing_ids_under_recursive():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        sub = root / "2026-09-01"
        (sub / "疑似水印").mkdir(parents=True)
        (sub / "A_7300000000000000001.mp4").write_bytes(b"x")
        (sub / "疑似水印" / "B_7300000000000000002.mp4").write_bytes(b"x")
        (root / "C_7300000000000000003.mp4").write_bytes(b"x")
        assert existing_ids_under(root) == {
            "7300000000000000001", "7300000000000000002",
            "7300000000000000003"}


# ---------- tests: 登录判定与目录名 ----------

def test_has_login_true_only_with_sessionid_value():
    assert has_login([{"name": "sessionid", "value": "abc"}]) is True
    assert has_login([{"name": "sessionid", "value": ""}]) is False
    assert has_login([{"name": "ttwid", "value": "x"}]) is False
    assert has_login([]) is False


def test_safe_dir_name():
    assert safe_dir_name("AI 短剧") == "AI 短剧"
    # ? 和 " 相邻 → 各替换为一个空格 → 两个连续空格
    assert safe_dir_name('a/b:c*d?"e<f>g|h') == "a b c d  e f g h"
    assert safe_dir_name("  ") == ""
    assert len(safe_dir_name("长" * 80)) == 50


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="抖音关键词搜索+批量无水印下载（仅限个人保存）")
    parser.add_argument("keyword", nargs="?", help="搜索关键词")
    parser.add_argument("--limit", type=int, default=10,
                        help="下载条数（默认 10）")
    parser.add_argument("--max-followers", type=int, default=None,
                        help="仅保留作者粉丝数 < N")
    parser.add_argument("--max-duration", type=int, default=None,
                        help="仅保留视频时长 < N 秒")
    parser.add_argument("--max-likes", type=int, default=None,
                        help="仅保留点赞数 < N")
    parser.add_argument("--block-keywords", default=None,
                        help="作者黑名单关键词(逗号分隔, 匹配简介/昵称/标题);"
                             " 默认内置搬运/侵权类词表, 传空串禁用")
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
        keywords = split_keywords(args.keyword)
        out_dir = make_dated_dir(DOWNLOADS_DIR)
        print(f"输出目录: {out_dir}")
        filters = (args.max_followers, args.max_duration, args.max_likes)
        if any(v is not None for v in filters):
            print(f"筛选: 粉丝<{args.max_followers or '∞'} "
                  f"时长<{args.max_duration or '∞'}s "
                  f"赞<{args.max_likes or '∞'}")
        # 跨目录全局去重：历史所有已下载视频不再收集
        seen0 = existing_ids_under(DOWNLOADS_DIR)
        if args.block_keywords is None:
            block_kw = DEFAULT_BLOCK_KEYWORDS
        else:
            block_kw = split_keywords(args.block_keywords)
        items = collect_many(keywords, args.limit, *filters, seen=seen0,
                             block_keywords=block_kw)
        print(f"搜索到 {len(items)} 条（目标 {args.limit}）")
        if len(items) < args.limit:
            print("提示：结果不足 limit，下载已拿到的条目")
        download_all(items, out_dir)
    except SearchError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
