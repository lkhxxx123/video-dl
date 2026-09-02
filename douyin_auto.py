#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""douyin_auto.py — 一条龙流水线：搜索筛选下载 → AI 验水印 → 凑够 N 条干净视频

多次执行自动跳过已处理视频：状态文件(auto_state.json) + 文件名尾部视频ID 双重去重。
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

import douyin_dl
import douyin_search
import series_detect
import watermark_filter as wf

ID_IN_NAME_RE = re.compile(r"_(\d{15,})\.mp4$")


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


# ---------- tests: 纯逻辑 ----------

def test_ids_from_filenames():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "标题A_7300000000000000001.mp4").write_bytes(b"x")
        (d / "B_7300000000000000002.mp4").write_bytes(b"x")
        (d / "无ID文件.mp4").write_bytes(b"x")
        (d / "其他.txt").write_bytes(b"x")
        sub = d / "疑似水印"
        sub.mkdir()
        (sub / "C_7300000000000000003.mp4").write_bytes(b"x")
        ids = ids_from_filenames(d, sub)
        assert ids == {"7300000000000000001", "7300000000000000002",
                       "7300000000000000003"}, ids


def test_state_roundtrip_and_find_by_id():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        state = load_state(d)
        assert state == {"processed": {}}
        state["processed"]["123"] = {"verdict": "clean"}
        save_state(d, state)
        assert load_state(d) == state
        f = d / "某标题_1234567890123456789.mp4"
        f.write_bytes(b"x")
        assert find_by_id(d, "1234567890123456789") == f
        assert find_by_id(d, "9999999999999999999") is None


# ---------- 纯逻辑：去重与状态 ----------

def ids_from_filenames(*dirs) -> set:
    """从各目录 mp4 文件名尾部的 _{id}.mp4 提取视频 ID（断状态丢失后的兜底）。"""
    ids = set()
    for d in dirs:
        if d and Path(d).is_dir():
            for f in Path(d).glob("*.mp4"):
                m = ID_IN_NAME_RE.search(f.name)
                if m:
                    ids.add(m.group(1))
    return ids


def load_state(out_dir: Path) -> dict:
    p = out_dir / "auto_state.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data.get("processed"), dict):
                return data
        except Exception:
            pass
    return {"processed": {}}


def save_state(out_dir: Path, state: dict) -> None:
    (out_dir / "auto_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def reconcile_state(out_dir: Path, state: dict) -> int:
    """状态自愈：clean/watermarked 记录对应的文件已不存在 → 删除记录。

    让"删目录=可重跑"成立（否则删了文件状态仍拦着不下）。
    skip（图集等语义跳过）不依赖文件存在，保留。返回清理条数。
    """
    alive = douyin_search.existing_ids_under(douyin_search.DOWNLOADS_DIR)
    drop = [vid for vid, v in state["processed"].items()
            if v.get("verdict") in ("clean", "watermarked")
            and vid not in alive]
    for vid in drop:
        del state["processed"][vid]
    if drop:
        save_state(out_dir, state)
        print(f"[状态清理] {len(drop)} 条记录的文件已不存在，已重置（可重新下载）")
    return len(drop)


def find_by_id(out_dir: Path, aweme_id: str):
    """按文件名尾部 ID 在目录中定位 mp4。"""
    for f in out_dir.glob(f"*_{aweme_id}.mp4"):
        return f
    return None


# ---------- 流水线 ----------

def judge_file(video: Path, api_key, base_url, model, frames_n, workdir) -> dict:
    frames = wf.extract_frames(video, frames_n, workdir)
    if not frames:
        raise wf.FilterError("抽帧失败（视频损坏？）")
    return wf.ask_vlm(frames, api_key, base_url, model)


def series_via_user_page(video_id, target_title, sec_uid, out_dir: Path,
                         api_key, base_url, model, max_episodes=100):
    """主页兜底：作品列表+主页截图 → AI 判定与目标视频同系列的分集。

    返回 (eps, name)；eps 为 [{aweme_id,title}]（发布时间升序），
    未命中/失败返回 ([], "")。
    """
    if not sec_uid:
        print("  !! 无作者 sec_uid，主页兜底不可用")
        return [], ""
    print("  → 改走作者主页AI识别…", flush=True)
    try:
        shot = out_dir / f"_userpage_{sec_uid[:8]}.png"
        posts = douyin_search.collect_user_posts(sec_uid, screenshot_to=shot)
    except douyin_search.SearchError as e:
        print(f"  !! 主页作品拉取失败: {e}")
        return [], ""
    try:
        v = series_detect.judge_series(video_id, target_title, posts, shot,
                                       api_key, base_url, model)
    except Exception as e:  # noqa: BLE001
        print(f"  !! AI判定失败: {e}")
        return [], ""
    if not v["is_series"]:
        print(f"  ✗ AI判定非同一系列: {v['reason'][:40]}")
        return [], ""
    if len(v["ids"]) > max_episodes:
        print(f"  ⚠ 分集 {len(v['ids'])} 超上限 {max_episodes}，"
              f"按发布序截取前 {max_episodes}（--max-episodes 可调）")
    ids = v["ids"][:max_episodes]
    by = {p["aweme_id"]: p for p in posts}
    eps = [{"aweme_id": i, "title": by[i]["title"]} for i in ids]
    print(f"  ✓ AI识别系列「{v['name'] or '?'}」共 {len(eps)} 集: "
          f"{v['reason'][:36]}")
    return eps, v["name"]


def download_mix(video_id: str, judge: bool, api_key: str = "",
                 base_url: str = "", model: str = "", frames_n: int = 6,
                 max_episodes: int = 100):
    """手动剧集模式：给定剧集任意一集，下载全部集（可选验水印）。

    三级拉取：合集/系列面板接口 → 作者主页AI识别（需 Key）→ 失败报错。
    """
    fb = douyin_dl.resolve_via_browser(video_id)
    print(f"标题: {(fb.get('title') or '?')[:36]}")
    print(f"作者: {fb.get('author') or '?'}  "
          f"主页: https://www.douyin.com/user/{fb.get('sec_uid') or '(无)'}")
    out_dir = douyin_search.make_dated_dir(douyin_search.DOWNLOADS_DIR)
    print(f"输出目录: {out_dir}")
    eps, name = [], fb.get("mix_name") or ""
    try:
        eps = douyin_search.collect_mix(video_id,
                                        sec_uid=fb.get("sec_uid") or "",
                                        mix_id=fb.get("mix_id") or "")
        print(f"剧集面板接口共拿到 {len(eps)} 集")
    except douyin_search.SearchError as e:
        print(f"!! 剧集面板接口失败: {e}")
    if not eps and judge:
        eps, name = series_via_user_page(
            video_id, fb.get("title") or "", fb.get("sec_uid"), out_dir,
            api_key, base_url, model, max_episodes)
        name = name or fb.get("mix_name") or \
            (fb.get("author") or "作者") + "剧集"
    if not eps:
        raise RuntimeError("该视频不属于任何合集/系列，且主页AI识别未命中")
    if len(eps) < 2:
        print("↳ 只拿到 1 集（非完整剧集），按单条下载")
        douyin_dl.run(f"https://www.douyin.com/video/{video_id}", out_dir)
        return
    sdir = out_dir / "剧集" / douyin_search.safe_dir_name(name or "未命名剧集")
    print(f"剧集: {name or '?'} 共 {len(eps)} 集 → {sdir}")
    quarantine = sdir / "疑似水印"
    ok = failed = 0
    for j, ep in enumerate(eps, 1):
        evid = ep["aweme_id"]
        prefix = douyin_search.episode_prefix(ep.get("ep") or j, len(eps))
        print(f"\n第{ep.get('ep') or j}集: {ep['title'][:30]}")
        try:
            douyin_dl.run(f"https://www.douyin.com/video/{evid}", sdir,
                          name_prefix=prefix)
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  下载失败: {e}")
            failed += 1
            continue
        if not judge:
            continue
        f = find_by_id(sdir, evid)
        if not f:
            continue
        try:
            v = judge_file(f, api_key, base_url, model, frames_n,
                           sdir / ".wm_frames")
            if v.get("has_author_watermark"):
                quarantine.mkdir(exist_ok=True)
                shutil.move(str(f), str(wf.unique_dest(quarantine / f.name)))
                print("  ⚠ 有作者水印 → 移走")
            else:
                print("  ✓ 干净")
        except Exception as e:  # noqa: BLE001
            print(f"  识图失败（文件保留）: {e}")
        time.sleep(random.uniform(1, 2))
    print(f"\n汇总: 下载 {ok} / 失败 {failed}（目录: {sdir}）")


def run(keywords, target, filters, frames_n, api_key, base_url, model,
        out_dir: Path, block_keywords=None, max_episodes=100,
        user_series=True):
    quarantine = out_dir / "疑似水印"
    workdir = out_dir / ".wm_frames"
    state = load_state(out_dir)
    reconcile_state(out_dir, state)

    # 疑似水印目录里的历史文件 → 直接视为已判水印（含手动 run_wm 移过去的）
    for vid in ids_from_filenames(quarantine):
        state["processed"].setdefault(vid, {"verdict": "watermarked",
                                            "desc": "历史疑似水印目录"})

    # 纳管历史未判定文件（此前单独跑 douyin_search 下载的）
    for f in list(out_dir.glob("*.mp4")):
        m = ID_IN_NAME_RE.search(f.name)
        if not m or m.group(1) in state["processed"]:
            continue
        vid = m.group(1)
        print(f"[验旧] {f.name[:40]}")
        try:
            v = judge_file(f, api_key, base_url, model, frames_n, workdir)
            if v.get("has_author_watermark"):
                quarantine.mkdir(exist_ok=True)
                shutil.move(str(f), str(wf.unique_dest(quarantine / f.name)))
                state["processed"][vid] = {"verdict": "watermarked",
                                           "desc": v.get("desc", "")[:60]}
                print(f"  ⚠ 有作者水印 → 移走")
            else:
                state["processed"][vid] = {"verdict": "clean"}
                print("  ✓ 干净")
        except Exception as e:  # noqa: BLE001
            print(f"  判定失败（下轮重试）: {e}")
        save_state(out_dir, state)

    done_ids = (set(state["processed"])
                | ids_from_filenames(out_dir, quarantine)
                | douyin_search.existing_ids_under(
                    douyin_search.DOWNLOADS_DIR))  # 跨目录全局去重
    clean = sum(1 for v in state["processed"].values()
                if v.get("verdict") == "clean")
    print(f"\n起点: 干净 {clean}/{target}，历史已处理 {len(done_ids)} 条")

    def process(vid, title, dest_dir=None, name_prefix=""):
        """下载单条 + AI 验水印 + 分流/计数。返回 clean/watermarked/skip/error。

        dest_dir: 指定下载目录（剧集分集存分类子目录时使用），默认主目录。
        name_prefix: 剧集集数前缀（如 '07_'），保证文件夹内按名排序即观看顺序。
        """
        nonlocal clean
        dest_dir = dest_dir or out_dir
        d_quarantine = dest_dir / "疑似水印"
        try:
            douyin_dl.run(f"https://www.douyin.com/video/{vid}", dest_dir,
                          name_prefix=name_prefix)
        except douyin_dl.ParseError as e:
            print(f"  跳过（下载）: {e}")
            state["processed"][vid] = {"verdict": "skip", "desc": str(e)}
            save_state(out_dir, state)
            return "skip"
        except Exception as e:  # noqa: BLE001 - 下载失败可重试
            print(f"  下载失败（下轮重试）: {e}")
            time.sleep(2)
            return "error"
        f = find_by_id(dest_dir, vid)
        if not f:
            print("  !! 下载后未找到文件（文件名异常？），跳过")
            return "error"
        try:
            v = judge_file(f, api_key, base_url, model, frames_n,
                           dest_dir / ".wm_frames")
        except Exception as e:  # noqa: BLE001 - 识图失败保留文件重试
            print(f"  识图失败（文件保留，下轮重判）: {e}")
            return "error"
        if v.get("has_author_watermark"):
            d_quarantine.mkdir(exist_ok=True)
            shutil.move(str(f), str(wf.unique_dest(d_quarantine / f.name)))
            state["processed"][vid] = {"verdict": "watermarked",
                                       "desc": v.get("desc", "")[:60]}
            print(f"  ⚠ 有作者水印 → 移走: {v.get('desc', '')[:40]}")
            save_state(out_dir, state)
            return "watermarked"
        state["processed"][vid] = {"verdict": "clean"}
        clean += 1
        print("  ✓ 干净，计入")
        save_state(out_dir, state)
        return "clean"

    def download_series(eps, name):
        """整部剧集逐集下载+验水印，存入 剧集/<名称>/ 分类子目录。"""
        sdir = out_dir / "剧集" / douyin_search.safe_dir_name(
            name or "未命名剧集")
        print(f"  ↳ 存入分类目录: 剧集/{sdir.name}")
        for j, ep in enumerate(eps, 1):
            evid = ep["aweme_id"]
            done_ids.add(evid)
            if evid in state["processed"]:
                continue
            prefix = douyin_search.episode_prefix(ep.get("ep") or j,
                                                   len(eps))
            print(f"  -- 第{ep.get('ep') or j}集 [{clean + 1}/{target}] "
                  f"{ep['title'][:24]}", flush=True)
            process(evid, ep["title"], sdir, name_prefix=prefix)
        print(f"  ⚑ 剧集完成（本部共 {len(eps)} 集）")

    while clean < target:
        need = target - clean
        print(f"\n=== 搜索补充候选（还需 {need} 条干净）===")
        try:
            batch = douyin_search.collect_many(keywords, need, *filters,
                                               seen=done_ids,
                                               block_keywords=block_keywords)
        except douyin_search.SearchError as e:
            print(f"!! 搜索失败: {e}")
            break
        if not batch:
            print("!! 所有关键词均已翻尽，没有新候选")
            break
        progressed = False
        for it in batch:
            done_ids.add(it["aweme_id"])
            if clean >= target:
                break
            vid, title = it["aweme_id"], it["title"]
            if vid in state["processed"]:
                continue
            print(f"\n→ [{clean + 1}/{target}] {title[:30] or vid}")
            if it.get("sec_uid"):
                print(f"作者: {it.get('nick') or '?'}  "
                      f"主页: https://www.douyin.com/user/{it['sec_uid']}")
            if it.get("mix_id"):
                # 剧集：拉全部集（豁免筛选），每集计数；选定即整部拿全
                print(f"  ⚑ 剧集: {(it.get('mix_name') or '?')[:24]}"
                      f" → 拉取全部集", flush=True)
                try:
                    eps = douyin_search.collect_mix(
                        vid, sec_uid=it.get("sec_uid") or "",
                        mix_id=it.get("mix_id") or "")
                except douyin_search.SearchError as e:
                    print(f"  !! 拉全集失败: {e}")
                    eps = []
                if len(eps) >= 2:
                    print(f"  共 {len(eps)} 集，逐集下载+验水印")
                    download_series(eps, it.get("mix_name"))
                    progressed = True
                    continue
                if eps:
                    print("  ↳ 面板只拿到 1 集（非完整剧集），按单条下载")
                # 面板接口失败/单集 → 主页AI识别兜底
                if user_series:
                    eps, sname = series_via_user_page(
                        vid, title, it.get("sec_uid"), out_dir, api_key,
                        base_url, model, max_episodes)
                    if eps:
                        sname = sname or it.get("mix_name") or \
                            (it.get("nick") or "作者") + "剧集"
                        print(f"  共 {len(eps)} 集，逐集下载+验水印")
                        download_series(eps, sname)
                        progressed = True
                        continue
                # 兜底也没拿到 → 至少保住单条
                print("  ↳ 整部拉取失败，按单条下载本集")
                process(vid, title)
                progressed = True
                continue
            if (user_series and it.get("sec_uid")
                    and series_detect.episode_hint(title)):
                # 无合集字段但标题带集数标记 → 主页AI识别同系列
                print("  ⚑ 标题含集数标记 → 作者主页AI识别系列", flush=True)
                eps, sname = series_via_user_page(
                    vid, title, it.get("sec_uid"), out_dir, api_key,
                    base_url, model, max_episodes)
                if eps:
                    sname = sname or (it.get("nick") or "作者") + "剧集"
                    print(f"  共 {len(eps)} 集，逐集下载+验水印")
                    download_series(eps, sname)
                    progressed = True
                    continue
                print("  ↳ AI识别未命中，按单条下载")
            process(vid, title)
            progressed = True
        if not progressed:
            print("!! 本轮无进展，退出（重跑命令可再试）")
            break

    n_wm = sum(1 for v in state["processed"].values()
               if v.get("verdict") == "watermarked")
    # 汇总含剧集分类子目录；疑似水印隔离区不计入
    n_mp4 = sum(1 for f in out_dir.rglob("*.mp4")
                if "疑似水印" not in f.parts)
    print(f"\n==== 结束 ====")
    print(f"干净 {clean}/{target}（目录现有 mp4 {n_mp4} 个）")
    print(f"累计判定: 水印移走 {n_wm} 条 / 已处理 {len(state['processed'])} 条")
    print(f"水印文件在: 各目录下 疑似水印/（主目录: {quarantine}）")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="一条龙: 搜索筛选下载 → AI 验水印 → 凑够 N 条干净视频")
    parser.add_argument("keyword", nargs="?",
                        help="关键词，中英文逗号分隔多个")
    parser.add_argument("--limit", type=int, default=50,
                        help="目标干净视频数（默认 50）")
    parser.add_argument("--max-followers", type=int, default=None)
    parser.add_argument("--max-duration", type=int, default=None)
    parser.add_argument("--max-likes", type=int, default=None)
    parser.add_argument("--block-keywords", default=None,
                        help="作者黑名单关键词(逗号分隔, 匹配简介/昵称/标题);"
                             " 默认内置搬运/侵权类词表, 传空串禁用")
    parser.add_argument("--max-episodes", type=int, default=100,
                        help="单部剧集最多下载集数（安全上限，默认 100）")
    parser.add_argument("--no-user-series", action="store_true",
                        help="禁用作者主页AI识别剧集兜底（只用合集/系列接口）")
    parser.add_argument("--frames", type=int, default=6,
                        help="每视频抽帧数（默认 6）")
    parser.add_argument("--model", default=wf.DEFAULT_MODEL,
                        help=f"视觉模型（默认 {wf.DEFAULT_MODEL}）")
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
        print("错误: 未设置环境变量 DASHSCOPE_API_KEY（百炼 API Key）",
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
        run(keywords, args.limit, filters, args.frames, api_key,
            args.base_url, args.model, out_dir, block_keywords=block_kw,
            max_episodes=args.max_episodes,
            user_series=not args.no_user_series)
    except KeyboardInterrupt:
        print("\n中断（进度已保存，重跑同命令自动续）")
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
