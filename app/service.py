#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app.service — 任务服务层（未来 UI 的唯一入口）。

用法（UI 与 CLI 平级）：
    cfg = JxConfig(keywords=["奇闻故事"], limit=5, api_key=key)
    run_jx(cfg, reporter=my_ui_reporter)

进度不通过返回值——所有输出经 core.reporting 双通道（log/urgent）
流出，install(reporter) 决定流向控制台还是界面。
"""
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

from core import bootstrap
from core.filter import DEFAULT_BLOCK_KEYWORDS
from core.naming import make_dated_dir, split_keywords
from core.paths import APP_ROOT, DOWNLOADS_DIR, KEY_PATH
from core.watermark import DEFAULT_BASE_URL, DEFAULT_MODEL
from .reporting import ConsoleReporter, install

CONFIG_PATH = APP_ROOT / "config.json"   # 识图模型/端点等 UI 配置（不入库）

# 无参数时的默认任务（原 run.py DEFAULT_*）
DEFAULT_JX_KEYWORDS = ["AI 短剧", "AI 动画短片", "ai漫剧", "AI 微短剧",
                       "原创AI短剧"]
DEFAULT_JX_LIMIT = 5
DEFAULT_JX_MAX_LIKES = 500000


# ---------- API Key ----------

def load_key() -> bool:
    """key.txt 第一行有效 Key → 注入环境变量。已有环境变量则直接用。

    key.txt 只放 key 本体（一行，不带备注）；不要求 sk- 开头——
    不同 OpenAI 兼容服务商的 key 前缀不同。
    识别规则：纯 ASCII、无空白、≥8 字符（天然排除中文备注/占位行）。
    """
    if os.environ.get("DASHSCOPE_API_KEY"):
        return True
    if KEY_PATH.exists():
        for line in KEY_PATH.read_text(encoding="utf-8").splitlines():
            k = line.strip()
            if (k and k.isascii() and " " not in k and "\t" not in k
                    and "在这里" not in k and len(k) >= 8):
                os.environ["DASHSCOPE_API_KEY"] = k
                return True
    return False


def get_api_key() -> Optional[str]:
    """取当前可用 Key（不注入只读取）；无则 None。UI 显示状态用。"""
    return os.environ.get("DASHSCOPE_API_KEY")


def require_api_key() -> str:
    """CLI 用：无 Key 打印指引并退出。"""
    if not load_key():
        import sys
        print("错误: 未找到 Key —— 用记事本打开 key.txt，把 Key 本体"
              "粘成一行（纯 Key，不带备注）", file=sys.stderr)
        sys.exit(1)
    return os.environ["DASHSCOPE_API_KEY"]


# ---------- UI 配置（识图模型/端点） ----------

def load_ai_config() -> dict:
    """config.json 中的模型/端点；缺省回落 MiniMax-M3。"""
    d = {"model": DEFAULT_MODEL, "base_url": DEFAULT_BASE_URL}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data.get("model"), str) and data["model"]:
                d["model"] = data["model"]
            if isinstance(data.get("base_url"), str) and data["base_url"]:
                d["base_url"] = data["base_url"]
        except Exception:
            pass
    return d


def save_ai_config(key: str = "", model: str = "", base_url: str = "") -> None:
    """保存 UI 的 AI 配置：key 写 key.txt（空则不动），模型/端点写 config.json。"""
    data = load_ai_config()
    if model:
        data["model"] = model.strip()
    if base_url:
        data["base_url"] = base_url.strip()
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    if key.strip():
        KEY_PATH.write_text(key.strip() + "\n", encoding="utf-8")


# ---------- 任务配置 ----------

@dataclass
class JxConfig:
    """合集模式（精选搜索→合集页拉全→采样验水印→整部下载）。"""
    keywords: List[str]
    limit: int = 10                       # 成功保留的剧数
    max_followers: Optional[int] = None   # 严格小于
    max_duration: Optional[int] = None
    max_likes: Optional[int] = None
    min_duration: Optional[int] = 30      # 严格大于，滤过短碎片
    block_keywords: List[str] = field(
        default_factory=lambda: list(DEFAULT_BLOCK_KEYWORDS))
    frames: int = 6                       # 每集抽帧数
    sample: int = 2                       # 首尾采样集数(0=逐集判定)
    min_episodes: int = 0                 # 总集数下限(小于则跳过, 0=不限)
    max_ep_duration: Optional[int] = 600  # 合并总集拦截(秒)
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""


@dataclass
class ClipsConfig:
    """散片模式（root搜索→发现即下→10分钟桶→3帧验水印）。"""
    keywords: List[str]
    limit: int = 10                       # 目标干净散片数
    max_followers: Optional[int] = None
    max_duration: Optional[int] = None
    max_likes: Optional[int] = None
    min_duration: Optional[int] = 30
    block_keywords: List[str] = field(
        default_factory=lambda: list(DEFAULT_BLOCK_KEYWORDS))
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""


def keywords_arg(cfg) -> List[str]:
    """兼容 CLI 字符串入参（逗号分隔）与 UI 列表入参。"""
    if isinstance(cfg.keywords, str):
        return split_keywords(cfg.keywords)
    return list(cfg.keywords)


# ---------- 任务入口 ----------

def run_jx(cfg: JxConfig, reporter=None) -> None:
    """执行合集抓取任务（阻塞至完成；进度经 log/urgent/event 通道流出）。

    reporter: 传 None 不动全局 sink（app.tasks 用线程本地 sink 路由）；
    CLI/简单调用方可传 ConsoleReporter。
    """
    from modes import jx
    if reporter is not None:
        install(reporter)
    bootstrap.setup_runtime_env()
    out_dir = make_dated_dir(DOWNLOADS_DIR)
    jx.run(keywords_arg(cfg), cfg.limit,
           (cfg.max_followers, cfg.max_duration, cfg.max_likes),
           cfg.block_keywords, cfg.frames, cfg.api_key, cfg.base_url,
           cfg.model, out_dir, sample=cfg.sample,
           max_ep_duration=cfg.max_ep_duration,
           min_episodes=cfg.min_episodes,
           min_duration=cfg.min_duration)


def run_clips(cfg: ClipsConfig, reporter=None) -> None:
    """执行散片抓取任务（阻塞至完成）。"""
    from modes import clips
    if reporter is not None:
        install(reporter)
    bootstrap.setup_runtime_env()
    out_dir = make_dated_dir(DOWNLOADS_DIR)
    clips.run(keywords_arg(cfg), cfg.limit,
              (cfg.max_followers, cfg.max_duration, cfg.max_likes),
              cfg.block_keywords, cfg.api_key, cfg.base_url,
              cfg.model, out_dir, min_duration=cfg.min_duration)


def default_jx_config() -> JxConfig:
    """无参数 CLI 的默认任务（与原 run.py 行为一致）。"""
    return JxConfig(keywords=list(DEFAULT_JX_KEYWORDS), limit=DEFAULT_JX_LIMIT,
                    max_likes=DEFAULT_JX_MAX_LIKES)
