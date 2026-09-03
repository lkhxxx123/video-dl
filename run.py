#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一启动器（替代 .bat）

用法:
  python run.py                          一条龙默认任务: 凑 50 条干净视频(需Key)
  python run.py auto [参数...]            一条龙, 其余参数透传 douyin_auto
  python run.py jx [参数...]              精选合集: 只下带合集标记的短剧(按部计数)
  python run.py search [参数...]          批量搜索下载(不验水印); 无参数=默认50条任务
  python run.py dl "口令或链接"           单条下载
  python run.py mix "剧集里任意一集的链接"  下载整部剧集(自动验水印, 有Key时)
                                            合集/系列接口失败时自动走
                                            作者主页AI识别兜底(--max-episodes 可调)
  python run.py wm [目录] [参数...]       AI 水印检测; 无参数=最新日期目录

Key 配置(仅 wm/auto 需要):
  把百炼 API Key 粘贴到同目录 key.txt 的 sk- 那一行;
  或设置环境变量 DASHSCOPE_API_KEY
"""
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)

DEFAULT_KEYWORDS = "AI 短剧,AI 动画短片,ai漫剧,AI 微短剧,原创AI短剧"
DEFAULT_SEARCH_ARGS = ["--limit", "50", "--max-followers", "10000",
                       "--max-duration", "120", "--max-likes", "500000"]
KEY_HINT = ("错误: 未找到 Key —— 用记事本打开 key.txt, 把百炼 API Key"
            "粘贴到 sk- 那一行; 或设置环境变量 DASHSCOPE_API_KEY")


def load_key() -> bool:
    """key.txt 第一行有效 Key → 注入环境变量。已有环境变量则直接用。

    key.txt 只放 key 本体（一行，不带备注）；不要求 sk- 开头——
    不同 OpenAI 兼容服务商的 key 前缀不同。
    识别规则：纯 ASCII、无空白、≥8 字符（天然排除中文备注/占位行）。
    """
    if os.environ.get("DASHSCOPE_API_KEY"):
        return True
    kf = SCRIPT_DIR / "key.txt"
    if kf.exists():
        for line in kf.read_text(encoding="utf-8").splitlines():
            k = line.strip()
            if (k and k.isascii() and " " not in k and "\t" not in k
                    and "在这里" not in k and len(k) >= 8):
                os.environ["DASHSCOPE_API_KEY"] = k
                return True
    return False


def latest_download_dir():
    """downloads 下最新的子目录（按修改时间），给 wm 模式做默认目录。"""
    root = SCRIPT_DIR / "downloads"
    if root.is_dir():
        dirs = [d for d in root.iterdir() if d.is_dir()]
        if dirs:
            return max(dirs, key=lambda p: p.stat().st_mtime)
    return root


def need_key(rest):
    """--selftest 不需要 Key; 其余情况需要。"""
    if "--selftest" in rest:
        return True
    if not load_key():
        print(KEY_HINT, file=sys.stderr)
        sys.exit(1)


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    mode = args[0] if args else "auto"
    rest = args[1:]

    if mode in ("help", "-h", "--help"):
        print(__doc__)
        return
    if mode == "dl":
        import douyin_dl
        douyin_dl.main(rest)
        return
    if mode == "search":
        import douyin_search
        if not rest:
            rest = [DEFAULT_KEYWORDS] + DEFAULT_SEARCH_ARGS
        douyin_search.main(rest)
        return
    if mode == "wm":
        import watermark_filter as wf
        if not rest:
            rest = [str(latest_download_dir())]
        need_key(rest)
        wf.main(rest)
        return
    if mode == "mix":
        import re as _re
        import douyin_auto
        import watermark_filter as _wf
        if not rest:
            print("用法: python run.py mix <剧集任意一集的链接或视频ID>",
                  file=sys.stderr)
            sys.exit(1)
        m = _re.search(r"(\d{15,})", rest[0])
        if not m:
            print("错误: 链接中未找到视频 ID", file=sys.stderr)
            sys.exit(1)
        judge = load_key()
        if not judge:
            print("(未配置 Key: 只下载全部集，不验水印，无AI识别兜底)")
        max_eps = 100
        if "--max-episodes" in rest:
            i = rest.index("--max-episodes")
            if i + 1 < len(rest) and rest[i + 1].isdigit():
                max_eps = int(rest[i + 1])
        try:
            douyin_auto.download_mix(
                m.group(1), judge,
                api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
                base_url=_wf.DEFAULT_BASE_URL, model=_wf.DEFAULT_MODEL,
                max_episodes=max_eps)
        except Exception as e:  # noqa: BLE001
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        return
    if mode in ("jx", "jingxuan"):
        import douyin_jx
        if not rest:
            rest = [DEFAULT_KEYWORDS] + DEFAULT_SEARCH_ARGS
        need_key(rest)
        douyin_jx.main(rest)
        return
    if mode == "auto":
        import douyin_auto
        if not rest:
            rest = [DEFAULT_KEYWORDS] + DEFAULT_SEARCH_ARGS
        need_key(rest)
        douyin_auto.main(rest)
        return
    print(f"未知模式: {mode}\n{__doc__}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
