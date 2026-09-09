#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.watermark — AI 检测视频中的作者自加水印并分流（平台无关）。

原 watermark_filter.py 整体迁入，并收编 douyin_auto.judge_file
（合集/散片两模式共用的"抽帧→判定→清理"封装）。
判定非 100% 准确：仅转移不删除，建议先 --dry-run 预览。
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

from .reporting import log
from .paths import find_tool
from . import selftest as _st

DEFAULT_MODEL = "MiniMax-M3"
DEFAULT_BASE_URL = "https://api.minimaxi.com/anthropic"
FRAME_WIDTH = 960   # 640 时顶部半透明小字水印(如 @作者名 新剧xxx)易看不清导致漏检
RETRIES = 2

# 抽帧子进程不弹终端窗口(Windows); 其他平台无此常量传 0
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


class FilterError(Exception):
    """致命配置/环境错误。"""


# ---------- 纯逻辑 ----------

def pick_timestamps(duration_s: float, n: int):
    """在 [3%, 97%] 时长内均匀取 n 个时间点（秒）。"""
    if duration_s <= 0 or n <= 0:
        return []
    lo, hi = duration_s * 0.03, duration_s * 0.97
    if n == 1:
        return [round((lo + hi) / 2, 2)]
    step = (hi - lo) / (n - 1)
    return [round(lo + i * step, 2) for i in range(n)]


def parse_verdict(text: str) -> dict:
    """防御式解析模型输出：剥 markdown 围栏、截取首个 {...}、校验关键字段。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                  flags=re.IGNORECASE)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"输出中无 JSON: {text[:80]}")
    data = json.loads(m.group(0))
    if not isinstance(data.get("has_author_watermark"), bool):
        raise ValueError("缺少 has_author_watermark 布尔字段")
    return data


def unique_dest(path: Path) -> Path:
    """目标已存在时追加 -1/-2… 后缀，绝不覆盖。"""
    if not path.exists():
        return path
    for i in range(1, 1000):
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
    raise FilterError(f"无法生成不重复文件名: {path}")


# ---------- 抽帧 ----------

def probe_duration(video: Path) -> float:
    out = subprocess.run(
        [find_tool("ffprobe"), "-v", "error", "-show_entries",
         "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW,
        encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise FilterError(f"ffprobe 失败: {out.stderr.strip()[:100]}")
    return float(out.stdout.strip())


def extract_frames(video: Path, n: int, workdir: Path):
    """均匀抽 n 帧缩放 jpg，返回成功生成的文件路径（按时间序）。"""
    workdir.mkdir(parents=True, exist_ok=True)
    ts = pick_timestamps(probe_duration(video), n)
    frames = []
    for i, t in enumerate(ts):
        out = workdir / f"{video.stem[:60]}_f{i}.jpg"
        r = subprocess.run(
            [find_tool("ffmpeg"), "-y", "-loglevel", "error",
             "-strict", "unofficial",
             "-ss", str(t),
             "-i", str(video), "-frames:v", "1",
             "-vf", f"scale={FRAME_WIDTH}:-2", str(out)],
            capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW,
            encoding="utf-8", errors="replace")
        # -strict unofficial: 部分视频为 Non full-range YUV, 严格模式转
        # mjpeg 直接报错 → 6 帧全失败 → 识图流程整体跳过(漏检)
        if r.returncode == 0 and out.exists():
            frames.append(out)
    return frames


# ---------- VLM 调用 ----------

PROMPT = """你是视频水印审核员。下面是同一个视频按时间顺序抽取的{N}帧截图（第1张最早）。
任务：判断视频中是否存在【当前发布作者自己添加的账号标识/引流元素】。

只有以下情况算水印（has_author_watermark = true）：
- 当前作者的账号名/昵称/抖音号/微信号/公众号名，以角标、水印文字、
  半透明 logo 形式叠加在画面上（位置固定或移动都算）
- 当前作者添加的引流文字/贴纸："感谢关注""求关注""关注看下集"等

以下一律不算水印（has_author_watermark = false）：
- 抖音/快手/西瓜等任何平台的角标、logo、@用户名角标——平台自动添加的
- 影视剧/综艺/素材自带的台标、字幕组标识、原视频遗留水印（搬运内容里
  看到的别人水印不是当前作者添加的）
- 剧名 logo、片头片尾标题花字、演员表、剧集自制标识
- "AI生成"等 AI 内容标注
- 进度条等播放器 UI、底部居中硬字幕、画面场景内自然文字（招牌/手机屏幕）

判定原则：
- 核心区分：标识是否【指向当前发布作者本人/其账号】。指向别处的
  （平台、原片方、他人）都不算
- 重点检查画面顶部/四角的【半透明文字水印】——短剧最常见的形态就是
  顶部一行"@作者名 新剧「剧名」"样式的浅色小字，对比度低但持续存在，
  这类必须判 true
- 水印可能间歇出现（淡入淡出/移动）或每帧位置不同，只要任意一帧出现
  即算；位置移动不影响判定
- 与发布作者昵称相关的水印（一致/包含其主体词）不属于"拿不准"，
  必须判 true；拿不准仅适用于与发布者无关的场合

{AUTHOR}
严格只输出 JSON（不要任何多余文字）：
{{"has_author_watermark": true或false, "type": "account或空", "moving": true或false, "desc": "简述依据", "frames_with_watermark": [帧序号,从1开始]}}"""


def _author_block(author: str) -> str:
    """作者信息块: 传入发布者昵称时给 VLM 提供比对基准(漏检的关键)。"""
    if not author:
        return "【发布作者昵称】未提供——仅当水印明显是账号名样式且无相反证据时判 true。\n"
    return (f"【发布作者昵称】「{author}」\n"
            f"若画面水印文字与该昵称相关（完全一致/包含其主体词/同一主体"
            f"如简称），即为当前作者自己的账号标识，必须判 true。\n")


def _anthropic_messages_url(base_url: str) -> str:
    """Anthropic 端点归一：/anthropic 或 .../v1/messages → 完整 messages URL。"""
    url = base_url.rstrip("/")
    if url.endswith("/v1/messages"):
        return url
    if url.endswith("/v1"):
        return url + "/messages"
    return url + "/v1/messages"


def _anthropic_text(data: dict) -> str:
    """从 Anthropic Messages 响应的 content blocks 拼出文本。"""
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if isinstance(b, dict))


def ask_vlm(frames, api_key: str, base_url: str, model: str,
            author: str = "") -> dict:
    """多帧一次调用，返回判定 dict；解析失败/HTTP 失败重试。

    author: 发布作者昵称——传给 VLM 作水印归属比对基准（缺失时
    VLM 无法确认水印归属，是漏检的主因）。
    自动分协议：base_url 含 /anthropic → Anthropic Messages 格式
    （MiniMax M3 等走此协议）；否则 OpenAI chat/completions 格式。
    """
    author_block = _author_block(author)
    is_anthropic = "/anthropic" in base_url
    if is_anthropic:
        content = []
        for f in frames:
            b64 = base64.b64encode(f.read_bytes()).decode()
            content.append({"type": "image",
                            "source": {"type": "base64",
                                       "media_type": "image/jpeg",
                                       "data": b64}})
        content.append({"type": "text",
                        "text": PROMPT.format(N=len(frames),
                                              AUTHOR=author_block)})
        body = {"model": model, "max_tokens": 1024,
                "messages": [{"role": "user", "content": content}]}
        url = _anthropic_messages_url(base_url)
        headers = {"x-api-key": api_key,
                   "Authorization": f"Bearer {api_key}",
                   "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}

        def extract(r):
            return _anthropic_text(r.json())
    else:
        content = []
        for f in frames:
            b64 = base64.b64encode(f.read_bytes()).decode()
            content.append({"type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}"}})
        content.append({"type": "text",
                        "text": PROMPT.format(N=len(frames),
                                              AUTHOR=author_block)})
        body = {"model": model,
                "messages": [{"role": "user", "content": content}]}
        url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"}

        def extract(r):
            return r.json()["choices"][0]["message"]["content"]

    last_exc = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=120)
            r.raise_for_status()
            return parse_verdict(extract(r))
        except Exception as e:  # noqa: BLE001 - 统一重试
            last_exc = e
            if attempt < RETRIES:
                time.sleep(3)
    raise last_exc


# ---------- 判定封装（原 douyin_auto.judge_file，两模式共用） ----------

def judge_file(video: Path, api_key, base_url, model, frames_n,
               workdir: Path, author: str = "") -> dict:
    """抽帧 → VLM 判定单条视频水印；判定完即清理抽帧 jpg。

    author: 发布作者昵称（水印归属比对基准，见 ask_vlm）。
    通宵跑几千条会无限累积占盘，故判定与清理绑定在 finally。
    """
    frames = extract_frames(video, frames_n, workdir)
    if not frames:
        raise FilterError("抽帧失败（视频损坏？）")
    try:
        return ask_vlm(frames, api_key, base_url, model, author=author)
    finally:
        for f in frames:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


# ---------- 编排 ----------

def run(video_dir: Path, n_frames: int, api_key: str, base_url: str,
        model: str, dry_run: bool, rejudge: bool = False):
    videos = sorted(video_dir.glob("*.mp4"))
    quarantine = video_dir / "疑似水印"
    if rejudge and quarantine.is_dir():
        # 平反模式：把疑似水印目录的文件移回主目录后按当前标准重判
        rescued = 0
        for f in list(quarantine.glob("*.mp4")):
            shutil.move(str(f), str(unique_dest(video_dir / f.name)))
            rescued += 1
        log(f"[rejudge] 已移回 {rescued} 个待重判文件")
        videos = sorted(video_dir.glob("*.mp4"))
    if not videos:
        raise FilterError(f"目录中没有 mp4: {video_dir}")
    workdir = video_dir / ".wm_frames"
    workdir.mkdir(exist_ok=True)
    report = []
    for idx, v in enumerate(videos, 1):
        log(f"\n[{idx}/{len(videos)}] {v.name[:40]}")
        entry = {"file": v.name, "status": "error", "reason": ""}
        try:
            frames = extract_frames(v, n_frames, workdir)
            if not frames:
                entry["reason"] = "抽帧失败"
                log("  错误: 抽帧失败")
                report.append(entry)
                continue
            # 文件名尾部带 来源@作者 → 作水印归属比对基准
            m = re.search(r"_来源@(.+?)_\d{15,}", v.name)
            author = m.group(1) if m else ""
            verdict = ask_vlm(frames, api_key, base_url, model, author=author)
            entry.update(verdict)
            if verdict.get("has_author_watermark"):
                if dry_run:
                    entry["status"] = "flagged(dry-run)"
                    log(f"  ⚠ 有作者水印（dry-run 不转移）: "
                          f"{verdict.get('desc', '')[:50]}")
                else:
                    quarantine.mkdir(exist_ok=True)
                    dest = unique_dest(quarantine / v.name)
                    shutil.move(str(v), str(dest))
                    entry["status"] = "moved"
                    entry["moved_to"] = dest.name
                    log(f"  ⚠ 有作者水印 → 已转移: "
                          f"{verdict.get('desc', '')[:50]}")
            else:
                entry["status"] = "clean"
                log("  ✓ 无作者水印")
        except Exception as e:  # noqa: BLE001 - 单条失败不中断
            entry["reason"] = f"{type(e).__name__}: {e}"
            log(f"  错误: {entry['reason']}")
        report.append(entry)
    (video_dir / "watermark_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    n_clean = sum(1 for e in report if e["status"] == "clean")
    n_moved = sum(1 for e in report
                  if e["status"] in ("moved", "flagged(dry-run)"))
    n_err = sum(1 for e in report if e["status"] == "error")
    log(f"\n汇总: 无水印 {n_clean} / 有水印 {n_moved} / 错误 {n_err}"
          f"（详情: {video_dir / 'watermark_report.json'}）")


# ---------- selftest ----------

def run_selftests():
    return _st.run_selftests(globals())


# ---------- tests: 纯逻辑 ----------

def test_pick_timestamps():
    assert pick_timestamps(100, 4) == [3.0, 34.33, 65.67, 97.0]
    assert pick_timestamps(0, 4) == []
    assert pick_timestamps(50, 1) == [25.0]


def test_parse_verdict_plain_and_fenced():
    v = parse_verdict('{"has_author_watermark": true, "moving": true}')
    assert v["has_author_watermark"] is True
    v = parse_verdict('```json\n{"has_author_watermark": false, '
                      '"moving": false}\n```')
    assert v["moving"] is False


def test_parse_verdict_wrapped_and_invalid():
    v = parse_verdict('结论：{"has_author_watermark": true, "moving": false,'
                      ' "desc": "角落有账号ID"} 以上')
    assert v["desc"] == "角落有账号ID"
    for bad in ("我觉得没有水印", '{"desc": "x"}'):
        try:
            parse_verdict(bad)
            assert False, f"应拒绝: {bad}"
        except ValueError:
            pass


def test_anthropic_url_and_text():
    assert _anthropic_messages_url(
        "https://api.minimaxi.com/anthropic") == \
        "https://api.minimaxi.com/anthropic/v1/messages"
    assert _anthropic_messages_url(
        "https://api.minimaxi.com/anthropic/v1") == \
        "https://api.minimaxi.com/anthropic/v1/messages"
    assert _anthropic_messages_url(
        "https://api.minimaxi.com/anthropic/v1/messages/") == \
        "https://api.minimaxi.com/anthropic/v1/messages"
    assert _anthropic_text({"content": [
        {"type": "thinking", "thinking": "…"},
        {"type": "text", "text": "结果"},
    ]}) == "结果"


def test_unique_dest():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.mp4"
        p.write_bytes(b"x")
        assert unique_dest(p) == p.with_name("a-1.mp4")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="AI 检测作者水印并分流（仅转移不删除，先 --dry-run 预览）")
    parser.add_argument("video_dir", nargs="?", help="视频目录")
    parser.add_argument("--frames", type=int, default=6,
                        help="每视频抽帧数（默认 6）")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"视觉模型（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="百炼 OpenAI 兼容端点")
    parser.add_argument("--dry-run", action="store_true",
                        help="完整检测与报告，但不移动文件")
    parser.add_argument("--rejudge", action="store_true",
                        help="先把 疑似水印\\ 里的文件移回主目录再重判"
                             "（换提示词/标准后平反用）")
    parser.add_argument("--selftest", action="store_true",
                        help="运行内置自测（不联网）")
    args = parser.parse_args(argv)
    if args.selftest:
        sys.exit(0 if run_selftests() else 1)
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not args.video_dir:
        parser.error("请提供视频目录")
    if not api_key:
        log("错误: 未设置环境变量 DASHSCOPE_API_KEY（阿里云百炼 API Key）",
              file=sys.stderr)
        sys.exit(1)
    try:
        run(Path(args.video_dir), args.frames, api_key, args.base_url,
            args.model, args.dry_run, args.rejudge)
    except FilterError as e:
        log(f"错误: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
