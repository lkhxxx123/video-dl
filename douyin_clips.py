#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""douyin_clips.py — 散片下载器（非剧集短视频，独立于合集流程）

用户需求(2026-09-03)：
① 搜索入口用 /root/search/{关键词}?type=general（用户指定）
② 输出 downloads/日期/散片/HH点MM分/ —— 与"剧集"同级；按下载时刻
   10 分钟分桶（每小时 6 个桶）
③ 每条验水印，只抽 3 帧（比合集的 6 帧省一半）
④ 不改动合集相关脚本（本文件独立实现收集逻辑）
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
from urllib.parse import quote

import douyin_auto
import douyin_dl
import douyin_search as ds
import watermark_filter as wf

FRAMES = 3  # 用户指定：散片只抽 3 帧

# root 入口 type=general 走综合搜索接口(general/search/single),
# 视频/精选路由走 search/item —— 两种都要监听
CLIP_SEARCH_PREFIXES = ("aweme/v1/web/search/item/",
                        "aweme/v1/web/general/search/single/")


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

def clip_routes(keyword: str):
    """搜索路由：用户指定的 root/search 优先，标准与精选兜底。"""
    kw = quote(keyword)
    return [f"https://www.douyin.com/root/search/{kw}?type=general",
            f"https://www.douyin.com/search/{kw}?type=video",
            f"https://www.douyin.com/jingxuan/search/{kw}?type=video"]


def hour_bucket_name(now=None) -> str:
    """下载时刻的 10 分钟桶名（如 '14点30分'）。每小时 6 个桶。"""
    lt = time.localtime(now)
    return time.strftime("%H点", lt) + f"{(lt.tm_min // 10) * 10:02d}分"


# ---------- 测试 ----------

def test_clip_routes_root_first():
    r = clip_routes("抖音ai创作大赛")
    assert r[0].startswith("https://www.douyin.com/root/search/")
    assert "type=general" in r[0]
    assert len(r) == 3


def test_hour_bucket_name():
    # 10 分钟粒度：分钟向下取整到 10 的倍数
    assert re.fullmatch(r"\d{2}点\d{2}分", hour_bucket_name())
    assert hour_bucket_name(
        time.mktime((2026, 9, 3, 9, 5, 0, 0, 0, -1))) == "09点00分"
    assert hour_bucket_name(
        time.mktime((2026, 9, 3, 14, 37, 0, 0, 0, -1))) == "14点30分"
    assert hour_bucket_name(
        time.mktime((2026, 9, 3, 23, 59, 0, 0, 0, -1))) == "23点50分"



# ---------- 散片独立状态（与合集 auto_state.json 互不串账） ----------

def load_state(out_dir: Path) -> dict:
    p = out_dir / "clips_state.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data.get("processed"), dict):
                return data
        except Exception:
            pass
    return {"processed": {}}


def save_state(out_dir: Path, state: dict) -> None:
    (out_dir / "clips_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def reconcile_state(out_dir: Path, state: dict) -> None:
    alive = ds.existing_ids_under(ds.DOWNLOADS_DIR)
    drop = [vid for vid, v in state["processed"].items()
            if v.get("verdict") in ("clean", "watermarked")
            and vid not in alive]
    for vid in drop:
        del state["processed"][vid]
    if drop:
        save_state(out_dir, state)
        print(f"[状态清理] {len(drop)} 条记录的文件已不存在，已重置")


# ---------- 收集（独立实现，不动合集脚本） ----------

def collect_clips(keywords, pool_size, filters, block_keywords, done_ids):
    """root/search 搜索收集散片候选（过滤筛选/黑名单/全局去重）。"""
    max_followers, max_duration, max_likes = filters
    with ds.open_browser() as ctx:
        page = ds._first_page(ctx)
        ds.ensure_login(ctx, page)
        merged = []
        for idx, kw in enumerate(keywords, 1):
            if len(merged) >= pool_size:
                break
            print(f"\n=== 关键词 [{idx}/{len(keywords)}] {kw}"
                  f"（还需 {pool_size - len(merged)} 条）", flush=True)
            seen_local, raw, scanned = set(), 0, 0
            state = {"verify": False, "prompted": False}

            def on_response(resp):
                nonlocal raw, scanned
                if not any(p in resp.url for p in CLIP_SEARCH_PREFIXES):
                    return
                try:
                    payload = ds.resp_json(resp)
                except Exception:
                    return
                if payload is None:
                    return
                if ds.is_verify_block(payload):
                    state["verify"] = True
                    return
                for it in ds.parse_search_response(payload, seen_local):
                    scanned += 1
                    if it["aweme_id"] in done_ids:
                        continue
                    raw += 1
                    ok, reason = ds.passes_filter(
                        it, max_followers, max_duration, max_likes)
                    if ok:
                        bad, word = ds.author_blocked(it, block_keywords)
                        if bad:
                            continue
                        merged.append(it)
                    else:
                        print(f"  跳过: "
                              f"{(it['title'] or it['aweme_id'])[:24]}"
                              f"（{reason}）", flush=True)

            page.on("response", on_response)
            try:
                for url in clip_routes(kw):
                    page.goto(url, timeout=30000)
                    ds._wait_captcha(page)
                    probe = time.time() + 12
                    while time.time() < probe and scanned == 0:
                        page.wait_for_timeout(1500)
                    if scanned:
                        break
                    print(f"  (路由 {url.split('/')[3]} 无数据，切换…)",
                          flush=True)
                # 长等待滑块
                deadline = time.time() + ds.VERIFY_WAIT
                while scanned == 0 and time.time() < deadline:
                    if state["verify"] and not state["prompted"]:
                        print(">>> 触发滑块验证：请在浏览器窗口中拖动完成拼图"
                              " <<<", flush=True)
                        state["prompted"] = True
                    page.wait_for_timeout(1500)
                # 翻页推进以 scanned(含已下载)计——旧数据页不算"无进展",
                # 否则二轮搜索翻不过前几页已下载内容
                idle = 0
                while len(merged) < pool_size and idle < ds.MAX_IDLE_SCROLLS:
                    before = scanned
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(int(ds.SCROLL_WAIT * 1000))
                    idle = 0 if scanned > before else idle + 1
            finally:
                page.remove_listener("response", on_response)
            print(f"累计候选 {len(merged)}/{pool_size}"
                  f"（扫描 {scanned} 条，其中已下载跳过 "
                  f"{scanned - raw} 条）")
            if idx < len(keywords) and len(merged) < pool_size:
                time.sleep(random.uniform(3, 5))
        return merged[:pool_size]


# ---------- 编排 ----------

def run(keywords, limit, filters, block_keywords, api_key, base_url,
        model, out_dir: Path):
    state = load_state(out_dir)
    reconcile_state(out_dir, state)
    done_ids = (set(state["processed"])
                | ds.existing_ids_under(ds.DOWNLOADS_DIR))
    print(f"起点: 已处理 {len(done_ids)} 条")
    stat = {"clean": 0, "wm": 0}

    clean = sum(1 for v in state["processed"].values()
                if v.get("verdict") == "clean")
    while clean < limit:
        need = limit - clean
        print(f"\n=== 搜索散片（还需 {need} 条干净）===")
        batch = collect_clips(keywords, max(need * 2, 8), filters,
                              block_keywords, done_ids)
        if not batch:
            print("!! 没有新候选（关键词翻尽或全被筛选/去重排除）")
            break
        progressed = False
        for it in batch:
            done_ids.add(it["aweme_id"])
            if clean >= limit:
                break
            vid, title = it["aweme_id"], it["title"]
            if vid in state["processed"]:
                continue
            print(f"\n→ [{clean + 1}/{limit}] {title[:32] or vid}")
            # 10 分钟分桶：与"剧集"同级 → 日期/散片/HH点MM分/
            cdir = out_dir / "散片" / hour_bucket_name()
            cdir.mkdir(parents=True, exist_ok=True)
            q = cdir / "疑似水印"
            try:
                douyin_dl.run(f"https://www.douyin.com/video/{vid}", cdir)
            except douyin_dl.ParseError as e:
                state["processed"][vid] = {"verdict": "skip",
                                           "desc": str(e)}
                save_state(out_dir, state)
                progressed = True
                continue
            except Exception as e:  # noqa: BLE001 - 失败可重试
                print(f"  下载失败（重跑续传）: {e}")
                time.sleep(2)
                continue
            f = douyin_auto.find_by_id(cdir, vid)
            if not f:
                continue
            try:
                v = douyin_auto.judge_file(f, api_key, base_url, model,
                                           FRAMES, cdir / ".wm_frames")
            except Exception as e:  # noqa: BLE001 - 识图失败保留重判
                print(f"  识图失败（保留，重跑重判）: {e}")
                continue
            if v.get("has_author_watermark"):
                q.mkdir(exist_ok=True)
                shutil.move(str(f), str(wf.unique_dest(q / f.name)))
                state["processed"][vid] = {
                    "verdict": "watermarked",
                    "desc": v.get("desc", "")[:60]}
                print(f"  ⚠ 有作者水印 → 移走")
                stat["wm"] += 1
            else:
                state["processed"][vid] = {"verdict": "clean"}
                clean += 1
                print("  ✓ 干净，计入")
                stat["clean"] += 1
            save_state(out_dir, state)
            progressed = True
            time.sleep(random.uniform(1, 2))
        if not progressed:
            print("!! 本轮无进展，退出（重跑同命令可再试）")
            break
    print(f"\n==== 结束 ====")
    print(f"干净散片 {clean}/{limit}｜本轮: 干净 {stat['clean']}"
          f" / 水印移走 {stat['wm']}")
    print(f"散片目录: {out_dir / '散片'}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="散片下载: root搜索→按小时分桶→每条3帧验水印")
    parser.add_argument("keyword", nargs="?", help="关键词，中英文逗号分隔")
    parser.add_argument("--limit", type=int, default=10,
                        help="目标干净散片数（默认 10，水印的不计数）")
    parser.add_argument("--max-followers", type=int, default=None)
    parser.add_argument("--max-duration", type=int, default=None)
    parser.add_argument("--max-likes", type=int, default=None)
    parser.add_argument("--block-keywords", default=None,
                        help="作者黑名单关键词; 默认内置搬运/侵权词表, 空串禁用")
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
    print(f"输出目录: {out_dir / '散片'}（按 10 分钟分桶）")
    filters = (args.max_followers, args.max_duration, args.max_likes)
    if any(v is not None for v in filters):
        print(f"筛选: 粉丝<{args.max_followers or '∞'} "
              f"时长<{args.max_duration or '∞'}s 赞<{args.max_likes or '∞'}")
    print(f"水印判定: {FRAMES} 帧/条")
    if args.block_keywords is None:
        block_kw = ds.DEFAULT_BLOCK_KEYWORDS
    else:
        block_kw = ds.split_keywords(args.block_keywords)
    try:
        run(keywords, args.limit, filters, block_kw, api_key,
            args.base_url, args.model, out_dir)
    except KeyboardInterrupt:
        print("\n中断（进度已保存，重跑同命令自动续）")
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
