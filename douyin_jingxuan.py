#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""douyin_jingxuan.py — 抖音精选搜索：只下载带合集标记的短剧（整部）

与 douyin_auto 的区别：
- 精选 /jingxuan/search/ 路由优先（实测更稳）
- 只收带合集/系列标记（mix_id）的候选，散视频不下
- --limit 计「部」数；每部剧独立目录 剧集/<剧名>/，按集数升序从第 1 集下起
其余（点赞/粉丝/时长筛选、作者黑名单、AI 验水印、全局去重、视频清单）
全部复用既有引擎。仅限个人离线保存，勿二次上传。
"""
import argparse
import os
import random
import shutil
import sys
import time
from pathlib import Path

import douyin_auto
import douyin_dl
import douyin_search
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
    """从候选池挑出带合集/系列标记的剧集，截取前 limit 部。

    返回 (选中列表, 命中总数)。
    """
    series = [it for it in pool if it.get("mix_id")]
    return series[:limit], len(series)


# ---------- tests ----------

def test_pick_series_only_mix_and_limit():
    pool = [
        {"aweme_id": "1", "mix_id": 111, "mix_name": "剧A"},
        {"aweme_id": "2", "mix_id": None},                      # 散视频
        {"aweme_id": "3", "mix_id": 333, "mix_name": "剧B"},
        {"aweme_id": "4", "mix_id": 444, "mix_name": "剧C"},
    ]
    got, total = pick_series(pool, 2)
    assert total == 3
    assert [g["mix_name"] for g in got] == ["剧A", "剧B"]
    # limit 大于命中数 → 全取
    got2, total2 = pick_series(pool, 10)
    assert len(got2) == 3 and total2 == 3


# ---------- 编排 ----------

def run(keywords, limit, filters, block_keywords, frames_n, api_key,
        base_url, model, out_dir: Path):
    state = douyin_auto.load_state(out_dir)
    done_ids = (set(state["processed"])
                | douyin_search.existing_ids_under(
                    douyin_search.DOWNLOADS_DIR))
    print(f"起点: 已处理 {len(done_ids)} 条")

    # 收集池放大 4 倍——带合集标记的候选通常只占少数
    print(f"\n=== 精选搜索（目标 {limit} 部剧）===")
    pool = douyin_search.collect_many(keywords, limit * 4, *filters,
                                      seen=done_ids,
                                      block_keywords=block_keywords,
                                      prefer_jingxuan=True)
    series_list, total_found = pick_series(pool, limit)
    print(f"\n候选中带合集标记: {total_found} 部 → 处理 {len(series_list)} 部")
    if not series_list:
        print("!! 没有带合集标记的候选（可放宽点赞阈值或换关键词）")
        return

    stat = {"clean": 0, "wm": 0, "skip_done": 0}
    n_new = 0
    for i, it in enumerate(series_list, 1):
        name = it.get("mix_name") or (it["title"][:20] or "未命名剧集")
        print(f"\n=== 剧集 [{i}/{len(series_list)}] {name[:24]} ===")
        try:
            eps = douyin_search.collect_mix(it["aweme_id"])
        except douyin_search.SearchError as e:
            print(f"  !! 拉全集失败: {e}")
            continue
        if len(eps) < 2:
            print("  ↳ 只拿到 1 集（非完整剧集），跳过")
            continue
        # 合集真实性门控：标题混杂的"杂物合集"不下整部
        cont, why = douyin_search.looks_continuous(eps)
        if not cont:
            print(f"  ↳ 合集不像连续剧集（{why}），跳过整部")
            continue
        print(f"  连续性校验: {why}")
        if all(ep["aweme_id"] in done_ids for ep in eps):
            print(f"  ↳ 全部 {len(eps)} 集已下载过，跳过")
            stat["skip_done"] += 1
            continue
        # 已按集数升序（无集数按发布时间）—— 从第 1 集开始
        sdir = out_dir / "剧集" / douyin_search.safe_dir_name(name)
        q = sdir / "疑似水印"
        print(f"  共 {len(eps)} 集 → {sdir}")
        for j, ep in enumerate(eps, 1):
            evid = ep["aweme_id"]
            if evid in done_ids:
                continue
            done_ids.add(evid)
            prefix = douyin_search.episode_prefix(ep.get("ep") or j,
                                                  len(eps))
            try:
                douyin_dl.run(f"https://www.douyin.com/video/{evid}",
                              sdir, name_prefix=prefix)
            except douyin_dl.ParseError as e:
                state["processed"][evid] = {"verdict": "skip",
                                            "desc": str(e)}
                douyin_auto.save_state(out_dir, state)
                continue
            except Exception as e:  # noqa: BLE001 - 单集失败不中断
                print(f"  {prefix}下载失败（重跑续传）: {e}")
                time.sleep(2)
                continue
            f = douyin_auto.find_by_id(sdir, evid)
            if not f:
                continue
            try:
                v = douyin_auto.judge_file(f, api_key, base_url, model,
                                           frames_n, sdir / ".wm_frames")
            except Exception as e:  # noqa: BLE001 - 识图失败保留重判
                print(f"  {prefix}识图失败（保留，重跑重判）: {e}")
                continue
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
        n_new += 1
        print(f"  ⚑ 本部完成")
    print(f"\n==== 结束 ====")
    print(f"新处理 {n_new} 部 / 已完整跳过 {stat['skip_done']} 部"
          f"｜分集: 干净 {stat['clean']} / 水印移走 {stat['wm']}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="精选搜索：只下带合集标记的短剧（整部/每剧一目录/从第1集）")
    parser.add_argument("keyword", nargs="?", help="关键词，中英文逗号分隔")
    parser.add_argument("--limit", type=int, default=10,
                        help="下载剧的部数（默认 10，按部计数）")
    parser.add_argument("--max-followers", type=int, default=None)
    parser.add_argument("--max-duration", type=int, default=None)
    parser.add_argument("--max-likes", type=int, default=None)
    parser.add_argument("--block-keywords", default=None,
                        help="作者黑名单关键词; 默认内置搬运/侵权词表, 空串禁用")
    parser.add_argument("--frames", type=int, default=6)
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
    keywords = douyin_search.split_keywords(args.keyword)
    out_dir = douyin_search.make_dated_dir(douyin_search.DOWNLOADS_DIR)
    print(f"输出目录: {out_dir}")
    filters = (args.max_followers, args.max_duration, args.max_likes)
    if any(v is not None for v in filters):
        print(f"筛选: 粉丝<{args.max_followers or '∞'} "
              f"时长<{args.max_duration or '∞'}s 赞<{args.max_likes or '∞'}")
    if args.block_keywords is None:
        block_kw = douyin_search.DEFAULT_BLOCK_KEYWORDS
    else:
        block_kw = douyin_search.split_keywords(args.block_keywords)
    try:
        run(keywords, args.limit, filters, block_kw, args.frames, api_key,
            args.base_url, args.model, out_dir)
    except KeyboardInterrupt:
        print("\n中断（进度已保存，重跑同命令自动续）")
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
