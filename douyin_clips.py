#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""douyin_clips.py — 散片下载器（非剧集短视频，独立于合集流程）

用户需求(2026-09-03)：
① 搜索入口用 /root/search/{关键词}?type=general（用户指定）；
   流式处理(2026-09-04)：发现一条合格候选立即下载+验水印，做完再翻页
② 输出 downloads/日期/散片/HH点MM分/ —— 与"剧集"同级；按下载时刻
   10 分钟分桶（每小时 6 个桶）
③ 每条验水印，只抽 3 帧（比合集的 6 帧省一半）
④ 不改动合集相关脚本（本文件独立实现收集逻辑）
⑤ 并行隔离(2026-09-04)：独立浏览器 profile(.browser-profile-clips)，
   带合集标记的候选跳过（让给 jx），可与合集模式并行互不干扰
⑥ 滚动定稿(2026-09-04)：时间窗已过的 10 分钟桶立即统计条数 HH点MM分(N)，
   不等整个任务结束
⑦ 跨午夜翻目录(2026-09-04)：通宵跑时过零点自动切到 downloads/新日期/，
   旧日期目录先收尾定稿
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

# 独立浏览器 profile：与合集(jx)共用 .browser-profile 会抢 Chromium 的
# profile 锁，并行跑时后开的一方起不来；散片用自己的目录
# （首次运行要在这个窗口单独扫一次码登录）
CLIPS_PROFILE_DIR = ds.SCRIPT_DIR / ".browser-profile-clips"


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


def test_bucket_dir_claim_and_finalize():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 已计数的旧桶 → 认领（不新建）
        old = root / "散片" / "10点10分(3)"
        old.mkdir(parents=True)
        assert bucket_dir(root, base="10点10分") == old
        # 无旧桶 → 新建裸桶
        got2 = bucket_dir(root, base="11点20分")
        assert got2.name == "11点20分" and got2.is_dir()
        # 定稿: 主目录 2 mp4 → (2); 疑似水印不计; 已正确的保持
        for i in range(2):
            (got2 / f"t{i}_730000000000000000{i}.mp4").write_bytes(b"x")
        q = got2 / "疑似水印"
        q.mkdir()
        (q / "w_7300000000000000009.mp4").write_bytes(b"x")
        for i in range(3):
            (old / f"d{i}_730000000000000001{i}.mp4").write_bytes(b"x")
        finalize_buckets(root)
        assert (root / "散片" / "11点20分(2)").is_dir()
        assert (root / "散片" / "10点10分(3)").is_dir()
        assert not (root / "散片" / "11点20分").exists()


def test_finalize_buckets_only_before_rolling():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        past = root / "散片" / "10点10分"      # 时间窗已过
        past.mkdir(parents=True)
        (past / "a_7300000000000000001.mp4").write_bytes(b"x")
        cur = root / "散片" / "11点20分"       # 当前桶（还可能写入）
        cur.mkdir(parents=True)
        (cur / "b_7300000000000000002.mp4").write_bytes(b"x")
        # 滚动定稿: 只收 10 点桶，当前 11 点桶不动
        finalize_buckets(root, only_before="11点20分")
        assert (root / "散片" / "10点10分(1)").is_dir()
        assert (root / "散片" / "11点20分").is_dir()
        assert not (root / "散片" / "11点20分(1)").exists()
        # 收尾（only_before=None）: 全部定稿
        finalize_buckets(root)
        assert (root / "散片" / "11点20分(1)").is_dir()


def test_roll_date_dir_midnight_switch():
    import tempfile
    import unittest.mock as mock
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old_day = root / "2026-09-04"
        bucket = old_day / "散片" / "23点50分"
        bucket.mkdir(parents=True)
        (bucket / "a_7300000000000000001.mp4").write_bytes(b"x")
        cur = {"dir": old_day}
        # 时间来到 09-05: 旧目录定稿 + 切到新日期目录
        with mock.patch.object(time, "strftime", return_value="2026-09-05"), \
             mock.patch.object(ds, "DOWNLOADS_DIR", root), \
             mock.patch.object(ds, "make_dated_dir",
                               side_effect=lambda r: r / "2026-09-05"):
            got = ds.roll_date_dir(cur, on_switch=finalize_buckets)
        assert got == root / "2026-09-05"
        assert cur["dir"] == got, "cur 引用必须同步更新"
        assert (old_day / "散片" / "23点50分(1)").is_dir(), "旧日期要收尾定稿"
    # 同一天不切换、不新建
    with tempfile.TemporaryDirectory() as td:
        same = Path(td) / "2026-09-05"
        same.mkdir()
        cur2 = {"dir": same}
        with mock.patch.object(time, "strftime", return_value="2026-09-05"):
            assert ds.roll_date_dir(cur2) is same




BUCKET_RE = re.compile(r"^(\d{2}点\d{2}分)(?:\((\d+)\))?$")


def bucket_dir(out_dir: Path, base: str = None) -> Path:
    """当前 10 分钟桶目录；优先认领已改名的 桶名(N) 既有目录。"""
    base = base or hour_bucket_name()
    root = out_dir / "散片"
    root.mkdir(parents=True, exist_ok=True)
    for d in sorted(root.iterdir()):
        m = BUCKET_RE.match(d.name) if d.is_dir() else None
        if m and m.group(1) == base:
            return d
    d = root / base
    d.mkdir(parents=True, exist_ok=True)
    return d


def finalize_buckets(out_dir: Path, only_before: str = None) -> None:
    """定稿：桶目录名追加合规条数 HH点MM分(N)。

    N = 主目录 mp4 数（干净合规的；疑似水印子目录不计）。
    only_before: 只定稿桶名早于它的历史桶——时间窗已过就立即统计，
    不等任务结束（滚动定稿）；None = 全部定稿（任务收尾用）。
    """
    root = out_dir / "散片"
    if not root.is_dir():
        return
    for d in sorted(root.iterdir()):
        m = BUCKET_RE.match(d.name) if d.is_dir() else None
        if not m:
            continue
        if only_before and m.group(1) >= only_before:
            continue  # 当前桶（还可能继续写入）不动
        n = len(list(d.glob("*.mp4")))
        target = f"{m.group(1)}({n})"
        if d.name == target:
            continue
        t = d.with_name(target)
        if t.exists():
            print(f"  !! 桶目录冲突跳过: {d.name} -> {target}", flush=True)
            continue
        try:
            d.rename(t)
            print(f"  ↳ 桶定稿: {target}", flush=True)
        except Exception:
            pass


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

def collect_clips_stream(keywords, filters, block_keywords, done_ids,
                         process, min_duration=None, out_dirs: dict = None):
    """流式收集+下载: 搜索响应每发现一条合格候选立即交给 process 下载
    判定（下载走独立 HTTP，浏览器停在搜索页不动），做完再继续滚动翻页。

    process(it) -> True 表示已达目标（clean 满 limit），立刻停止收集。
    out_dirs: {"dir": Path} 可变引用（跨午夜翻日期用），给定时启用滚动
    定稿：时间窗已过的桶每处理一条就立即统计条数。返回处理条数。
    """
    max_followers, max_duration, max_likes = filters
    handled = 0
    with ds.open_browser(profile_dir=CLIPS_PROFILE_DIR) as ctx:
        page = ds._first_page(ctx)
        ds.ensure_login(ctx, page)
        stop = {"flag": False}

        for idx, kw in enumerate(keywords, 1):
            if stop["flag"]:
                break
            print(f"\n=== 关键词 [{idx}/{len(keywords)}] {kw} ===",
                  flush=True)
            seen_local, raw, scanned, mix_skipped = set(), 0, 0, 0
            state = {"verify": False, "prompted": False}
            pending = []

            def on_response(resp):
                nonlocal raw, scanned, mix_skipped
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
                    if it.get("mix_id"):
                        # 带合集标记 → 合集(jx)的地盘：跳过，并行跑时
                        # 两边不会重复下载同一视频（散片只要独立单条）
                        mix_skipped += 1
                        continue
                    ok, reason = ds.passes_filter(
                        it, max_followers, max_duration, max_likes,
                        min_duration)
                    if ok:
                        bad, word = ds.author_blocked(it, block_keywords)
                        if bad:
                            continue
                        pending.append(it)
                    else:
                        print(f"  跳过: "
                              f"{(it['title'] or it['aweme_id'])[:24]}"
                              f"（{reason}）", flush=True)

            def drain():
                """新发现候选逐条下载判定（发现即下，做完再继续搜）。"""
                nonlocal handled
                while pending and not stop["flag"]:
                    it = pending.pop(0)
                    done_ids.add(it["aweme_id"])
                    handled += 1
                    if process(it):
                        stop["flag"] = True
                    # 滚动定稿：时间窗已过的桶立即统计条数，不等任务结束
                    # （读 cur 引用——process 里可能已跨午夜翻过目录）
                    if out_dirs is not None:
                        finalize_buckets(out_dirs["dir"],
                                         only_before=hour_bucket_name())

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
                drain()  # 首屏合格候选立即开始下载
                # 翻页推进以 scanned(含已下载)计——旧数据页不算"无进展",
                # 否则二轮搜索翻不过前几页已下载内容
                idle = 0
                while idle < ds.MAX_IDLE_SCROLLS and not stop["flag"]:
                    before = scanned
                    page.mouse.wheel(0, 2000)
                    page.wait_for_timeout(int(ds.SCROLL_WAIT * 1000))
                    drain()  # 本屏新候选立即下载，做完再滚
                    idle = 0 if scanned > before else idle + 1
            finally:
                page.remove_listener("response", on_response)
            print(f"  (「{kw}」扫描 {scanned} 条，已下载跳过 "
                  f"{scanned - raw} 条，合集让给jx {mix_skipped} 条，"
                  f"散片新候选 {raw - mix_skipped} 条)", flush=True)
            if idx < len(keywords) and not stop["flag"]:
                time.sleep(random.uniform(3, 5))  # 词间降温
    return handled


# ---------- 编排 ----------

def run(keywords, limit, filters, block_keywords, api_key, base_url,
        model, out_dir: Path, min_duration=None):
    state = load_state(out_dir)
    reconcile_state(out_dir, state)
    done_ids = (set(state["processed"])
                | ds.existing_ids_under(ds.DOWNLOADS_DIR))
    print(f"起点: 已处理 {len(done_ids)} 条")
    stat = {"clean": 0, "wm": 0}

    # --limit 语义: 本轮新增 N 条干净（与 jx 一致）；去重靠 done_ids，
    # 已下载的天然跳过，不从历史 state 累计（否则换关键词也凑数直接退出）
    clean = 0
    # 可变目录引用: 跨午夜自动翻日期（通宵跑），下载/状态/分桶全跟着切
    cur = {"dir": out_dir}

    def process(it) -> bool:
        """下载+判定单条散片（发现即下）。返回 True = 已达 limit 应停搜。"""
        nonlocal clean
        vid, title = it["aweme_id"], it["title"]
        if vid in state["processed"]:
            return clean >= limit
        print(f"\n→ [{clean + 1}/{limit}] {title[:32] or vid}", flush=True)
        # 10 分钟分桶：与"剧集"同级 → 日期/散片/HH点MM分/（跨午夜先翻日）
        cdir = bucket_dir(ds.roll_date_dir(cur, on_switch=finalize_buckets))
        q = cdir / "疑似水印"
        try:
            douyin_dl.run(f"https://www.douyin.com/video/{vid}", cdir)
        except douyin_dl.ParseError as e:
            state["processed"][vid] = {"verdict": "skip", "desc": str(e)}
            save_state(cur["dir"], state)
        except Exception as e:  # noqa: BLE001 - 失败可重试
            print(f"  下载失败（重跑续传）: {e}")
            time.sleep(2)
            return clean >= limit
        f = douyin_auto.find_by_id(cdir, vid)
        if not f:
            return clean >= limit
        try:
            v = douyin_auto.judge_file(f, api_key, base_url, model,
                                       FRAMES, cdir / ".wm_frames")
        except Exception as e:  # noqa: BLE001 - 识图失败保留重判
            print(f"  识图失败（保留，重跑重判）: {e}")
            return clean >= limit
        if v.get("has_author_watermark"):
            q.mkdir(exist_ok=True)
            shutil.move(str(f), str(wf.unique_dest(q / f.name)))
            state["processed"][vid] = {
                "verdict": "watermarked",
                "desc": v.get("desc", "")[:60]}
            print(f"  ⚠ 有作者水印 → 移走", flush=True)
            stat["wm"] += 1
        else:
            state["processed"][vid] = {"verdict": "clean"}
            clean += 1
            print(f"  ✓ 干净，计入 [{clean}/{limit}]", flush=True)
            stat["clean"] += 1
        save_state(cur["dir"], state)
        time.sleep(random.uniform(1, 2))
        return clean >= limit

    try:
        # 流式: 搜索发现一条 → 立刻下载判定 → 做完再继续滚动翻页
        handled = collect_clips_stream(keywords, filters, block_keywords,
                                       done_ids, process,
                                       min_duration=min_duration,
                                       out_dirs=cur)
        if not handled:
            print("!! 没有新候选（关键词翻尽或全被筛选/去重排除）")
    finally:
        # 无论正常结束/中断(Ctrl+C)/报错，都给桶目录定稿条数
        finalize_buckets(cur["dir"])
    print(f"\n==== 结束 ====")
    print(f"本轮干净散片 {clean}/{limit}｜水印移走 {stat['wm']}")
    print(f"散片目录: {cur['dir'] / '散片'}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="散片下载: root搜索→流式发现即下→10分钟分桶→3帧验水印")
    parser.add_argument("keyword", nargs="?", help="关键词，中英文逗号分隔")
    parser.add_argument("--limit", type=int, default=10,
                        help="目标干净散片数（默认 10，水印的不计数）")
    parser.add_argument("--max-followers", type=int, default=None)
    parser.add_argument("--max-duration", type=int, default=None)
    parser.add_argument("--max-likes", type=int, default=None)
    parser.add_argument("--min-duration", type=int, default=30,
                        help="时长下限秒(滤过短碎片, 严格大于; 0=关闭, 默认30)")
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
            args.base_url, args.model, out_dir,
            min_duration=args.min_duration or None)
    except KeyboardInterrupt:
        print("\n中断（进度已保存，重跑同命令自动续）")
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
