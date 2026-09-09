#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一入口

用法:
  python run.py jx "关键词" [参数...]    合集: 精选搜索→合集页拉全→采样验水印→整部下载
                                        不带参数 = 默认任务
  python run.py clips "关键词" [参数...] 散片: root搜索→发现即下→10分钟分桶
  python run.py login [--profile clips]  扫码登录（散片独立登录态需单独登录）
  python run.py doctor                   环境自检(ffmpeg/Chromium/Key)
  python run.py selftest                 聚合全部内置自测(不联网)
  python run.py wm <目录> [--dry-run|--rejudge]  水印重判工具
  python run.py dl "分享口令"            单条视频下载

Key 配置: key.txt 只放一行 Key 本体（任何 OpenAI 兼容服务商）
识图默认 MiniMax-M3（Anthropic 协议端点）；换百炼加
--model qwen3.8-flash --base-url <百炼兼容端点> 并换 key

架构: core(基建) + platforms(平台适配) + modes(编排) + app(服务层)
——接新平台=新增 platforms/<name>；接 UI=换 app 的 reporter 实现。
"""
import sys

from core import bootstrap
from core.reporting import log, urgent

bootstrap.setup_stdio()
bootstrap.chdir_app_root()
bootstrap.setup_runtime_env()

_PLATFORMS_HOME = "https://www.douyin.com/"


def _no_key_guard(rest) -> bool:
    """selftest/doctor/help 不需要 Key；jx/clips/wm/dl 需要（modes 内查）。"""
    return True


def cmd_login(rest):
    import argparse
    from core.browser import login_only
    from core.paths import CLIPS_PROFILE_DIR
    p = argparse.ArgumentParser(prog="run.py login")
    p.add_argument("--profile", choices=["main", "clips"], default="main",
                   help="clips=散片独立登录态(首次需单独扫码)")
    a = p.parse_args(rest)
    profile = CLIPS_PROFILE_DIR if a.profile == "clips" else None
    login_only(_PLATFORMS_HOME, profile_dir=profile)


def cmd_doctor(rest):
    """环境自检：ffmpeg/ffprobe 可用 → Chromium 可启动 → Key 已配置。"""
    import subprocess
    from core import paths
    from core.reporting import log, urgent
    ok = True

    log("== 视频下载器环境自检 ==")
    # 1) ffmpeg / ffprobe
    for tool in ("ffmpeg", "ffprobe"):
        path = paths.find_tool(tool)
        try:
            r = subprocess.run([path, "-version"], capture_output=True,
                               text=True, timeout=30)
            ver = (r.stdout or "").splitlines()[0][:60] if r.returncode == 0 \
                else "(无法执行)"
            ok &= r.returncode == 0
            log(f"  {'✓' if r.returncode == 0 else '✗'} {tool}: {ver}"
                f"  [{path}]")
        except Exception as e:  # noqa: BLE001
            ok = False
            log(f"  ✗ {tool}: {e}", err=True)
    # 2) Chromium
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        log("  ✓ Chromium: 可启动")
    except Exception as e:  # noqa: BLE001
        ok = False
        log(f"  ✗ Chromium: {e}", err=True)
        urgent("    → 执行: playwright install chromium")
    # 3) Key
    from app.service import load_key
    if load_key():
        log("  ✓ API Key: 已配置(key.txt/环境变量)")
    else:
        ok = False
        log("  ✗ API Key: 未找到 —— key.txt 里粘一行 Key 本体", err=True)
    log(f"== {'环境正常' if ok else '存在问题，见上方 ✗ 项'} ==")
    sys.exit(0 if ok else 1)


def cmd_selftest(rest):
    """聚合全部模块内置自测（不联网、不开浏览器）；任一 FAIL 退出码 1。"""
    from core import naming, state, browser, filter as filter_mod, watermark
    from platforms.douyin import dl, search, series
    from modes import auto, jx, clips
    modules = [naming, state, browser, filter_mod, watermark,
               dl, search, series, auto, jx, clips]
    ok = True
    for m in modules:
        log(f"[{m.__name__}]")
        ok = m.run_selftests() and ok
    log(f"\n总计: {'全部通过 ✓' if ok else '存在 FAIL ✗'}")
    sys.exit(0 if ok else 1)


def main():
    args = sys.argv[1:]
    mode = args[0] if args else None
    if mode is None:
        # 无参数: exe 双击 → 图形界面; 开发态保持旧行为(默认合集任务)
        mode = "ui" if getattr(sys, "frozen", False) else "jx"
    rest = args[1:]
    if mode in ("-h", "--help", "help"):
        print(__doc__)
        return
    if mode in ("jx", "jingxuan"):
        from modes import jx
        jx.main(rest)
        return
    if mode in ("clips", "sp"):
        from modes import clips
        clips.main(rest)
        return
    if mode == "login":
        cmd_login(rest)
        return
    if mode == "doctor":
        cmd_doctor(rest)
        return
    if mode == "selftest":
        cmd_selftest(rest)
        return
    if mode == "wm":
        from core import watermark
        watermark.main(rest)
        return
    if mode == "dl":
        from platforms.douyin import dl
        dl.main(rest)
        return
    if mode in ("ui", "gui"):
        from app.ui import gui
        gui.show()
        return
    print(f"未知模式: {mode}（支持 jx/clips/ui/login/doctor/selftest/wm/dl）\n"
          f"{__doc__}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - 顶层兜底: 落日志+弹窗(无控制台也能看到)
        import traceback
        err = traceback.format_exc()
        try:
            log(err, err=True)
        except Exception:  # noqa: BLE001
            pass
        if getattr(sys, "frozen", False):
            import ctypes
            tail = err.strip().splitlines()[-1] if err.strip() else "未知错误"
            ctypes.windll.user32.MessageBoxW(
                0, f"启动出错:\n{tail}\n\n详情见 logs\\运行日志.txt",
                "视频下载器", 0x10)
