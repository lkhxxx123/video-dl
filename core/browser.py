#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.browser — playwright 持久化浏览器 / 登录 / 验证码等待 / 断连判定。

此处只管跨平台共通动作：开浏览器、等人扫码、等人过验证、页签复用、
响应 JSON 兜底解析、浏览器断连识别。平台差异部分（搜索路由、接口
拦截、登录首页 URL）在 platforms/<name>/。
"""
import json
import time
from contextlib import contextmanager

from .errors import SearchError
from .paths import PROFILE_DIR
from .reporting import checkpoint, log, urgent
from . import selftest as _st

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False


def resp_json(resp):
    """playwright resp.json() 实测(2026-09)对部分站点接口会抛错，
    统一走 text()+json.loads 兜底；失败返回 None。"""
    try:
        return resp.json()
    except Exception:
        try:
            return json.loads(resp.text())
        except Exception:
            return None


def is_conn_dead(e) -> bool:
    """判定异常是否为浏览器/driver 断连（整个浏览器崩溃或被关）。

    区别于单页签被关（Tab Discard，工厂可自愈）：断连后所有页面操作
    都会失败，调用方应停止本轮而非逐条空烧。
    """
    s = str(e)
    return ("Connection closed" in s
            or "Browser has been closed" in s
            or "Target page, context or browser has been closed" in s)


def _window_center_args() -> list:
    """把 Chromium 窗口放到屏幕正中（用户指定；滑块/扫码时最显眼）。"""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        try:  # 开 DPI 感知拿物理分辨率(否则缩放屏上居中会偏)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001 - 已设置/不支持则忽略
            pass
        w = user32.GetSystemMetrics(0)   # 屏幕宽(物理像素)
        h = user32.GetSystemMetrics(1)
        x = max(0, (w - 1300) // 2)
        y = max(0, (h - 930) // 2)
        return [f"--window-position={x},{y}", "--window-size=1300,930"]
    except Exception:  # noqa: BLE001 - 拿不到屏幕尺寸就默认位置
        return []


@contextmanager
def open_browser(profile_dir=None):
    """打开持久化登录浏览器。profile_dir 指定独立 profile 目录
    （并行模式传独立目录如 .browser-profile-clips，互不抢 Chromium 锁）。"""
    if not PLAYWRIGHT_OK:
        raise SearchError(
            "playwright 未安装。先执行: "
            "pip install playwright && playwright install chromium")
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(profile_dir or PROFILE_DIR), headless=False,
                args=["--disable-blink-features=AutomationControlled",
                      *_window_center_args()],
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 900})
            # 降低自动化指纹，减少风控验证码概率（实测 2026-09）
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver',"
                " {get: () => undefined})")
            try:
                yield context
            finally:
                context.close()
    except SearchError:
        raise
    except Exception as e:  # playwright.Error 等
        if "Executable doesn't exist" in str(e):
            raise SearchError(
                "Chromium 未下载。先执行: playwright install chromium")
        raise SearchError(f"浏览器错误: {e}")


def first_page(context):
    return context.pages[0] if context.pages else context.new_page()


def has_login(cookies) -> bool:
    """playwright context.cookies() 中存在非空 sessionid 即视为已登录。"""
    return any(c.get("name") == "sessionid" and c.get("value")
               for c in cookies)


def ensure_login(context, page, login_url: str, timeout: int = 600) -> None:
    """无登录态时打开平台首页引导扫码，等待登录后任务自动继续。

    - 浏览器窗口前置（bring_to_front），避免被 UI 窗口挡住"闪一下就没"
    - 等待期间每 60s 经 urgent 通道重复提醒（UI 任务卡显示 ⏳ 待扫码）
    - timeout 默认 10 分钟；超时抛 SearchError，文案引导重新执行

    login_url: 平台首页（由调用方/平台层传入，core 不持有平台 URL）。
    """
    if has_login(context.cookies()):
        return
    page.goto(login_url, timeout=30000)
    try:
        page.bring_to_front()  # 前置登录窗口
    except Exception:  # noqa: BLE001 - 置顶失败不影响流程
        pass
    urgent(f">>> 未检测到登录态：请在弹出的浏览器窗口中扫码登录"
           f"（最长等待 {timeout // 60} 分钟，扫码后任务自动继续） <<<")
    deadline = time.time() + timeout
    next_remind = time.time() + 60
    while time.time() < deadline:
        checkpoint()
        if has_login(context.cookies()):
            log("登录成功。")
            return
        if time.time() >= next_remind:
            urgent(f">>> 仍在等待扫码…剩余约 {max(1, int((deadline - time.time()) // 60))} 分钟 <<<")
            next_remind = time.time() + 60
        page.wait_for_timeout(2000)
    raise SearchError(
        f"扫码等待超时（{timeout}s）——请重新执行任务，在弹出的浏览器"
        "窗口完成扫码；也可先点「扫码登录」单独登录")


def login_only(login_url: str, profile_dir=None) -> None:
    """只扫码登录不干活（CLI login 入口）。

    profile_dir: 独立 profile 目录（如散片的 .browser-profile-clips——
    其登录态独立，首次使用需在本 profile 下单独扫码）。"""
    with open_browser(profile_dir=profile_dir) as context:
        ensure_login(context, first_page(context), login_url)


def wait_captcha(page, timeout=90) -> None:
    """页面标题含"验证"时提示用户手动完成验证码并等待放行。"""
    prompted = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "验证" not in page.title():
            return
        if not prompted:
            urgent(">>> 触发验证码：请在浏览器窗口中手动完成验证 <<<")
            prompted = True
        page.wait_for_timeout(2000)
    raise SearchError(f"验证码等待超时（{timeout}s）")


# ---------- selftest ----------

def run_selftests():
    return _st.run_selftests(globals())


# ---------- tests ----------

def test_has_login_true_only_with_sessionid_value():
    assert has_login([{"name": "sessionid", "value": "abc"}]) is True
    assert has_login([{"name": "sessionid", "value": ""}]) is False
    assert has_login([{"name": "ttwid", "value": "x"}]) is False
    assert has_login([]) is False
