#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.paths — 路径唯一锚点（全项目只在此处解析根目录与数据文件位置）

开发态: 根目录 = 仓库根（core/ 的上一级），替代原三份 SCRIPT_DIR 副本；
冻结态(exe 便携包): 根目录 = exe 所在目录——key.txt / 浏览器登录态 /
下载目录 / 弃剧名单全部落 exe 旁（便携式数据目录，拷走文件夹即搬走全部数据）。
"""
import shutil
import sys
from pathlib import Path


def app_root() -> Path:
    """应用根目录：冻结态取 exe 所在目录，开发态取仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


APP_ROOT = app_root()
DOWNLOADS_DIR = APP_ROOT / "downloads"
PROFILE_DIR = APP_ROOT / ".browser-profile"              # 主登录态(合集/jx)
CLIPS_PROFILE_DIR = APP_ROOT / ".browser-profile-clips"  # 散片独立登录态
SKIP_LIST_PATH = APP_ROOT / "watermark_skip.json"        # 弃剧名单（入库跟踪）
KEY_PATH = APP_ROOT / "key.txt"                          # API Key（绝不入库）
RUNTIME_TOOLS_DIR = APP_ROOT / "runtime" / "tools"       # exe 包内 ffmpeg 等


def find_tool(name: str) -> str:
    """定位外部工具（ffmpeg/ffprobe）：exe 包内 runtime/tools 优先，PATH 兜底。

    返回可执行文件绝对路径或裸命令名（PATH 命中时交给 subprocess 按 PATH
    解析）；都找不到返回裸名，让 subprocess 抛原生 FileNotFoundError。
    """
    bundled = RUNTIME_TOOLS_DIR / f"{name}.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which(name) or name
