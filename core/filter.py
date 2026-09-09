#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.filter — 候选筛选与作者黑名单（平台无关的判定逻辑）。

item schema 契约（由各平台搜索解析器产出）：
{aweme_id, title, digg, duration_ms, followers, sig, nick, sec_uid, ...}
"""
import core.selftest
from . import selftest as _st

# 搬运/侵权类账号黑名单（匹配 简介/昵称/标题）
DEFAULT_BLOCK_KEYWORDS = [
    # 搬运/二传声明
    "搬运", "转载", "二传", "搬用", "搬运工", "全网搬运",
    "每日搬运", "搬运合集", "影视搬运",
    # 侵权/删除类免责声明
    "侵权", "联系删除", "如有侵权", "侵权删", "侵权请联系",
    "私聊删除", "侵删", "违规请联系",
    # 仅供类免责
    "仅供欣赏", "仅供学习", "仅供交流", "仅供个人", "仅供参考",
    "仅供娱乐", "请勿商用", "禁止商用", "勿用于商业",
    # 禁止类
    "禁止搬运", "禁搬运", "请勿搬运", "勿搬运", "禁止转载",
    "严禁搬运", "严禁转载", "禁止二传", "禁止二次上传",
    # 出处/来源声明（搬运号常见）
    "出处见水印", "来源见水印", "版权归原", "版权归作者",
    "原作者", "原创作者所有", "视频来源网络", "素材来源网络",
    "如有侵权请", "联系我删除",
    # 免责
    "免责", "免责声明",
]


def passes_filter(item: dict, max_followers=None, max_duration=None,
                  max_likes=None, min_duration=None):
    """筛选判定：严格小于/大于才保留；启用的条件遇字段未知即拒绝。

    min_duration: 时长下限（秒），严格大于才保留（滤过短碎片）。
    """
    if min_duration:
        if item["duration_ms"] is None:
            return False, "时长未知"
        if item["duration_ms"] <= min_duration * 1000:
            return False, \
                f"时长 {item['duration_ms'] // 1000}s <= {min_duration}s"
    if max_likes is not None:
        if item["digg"] is None:
            return False, "点赞数未知"
        if item["digg"] >= max_likes:
            return False, f"点赞 {item['digg']} >= {max_likes}"
    if max_duration is not None:
        if item["duration_ms"] is None:
            return False, "时长未知"
        if item["duration_ms"] >= max_duration * 1000:
            return False, \
                f"时长 {item['duration_ms'] // 1000}s >= {max_duration}s"
    if max_followers is not None:
        if item["followers"] is None:
            return False, "粉丝数未知"
        if item["followers"] >= max_followers:
            return False, f"粉丝 {item['followers']} >= {max_followers}"
    return True, ""


def author_blocked(item: dict, keywords):
    """作者简介/昵称/视频标题命中黑名单关键词（搬运/侵权类账号）。"""
    if not keywords:
        return False, ""
    text = " ".join([item.get("sig", ""), item.get("nick", ""),
                     item.get("title", "")])
    for kw in keywords:
        if kw and kw in text:
            return True, kw
    return False, ""


# ---------- selftest ----------

def run_selftests():
    return _st.run_selftests(globals())


# ---------- tests: 筛选 ----------

def _item(**kw):
    base = {"aweme_id": "x", "title": "t", "digg": 100,
            "duration_ms": 60000, "followers": 500}
    base.update(kw)
    return base


def test_passes_filter_all_pass():
    ok, why = passes_filter(_item(), max_followers=10000,
                            max_duration=120, max_likes=1000)
    assert ok is True and why == ""


def test_passes_filter_rejects_each_dimension():
    assert passes_filter(_item(followers=10000),
                         max_followers=10000) == (False, "粉丝 10000 >= 10000")
    assert passes_filter(_item(duration_ms=120000),
                         max_duration=120) == (False, "时长 120s >= 120s")
    assert passes_filter(_item(digg=1000),
                         max_likes=1000) == (False, "点赞 1000 >= 1000")


def test_passes_filter_min_duration():
    ok, _ = passes_filter(_item(duration_ms=31000), min_duration=30)
    assert ok is True
    assert passes_filter(_item(duration_ms=30000),
                         min_duration=30) == (False, "时长 30s <= 30s")
    assert passes_filter(_item(duration_ms=15000),
                         min_duration=30)[0] is False
    assert passes_filter(_item(duration_ms=None),
                         min_duration=30)[0] is False
    # 不启用时不受影响
    assert passes_filter(_item(duration_ms=5000))[0] is True


def test_passes_filter_unknown_rejects_only_when_active():
    assert passes_filter(_item(followers=None), max_followers=100)[0] is False
    assert passes_filter(_item(digg=None), max_likes=100)[0] is False
    assert passes_filter(_item(duration_ms=None), max_duration=60)[0] is False
    assert passes_filter(_item(followers=None, digg=None,
                               duration_ms=None))[0] is True


# ---------- tests: 黑名单 ----------

def test_author_blocked():
    it = {"sig": "每天更新短剧 禁止搬运", "nick": "xx号", "title": "t"}
    ok, kw = author_blocked(it, ["搬运"])
    assert ok is True and kw == "搬运"
    assert author_blocked(it, ["毫无关系"])[0] is False
    assert author_blocked(it, None)[0] is False
    assert author_blocked(it, [])[0] is False
    clean = {"sig": "原创作者", "nick": "正经营", "title": "无水印"}
    assert author_blocked(clean, ["搬运", "侵权"])[0] is False
