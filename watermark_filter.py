#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""watermark_filter.py — AI 检测视频中的作者自加水印并分流
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

DEFAULT_MODEL = "MiniMax-M3"
DEFAULT_BASE_URL = "https://api.minimaxi.com/anthropic"
FRAME_WIDTH = 640
RETRIES = 2


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
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, timeout=30,
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
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t),
             "-i", str(video), "-frames:v", "1",
             "-vf", f"scale={FRAME_WIDTH}:-2", str(out)],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        if r.returncode == 0 and out.exists():
            frames.append(out)
    return frames


# ---------- Qwen-VL 调用 ----------

PROMPT = """你是视频水印审核员。下面是同一个视频按时间顺序抽取的{N}帧截图（第1张最早）。
找出视频中"叠加标识元素"并分类：
- account（账号/引流类）：作者账号名、抖音号、公众号、"感谢关注/求关注"类
  引流文字、作者半透明 logo。位置固定或移动都算。
- platform（平台标识类）：任何第三方平台的叠加标识——抖音（角标/logo/
  @抖音小助手/DOU+）、快手、微博（含@微博）、小红书（含小红书号）、
  B站/哔哩哔哩、视频号、TikTok、西瓜视频、今日头条等——【都算水印，需转移】。
- ai_label（AI标识类）："AI生成"、"内容由AI生成"、"豆包AI生成"等 AI 内容
  标注，属内容属性说明，【不算水印】。
- title（标题花字类）：作者叠加的标题、吐槽花字（如黄色描边文案），
  属内容装饰，【不算水印】。
以下同样不算水印：进度条等播放器 UI、底部居中硬字幕、画面场景内自然文字
（招牌/手机屏幕/片头标题动画）。
判定规则：存在 account 或 platform 类元素时 has_author_watermark 为 true。
严格只输出 JSON（不要任何多余文字）：
{{"has_author_watermark": true或false, "type": "account或platform或ai_label或title或空", "moving": true或false, "desc": "简述依据", "frames_with_watermark": [帧序号,从1开始]}}"""


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


def ask_vlm(frames, api_key: str, base_url: str, model: str) -> dict:
    """多帧一次调用，返回判定 dict；解析失败/HTTP 失败重试。

    自动分协议：base_url 含 /anthropic → Anthropic Messages 格式
    （MiniMax M3 等走此协议）；否则 OpenAI chat/completions 格式。
    """
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
                        "text": PROMPT.format(N=len(frames))})
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
        content.append({"type": "text", "text": PROMPT.format(N=len(frames))})
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
        print(f"[rejudge] 已移回 {rescued} 个待重判文件")
        videos = sorted(video_dir.glob("*.mp4"))
    if not videos:
        raise FilterError(f"目录中没有 mp4: {video_dir}")
    workdir = video_dir / ".wm_frames"
    workdir.mkdir(exist_ok=True)
    report = []
    for idx, v in enumerate(videos, 1):
        print(f"\n[{idx}/{len(videos)}] {v.name[:40]}")
        entry = {"file": v.name, "status": "error", "reason": ""}
        try:
            frames = extract_frames(v, n_frames, workdir)
            if not frames:
                entry["reason"] = "抽帧失败"
                print("  错误: 抽帧失败")
                report.append(entry)
                continue
            verdict = ask_vlm(frames, api_key, base_url, model)
            entry.update(verdict)
            if verdict.get("has_author_watermark"):
                if dry_run:
                    entry["status"] = "flagged(dry-run)"
                    print(f"  ⚠ 有作者水印（dry-run 不转移）: "
                          f"{verdict.get('desc', '')[:50]}")
                else:
                    quarantine.mkdir(exist_ok=True)
                    dest = unique_dest(quarantine / v.name)
                    shutil.move(str(v), str(dest))
                    entry["status"] = "moved"
                    entry["moved_to"] = dest.name
                    print(f"  ⚠ 有作者水印 → 已转移: "
                          f"{verdict.get('desc', '')[:50]}")
            else:
                entry["status"] = "clean"
                print("  ✓ 无作者水印")
        except Exception as e:  # noqa: BLE001 - 单条失败不中断
            entry["reason"] = f"{type(e).__name__}: {e}"
            print(f"  错误: {entry['reason']}")
        report.append(entry)
    (video_dir / "watermark_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    n_clean = sum(1 for e in report if e["status"] == "clean")
    n_moved = sum(1 for e in report if e["status"] in ("moved", "flagged(dry-run)"))
    n_err = sum(1 for e in report if e["status"] == "error")
    print(f"\n汇总: 无水印 {n_clean} / 有水印 {n_moved} / 错误 {n_err}"
          f"（详情: {video_dir / 'watermark_report.json'}）")


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
        print("错误: 未设置环境变量 DASHSCOPE_API_KEY（阿里云百炼 API Key）",
              file=sys.stderr)
        sys.exit(1)
    try:
        run(Path(args.video_dir), args.frames, api_key, args.base_url,
            args.model, args.dry_run, args.rejudge)
    except FilterError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
