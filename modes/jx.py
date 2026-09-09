#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""douyin_jx.py — 精选合集短剧下载器（按用户手动流程实现）

流程（用户实测指认）：
① 精选搜索 /jingxuan/search/{关键词}?type=video，只收带合集标记的候选
② 进作者主页（等效于视频页右上角点作者名）→ 切到"合集"页
③ 在合集页点进具体合集 → 滑动加载当前合集的所有剧集 → 逐集下载+AI验水印
流式(2026-09-05)：搜索发现一部合格候选立即处理（worker 页走②③），
做完再继续滚动搜索页——不再整池预收集后批量处理

每部剧独立目录 剧集/<HH点>/<剧名>/（按下载起始小时分桶，与散片时间桶
同风格；认领历史目录沿用原位置防分裂），集数零填充前缀（01_..）；
点赞/粉丝/时长筛选、作者黑名单、全局去重、视频清单与 auto 一致。
仅限个人离线保存；请尊重创作者版权，勿二次上传。
"""
import argparse
import json
import os
import random
import re
import shutil
import sys
import time
from pathlib import Path

import core.selftest
from core.reporting import checkpoint, event, log, urgent
import core.watermark as wf
from core import paths
from core.browser import is_conn_dead
from modes import auto as douyin_auto
from platforms.douyin import dl as douyin_dl
from platforms.douyin import search as ds
from platforms.douyin.series import looks_continuous


# ---------- selftest ----------

def run_selftests():
    return core.selftest.run_selftests(globals())


# ---------- 纯逻辑 ----------

def pick_series(pool, limit):
    """候选池 → 带合集标记的剧集，按 mix_id 去重，截取前 limit 部。"""
    seen_mix, series = set(), []
    for it in pool:
        mid = it.get("mix_id")
        if not mid or mid in seen_mix:
            continue
        seen_mix.add(mid)
        series.append(it)
    return series[:limit], len(series)


# 剧集目录按下载起始小时分桶（用户需求: 与散片时间桶同风格）
# 剧集/<HH点>/<剧名>(N集)/；过渡期曾用平铺 HH点_剧名，认领时都要兼容
HOUR_PREFIX_RE = re.compile(r"^(\d{2}点)_")
BUCKET_NAME_RE = re.compile(r"^\d{2}点$")


def series_dir_core(dname: str) -> str:
    """目录名去掉可选的 HH点_ 时间前缀（认领匹配用；老目录无前缀原样返回）。"""
    m = HOUR_PREFIX_RE.match(dname)
    return dname[m.end():] if m else dname


def claim_series_dir(jdir: Path, base: str, hour: str) -> Path:
    """系列目录定位: 剧集/<hour>/<base>/ 为新默认；先认领历史目录
    （小时桶内 / 过渡期平铺 HH点_名 / 老式裸名平铺）防续传目录分裂。
    返回路径（不创建，目录由下载器懒建）。"""
    fresh = jdir / hour / base
    if not jdir.is_dir():
        return fresh
    cands = []
    for d in sorted(jdir.iterdir()):
        if not d.is_dir() or d.name.startswith("有水印弃用-"):
            continue
        if BUCKET_NAME_RE.match(d.name):  # 小时桶 → 桶内各剧目录
            cands.extend(x for x in sorted(d.iterdir()) if x.is_dir())
        else:  # 平铺（老式裸名 / 过渡期 HH点_名）
            cands.append(d)
    for d in cands:
        if (not d.name.startswith("有水印弃用-")
                and series_dir_core(d.name).startswith(base)):
            return d
    return fresh


# 点开合集卡片：从"更新至N集"叶子向上爬到包含剧名的最近容器再点击
# （直接全文匹配会误中大容器；叶子只含"更新至N集"，剧名在同级）
_CLICK_CARD_JS = r"""(name) => {
    const leaves = [...document.querySelectorAll('*')]
      .filter(el => el.children.length === 0 &&
             (el.textContent || '').includes('更新至'));
    for (const leaf of leaves) {
        let el = leaf;
        for (let i = 0; i < 8 && el; i++) {
            const t = el.innerText || '';
            if (t.includes(name)) {
                el.click();
                return t.replace(/\n/g, ' ').slice(0, 80);
            }
            el = el.parentElement;
        }
    }
    return null;
}"""

# 滚动：窗口到底 + 所有内部可滚动容器(含 shadow DOM)增量滚动并派发事件
# （懒加载列表常只监听渐进 scroll 事件，一步跳底可能不触发翻页）
_SCROLL_ALL_JS = r"""() => {
    const walk = (root, out) => {
        root.querySelectorAll('*').forEach(el => {
            out.push(el);
            if (el.shadowRoot) walk(el.shadowRoot, out);
        });
    };
    const all = [];
    walk(document, all);
    window.scrollTo(0, document.body.scrollHeight);
    let n = 0;
    for (const el of all) {
        const s = getComputedStyle(el);
        if ((s.overflowY === 'auto' || s.overflowY === 'scroll') &&
            el.scrollHeight > el.clientHeight + 50) {
            el.scrollTop += Math.max(200, el.clientHeight * 0.9);
            if (el.scrollTop + el.clientHeight >= el.scrollHeight - 5) {
                el.scrollTop = el.scrollHeight;
            }
            el.dispatchEvent(new Event('scroll', {bubbles: true}));
            n++;
        }
    }
    return n;
}"""


def is_compilation(eps, max_sec):
    """合并总集类合集判定：超时长(>max_sec)集占比 ≥60% → (True, 超长数, 总数)。

    dur 缺失按不超长计（series 接口部分条目无时长字段）。
    """
    if len(eps) < 2 or not max_sec:
        return False, 0, len(eps)
    limit_ms = max_sec * 1000
    n_long = sum(1 for e in eps if (e.get("dur") or 0) > limit_ms)
    return n_long * 10 >= len(eps) * 6, n_long, len(eps)


# ---------- 测试 ----------

def test_pick_series_dedup_and_limit():
    pool = [
        {"aweme_id": "1", "mix_id": 111, "mix_name": "剧A"},
        {"aweme_id": "2", "mix_id": None},
        {"aweme_id": "3", "mix_id": 333, "mix_name": "剧B"},
        {"aweme_id": "5", "mix_id": 111, "mix_name": "剧A重复"},
    ]
    got, total = pick_series(pool, 2)
    assert total == 2 and [g["mix_name"] for g in got] == ["剧A", "剧B"]
    got2, _ = pick_series(pool, 10)
    assert len(got2) == 2 and len({g["mix_id"] for g in got2}) == 2


def test_series_dir_core_strips_hour_prefix():
    # 过渡期平铺目录: HH点_ 前缀剥离后用于认领匹配
    assert series_dir_core("15点_古井穿越(16集)") == "古井穿越(16集)"
    assert series_dir_core("09点_未命名剧集") == "未命名剧集"
    # 老目录（无时间前缀）原样返回，同样能被认领
    assert series_dir_core("古井穿越(16集)") == "古井穿越(16集)"
    # 数字开头但非时间前缀的目录名不受影响
    assert series_dir_core("3天的旅行(5集)") == "3天的旅行(5集)"
    # "点"后无下划线不视为时间前缀（如剧名本身含"15点"）
    assert series_dir_core("15点的约定(3集)") == "15点的约定(3集)"


def test_claim_series_dir_buckets_and_legacy():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        jdir = Path(td) / "剧集"
        # 三种历史形态 + 弃用目录
        (jdir / "19点" / "古井穿越(16集)").mkdir(parents=True)   # 桶内
        (jdir / "18点_天运纨绔(2集)").mkdir(parents=True)         # 过渡期平铺
        (jdir / "摄政王的心尖药引(24集)").mkdir(parents=True)     # 老式裸名平铺
        (jdir / "20点" / "有水印弃用-偏爱女反派(4集)").mkdir(parents=True)
        # 桶内有同名 → 认领（即使当前小时不同，续传不分裂）
        assert claim_series_dir(jdir, "古井穿越", "21点") == \
            jdir / "19点" / "古井穿越(16集)"
        # 过渡期平铺 → 认领
        assert claim_series_dir(jdir, "天运纨绔", "21点") == \
            jdir / "18点_天运纨绔(2集)"
        # 老式裸名平铺 → 认领
        assert claim_series_dir(jdir, "摄政王的心尖药引", "21点") == \
            jdir / "摄政王的心尖药引(24集)"
        # 弃用目录不认领；全新剧 → 当前小时桶
        assert claim_series_dir(jdir, "偏爱女反派", "21点") == \
            jdir / "21点" / "偏爱女反派"
        assert claim_series_dir(jdir, "新剧", "21点") == \
            jdir / "21点" / "新剧"
        # 剧集目录不存在时直接给桶内路径
        empty = Path(td) / "不存在"
        assert claim_series_dir(empty, "X", "08点") == empty / "08点" / "X"


def test_is_compilation():
    # 10 集中 7 集超 10 分钟 → 合并总集
    eps = [{"dur": 700000}] * 7 + [{"dur": 200000}] * 3
    flag, n, m = is_compilation(eps, 600)
    assert flag is True and (n, m) == (7, 10)
    # 10 集中 4 集超 → 正常长剧
    eps2 = [{"dur": 700000}] * 4 + [{"dur": 200000}] * 6
    assert is_compilation(eps2, 600)[0] is False
    # 时长缺失不误伤（series 形态）
    eps3 = [{"dur": 0}] * 10
    assert is_compilation(eps3, 600)[0] is False
    assert is_compilation(eps3, None)[0] is False


def test_click_card_js_contents():
    assert "更新至" in _CLICK_CARD_JS and "el.click()" in _CLICK_CARD_JS
    # r-string 保证 /\n/g 的反斜杠n 原样送达 JS（此前真换行导致正则报错）
    assert "\\n" in _CLICK_CARD_JS
    assert "scrollTo" in _SCROLL_ALL_JS


# ---------- 核心：按用户流程收集合集全部剧集 ----------

def collect_collection(entry_video_id, sec_uid, mix_id="", mix_name="",
                       early_stop=0, fast=False, page=None):
    """作者主页 → 合集页 → 点进具体合集 → 滑动拉全部分集。

    返回按集数/发布时间升序的 [{aweme_id, title, ep, ct}]（从第 1 集开始）。
    入口视频付费则抛 SearchError（由 run() 跳过该合集）。
    page: 复用已打开的页面（流式模式的 worker 页）；None 自开浏览器。
    """
    if page is None:
        with ds.open_browser() as ctx:
            p = ds.first_page(ctx)
            ds.ensure_login(ctx, p, ds.DOUYIN_HOME)
            return collect_collection(entry_video_id, sec_uid, mix_id,
                                      mix_name, early_stop, fast, page=p)

    target = str(mix_id) if mix_id else ""

    # 入口视频付费检测：精选搜索可能命中付费合集（整部付费/首集付费试看），
    # 提早拦截省去作者页+合集卡+滑页一轮空转；下载时也会 404，但入口
    # 检测能提前归类到"付费跳过"而非"未拦截到合集剧集接口"
    if entry_video_id and ds.check_paid_entry(page, entry_video_id):
        raise ds.SearchError(
            f"入口视频需付费 ({entry_video_id})")
    state = {"target": target, "total": None}
    seen, items = set(), []

    def on_response(resp):
        url = resp.url
        if not any(s in url for s in ds.EPISODE_URL_SUBSTRS):
            return
        cid = ds._episode_collection_id(url)
        if state["target"]:
            if cid and cid != state["target"]:
                return  # 其他合集，忽略
        elif cid:
            state["target"] = cid  # 从首个剧集响应学习目标合集
        payload = ds.resp_json(resp)
        if payload is None:
            return
        try:
            fresh = ds.parse_mix_response(payload, seen)
        except Exception:
            return
        if fresh:
            items.extend(fresh)
            log(f"  (+{len(fresh)} 集, 累计 {len(items)})", flush=True)

    page.on("response", on_response)
    try:
        # ② 作者主页（等效：视频页右上角作者名 → 主页）
        page.goto(f"https://www.douyin.com/user/{sec_uid}",
                  timeout=30000)
        ds.wait_captcha(page)
        page.wait_for_timeout(2000)
        # ③ 切换到"合集"页
        try:
            page.get_by_text("合集", exact=True).first.click(
                timeout=5000)
            log("  (已切换到合集页)", flush=True)
        except Exception:
            page.goto(f"https://www.douyin.com/user/{sec_uid}"
                      "?showSubTab=compilation&showTab=post",
                      timeout=30000)
            ds.wait_captcha(page)
            log("  (直达合集页)", flush=True)
        page.wait_for_timeout(2500)
        # ③' 点进具体合集（卡片带权威"更新至N集"）
        clicked = None
        deadline = time.time() + 15
        while time.time() < deadline and not clicked:
            try:
                clicked = page.evaluate(_CLICK_CARD_JS, mix_name)
            except Exception:
                clicked = None
            if not clicked:
                page.wait_for_timeout(1500)
        if clicked:
            log(f"  (已进入合集: {clicked[:50]})", flush=True)
            m = re.search(r"更新至\s*(\d+)\s*集", clicked)
            if m:
                state["total"] = int(m.group(1))
                log(f"  (该合集共 {state['total']} 集)", flush=True)
        else:
            log("  (未找到合集卡片，收集页面现有内容)", flush=True)
        # fast=True 冲刺翻页(只要尾部, 等待砍到1/3): 稳2500→800,
        # 每轮1800→500, idle 20→10
        settle_ms = 800 if fast else 2500
        round_ms = 500 if fast else 1800
        max_idle = 10 if fast else 20
        page.wait_for_timeout(settle_ms)
        # 播放页形态时集数列表可能折叠，尝试展开
        try:
            page.get_by_text("展开", exact=True).first.click(
                timeout=4000)
            log("  (已展开集数列表)", flush=True)
            page.wait_for_timeout(2000)
        except Exception:
            pass
        # 等首批剧集响应
        deadline = time.time() + 20
        while time.time() < deadline and not items:
            checkpoint()
            page.wait_for_timeout(1500)
        # ③'' 持续滑动加载当前合集所有剧集
        stopped_early = False
        idle = 0
        while idle < max_idle:
            if state["total"] and len(items) >= state["total"]:
                break
            checkpoint()
            if early_stop >= 2 and len(items) >= early_stop:
                stopped_early = True
                log(f"  (已集齐采样所需 {len(items)} 集，提前返回)",
                      flush=True)
                break
            before = len(items)
            try:
                page.evaluate(_SCROLL_ALL_JS)
            except Exception:
                pass
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(round_ms)
            idle = 0 if len(items) > before else idle + 1
    finally:
        page.remove_listener("response", on_response)
        # 卸载合集播放页：视频自动播放，挂后台到下一部剧期间持续
        # 吃内存/带宽；跳回空白页停掉
        try:
            page.goto("about:blank", timeout=10000)
        except Exception:
            pass
    if not items:
        raise ds.SearchError("未拦截到合集剧集接口（可能触发验证）")
    if state["total"] and len(items) < state["total"]:
        log(f"  ⚠ 只拿到 {len(items)}/{state['total']} 集（滑动未翻完）",
              flush=True)
    return ds.sort_episodes(items), not stopped_early


# ---------- 流式编排：发现一部 → 立刻处理 → 再继续搜 ----------

def collect_series_stream(keywords, filters, block_keywords, done_ids,
                          process_series, min_duration=None):
    """流式收集+处理: 搜索页每发现一个带合集标记的合格候选，立即交给
    process_series(it, get_worker_page) 处理（worker 页走付费检测/作者主页/
    合集页/滑动拉集，下载走独立 HTTP），做完再继续滚动搜索页。

    搜索页与 worker 页共用一个浏览器的两个页签——同 profile 不抢锁，
    且 page.on("response") 只收本页请求，互不干扰。
    process_series 返回 True 表示已达 limit，立刻停止收集。返回处理条数。
    """
    max_followers, max_duration, max_likes = filters
    handled = 0
    with ds.open_browser() as ctx:
        search_page = ds.first_page(ctx)
        worker = {"page": ctx.new_page()}

        def get_worker_page():
            """合集流程/兜底共用页签（懒复活）：后台闲置页签会被
            Chromium 丢弃(Tab Discard)，用时检查 is_closed，关了重开。"""
            pg = worker["page"]
            if pg.is_closed():
                log("  (worker 页签失效，重开)", flush=True)
                pg = ctx.new_page()
                worker["page"] = pg
            return pg

        ds.ensure_login(ctx, search_page, ds.DOUYIN_HOME)
        stop = {"flag": False}

        for idx, kw in enumerate(keywords, 1):
            if stop["flag"]:
                break
            log(f"\n=== 精选搜索 关键词 [{idx}/{len(keywords)}] {kw} ===",
                  flush=True)
            seen_local, raw, scanned, no_mix = set(), 0, 0, 0
            state = {"verify": False, "prompted": False}
            pending = []
            seen_mix = set()  # 本词内按合集去重（跨词靠 done_ids/skip_list）

            def on_response(resp):
                nonlocal raw, scanned, no_mix
                if ds.SEARCH_URL_PREFIX not in resp.url:
                    return
                try:
                    payload = resp.json()
                except Exception:
                    return
                if ds.is_verify_block(payload):
                    state["verify"] = True
                    return
                for it in ds.parse_search_response(payload, seen_local):
                    scanned += 1
                    if it["aweme_id"] in done_ids:
                        continue
                    if not it.get("mix_id"):
                        no_mix += 1  # 无合集标记 → 散片(clips)的地盘
                        continue
                    mid = str(it["mix_id"])
                    if mid in seen_mix:
                        continue  # 同合集多集入口，一部只处理一次
                    seen_mix.add(mid)
                    raw += 1
                    ok, reason = ds.passes_filter(
                        it, max_followers, max_duration, max_likes,
                        min_duration)
                    if ok:
                        bad, word = ds.author_blocked(it, block_keywords)
                        if bad:
                            continue
                        pending.append(it)
                    else:
                        log(f"  跳过: "
                              f"{(it['title'] or it['aweme_id'])[:24]}"
                              f"（{reason}）", flush=True)

            def drain():
                """新发现候选逐部处理（发现即处理，做完再继续搜）。"""
                nonlocal handled
                while pending and not stop["flag"]:
                    it = pending.pop(0)
                    done_ids.add(it["aweme_id"])
                    handled += 1
                    try:
                        if process_series(it, get_worker_page):
                            stop["flag"] = True
                    except Exception as e:  # noqa: BLE001
                        if is_conn_dead(e):
                            # 浏览器/driver 整体断连——停本轮不空烧，
                            # 进度已保存，重跑同命令续传
                            log("  !! 浏览器已断开，停止本轮"
                                  "（重跑同命令续传）", flush=True)
                            stop["flag"] = True
                        else:
                            raise

            search_page.on("response", on_response)
            try:
                for url in ds._search_urls(kw, prefer_jingxuan=True):
                    search_page.goto(url, timeout=30000)
                    ds.wait_captcha(search_page)
                    probe = time.time() + 12
                    while time.time() < probe and scanned == 0:
                        search_page.wait_for_timeout(1500)
                    if scanned:
                        log(f"  (路由命中: {url.split('/')[3]})", flush=True)
                        break
                    log(f"  (路由 {url.split('/')[3]} 无数据，切换…)",
                          flush=True)
                # 长等待滑块
                deadline = time.time() + ds.VERIFY_WAIT
                while scanned == 0 and time.time() < deadline:
                    if state["verify"] and not state["prompted"]:
                        urgent(">>> 触发滑块验证：请在浏览器窗口中拖动完成拼图 <<<")
                        state["prompted"] = True
                    search_page.wait_for_timeout(1500)
                drain()  # 首屏合格候选立即开始处理
                # 翻页推进以 scanned(含已下载)计——旧数据页不算"无进展"
                idle = 0
                last_reload = time.time()
                rescan = {"target": None, "steps": 0}
                while idle < ds.MAX_IDLE_SCROLLS and not stop["flag"]:
                    if time.time() - last_reload > ds.SEARCH_RELOAD_S:
                        # 定期重载释放 DOM 内存；重载后快进重扫已见
                        # 部分（seen_local 去重，不会重复处理）
                        rescan["target"] = scanned
                        rescan["steps"] = 0
                        log("  (定期重载搜索页，释放 DOM 内存…)", flush=True)
                        search_page.reload(timeout=30000)
                        ds.wait_captcha(search_page)
                        search_page.wait_for_timeout(2000)
                        last_reload = time.time()
                        idle = 0
                        continue
                    before = scanned
                    if rescan["target"] is not None:
                        # 快进重扫：大步滚动+短等待（内容大概率已见过）
                        rescan["steps"] += 1
                        if scanned >= rescan["target"] or rescan["steps"] > 400:
                            rescan["target"] = None
                            search_page.mouse.wheel(0, 2000)
                            search_page.wait_for_timeout(
                                int(ds.SCROLL_WAIT * 1000))
                        else:
                            search_page.mouse.wheel(0, 8000)
                            search_page.wait_for_timeout(600)
                        drain()
                        continue
                    search_page.mouse.wheel(0, 2000)
                    search_page.wait_for_timeout(int(ds.SCROLL_WAIT * 1000))
                    drain()  # 本屏新候选立即处理，做完再滚
                    idle = 0 if scanned > before else idle + 1
            finally:
                search_page.remove_listener("response", on_response)
            log(f"  (「{kw}」扫描 {scanned} 条，已下载跳过 "
                  f"{scanned - raw - no_mix} 条，散片让给clips {no_mix} 条，"
                  f"合集候选 {raw} 条)", flush=True)
            if idx < len(keywords) and not stop["flag"]:
                time.sleep(random.uniform(3, 5))  # 词间降温
    return handled



# ---------- 编排 ----------

def run(keywords, limit, filters, block_keywords, frames_n, api_key,
        base_url, model, out_dir: Path, sample=2, max_ep_duration=600,
        min_episodes=0, min_duration=30):
    state = douyin_auto.load_state(out_dir)
    douyin_auto.reconcile_state(out_dir, state)
    # 项目级弃剧名单(跨天): 有水印弃用的合集直接跳过, 不再重复采样
    skip_path = paths.SKIP_LIST_PATH
    skip_list = {}
    if skip_path.exists():
        try:
            skip_list = json.loads(skip_path.read_text(encoding="utf-8"))
        except Exception:
            skip_list = {}
    done_ids = (set(state["processed"])
                | ds.existing_ids_under(ds.DOWNLOADS_DIR))
    log(f"起点: 已处理 {len(done_ids)} 条")

    # 可变目录引用: 跨午夜翻日期（通宵跑），每部剧开始前检查——
    # 粒度=整部剧，绝不在一部剧中途切目录
    cur = {"dir": out_dir}
    stat = {"clean": 0, "wm": 0, "skip_done": 0}
    n_new = 0

    def process_series(it, get_worker_page) -> bool:
        """处理单部合集候选（发现即处理）。返回 True = 已达 limit 应停搜。

        get_worker_page: worker 页工厂（合集页浏览+兜底共用，页签被
        Chromium 丢弃时自动重开）。"""
        nonlocal n_new
        mid_str = str(it.get("mix_id") or "")
        name = it.get("mix_name") or (it["title"][:20] or "未命名剧集")
        log(f"\n=== 剧集 {name[:24]} ===")
        if it.get("sec_uid"):
            log(f"作者: {it.get('nick') or '?'}  "
                  f"主页: https://www.douyin.com/user/{it['sec_uid']}")
        log(f"入口视频: {it['aweme_id']}")
        if mid_str in skip_list:
            rec = skip_list[mid_str]
            log(f"  ↳ 已弃剧记录（{rec.get('date', '?')} "
                  f"{rec.get('reason', '')}），直接跳过")
            done_ids.add(it["aweme_id"])
            stat["skip_done"] += 1
            return n_new >= limit

        def record_abandon(reason, count=None):
            if mid_str:
                skip_list[mid_str] = {"name": name[:40],
                                      "reason": reason,
                                      "date": time.strftime("%Y-%m-%d")}
                try:
                    skip_path.write_text(
                        json.dumps(skip_list, ensure_ascii=False, indent=1),
                        encoding="utf-8")
                except Exception:
                    pass
            # 目录改名: 前置弃用标记 + 集数（不影响去重——去重靠文件
            # 名尾部ID；时间信息在上层小时桶目录名上）
            try:
                if sdir.is_dir() and not sdir.name.startswith(
                        "有水印弃用-"):
                    tag = "有水印弃用-" + ds.safe_dir_name(name)
                    if count:
                        tag += f"({count}集)"
                    sdir.rename(sdir.with_name(tag))
                    log(f"  ↳ 目录已标记: {tag}", flush=True)
            except Exception:
                pass

        # 目录按下载起始小时分桶: 剧集/<HH点>/<剧名>/（与散片时间桶
        # 同风格）；认领历史目录（桶内/过渡期平铺/老式裸名）防分裂
        base = ds.safe_dir_name(name)
        jdir = ds.roll_date_dir(cur) / "剧集"
        sdir = claim_series_dir(jdir, base, time.strftime("%H点"))
        q = sdir / "疑似水印"

        def fetch_and_judge(ep, j, total_eps):
            """下载并判定单集。返回 clean/watermarked/skip/error。"""
            evid = ep["aweme_id"]
            done_ids.add(evid)
            prefix = ds.episode_prefix(ep.get("ep") or j, total_eps)
            try:
                douyin_dl.run(f"https://www.douyin.com/video/{evid}",
                              sdir, name_prefix=prefix,
                              fallback_page=get_worker_page)
            except douyin_dl.ParseError as e:
                state["processed"][evid] = {"verdict": "skip",
                                            "desc": str(e)}
                douyin_auto.save_state(cur["dir"], state)
                return "skip"
            except Exception as e:  # noqa: BLE001 - 单集失败不中断
                if is_conn_dead(e):
                    raise  # 浏览器整体断连 → 交给 drain 层停轮
                log(f"  {prefix}下载失败（重跑续传）: {e}")
                time.sleep(2)
                return "error"
            f = douyin_auto.find_by_id(sdir, evid)
            if not f:
                return "error"
            try:
                v = wf.judge_file(f, api_key, base_url, model,
                                  frames_n, sdir / ".wm_frames",
                                  author=it.get("nick") or "")
            except Exception as e:  # noqa: BLE001 - 识图失败保留重判
                log(f"  {prefix}识图失败（保留，重跑重判）: {e}")
                return "error"
            if v.get("has_author_watermark"):
                q.mkdir(exist_ok=True)
                shutil.move(str(f), str(wf.unique_dest(q / f.name)))
                state["processed"][evid] = {
                    "verdict": "watermarked",
                    "desc": v.get("desc", "")[:60]}
                log(f"  ⚠ {prefix}有作者水印 → 移走")
                stat["wm"] += 1
            else:
                state["processed"][evid] = {"verdict": "clean"}
                log(f"  ✓ {prefix}干净")
                stat["clean"] += 1
            douyin_auto.save_state(cur["dir"], state)
            time.sleep(random.uniform(1, 2))
            return "watermarked" if v.get("has_author_watermark") else "clean"

        # (download_only 已删除: 中间集免判盲区实测漏检, 全集判定)

        # 采样提速①：集合一够采样量就提前返回，先下前几集验水印；
        # 全有水印 → 弃剧（全集没滑完、其余没下载，最快路径）
        early = sample if (sample or 0) >= 2 else 0
        try:
            eps, complete = collect_collection(
                it["aweme_id"], it.get("sec_uid") or "",
                mix_id=str(it.get("mix_id") or ""), mix_name=name,
                early_stop=early, page=get_worker_page())
        except ds.SearchError as e:
            if "入口视频需付费" in str(e):
                log(f"  ⚑ 入口视频需付费 → 弃剧", flush=True)
                record_abandon("入口视频需付费")
                stat["abandoned"] = stat.get("abandoned", 0) + 1
                log("  ⚑ 本部完成（付费跳过，不占 limit 配额）")
                event({"type": "progress", "done": n_new, "total": limit,
                       "unit": "部", "now": "付费合集跳过"})
            else:
                log(f"  !! 拉合集失败: {e}")
            return n_new >= limit
        if not complete and len(eps) >= 2:
            head = ds.sort_episodes(eps)[:sample]
            verdicts = [fetch_and_judge(ep, j, max(len(eps), sample))
                        for j, ep in enumerate(head, 1)]
            judged = [v for v in verdicts
                      if v in ("clean", "watermarked")]
            # 前几集任一有水印 → 开头就挂印，后面逻辑全不走（用户规则）
            if any(v == "watermarked" for v in judged):
                log(f"  ⚑ 前{len(judged)}集采样即有水印"
                      f"（{sum(1 for v in judged if v == 'watermarked')}"
                      f"/{len(judged)}）→ 弃剧（后续逻辑全跳过）", flush=True)
                record_abandon("前几集采样即有水印")
                stat["abandoned"] = stat.get("abandoned", 0) + 1
                log("  ⚑ 本部完成（采样弃剧，不占 limit 配额）")
                event({"type": "progress", "done": n_new, "total": limit,
                       "unit": "部", "now": "采样弃剧"})
                return n_new >= limit
            log("  (采样通过 → 冲刺翻页拉取全剧集目录（只取列表不下载）…)",
                  flush=True)
            try:
                eps, complete = collect_collection(
                    it["aweme_id"], it.get("sec_uid") or "",
                    mix_id=str(it.get("mix_id") or ""), mix_name=name,
                    fast=True, page=get_worker_page())
            except ds.SearchError as e:
                log(f"  !! 拉全集失败: {e}")
                return n_new >= limit
        # 滑动偶发不全：拿到的全已下载且数量偏少 → 重拉一次
        if (eps and len(eps) < 8
                and all(ep["aweme_id"] in done_ids for ep in eps)):
            log("  ↳ 疑似滑动不全，重拉一次…", flush=True)
            try:
                eps2, _ = collect_collection(
                    it["aweme_id"], it.get("sec_uid") or "",
                    mix_id=str(it.get("mix_id") or ""), mix_name=name,
                    page=get_worker_page())
            except ds.SearchError:
                eps2 = []
            if len(eps2) > len(eps):
                eps = eps2
        # 合并总集拦截（全集列表到手后、采样下载前——最快拦截点）
        comp, n_long, m = is_compilation(eps, max_ep_duration)
        if comp:
            log(f"  ⚑ 合并总集类合集（{n_long}/{m} 集超 "
                f"{max_ep_duration // 60} 分钟）→ 弃剧", flush=True)
            record_abandon(f"合并总集({n_long}/{m}集超{max_ep_duration}s)")
            stat["abandoned"] = stat.get("abandoned", 0) + 1
            event({"type": "progress", "done": n_new, "total": limit,
                   "unit": "部", "now": "合并总集弃剧"})
            log("  ⚑ 本部完成（合并总集弃剧，不占 limit 配额）")
            return n_new >= limit
        # 总集数下限（数量少的合集不要，不占配额；记弃剧名单防重跑空转）
        if min_episodes and len(eps) < min_episodes:
            log(f"  ⚑ 总集数 {len(eps)} < {min_episodes} → 跳过（数量少）",
                flush=True)
            record_abandon(f"总集数不足({len(eps)}<{min_episodes})",
                           count=len(eps))
            stat["abandoned"] = stat.get("abandoned", 0) + 1
            event({"type": "progress", "done": n_new, "total": limit,
                   "unit": "部", "now": f"总集数不足跳过({len(eps)}集)"})
            return n_new >= limit
        if len(eps) < 2:
            log("  ↳ 只拿到 1 集（非完整剧集），跳过")
            return n_new >= limit
        cont, why = looks_continuous(eps)
        log(f"  连续性: {'✓ ' + why if cont else '△ 标题不规整（' + why + '），仍按合集下载'}")
        if all(ep["aweme_id"] in done_ids for ep in eps):
            log(f"  ↳ 全部 {len(eps)} 集已下载过，跳过（不占配额）")
            done_ids.add(it["aweme_id"])  # 入口非分集(导流片), 排除重翻
            stat["skip_done"] += 1
            return n_new >= limit
        log(f"  共 {len(eps)} 集 → {sdir}")
        # 采样提速③(2026-09-08 修订)：首2+尾2【优先判定】快速弃剧；
        # 中间集不再免判——nojudge 盲区实测漏检(每集都有水印的剧
        # 首尾恰好干净, 如 AI视频素材: 思政小管家水印集) → 全集判定
        if (sample or 0) >= 2 and len(eps) >= 2:
            tail_pending = [ep for ep in eps[-sample:]
                            if ep["aweme_id"] not in done_ids]
            if tail_pending:
                log(f"  → 直接下载最后 {len(tail_pending)} 集采样判定…",
                      flush=True)
            positions = (list(enumerate(eps[:sample], 1))
                         + list(enumerate(eps[-sample:],
                                          len(eps) - sample + 1)))
            verdicts = []
            for j, ep in positions:
                evid = ep["aweme_id"]
                if evid in done_ids:
                    verdicts.append(
                        state["processed"].get(evid, {}).get("verdict")
                        or "unknown")
                    continue
                verdicts.append(fetch_and_judge(ep, j, len(eps)))
            eff = [v for v in verdicts
                   if v in ("clean", "watermarked")]
            # 统一规则：采样4集中任一有水印 → 弃剧（无慢路径）
            if any(v == "watermarked" for v in eff):
                wm_n = sum(1 for v in eff if v == "watermarked")
                log(f"  ⚑ 采样{len(eff)}集中{wm_n}集有水印 → 弃剧",
                      flush=True)
                record_abandon(f"采样{wm_n}/{len(eff)}集有水印",
                               count=len(eps))
                stat["abandoned"] = stat.get("abandoned", 0) + 1
                log("  ⚑ 本部完成（采样弃剧，不占 limit 配额）")
                event({"type": "progress", "done": n_new, "total": limit,
                       "unit": "部", "now": "采样弃剧"})
                return n_new >= limit
            # (2026-09-08 修订) 采样集均无水印后, 中间集【不再免判】——
            # nojudge 盲区实测漏检: 每集都有水印的剧(思政小管家水印集)
            # 首尾恰好干净, 中间集水印完全检测不到 → 全集判定
        for j, ep in enumerate(eps, 1):
            if ep["aweme_id"] in done_ids:
                continue
            fetch_and_judge(ep, j, len(eps))
        # 成功保留: 目录名加集数（幂等，已带括号则跳过；小时在桶目录上）
        try:
            target = f"{base}({len(eps)}集)"
            if (sdir.is_dir() and sdir.name != target
                    and not sdir.name.startswith("有水印弃用-")):
                sdir.rename(sdir.with_name(target))
                log(f"  ↳ 目录改名: {target}", flush=True)
        except Exception:
            pass
        n_new += 1
        log("  ⚑ 本部完成")
        event({"type": "progress", "done": n_new, "total": limit,
               "unit": "部", "now": f"已保留 {n_new}/{limit} 部"})
        return n_new >= limit

    # 流式: 搜索发现一部 → 立刻下载处理 → 做完再继续滚动翻页
    handled = collect_series_stream(keywords, filters, block_keywords,
                                    done_ids, process_series,
                                    min_duration=min_duration)
    if not handled:
        log("!! 没有新候选（关键词翻尽或全被筛选/去重排除）")
    event({"type": "done", "done": n_new, "total": limit})
    log(f"\n==== 结束 ====")
    log(f"成功保留 {n_new} 部 / 弃用 {stat.get('abandoned', 0)} 部"
          f" / 已完整跳过 {stat['skip_done']} 部"
          f"｜分集: 干净 {stat['clean']} / 水印移走 {stat['wm']}")



def main(argv=None):
    parser = argparse.ArgumentParser(
        description="精选合集：搜索→作者合集页→具体合集滑动拉全→逐集下载验水印")
    parser.add_argument("keyword", nargs="?", help="关键词，中英文逗号分隔")
    parser.add_argument("--limit", type=int, default=10,
                        help="成功保留的剧数（默认 10；弃用剧/已完整剧"
                             " 不计入）")
    parser.add_argument("--max-followers", type=int, default=None)
    parser.add_argument("--max-duration", type=int, default=None)
    parser.add_argument("--max-likes", type=int, default=None)
    parser.add_argument("--min-duration", type=int, default=30,
                        help="时长下限秒(滤过短碎片, 严格大于; 0=关闭, 默认30)")
    parser.add_argument("--block-keywords", default=None,
                        help="作者黑名单关键词; 默认内置搬运/侵权词表, 空串禁用")
    parser.add_argument("--frames", type=int, default=6)
    parser.add_argument("--max-ep-duration", type=int, default=600,
                        help="单集时长上限秒(合并总集拦截: 超时长集占比"
                             "达六成判弃; 0=关闭, 默认600)")
    parser.add_argument("--sample", type=int, default=2,
                        help="采样提速: 先下前N集验水印, 全有水印则跳过整部"
                             " (默认2, 0=关闭逐集判定)")
    parser.add_argument("--min-episodes", type=int, default=0,
                        help="总集数下限: 合集总集数小于该值则跳过"
                             " (0=不限)")
    parser.add_argument("--model", default=wf.DEFAULT_MODEL)
    parser.add_argument("--base-url", default=wf.DEFAULT_BASE_URL)
    parser.add_argument("--selftest", action="store_true",
                        help="运行内置自测（不联网）")
    args = parser.parse_args(argv)
    if args.selftest:
        sys.exit(0 if run_selftests() else 1)
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not args.keyword:
        parser.error("请提供搜索关键词")
    if not api_key:
        log("错误: 未设置 DASHSCOPE_API_KEY（key.txt 或环境变量）",
              file=sys.stderr)
        sys.exit(1)
    keywords = ds.split_keywords(args.keyword)
    out_dir = ds.make_dated_dir(ds.DOWNLOADS_DIR)
    log(f"输出目录: {out_dir}")
    filters = (args.max_followers, args.max_duration, args.max_likes)
    if any(v is not None for v in filters):
        log(f"筛选: 粉丝<{args.max_followers or '∞'} "
              f"时长<{args.max_duration or '∞'}s 赞<{args.max_likes or '∞'}")
    if args.block_keywords is None:
        block_kw = ds.DEFAULT_BLOCK_KEYWORDS
    else:
        block_kw = ds.split_keywords(args.block_keywords)
    try:
        run(keywords, args.limit, filters, block_kw, args.frames, api_key,
            args.base_url, args.model, out_dir, sample=args.sample,
            max_ep_duration=args.max_ep_duration or None,
            min_episodes=args.min_episodes or 0,
            min_duration=args.min_duration or None)
    except KeyboardInterrupt:
        log("\n中断（进度已保存，重跑同命令自动续）")
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
