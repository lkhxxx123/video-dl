#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一启动器（仅两个模式）

用法:
  python run.py jx [参数...]              合集: 精选搜索→作者合集页→采样验水印→整部下载
                                          不带参数 = 默认任务(见 DEFAULT_*)
  python run.py clips "关键词" [参数...]   散片: root搜索→按小时分桶→3帧验水印

Key 配置(两个模式都要): key.txt 只放一行 Key 本体
（默认百炼 qwen3.8-flash；MiniMax 见 README）

依赖库（勿删）: douyin_dl(下载引擎) douyin_search(浏览器/搜索)
douyin_auto(状态/判定) watermark_filter(识图) series_detect(集数标记)
"""
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)

DEFAULT_KEYWORDS = "AI 短剧,AI 动画短片,ai漫剧,AI 微短剧,原创AI短剧"
DEFAULT_SEARCH_ARGS = ["--limit", "5", "--max-likes", "500000"]


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


def need_key(rest):
    if "--selftest" in rest:
        return True
    if not load_key():
        print("错误: 未找到 Key —— 用记事本打开 key.txt，把 Key 本体"
              "粘成一行（纯 Key，不带备注）", file=sys.stderr)
        sys.exit(1)


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    mode = args[0] if args else "jx"
    rest = args[1:]
    if mode in ("-h", "--help", "help"):
        print(__doc__)
        return
    if mode in ("jx", "jingxuan"):
        import douyin_jx
        if not rest:
            rest = [DEFAULT_KEYWORDS] + DEFAULT_SEARCH_ARGS
        need_key(rest)
        douyin_jx.main(rest)
        return
    if mode in ("clips", "sp"):
        import douyin_clips
        if not rest:
            print('用法: python run.py clips "关键词" [参数…]',
                  file=sys.stderr)
            sys.exit(1)
        need_key(rest)
        douyin_clips.main(rest)
        return
    print(f"未知模式: {mode}（仅支持 jx / clips）\n{__doc__}",
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
