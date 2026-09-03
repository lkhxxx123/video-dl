#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""douyin_jx.py — 精选合集短剧下载器（按用户手动流程实现）

流程（用户实测指认）：
① 精选搜索 /jingxuan/search/{关键词}?type=video，只收带合集标记的候选
② 进作者主页（等效于视频页右上角点作者名）→ 切到"合集"页
③ 在合集页点进具体合集 → 滑动加载当前合集的所有剧集 → 逐集下载+AI验水印

每部剧独立目录 剧集/<剧名>/，集数零填充前缀（01_..），从第 1 集开始；
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

import douyin_auto
import douyin_dl
import douyin_search as ds
import watermark_filter as wf


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


def test_click_card_js_contents():
    assert "更新至" in _CLICK_CARD_JS and "el.click()" in _CLICK_CARD_JS
    # r-string 保证 /\n/g 的反斜杠n 原样送达 JS（此前真换行导致正则报错）
    assert "\\n" in _CLICK_CARD_JS
    assert "scrollTo" in _SCROLL_ALL_JS


# ---------- 核心：按用户流程收集合集全部剧集 ----------

def collect_collection(entry_video_id, sec_uid, mix_id="", mix_name="",
                       early_stop=0, fast=False):
    """作者主页 → 合集页 → 点进具体合集 → 滑动拉全部分集。

    返回按集数/发布时间升序的 [{aweme_id, title, ep, ct}]（从第 1 集开始）。
    """
    target = str(mix_id) if mix_id else ""
    with ds.open_browser() as ctx:
        page = ds._first_page(ctx)
        ds.ensure_login(ctx, page)
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
                print(f"  (+{len(fresh)} 集, 累计 {len(items)})", flush=True)

        page.on("response", on_response)
        try:
            # ② 作者主页（等效：视频页右上角作者名 → 主页）
            page.goto(f"https://www.douyin.com/user/{sec_uid}",
                      timeout=30000)
            ds._wait_captcha(page)
            page.wait_for_timeout(2000)
            # ③ 切换到"合集"页
            try:
                page.get_by_text("合集", exact=True).first.click(
                    timeout=5000)
                print("  (已切换到合集页)", flush=True)
            except Exception:
                page.goto(f"https://www.douyin.com/user/{sec_uid}"
                          "?showSubTab=compilation&showTab=post",
                          timeout=30000)
                ds._wait_captcha(page)
                print("  (直达合集页)", flush=True)
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
                print(f"  (已进入合集: {clicked[:50]})", flush=True)
                m = re.search(r"更新至\s*(\d+)\s*集", clicked)
                if m:
                    state["total"] = int(m.group(1))
                    print(f"  (该合集共 {state['total']} 集)", flush=True)
            else:
                print("  (未找到合集卡片，收集页面现有内容)", flush=True)
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
                print("  (已展开集数列表)", flush=True)
                page.wait_for_timeout(2000)
            except Exception:
                pass
            # 等首批剧集响应
            deadline = time.time() + 20
            while time.time() < deadline and not items:
                page.wait_for_timeout(1500)
            # ③'' 持续滑动加载当前合集所有剧集
            stopped_early = False
            idle = 0
            while idle < max_idle:
                if state["total"] and len(items) >= state["total"]:
                    break
                if early_stop >= 2 and len(items) >= early_stop:
                    stopped_early = True
                    print(f"  (已集齐采样所需 {len(items)} 集，提前返回)",
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
        if not items:
            raise ds.SearchError("未拦截到合集剧集接口（可能触发验证）")
        if state["total"] and len(items) < state["total"]:
            print(f"  ⚠ 只拿到 {len(items)}/{state['total']} 集（滑动未翻完）",
                  flush=True)
        return ds.sort_episodes(items), not stopped_early


# ---------- 编排 ----------

def run(keywords, limit, filters, block_keywords, frames_n, api_key,
        base_url, model, out_dir: Path, sample=2):
    state = douyin_auto.load_state(out_dir)
    douyin_auto.reconcile_state(out_dir, state)
    # 项目级弃剧名单(跨天): 有水印弃用的合集直接跳过, 不再重复采样
    skip_path = ds.SCRIPT_DIR / "watermark_skip.json"
    skip_list = {}
    if skip_path.exists():
        try:
            skip_list = json.loads(skip_path.read_text(encoding="utf-8"))
        except Exception:
            skip_list = {}
    done_ids = (set(state["processed"])
                | ds.existing_ids_under(ds.DOWNLOADS_DIR))
    print(f"起点: 已处理 {len(done_ids)} 条")

    print(f"\n=== 精选搜索（目标新下载 {limit} 部剧）===")
    pool = ds.collect_many(keywords, limit * 4, *filters, seen=done_ids,
                           block_keywords=block_keywords,
                           prefer_jingxuan=True)
    series_all, total_found = pick_series(pool, 10 ** 6)
    print(f"\n候选中带合集标记: {total_found} 部（已完整的自动跳过，不占配额）")
    if not series_all:
        print("!! 没有带合集标记的候选（可放宽点赞阈值或换关键词）")
        return

    stat = {"clean": 0, "wm": 0, "skip_done": 0, "nojudge": 0}
    n_new = 0
    for i, it in enumerate(series_all, 1):
        if n_new >= limit:
            break
        name = it.get("mix_name") or (it["title"][:20] or "未命名剧集")
        print(f"\n=== 剧集 [候选{i}] {name[:24]} ===")
        if it.get("sec_uid"):
            print(f"作者: {it.get('nick') or '?'}  "
                  f"主页: https://www.douyin.com/user/{it['sec_uid']}")
        print(f"入口视频: {it['aweme_id']}")
        mid_str = str(it.get("mix_id") or "")
        if mid_str in skip_list:
            rec = skip_list[mid_str]
            print(f"  ↳ 已弃剧记录（{rec.get('date', '?')} "
                  f"{rec.get('reason', '')}），直接跳过")
            stat["skip_done"] += 1
            continue

        def record_abandon(reason):
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

        sdir = out_dir / "剧集" / ds.safe_dir_name(name)
        q = sdir / "疑似水印"

        def fetch_and_judge(ep, j, total_eps):
            """下载并判定单集。返回 clean/watermarked/skip/error。"""
            evid = ep["aweme_id"]
            done_ids.add(evid)
            prefix = ds.episode_prefix(ep.get("ep") or j, total_eps)
            try:
                douyin_dl.run(f"https://www.douyin.com/video/{evid}",
                              sdir, name_prefix=prefix)
            except douyin_dl.ParseError as e:
                state["processed"][evid] = {"verdict": "skip",
                                            "desc": str(e)}
                douyin_auto.save_state(out_dir, state)
                return "skip"
            except Exception as e:  # noqa: BLE001 - 单集失败不中断
                print(f"  {prefix}下载失败（重跑续传）: {e}")
                time.sleep(2)
                return "error"
            f = douyin_auto.find_by_id(sdir, evid)
            if not f:
                return "error"
            try:
                v = douyin_auto.judge_file(f, api_key, base_url, model,
                                           frames_n, sdir / ".wm_frames")
            except Exception as e:  # noqa: BLE001 - 识图失败保留重判
                print(f"  {prefix}识图失败（保留，重跑重判）: {e}")
                return "error"
            if v.get("has_author_watermark"):
                q.mkdir(exist_ok=True)
                shutil.move(str(f), str(wf.unique_dest(q / f.name)))
                state["processed"][evid] = {
                    "verdict": "watermarked",
                    "desc": v.get("desc", "")[:60]}
                print(f"  ⚠ {prefix}有作者水印 → 移走")
                stat["wm"] += 1
            else:
                state["processed"][evid] = {"verdict": "clean"}
                print(f"  ✓ {prefix}干净")
                stat["clean"] += 1
            douyin_auto.save_state(out_dir, state)
            time.sleep(random.uniform(1, 2))
            return "watermarked" if v.get("has_author_watermark") else "clean"

        def download_only(ep, j):
            """首尾采样通过后：只下载不判定（省识图调用）。"""
            evid = ep["aweme_id"]
            done_ids.add(evid)
            prefix = ds.episode_prefix(ep.get("ep") or j, len(eps))
            try:
                douyin_dl.run(f"https://www.douyin.com/video/{evid}",
                              sdir, name_prefix=prefix)
            except Exception as e:  # noqa: BLE001
                print(f"  {prefix}下载失败（重跑续传）: {e}")
                time.sleep(2)
                return
            state["processed"][evid] = {
                "verdict": "nojudge",
                "desc": "首尾采样均无水印，跳过判定"}
            douyin_auto.save_state(out_dir, state)
            print(f"  ↓ {prefix}已下载（未判定）")
            stat["nojudge"] += 1
            time.sleep(random.uniform(1, 2))

        # 采样提速①：集合一够采样量就提前返回，先下前几集验水印；
        # 全有水印 → 弃剧（全集没滑完、其余没下载，最快路径）
        early = sample if (sample or 0) >= 2 else 0
        try:
            eps, complete = collect_collection(
                it["aweme_id"], it.get("sec_uid") or "",
                mix_id=str(it.get("mix_id") or ""), mix_name=name,
                early_stop=early)
        except ds.SearchError as e:
            print(f"  !! 拉合集失败: {e}")
            continue
        if not complete and len(eps) >= 2:
            head = ds.sort_episodes(eps)[:sample]
            verdicts = [fetch_and_judge(ep, j, max(len(eps), sample))
                        for j, ep in enumerate(head, 1)]
            judged = [v for v in verdicts
                      if v in ("clean", "watermarked")]
            # 前几集任一有水印 → 开头就挂印，后面逻辑全不走（用户规则）
            if any(v == "watermarked" for v in judged):
                print(f"  ⚑ 前{len(judged)}集采样即有水印"
                      f"（{sum(1 for v in judged if v == 'watermarked')}"
                      f"/{len(judged)}）→ 弃剧（后续逻辑全跳过）", flush=True)
                record_abandon("前几集采样即有水印")
                n_new += 1
                print("  ⚑ 本部完成（采样弃剧）")
                continue
            print("  (采样通过 → 冲刺翻页拉取全剧集目录（只取列表不下载）…)",
                  flush=True)
            try:
                eps, complete = collect_collection(
                    it["aweme_id"], it.get("sec_uid") or "",
                    mix_id=str(it.get("mix_id") or ""), mix_name=name,
                    fast=True)
            except ds.SearchError as e:
                print(f"  !! 拉全集失败: {e}")
                continue
        # 滑动偶发不全：拿到的全已下载且数量偏少 → 重拉一次
        if (eps and len(eps) < 8
                and all(ep["aweme_id"] in done_ids for ep in eps)):
            print("  ↳ 疑似滑动不全，重拉一次…", flush=True)
            try:
                eps2, _ = collect_collection(
                    it["aweme_id"], it.get("sec_uid") or "",
                    mix_id=str(it.get("mix_id") or ""), mix_name=name)
            except ds.SearchError:
                eps2 = []
            if len(eps2) > len(eps):
                eps = eps2
        if len(eps) < 2:
            print("  ↳ 只拿到 1 集（非完整剧集），跳过")
            continue
        cont, why = ds.looks_continuous(eps)
        print(f"  连续性: {'✓ ' + why if cont else '△ 标题不规整（' + why + '），仍按合集下载'}")
        if all(ep["aweme_id"] in done_ids for ep in eps):
            print(f"  ↳ 全部 {len(eps)} 集已下载过，跳过（不占配额）")
            stat["skip_done"] += 1
            continue
        print(f"  共 {len(eps)} 集 → {sdir}")
        # 采样提速③：首2集已判过 → 此处直接下载最后2集判定；
        # 均无水印 → 中间集只下载不判定（首集抓"从头有水印"，
        # 尾集抓"中途才加水印"——作者涨粉后加印常见）
        skip_judge = False
        if (sample or 0) >= 2 and len(eps) > 2 * sample:
            tail_pending = [ep for ep in eps[-sample:]
                            if ep["aweme_id"] not in done_ids]
            if tail_pending:
                print(f"  → 直接下载最后 {len(tail_pending)} 集采样判定…",
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
                print(f"  ⚑ 采样{len(eff)}集中{wm_n}集有水印 → 弃剧",
                      flush=True)
                record_abandon(f"采样{wm_n}/{len(eff)}集有水印")
                n_new += 1
                print("  ⚑ 本部完成（采样弃剧）")
                continue
            # 全净才免判中间；且要求至少 2*sample-1 集有效判定
            # （防识图失败被当成通过）
            if (len(eff) >= 2 * sample - 1
                    and all(v == "clean" for v in eff)):
                skip_judge = True
                mid = len(eps) - 2 * sample
                print(f"  ⚑ 首尾{len(eff)}集均无水印 → 中间 {mid} 集"
                      f"只下载不判定", flush=True)
        for j, ep in enumerate(eps, 1):
            if ep["aweme_id"] in done_ids:
                continue
            if skip_judge:
                download_only(ep, j)
            else:
                fetch_and_judge(ep, j, len(eps))
        n_new += 1
        print("  ⚑ 本部完成")
    print(f"\n==== 结束 ====")
    print(f"新处理 {n_new} 部 / 已完整跳过 {stat['skip_done']} 部"
          f"｜分集: 干净 {stat['clean']} / 水印移走 {stat['wm']}"
          f" / 未判定 {stat['nojudge']}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="精选合集：搜索→作者合集页→具体合集滑动拉全→逐集下载验水印")
    parser.add_argument("keyword", nargs="?", help="关键词，中英文逗号分隔")
    parser.add_argument("--limit", type=int, default=10,
                        help="下载剧的部数（默认 10，按部计数）")
    parser.add_argument("--max-followers", type=int, default=None)
    parser.add_argument("--max-duration", type=int, default=None)
    parser.add_argument("--max-likes", type=int, default=None)
    parser.add_argument("--block-keywords", default=None,
                        help="作者黑名单关键词; 默认内置搬运/侵权词表, 空串禁用")
    parser.add_argument("--frames", type=int, default=6)
    parser.add_argument("--sample", type=int, default=2,
                        help="采样提速: 先下前N集验水印, 全有水印则跳过整部"
                             " (默认2, 0=关闭逐集判定)")
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
        print("错误: 未设置 DASHSCOPE_API_KEY（key.txt 或环境变量）",
              file=sys.stderr)
        sys.exit(1)
    keywords = ds.split_keywords(args.keyword)
    out_dir = ds.make_dated_dir(ds.DOWNLOADS_DIR)
    print(f"输出目录: {out_dir}")
    filters = (args.max_followers, args.max_duration, args.max_likes)
    if any(v is not None for v in filters):
        print(f"筛选: 粉丝<{args.max_followers or '∞'} "
              f"时长<{args.max_duration or '∞'}s 赞<{args.max_likes or '∞'}")
    if args.block_keywords is None:
        block_kw = ds.DEFAULT_BLOCK_KEYWORDS
    else:
        block_kw = ds.split_keywords(args.block_keywords)
    try:
        run(keywords, args.limit, filters, block_kw, args.frames, api_key,
            args.base_url, args.model, out_dir, sample=args.sample)
    except KeyboardInterrupt:
        print("\n中断（进度已保存，重跑同命令自动续）")
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
