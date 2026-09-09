#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""app.ui.bridge — pywebview JS 桥（UI ↔ 服务层的唯一通道）。

前端经 window.pywebview.api.<方法>() 调用；全部返回可 JSON 化的 dict。
UI 不直接触碰 core/platforms/modes。
"""
import os
import re
import threading
from pathlib import Path

from core import paths
from core.browser import login_only
from core.naming import split_keywords
from core.filter import DEFAULT_BLOCK_KEYWORDS
from app.service import (ClipsConfig, JxConfig, load_ai_config, load_key,
                         save_ai_config)
from app.tasks import TaskManager

PLATFORM_HOME = "https://www.douyin.com/"
PLATFORM_NAMES = {"douyin": "抖音", "kuaishou": "快手", "bili": "B站",
                  "xhs": "小红书"}
PLATFORMS = [{"id": "douyin", "name": "抖音", "enabled": True},
             {"id": "kuaishou", "name": "快手", "enabled": False},
             {"id": "bili", "name": "B站", "enabled": False},
             {"id": "xhs", "name": "小红书", "enabled": False}]


def _opt_int(v):
    """表单字符串 → int 或 None（空串/非法 = 不限）。"""
    try:
        v = int(str(v).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


class Bridge:
    """js_api 对象（pywebview 注入为 window.pywebview.api）。"""

    def __init__(self, tm: TaskManager):
        self.tm = tm
        self.login_state = {"running": False, "ok": False, "msg": ""}
        self.media_base = ""    # 本地媒体服务 base(启动时注入)

    # ---------- 元信息 ----------

    def get_initial(self) -> dict:
        """首屏：平台列表/AI 配置状态/登录态。"""
        return {
            "platforms": PLATFORMS,
            "ai": self.get_ai_config(),
            "login": {"douyin": (paths.PROFILE_DIR.exists()
                                 or paths.CLIPS_PROFILE_DIR.exists())},
        }

    # ---------- 任务 ----------

    def start_task(self, mode: str, params: dict) -> dict:
        err = None
        r = self._start_task_checked(mode, params)
        if isinstance(r, dict) and r.get("error"):
            err = r["error"]
        else:
            err = None
        if err:
            print(f"[start_task 拒绝] {mode}: {err}", flush=True)  # 落盘留痕
        return r

    def _start_task_checked(self, mode: str, params: dict) -> dict:
        if mode not in ("jx", "clips"):
            return {"error": f"未知模式: {mode}"}
        if not load_key():
            return {"error": "未配置 API Key —— 点顶栏「AI 配置」填入"}
        # 从未登录预检(登录过期由任务内 ensure_login 弹窗兜底):
        # UI 下合集/散片共用主 profile, 检查主登录目录即可
        if not paths.PROFILE_DIR.exists():
            return {"error": "尚未登录抖音 —— 请先点顶栏「扫码登录」"}
        ai = load_ai_config()
        common = dict(
            keywords=split_keywords(params.get("keywords") or ""),
            limit=_opt_int(params.get("limit")) or 10,
            max_followers=_opt_int(params.get("max_followers")),
            max_duration=_opt_int(params.get("max_duration")),
            max_likes=_opt_int(params.get("max_likes")),
            min_duration=_opt_int(params.get("min_duration")) or 30,
            model=ai["model"], base_url=ai["base_url"],
            api_key=os.environ.get("DASHSCOPE_API_KEY", ""),
        )
        if not common["keywords"]:
            return {"error": "请至少填写一个关键词"}
        if mode == "jx":
            cfg = JxConfig(
                **common,
                block_keywords=_split_or_default(params.get("block_keywords")),
                frames=_opt_int(params.get("frames")) or 6,
                sample=_opt_int(params.get("sample")) or 0,
                min_episodes=_opt_int(params.get("min_episodes")) or 0,
                max_ep_duration=_opt_int(params.get("max_ep_duration")) or 0,
            )
        else:
            cfg = ClipsConfig(
                **common,
                block_keywords=_split_or_default(params.get("block_keywords")),
            )
        tid = self.tm.start(mode, cfg, common["keywords"][0])
        return {"id": tid}

    def get_tasks(self) -> list:
        return self.tm.list()

    def cancel_task(self, tid) -> dict:
        self.tm.cancel(int(tid))
        return {"ok": True}

    # ---------- 登录 ----------

    def start_login(self, profile: str = "main") -> dict:
        """开浏览器扫码（独立线程；登录窗口由 playwright 弹出）。

        结果经 get_login_state 轮询回 UI（浏览器窗口被手动关闭等
        情况给友好提示，不裸抛）。"""
        if self.login_state.get("running"):
            return {"ok": True, "msg": "登录窗口已打开，请完成扫码"}
        profile_dir = paths.CLIPS_PROFILE_DIR if profile == "clips" else None
        self.login_state = {"running": True, "ok": False, "msg": "等待扫码…"}
        threading.Thread(target=self._login_thread, args=(profile_dir,),
                         daemon=True, name="login").start()
        return {"ok": True}

    def _login_thread(self, profile_dir) -> None:
        try:
            login_only(PLATFORM_HOME, profile_dir=profile_dir)
            self.login_state = {"running": False, "ok": True,
                                "msg": "登录成功"}
        except Exception as e:  # noqa: BLE001 - 反馈到 UI 而非裸抛
            msg = str(e)
            if "has been closed" in msg:
                msg = "登录窗口被关闭——请重新点「扫码登录」"
            self.login_state = {"running": False, "ok": False, "msg": msg}

    def get_login_state(self) -> dict:
        return dict(self.login_state)

    # ---------- 目录树 / 预览 ----------

    def list_tree(self) -> list:
        return scan_downloads()

    def file_url(self, rel_path: str) -> dict:
        """视频文件 → 本地媒体服务的 HTTP URL（支持 Range 拖动播放）。"""
        from urllib.parse import quote
        return {"url": f"{self.media_base}/video/{quote(rel_path)}"}

    def video_meta(self, rel_path: str) -> dict:
        p = (paths.APP_ROOT / rel_path) if not Path(rel_path).is_absolute() \
            else Path(rel_path)
        size_mb = p.stat().st_size / 1024 / 1024 if p.exists() else 0
        return {"name": p.name, "sizeMb": round(size_mb, 1),
                "wm": "疑似水印" in p.parts}

    def open_folder(self, rel_path: str) -> dict:
        """资源管理器打开视频所在目录（不选中文件，简单可靠）。"""
        p = (paths.APP_ROOT / rel_path) if not Path(rel_path).is_absolute() \
            else Path(rel_path)
        os.startfile(str(p.parent))
        return {"ok": True}

    # ---------- AI 配置 ----------

    def get_ai_config(self) -> dict:
        ai = load_ai_config()
        return {"hasKey": load_key(), "model": ai["model"],
                "baseUrl": ai["base_url"]}

    def save_ai_config(self, key: str, model: str, base_url: str) -> dict:
        save_ai_config(key=key or "", model=model or "",
                       base_url=base_url or "")
        # key 写入后立即注入本进程环境，任务无需重启
        if key.strip():
            load_key()
        return self.get_ai_config()


# ---------- 辅助 ----------

def _split_or_default(v):
    """黑名单输入：空 = 内置词表；"off" = 禁用；否则逗号拆分。"""
    if v is None or str(v).strip() == "":
        return list(DEFAULT_BLOCK_KEYWORDS)
    text = str(v).strip()
    return [] if text == "off" else split_keywords(text)


def _natkey(p):
    """自然排序 key: 数字段按数值比较(01 < 02 < 004 < 099), 与资源
    管理器一致。普通字符串字典序会把 004 排到 01 前面。"""
    return [(0, int(t)) if t.isdigit() else (1, t.lower())
            for t in re.split(r"(\d+)", p.name if hasattr(p, "name") else p)]


# ---------- 目录树扫描 ----------

def _video_node(p: Path) -> dict:
    rel = str(p.relative_to(paths.APP_ROOT))
    return {"name": p.name, "type": "video", "path": rel,
            "wm": "疑似水印" in p.parts}


def scan_downloads() -> list:
    """downloads/ → 树：日期 → 平台 → 合集/散片 → 剧名|桶 → 文件。

    存量结构（日期下直接 剧集/散片，无平台层）按抖音展示（方案A兼容）。
    """
    root = paths.DOWNLOADS_DIR
    days = []
    if not root.is_dir():
        return days
    for day in sorted((d for d in root.iterdir() if d.is_dir()),
                      key=lambda d: d.name, reverse=True):  # 日期倒序(最新在前)
        day_node = {"name": day.name, "type": "date", "children": []}
        kinds_here = [d for d in day.iterdir()
                      if d.is_dir() and d.name in ("剧集", "散片")]
        if kinds_here:  # 存量结构 → 视作抖音
            day_node["children"].append(
                _platform_node("douyin", kinds_here))
        else:           # 新结构: 日期/<平台>/剧集|散片
            for pf in sorted(d for d in day.iterdir() if d.is_dir()):
                kinds = [d for d in pf.iterdir() if d.is_dir()]
                day_node["children"].append(
                    _platform_node(pf.name, kinds))
        days.append(day_node)
    return days


def _platform_node(pf_id: str, kind_dirs: list) -> dict:
    node = {"name": PLATFORM_NAMES.get(pf_id, pf_id), "platform": pf_id,
            "type": "platform", "children": []}
    for kd in sorted(kind_dirs, key=lambda d: d.name):
        kind_node = {"name": kd.name, "type": "kind", "children": []}
        for sub in _group_nodes(kd):
            kind_node["children"].append(sub)
        node["children"].append(kind_node)
    return node


def _group_nodes(kind_dir: Path) -> list:
    """合集/散片层的直接子目录 → 组节点（剧集兼容 小时桶/平铺/弃用 三形态）。"""
    out = []
    for d in sorted((x for x in kind_dir.iterdir() if x.is_dir()),
                    key=_natkey):
        mp4s = sorted(d.glob("*.mp4"), key=_natkey)
        subs = [x for x in d.iterdir() if x.is_dir()]
        if mp4s or not subs:
            # 平铺剧目录 / 空桶：自身即组节点
            out.append(_group_node(d, mp4s))
        else:
            # 小时桶（剧集 HH点）→ 展开一层为剧节点
            for s in sorted(subs, key=_natkey):
                out.append(_group_node(s, sorted(s.glob("*.mp4"), key=_natkey)))
    return out


def _group_node(d: Path, mp4s: list) -> dict:
    n = len(mp4s)
    label = f"{d.name}({n})" if n and "(" not in d.name else d.name
    return {"name": label, "type": "group", "path": _rel(d),
            "abandoned": d.name.startswith("有水印弃用-"),
            "children": [_video_node(v) for v in mp4s]}


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(paths.APP_ROOT))
    except ValueError:
        return str(p)
