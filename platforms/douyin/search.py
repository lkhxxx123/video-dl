#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""platforms.douyin.search — 抖音关键词搜索 + 合集/主页收集 + 付费检测

依赖 core（浏览器/筛选/命名）与 platforms.douyin.dl（单条下载）。
仅限个人离线保存；请尊重创作者版权，勿二次上传。
"""
import argparse
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import core.selftest
from core.reporting import log, urgent
from core import selftest as _st
from core.browser import (checkpoint, ensure_login, first_page,
                          login_only, open_browser, resp_json, wait_captcha)
from core.errors import SearchError
from core.filter import DEFAULT_BLOCK_KEYWORDS, author_blocked, passes_filter
from core.naming import (episode_prefix, existing_ids_under, make_dated_dir,
                         roll_date_dir, safe_dir_name, split_keywords)
from core.paths import APP_ROOT as SCRIPT_DIR
from core.paths import DOWNLOADS_DIR
from platforms.douyin.config import (DETAIL_SUBSTR, EPISODE_URL_SUBSTRS,
                                     LOGIN_TIMEOUT, MAX_IDLE_SCROLLS,
                                     SCROLL_WAIT, SEARCH_RELOAD_S,
                                     SEARCH_URL_PREFIX, USER_POST_URL_SUBSTR,
                                     VERIFY_WAIT)
from platforms.douyin.dl import ParseError, run as dl_run

DOUYIN_HOME = "https://www.douyin.com/"  # 登录首页
PAID_DETAIL_SUBSTR = DETAIL_SUBSTR       # 付费检测拦同一 detail 接口


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


def parse_mix_response(payload: dict, seen: set):
    """mix/series 接口响应 → 新增集条目 [{aweme_id,title,ep,ct,dur}]。

    dur: 单集时长毫秒（video.duration，顶层 duration 兜底）——
    用于合并总集类合集的判定（jx --max-ep-duration）。
    """
    out = []
    for e in payload.get("aweme_list") or []:
        aid = e.get("aweme_id")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        mix = e.get("mix_info") or {}
        dur = (e.get("video") or {}).get("duration") or e.get("duration") or 0
        out.append({"aweme_id": aid, "title": e.get("desc") or "",
                    "ep": mix.get("current_episode") or 0,
                    "ct": e.get("create_time") or 0,
                    "dur": dur})
    return out


def sort_episodes(items):
    """剧集排序：有官方集数按集数升序；否则按发布时间升序（返回新列表）。"""
    if any(it.get("ep") for it in items):
        return sorted(items, key=lambda x: (x.get("ep") or 0,
                                            x.get("ct") or 0))
    return sorted(items, key=lambda x: x.get("ct") or 0)


def is_verify_block(payload: dict) -> bool:
    """识别风控软拦截：HTTP 200 + status_code 0 + search_nil_info 标记。"""
    nil = payload.get("search_nil_info") or {}
    return nil.get("search_nil_type") == "verify_check"


# ---------- 付费检测 ----------

def is_paid_aweme(payload: dict) -> bool:
    """从 detail 接口响应判定是否付费/试看内容。

    实测(2026-09) 关键字段：aweme_detail.series_info.is_charge == 1
    （短剧入口挂在收费合集下，整部需要付费解锁；免费 series_info.is_charge=0）。
    其他维度作为兜底：
    - series_info.is_charge_series == 1：series/detail 接口的同名同义字段
    - is_preview == 1：试看模式标记（前 N 秒免费后续需付费）
    - payment_info / pay_info：单集付费信息块存在
    - video.video_status ∈ {4, 6}：付费/受限状态码

    返回 False 时不保证非付费——可能是接口未拦截到/字段未对齐，让下游下载
    404 兜底，避免误伤免费内容。
    """
    if not payload:
        return False
    data = payload.get("aweme_detail") or payload
    if not data or not isinstance(data, dict):
        return False
    # PRIMARY: 收费合集（实测——免费 series_info.is_charge=0，未挂合集则无 series_info）
    si = data.get("series_info")
    if isinstance(si, dict):
        if si.get("is_charge") == 1 or si.get("is_charge_series") == 1:
            return True
    # FALLBACK: 单集付费/试看标记
    if data.get("is_preview") == 1:
        return True
    if data.get("payment_info") or data.get("pay_info"):
        return True
    v = data.get("video") or {}
    if v.get("pay_info") or v.get("payment_info"):
        return True
    if v.get("video_status") in (4, 6):
        return True
    return False


def check_paid_entry(page, entry_aweme_id: str, timeout: int = 15) -> bool:
    """访问入口视频页，拦截 detail 接口判定是否付费。

    返回 True 表示已确认为付费内容；超时/未拦截到/异常 → False（保守：让
    下游走完整流程，最终下载 404 时兜底，避免误伤免费内容）。
    """
    got = []

    def on_resp(r):
        if PAID_DETAIL_SUBSTR not in r.url:
            return
        p = resp_json(r)
        if p is not None:
            got.append(p)

    page.on("response", on_resp)
    try:
        page.goto(f"https://www.douyin.com/video/{entry_aweme_id}",
                  timeout=30000)
        wait_captcha(page)
        deadline = time.time() + timeout
        while time.time() < deadline and not got:
            page.wait_for_timeout(1000)
    finally:
        page.remove_listener("response", on_resp)
    if not got:
        return False
    return is_paid_aweme(got[0])


def _search_urls(keyword: str, prefer_jingxuan: bool = False):
    """搜索路由优先级：标准 /search/ 为主，精选 /jingxuan/search/ 为兜底。

    实测(2026-09)：标准路由偶发 503（风控/抖动），精选路由仍可用。
    prefer_jingxuan=True 时精选优先（douyin_jingxuan 场景）。
    """
    kw = quote(keyword)
    urls = [f"https://www.douyin.com/search/{kw}?type=video",
            f"https://www.douyin.com/jingxuan/search/{kw}?type=video"]
    return urls[::-1] if prefer_jingxuan else urls


def _episode_collection_id(url: str):
    """从剧集接口 URL 提取合集/系列 ID（mix_id= / series_id= 参数）。"""
    m = re.search(r"[?&](?:mix_id|series_id)=(\d+)", url)
    return m.group(1) if m else None


def collect_mix(video_id: str, sec_uid: str = "", mix_id="", mix_name=""):
    """用户指认的标准流程收集合集全部集。

    ① 作者主页合集标签(showSubTab=compilation)
    ② 点开目标短剧卡片 → 短剧页(user?modal_id=xxx)，展示当前短剧所有剧集
    ③ 拦截剧集接口(mix/series)，按合集ID过滤，滚动(窗口+内部容器)翻页拉全，
       以卡片"更新至N集"为完成度校验
    ④ 兜底：作者作品流按合集ID分组（懒加载偶发卡壳，尽力而为）
    返回按集数/发布时间升序的 [{aweme_id, title, ep, ct}]。
    """
    target = str(mix_id) if mix_id else ""

    def _learn_or_filter(url, state):
        cid = _episode_collection_id(url)
        if state["target"]:
            return not cid or cid == state["target"]
        if cid:
            state["target"] = cid
        return True

    with open_browser() as context:
        page = first_page(context)
        ensure_login(context, page, DOUYIN_HOME)
        if not sec_uid:
            got = {}

            def on_detail(r):
                if DETAIL_SUBSTR in r.url:
                    p = resp_json(r)
                    if p:
                        got["d"] = p.get("aweme_detail") or p

            page.on("response", on_detail)
            try:
                page.goto(f"https://www.douyin.com/video/{video_id}",
                          timeout=30000)
                wait_captcha(page)
                deadline = time.time() + 20
                while time.time() < deadline and "d" not in got:
                    page.wait_for_timeout(1500)
            finally:
                page.remove_listener("response", on_detail)
            d = got.get("d") or {}
            sec_uid = (d.get("author") or {}).get("sec_uid") or ""
            if not sec_uid:
                raise SearchError("无法获取作者 sec_uid")

        # ① 合集标签页
        page.goto(f"https://www.douyin.com/user/{sec_uid}"
                  "?showSubTab=compilation&showTab=post", timeout=30000)
        wait_captcha(page)
        # ② 点开目标短剧卡片（"更新至N集"叶子向上爬到含剧名的容器）
        card_total = None
        if mix_name:
            deadline = time.time() + 15
            clicked = None
            while time.time() < deadline and not clicked:
                clicked = page.evaluate(
                    """(name) => {
                        const leaves = [...document.querySelectorAll('*')]
                          .filter(el => el.children.length === 0 &&
                                 (el.textContent || '')
                                 .includes('更新至'));
                        for (const leaf of leaves) {
                            let el = leaf;
                            for (let i = 0; i < 8 && el; i++) {
                                const t = el.innerText || '';
                                if (t.includes(name)) {
                                    el.click();
                                    return t.replace(/\\n/g, ' ')
                                             .slice(0, 80);
                                }
                                el = el.parentElement;
                            }
                        }
                        return null;
                    }""", mix_name)
                if not clicked:
                    page.wait_for_timeout(1500)
            if clicked:
                log(f"  (已进入短剧页: {clicked[:50]})", flush=True)
                m = re.search(r"更新至\s*(\d+)\s*集", clicked)
                if m:
                    card_total = int(m.group(1))
            else:
                log("  (未找到合集卡片)", flush=True)
            page.wait_for_timeout(2500)

        # ③ 短剧页拦截剧集接口，滚动拉全
        state = {"target": target, "total": card_total}
        seen, items = set(), []

        def on_response(resp):
            if not any(s in resp.url for s in EPISODE_URL_SUBSTRS):
                return
            if not _learn_or_filter(resp.url, state):
                return
            payload = resp_json(resp)
            if payload is None:
                return
            try:
                fresh = parse_mix_response(payload, seen)
            except Exception:
                return
            if fresh:
                items.extend(fresh)
                log(f"  (+{len(fresh)} 集, 累计 {len(items)})", flush=True)

        page.on("response", on_response)
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not items:
                page.wait_for_timeout(1500)
            idle = 0
            while idle < 15:
                checkpoint()
                if state["total"] and len(items) >= state["total"]:
                    break
                before = len(items)
                try:
                    page.evaluate(
                        "window.scrollTo(0, document.body.scrollHeight)")
                except Exception:
                    pass
                try:
                    page.evaluate(
                        """() => {
                            for (const el of document.querySelectorAll('*')) {
                                const s = getComputedStyle(el);
                                if ((s.overflowY === 'auto' ||
                                     s.overflowY === 'scroll') &&
                                    el.scrollHeight >
                                        el.clientHeight + 50) {
                                    el.scrollTop = el.scrollHeight;
                                }
                            }
                        }""")
                except Exception:
                    pass
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(1800)
                idle = 0 if len(items) > before else idle + 1
        finally:
            page.remove_listener("response", on_response)
        if state["total"] and len(items) < state["total"]:
            log(f"  ⚠ 短剧页只拿到 {len(items)}/{state['total']} 集，"
                  f"转作品流兜底", flush=True)
        if len(items) >= 2:
            if state["total"]:
                log(f"  (短剧页拿到 {len(items)}/{state['total']} 集)",
                      flush=True)
            return sort_episodes(items)

        # ④ 兜底：作者作品流按合集ID分组
        page.goto(f"https://www.douyin.com/user/{sec_uid}", timeout=30000)
        posts, _ = _collect_posts_in_page(page)
        if not state["target"]:
            entry = next((p for p in posts if p["aweme_id"] == video_id),
                         None)
            if entry and entry.get("sid"):
                state["target"] = entry["sid"]
        eps = [p for p in posts
               if p.get("sid") and p["sid"] == state["target"]]
        if not eps:
            raise SearchError(
                f"短剧页与作品流均未拿到分集（目标={state['target'] or '?'}）")
        log(f"  (作品流兜底分组: {len(eps)} 集)", flush=True)
        return [{"aweme_id": p["aweme_id"], "title": p["title"],
                 "ep": 0, "ct": p["create_time"]} for p in eps]


def parse_post_response(payload: dict, seen: set):
    """作者主页作品接口 → 新增 [{aweme_id, title, create_time, sid}]。

    sid: 该作品所属合集/系列 ID（series_info.series_id 或 mix_info.mix_id）。
    """
    out = []
    for e in payload.get("aweme_list") or []:
        aid = e.get("aweme_id")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        sid = ((e.get("series_info") or {}).get("series_id")
               or (e.get("mix_info") or {}).get("mix_id") or "")
        out.append({"aweme_id": aid, "title": e.get("desc") or "",
                    "create_time": e.get("create_time") or 0,
                    "sid": str(sid)})
    return out


def _collect_posts_in_page(page, max_scrolls=120):
    """在已打开的作者主页页面上拦截作品接口并滚动翻完。

    返回 (items, has_more_flag)；items 为 [{aweme_id,title,create_time,sid}]。
    """
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
        wait_captcha(page)
        deadline = time.time() + 20
        while time.time() < deadline:
            if items:
                break
            page.wait_for_timeout(1500)
        page.wait_for_timeout(2500)  # 首批到位后先稳一稳再滚(懒加载易漏)
        # 主页头部"作品 N"总数做硬校验（懒加载偶发卡壳，25/43 截断实例）
        expected = None
        try:
            m = re.search(r"作品\s*[\|｜:]?\s*(\d+)",
                          page.inner_text("body")[:3000])
            if m:
                expected = int(m.group(1))
                log(f"  (主页作品总数: {expected})", flush=True)
        except Exception:
            pass
        # 滚动翻页加载全部作品；has_more=0 或达主页总数 停；
        # idle 20 防卡死(懒加载慢)，封顶防死滚
        idle = scrolls = 0
        while (state["has_more"] and idle < 20
               and scrolls < max_scrolls
               and not (expected and len(items) >= expected)):
            checkpoint()
            before = len(items)
            try:
                page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(2000)
            idle = 0 if len(items) > before else idle + 1
            scrolls += 1
        if expected and len(items) < expected:
            log(f"  ⚠ 作品流只翻到 {len(items)}/{expected}（懒加载卡壳）",
                  flush=True)
    finally:
        page.remove_listener("response", on_response)
    items.sort(key=lambda x: x["create_time"] or 10 ** 12)
    return items, state["has_more"]


def collect_user_posts(sec_uid: str, screenshot_to=None, max_scrolls=120):
    """打开作者主页，拦截 /aweme/v1/web/aweme/post/ 收集全部作品。

    返回按发布时间升序 [{aweme_id, title, create_time, sid}]（无时间的排末尾）；
    screenshot_to: 可选 Path，回顶后截主页图（剧集判定的辅助证据）。
    翻页以响应 has_more=0 为准（实测(2026-09)懒加载较慢，需高耐心阈值）。
    """
    with open_browser() as context:
        page = first_page(context)
        ensure_login(context, page, DOUYIN_HOME)
        page.goto(f"https://www.douyin.com/user/{sec_uid}", timeout=30000)
        if screenshot_to:
            try:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(1200)
                page.screenshot(path=str(screenshot_to))
            except Exception:
                pass
        items, _ = _collect_posts_in_page(page, max_scrolls)
        if not items:
            raise SearchError("未拦截到主页作品接口（可能触发验证）")
        return items


# ---------- 下载编排 ----------

def download_all(items, out_dir: Path):
    ok, skipped = 0, 0
    failed_items = []
    total = len(items)
    for i, it in enumerate(items, 1):
        aweme_id, title = it["aweme_id"], it["title"]
        log(f"\n[{i}/{total}] {title[:30] or aweme_id}")
        try:
            dl_run(
                f"https://www.douyin.com/video/{aweme_id}", out_dir)
            ok += 1
        except ParseError as e:
            log(f"  跳过: {e}")
            skipped += 1
        except Exception as e:  # noqa: BLE001 - 单条失败不中断批次
            log(f"  失败: {e}")
            failed_items.append(it)
        if i < total:
            time.sleep(random.uniform(1, 2))
    # 失败补漏：整轮结束后统一再试一轮（多数是分享页风控，缓一缓能过）
    rescued = 0
    if failed_items:
        log(f"\n--- {len(failed_items)} 条失败，等 10s 后补漏一轮 ---",
              flush=True)
        time.sleep(10)
        for it in failed_items:
            try:
                dl_run(
                    f"https://www.douyin.com/video/{it['aweme_id']}", out_dir)
                ok += 1
                rescued += 1
            except Exception as e:  # noqa: BLE001
                log(f"  仍失败: {e}")
                time.sleep(2)
    failed = len(failed_items) - rescued
    log(f"\n汇总: 成功 {ok} / 跳过 {skipped} / 失败 {failed}"
          f"（补漏救回 {rescued}）")
    return ok, skipped, failed


def _collect_page(context, page, keyword, limit, max_followers=None,
                  max_duration=None, max_likes=None, seen=None,
                  block_keywords=None, prefer_jingxuan=False,
                  min_duration=None):
    """单个关键词的搜索收集（在已打开的浏览器页签内跳转）。

    成功返回合格列表；验证超时/无数据抛 SearchError（由调用方决定是否继续）。
    """
    done = seen if seen is not None else set()
    seen_local = set()  # 本轮解析去重(不含历史; 已见条目计入 scanned 推进)
    kept, raw, scanned = [], 0, 0
    state = {"verify": False, "prompted": False, "prompted_at": 0.0,
             "reloaded": False}

    def on_response(resp):
        nonlocal raw, scanned
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
        for it in parse_search_response(payload, seen_local):
            scanned += 1
            if it["aweme_id"] in done:
                continue  # 历史/前轮已见: 静默跳过但计入推进
            raw += 1
            ok, reason = passes_filter(it, max_followers, max_duration,
                                       max_likes, min_duration)
            if ok:
                bad, kw = author_blocked(it, block_keywords)
                if bad:
                    log(f"  跳过: {(it['title'] or it['aweme_id'])[:24]}"
                          f"（作者黑名单: 简介含「{kw}」）", flush=True)
                    continue
                kept.append(it)
            else:
                log(f"  跳过: {(it['title'] or it['aweme_id'])[:24]}"
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
            wait_captcha(page)
            probe_deadline = time.time() + 12
            while scanned == 0 and time.time() < probe_deadline:
                page.wait_for_timeout(1500)
            if scanned:
                extra = f"（被跳转到 {landed}）" if landed != route else ""
                log(f"  (路由 {route} 命中{extra})", flush=True)
                break
            log(f"  (路由 {route} 无数据[HTTP {status}]"
                  f"实际落点 {landed}，切换下一条路由…)", flush=True)
        # 长等待：等首条有数据的响应；遇软拦截(verify_check)提示用户滑验证。
        # 提示 90s 后仍无数据则刷新页面重发搜索（验证通过后刷新即可拿到）
        first_deadline = time.time() + VERIFY_WAIT
        while scanned == 0 and time.time() < first_deadline:
            checkpoint()
            if state["verify"] and not state["prompted"]:
                urgent(">>> 触发滑块验证：请在浏览器窗口中拖动滑块完成拼图 <<<")
                state["prompted"] = True
                state["prompted_at"] = time.time()
            if (state["prompted"] and not state["reloaded"]
                    and time.time() - state["prompted_at"] > 90):
                log("  (刷新页面重新触发搜索…)", flush=True)
                page.reload(timeout=30000)
                wait_captcha(page)
                state["reloaded"] = True
            page.wait_for_timeout(1500)
        idle = 0
        while len(kept) < limit and idle < MAX_IDLE_SCROLLS:
            checkpoint()
            before = scanned
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(int(SCROLL_WAIT * 1000))
            idle = 0 if scanned > before else idle + 1
    finally:
        page.remove_listener("response", on_response)
    if raw == 0:
        if state["verify"]:
            raise SearchError(f"[{keyword}] 验证未完成或未通过")
        raise SearchError(f"[{keyword}] 未拦截到搜索响应：可能改版或风控")
    log(f"本词合格 {len(kept)} / 共 {raw} 条")
    return kept[:limit]


def collect_many(keywords, limit, max_followers=None, max_duration=None,
                 max_likes=None, seen=None, block_keywords=None,
                 prefer_jingxuan=False, min_duration=None):
    """多关键词聚合：开一次浏览器，逐词收集，全局去重，凑够 limit 即停。

    seen: 额外提供的"已处理 ID 集合"（流水线复用，跳过历史视频）。
    block_keywords: 作者黑名单关键词（过滤搬运/侵权类账号）。
    prefer_jingxuan: 精选路由优先。
    注意：复制入参 set —— 搜索命中的候选 ID 不会污染调用方集合
    （否则入口候选视频作为剧集分集时会被误判"已处理"而静默跳过）。
    """
    seen_ids = set(seen) if seen is not None else set()
    merged = []
    with open_browser() as context:
        page = first_page(context)
        ensure_login(context, page, DOUYIN_HOME)
        for idx, kw in enumerate(keywords, 1):
            if len(merged) >= limit:
                break
            remaining = limit - len(merged)
            log(f"\n=== 关键词 [{idx}/{len(keywords)}] {kw}"
                  f"（还需 {remaining} 条）===", flush=True)
            try:
                merged.extend(_collect_page(context, page, kw, remaining,
                                            max_followers, max_duration,
                                            max_likes, seen_ids,
                                            block_keywords,
                                            prefer_jingxuan,
                                            min_duration))
            except SearchError as e:
                log(f"  !! {e}，跳到下一个关键词", flush=True)
            log(f"累计合格 {len(merged)}/{limit}")
            if idx < len(keywords) and len(merged) < limit:
                time.sleep(random.uniform(3, 5))  # 词间降温
    return merged[:limit]


# ---------- selftest ----------

def run_selftests():
    return _st.run_selftests(globals())


# ---------- tests: 搜索响应解析 ----------

def test_parse_search_response_empty_or_broken():
    assert parse_search_response({}, set()) == []
    assert parse_search_response({"data": None, "aweme_list": None}, set()) == []


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


def test_parse_search_response_carries_sec_uid():
    payload = {"aweme_list": [{
        "aweme_id": "111", "desc": "标题A",
        "author": {"sec_uid": "MS4wABCD", "nickname": "作者B"}}]}
    got = parse_search_response(payload, set())[0]
    assert got["sec_uid"] == "MS4wABCD" and got["nick"] == "作者B"


def test_parse_search_response_legacy_data_shape():
    # 兼容旧形态 data[].aweme_info；粉丝回退 mplatform_followers_count
    payload = {"data": [{"aweme_info": {
        "aweme_id": "333", "desc": "标题C",
        "statistics": {"digg_count": 7},
        "author": {"mplatform_followers_count": 999}}}]}
    got = parse_search_response(payload, set())
    assert got[0]["aweme_id"] == "333" and got[0]["digg"] == 7
    assert got[0]["followers"] == 999 and got[0]["duration_ms"] is None


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


def test_sort_episodes():
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
    # (episode_prefix 断言已迁 core.naming)


def test_is_verify_block():
    # 实测(2026-09)：软拦截 = HTTP 200 + status_code 0 + search_nil_info 标记
    blocked = {"status_code": 0, "aweme_list": [],
               "search_nil_info": {"search_nil_type": "verify_check"}}
    assert is_verify_block(blocked) is True
    assert is_verify_block({"status_code": 0, "aweme_list": []}) is False
    assert is_verify_block({}) is False
    assert is_verify_block({"search_nil_info": {"search_nil_type": "other"}}) is False


# ---------- tests: 付费检测 ----------

def test_is_paid_aweme_series_charge_is_charge():
    # 实测(2026-09)：收费合集的核心信号——series_info.is_charge=1
    # 免费入口视频：series_info.is_charge=0；非合集视频：series_info 缺失
    paid = {"aweme_detail":
            {"series_info": {"is_charge": 1, "series_id": "7673..."}}}
    assert is_paid_aweme(paid) is True


def test_is_paid_aweme_series_charge_is_charge_series_alias():
    # series/detail 接口的同义字段名（实测部分响应两种命名都用）
    paid = {"aweme_detail":
            {"series_info": {"is_charge_series": 1}}}
    assert is_paid_aweme(paid) is True


def test_is_paid_aweme_series_free():
    # 免费合集——series_info.is_charge=0 应当不误判
    free = {"aweme_detail":
            {"series_info": {"is_charge": 0, "series_id": "7673..."}}}
    assert is_paid_aweme(free) is False


def test_is_paid_aweme_is_preview():
    # 试看模式（前 N 秒免费，后续需付费）
    assert is_paid_aweme({"aweme_detail": {"is_preview": 1}}) is True


def test_is_paid_aweme_video_status_paid():
    # 实测：video_status=4 为付费受限
    assert is_paid_aweme({"aweme_detail":
                          {"video": {"video_status": 4}}}) is True


def test_is_paid_aweme_video_status_restricted():
    # 实测：video_status=6 为另一种受限
    assert is_paid_aweme({"aweme_detail":
                          {"video": {"video_status": 6}}}) is True


def test_is_paid_aweme_payment_info():
    assert is_paid_aweme({"aweme_detail":
                          {"payment_info": {"amount": 1.0}}}) is True


def test_is_paid_aweme_pay_info_nested():
    # pay_info 在 video 嵌套层（实测部分合集放这里）
    assert is_paid_aweme({"aweme_detail":
                          {"video": {"pay_info": {"id": "x"}}}}) is True


def test_is_paid_aweme_top_level_shape():
    # detail 接口偶发无 aweme_detail 包裹，直接是 payload
    assert is_paid_aweme({"is_preview": 1}) is True
    assert is_paid_aweme({"video": {"video_status": 4}}) is True
    # series 字段在顶层也能命中（实测少数响应如此）
    assert is_paid_aweme({"series_info": {"is_charge": 1}}) is True


def test_is_paid_aweme_free_realistic_shape():
    # 实测(2026-09) 免费入口视频的真实响应：series_info.is_charge=0，
    # 其余字段（is_preview / video_status / preview_video_status）
    # 在免费/付费下都常为 None/1，不构成判别信号
    payload = {"aweme_detail": {
        "series_info": {"is_charge": 0, "series_id": "7673..."},
        "is_preview": None,
        "preview_video_status": 1,
        "video": {"video_status": None, "pay_info": None,
                  "payment_info": None},
        "status": {"listen_video_status": 2, "part_see": 0}}}
    assert is_paid_aweme(payload) is False


def test_is_paid_aweme_no_series_info():
    # 非合集视频（无 series_info）——不误判（让下游 404 兜底）
    payload = {"aweme_detail": {
        "is_preview": None, "preview_video_status": 1,
        "video": {"video_status": None}}}
    assert is_paid_aweme(payload) is False


def test_is_paid_aweme_empty_or_invalid():
    # 空/非法输入 → False（保守：让下游 404 兜底）
    assert is_paid_aweme({}) is False
    assert is_paid_aweme(None) is False
    assert is_paid_aweme({"aweme_detail": None}) is False
    assert is_paid_aweme({"aweme_detail": "garbage"}) is False


# ---------- tests: 接口 URL 与搜索编排 ----------

def test_episode_collection_id():
    assert _episode_collection_id(
        "https://www.douyin.com/aweme/v1/web/mix/aweme/"
        "?mix_id=7301234567890&cursor=0") == "7301234567890"
    assert _episode_collection_id(
        "https://www.douyin.com/aweme/v1/web/series/aweme/"
        "?series_id=7312345678901&cursor=10") == "7312345678901"
    assert _episode_collection_id("https://www.douyin.com/other?a=1") is None


def test_search_urls_fallback_routes():
    urls = _search_urls("AI 短剧")
    assert urls[0] == ("https://www.douyin.com/search/AI%20%E7%9F%AD%E5%89%A7"
                       "?type=video")
    assert urls[1] == ("https://www.douyin.com/jingxuan/search/"
                       "AI%20%E7%9F%AD%E5%89%A7?type=video")
    # 精选优先时顺序反转
    jx = _search_urls("AI 短剧", prefer_jingxuan=True)
    assert jx[0].startswith("https://www.douyin.com/jingxuan/")


def test_collect_many_copies_caller_seen():
    """搜索不得污染调用方 seen（入口候选=剧集分集时会被误判已处理）。"""
    import unittest.mock as mock
    caller = {"X"}
    got = {}

    def fake_collect(*args, **kwargs):
        got["seen"] = args[7] if len(args) > 7 else kwargs.get("seen_ids")
        return []

    with mock.patch.object(sys.modules[__name__], "_collect_page",
                           fake_collect), \
         mock.patch.object(sys.modules[__name__], "first_page",
                           return_value=None), \
         mock.patch.object(sys.modules[__name__], "ensure_login",
                           return_value=None), \
         mock.patch.object(sys.modules[__name__], "open_browser") as ob:
        ob.return_value.__enter__.return_value = object()
        collect_many(["词"], 5, seen=caller)
    assert got["seen"] is not caller, "collect 收到的是调用方原集合"
    assert caller == {"X"}, "调用方集合被污染"


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
            login_only(DOUYIN_HOME)
            return
        if not args.keyword:
            parser.error("请提供搜索关键词")
        keywords = split_keywords(args.keyword)
        out_dir = make_dated_dir(DOWNLOADS_DIR)
        log(f"输出目录: {out_dir}")
        filters = (args.max_followers, args.max_duration, args.max_likes)
        if any(v is not None for v in filters):
            log(f"筛选: 粉丝<{args.max_followers or '∞'} "
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
        log(f"搜索到 {len(items)} 条（目标 {args.limit}）")
        if len(items) < args.limit:
            log("提示：结果不足 limit，下载已拿到的条目")
        download_all(items, out_dir)
    except SearchError as e:
        log(f"错误: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
