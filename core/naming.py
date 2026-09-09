#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.naming — 文件名/目录/视频清单/关键词等纯命名工具（平台无关）。

从 douyin_dl(文件名清洗/视频清单) 与 douyin_search(日期目录/全局去重/
集数前缀/关键词拆分) 收敛而来；全部为纯逻辑，selftest 内置覆盖。
"""
import json
import re
import time
from pathlib import Path

from .reporting import log
from .paths import DOWNLOADS_DIR

# ---------- 文件名清洗 ----------

INVALID_FN_RE = re.compile(r'[\\/:*?"<>|\r\n]')

HASHTAG_RE = re.compile(r"#[^\s#]+")

# 文件名尾部的视频 ID——全局去重的唯一依据（状态丢失后的兜底也靠它）
ID_TAIL_RE = re.compile(r"_(\d{15,})\.mp4$")


def build_filename(title: str, aweme_id: str, author: str = "") -> str:
    """标题去话题标签+清洗+截断50字，附 来源@作者；ID 恒在末尾。

    形如: 标题_来源@作者_ID.mp4；无作者: 标题_ID.mp4；空标题回退纯 ID。
    话题标签(#xxx)整体剔除；连续空白折叠为单空格。
    """
    t = HASHTAG_RE.sub("", title or "")
    clean = INVALID_FN_RE.sub(" ", t)
    clean = re.sub(r"\s+", " ", clean).strip()[:50].strip()
    nick = INVALID_FN_RE.sub(" ", author or "").strip()[:24].strip()
    if not clean:
        clean = nick.lstrip("@")
        nick = ""
    parts = [p for p in (clean, f"来源@{nick}" if nick else "",
                         str(aweme_id)) if p]
    return "_".join(parts) + ".mp4"


def safe_dir_name(name: str) -> str:
    """清洗为合法目录名：非法字符→空格、strip、截 50 字符。"""
    return INVALID_FN_RE.sub(" ", name).strip()[:50]


# ---------- 视频清单 ----------

def record_manifest(out_dir: Path, filename: str, title: str, author: str,
                    mix_name: str = "", vid: str = "") -> None:
    """登记视频元信息（作者/短剧名/标题）到 视频清单.json + .csv。

    按文件名去重，重复调用不产生重复行；CSV 用 UTF-8-BOM，Excel 直接可开。
    """
    mf = out_dir / "视频清单.json"
    data = {}
    if mf.exists():
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if filename in data:
        return
    data[filename] = {"time": time.strftime("%Y-%m-%d %H:%M"),
                      "author": author or "", "mix": mix_name or "",
                      "title": title or "", "vid": vid or ""}
    mf.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                  encoding="utf-8")
    with open(out_dir / "视频清单.csv", "w", encoding="utf-8-sig",
              newline="") as f:
        f.write("下载时间,作者,短剧名,标题,文件名,视频ID\n")
        for fn, m in data.items():
            row = [m["time"], m["author"], m["mix"], m["title"], fn,
                   m["vid"]]
            f.write(",".join('"' + str(c).replace('"', '""') + '"'
                             for c in row) + "\n")


# ---------- 日期目录与全局去重 ----------

def make_dated_dir(root: Path) -> Path:
    """按日期建目录 root/YYYY-MM-DD。

    同一天多次运行共用同一目录（数据叠加）——去重由全局 ID 扫描保证，
    状态文件/视频清单在同日内累积，断点续跑更顺。
    """
    d = root / time.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def roll_date_dir(cur: dict, root: Path = None, on_switch=None) -> Path:
    """跨午夜翻日期目录（通宵跑场景）：cur["dir"] 名 ≠ 今天 → 切到
    <root>/新日期/ 并更新 cur["dir"]；同一天原样返回。

    root: 显式传入目录根（默认下载根）——参数化消除对全局常量的耦合，
    测试无需 patch 模块属性。
    on_switch(旧目录): 切换前回调（散片模式用来给旧目录的桶收尾定稿）。
    调用粒度由调用方决定——散片每条一查，合集每部剧一查（不拆一部剧）。
    """
    today = time.strftime("%Y-%m-%d")
    if cur["dir"].name != today:
        old = cur["dir"]
        if on_switch:
            on_switch(old)
        cur["dir"] = make_dated_dir(root or DOWNLOADS_DIR)
        log(f"\n↳ 跨过午夜 → 切换日期目录 {old.name} → {cur['dir'].name}",
              flush=True)
    return cur["dir"]


def existing_ids_under(root: Path) -> set:
    """递归收集 root 下所有 mp4 文件名尾部的视频 ID（跨目录全局去重）。"""
    ids = set()
    if root.is_dir():
        for f in root.rglob("*.mp4"):
            m = ID_TAIL_RE.search(f.name)
            if m:
                ids.add(m.group(1))
    return ids


# ---------- 集数前缀 / 关键词 ----------

def episode_prefix(n: int, total: int) -> str:
    """集数文件名前缀：零填充保证资源管理器按名排序即观看顺序。"""
    width = max(2, len(str(max(total, 1))))
    return f"{n:0{width}d}_"


def split_keywords(text: str):
    """按中英文逗号拆分关键词，去空白与空项。"""
    return [k.strip() for k in re.split(r"[,，]", text) if k.strip()]


# ---------- selftest ----------

def run_selftests():
    from . import selftest as _st
    return _st.run_selftests(globals())


# ---------- tests: 文件名清洗 ----------

def test_build_filename_cleans_and_truncates():
    title = '好"视频:/<标题>|续\n第二行' + "长" * 80
    name = build_filename(title, "7300000000000000000")
    for ch in '\\/:*?"<>|\r\n':
        assert ch not in name, f"非法字符 {ch!r} 残留"
    assert len(name) < 80, "应当截断标题"
    assert name.endswith("_7300000000000000000.mp4")


def test_build_filename_keeps_normal_title():
    assert build_filename("普通的标题", "123") == "普通的标题_123.mp4"


def test_build_filename_with_author():
    assert build_filename("标题A", "123", "阿刀Al短剧") == \
        "标题A_来源@阿刀Al短剧_123.mp4"
    # 作者名清洗非法字符；ID 恒在末尾
    assert build_filename('t"t', "456", '作/者:名') == \
        't t_来源@作 者 名_456.mp4'
    # 空标题时用作者名兜底（作标题，不带来源@）
    assert build_filename("", "789", "某人") == "某人_789.mp4"


def test_build_filename_strips_hashtags():
    # 话题标签整体剔除，多余空白折叠
    assert build_filename(
        "好看 #AI短剧 #ai漫剧 #抖音ai创作大赛", "1", "作者X") == \
        "好看_来源@作者X_1.mp4"
    assert build_filename("纯话题 #只有标签", "2") == "纯话题_2.mp4"
    # 孤立 # 不构成话题标签，保留；有效标签(#中、#后)剔除
    assert build_filename("前#中##后 续", "3") == "前# 续_3.mp4"


def test_build_filename_fallback_id_only():
    assert build_filename("", "1234567890123456789") == \
        "1234567890123456789.mp4"
    assert build_filename("   ", "1234567890123456789") == \
        "1234567890123456789.mp4"


def test_safe_dir_name():
    assert safe_dir_name("AI 短剧") == "AI 短剧"
    # ? 和 " 相邻 → 各替换为一个空格 → 两个连续空格
    assert safe_dir_name('a/b:c*d?"e<f>g|h') == "a b c d  e f g h"
    assert safe_dir_name("  ") == ""
    assert len(safe_dir_name("长" * 80)) == 50


# ---------- tests: 视频清单 ----------

def test_record_manifest_no_dup_and_csv():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        record_manifest(d, "a.mp4", "标题A", "作者X", "剧Z", "1" * 19)
        record_manifest(d, "a.mp4", "重复不写", "重复", "", "1" * 19)
        record_manifest(d, "b.mp4", '含"引号"的标题', "作者Y", "", "2" * 19)
        j = json.loads((d / "视频清单.json").read_text(encoding="utf-8"))
        assert len(j) == 2 and j["a.mp4"]["author"] == "作者X"
        csv_text = (d / "视频清单.csv").read_text(encoding="utf-8-sig")
        assert "作者X" in csv_text and "剧Z" in csv_text
        assert "重复不写" not in csv_text
        assert csv_text.count("\n") == 3  # 表头 + 2 行


# ---------- tests: 日期目录与全局去重 ----------

def test_make_dated_dir_same_day_reused():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        d1 = make_dated_dir(root)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", d1.name), d1.name
        # 同一天多次运行 → 同一目录（叠加），不再 -1/-2
        assert make_dated_dir(root) == d1
        assert len(list(root.iterdir())) == 1


def test_existing_ids_under_recursive():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        sub = root / "2026-09-01"
        (sub / "疑似水印").mkdir(parents=True)
        (sub / "A_7300000000000000001.mp4").write_bytes(b"x")
        (sub / "疑似水印" / "B_7300000000000000002.mp4").write_bytes(b"x")
        (root / "C_7300000000000000003.mp4").write_bytes(b"x")
        assert existing_ids_under(root) == {
            "7300000000000000001", "7300000000000000002",
            "7300000000000000003"}


# ---------- tests: 集数前缀 / 关键词 ----------

def test_episode_prefix():
    # 前缀零填充：两位数总量补两位，三位补三位
    assert episode_prefix(7, 34) == "07_"
    assert episode_prefix(7, 120) == "007_"
    assert episode_prefix(34, 34) == "34_"


def test_split_keywords():
    assert split_keywords("AI 短剧,AI 动画，ai漫剧") == \
        ["AI 短剧", "AI 动画", "ai漫剧"]
    assert split_keywords(" 单词 ") == ["单词"]
    assert split_keywords("a,,b，") == ["a", "b"]
