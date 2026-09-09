#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.download — HTTP 下载引擎（流式/断流整档重试，平台无关）。

从 douyin_dl 收敛而来；平台专属的链接解析/浏览器兜底仍在各自平台层。
"""
import time

import requests

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
TIMEOUT = 10
RETRIES = 2
# web CDN 直链需要的请求头（实测 2026-09：无 Referer 会 403）
WEB_HEADERS = {
    "Referer": "https://www.douyin.com/",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
}


class ParseError(Exception):
    """解析失败（链接无效 / 页面结构变更 / 触发风控等）。"""


def http_get_with_retry(session, url, stream=False, headers=None):
    """GET，10s 超时，自动重试 RETRIES 次；最终失败抛最后一个异常。"""
    last_exc = None
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT, stream=stream,
                            headers=headers)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc


def download_video(session, url, dest, headers=None) -> None:
    """流式下载到 dest；断流/异常/过小内容自动整档重试(RETRIES+1 次)。

    实测(2026-09-05): web CDN 直链下长视频(50MB+)中途断流常见
    (ChunkedEncodingError——GET 层的重试只覆盖建连，管不到流中途)，
    单次尝试失败率可观，整档重试即可恢复。半成品每轮清理。
    """
    last_exc = None
    for attempt in range(RETRIES + 1):
        dest.unlink(missing_ok=True)  # 清上轮半成品，避免幂等误判
        r = None
        try:
            r = http_get_with_retry(session, url, stream=True,
                                    headers=headers)
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        total += len(chunk)
            if total < 1024:
                raise ParseError(f"下载内容异常（仅 {total} 字节），"
                                 f"链接可能已失效")
            return
        except Exception as e:  # noqa: BLE001 - 断流/超时/过小统一重试
            last_exc = e
            if attempt < RETRIES:
                time.sleep(1.5 * (attempt + 1))
        finally:
            if r is not None:
                r.close()
    dest.unlink(missing_ok=True)  # 最终失败也清半成品（循环顶只清下一轮）
    raise last_exc
