#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""core.state — 处理状态机 / 全局去重 / 状态自愈。

原 douyin_auto(auto_state.json) 与 douyin_clips(clips_state.json) 两套
重复实现合并为一套，filename 参数区分状态文件，互不串账。
"""
import json
from pathlib import Path

from .reporting import log
from .naming import ID_TAIL_RE, existing_ids_under
from .paths import DOWNLOADS_DIR
from . import selftest as _st

DEFAULT_STATE_FILE = "auto_state.json"


def ids_from_filenames(*dirs) -> set:
    """从各目录 mp4 文件名尾部的 _{id}.mp4 提取视频 ID（断状态丢失后的兜底）。"""
    ids = set()
    for d in dirs:
        if d and Path(d).is_dir():
            for f in Path(d).glob("*.mp4"):
                m = ID_TAIL_RE.search(f.name)
                if m:
                    ids.add(m.group(1))
    return ids


def load_state(out_dir: Path, filename: str = DEFAULT_STATE_FILE) -> dict:
    p = out_dir / filename
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data.get("processed"), dict):
                return data
        except Exception:
            pass
    return {"processed": {}}


def save_state(out_dir: Path, state: dict,
               filename: str = DEFAULT_STATE_FILE) -> None:
    (out_dir / filename).write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def reconcile_state(out_dir: Path, state: dict,
                    filename: str = DEFAULT_STATE_FILE) -> int:
    """状态自愈：clean/watermarked 记录对应的文件已不存在 → 删除记录。

    让"删目录=可重跑"成立（否则删了文件状态仍拦着不下）。
    skip（图集等语义跳过）不依赖文件存在，保留。返回清理条数。
    """
    alive = existing_ids_under(DOWNLOADS_DIR)
    drop = [vid for vid, v in state["processed"].items()
            if v.get("verdict") in ("clean", "watermarked")
            and vid not in alive]
    for vid in drop:
        del state["processed"][vid]
    if drop:
        save_state(out_dir, state, filename)
        log(f"[状态清理] {len(drop)} 条记录的文件已不存在，"
              f"已重置（可重新下载）")
    return len(drop)


def find_by_id(out_dir: Path, aweme_id: str):
    """按文件名尾部 ID 在目录中定位 mp4。"""
    for f in out_dir.glob(f"*_{aweme_id}.mp4"):
        return f
    return None


# ---------- selftest ----------

def run_selftests():
    return _st.run_selftests(globals())


# ---------- tests ----------

def test_ids_from_filenames():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "标题A_7300000000000000001.mp4").write_bytes(b"x")
        (d / "B_7300000000000000002.mp4").write_bytes(b"x")
        (d / "无ID文件.mp4").write_bytes(b"x")
        (d / "其他.txt").write_bytes(b"x")
        sub = d / "疑似水印"
        sub.mkdir()
        (sub / "C_7300000000000000003.mp4").write_bytes(b"x")
        ids = ids_from_filenames(d, sub)
        assert ids == {"7300000000000000001", "7300000000000000002",
                       "7300000000000000003"}, ids


def test_state_roundtrip_and_find_by_id():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        state = load_state(d)
        assert state == {"processed": {}}
        state["processed"]["123"] = {"verdict": "clean"}
        save_state(d, state)
        assert load_state(d) == state
        f = d / "某标题_1234567890123456789.mp4"
        f.write_bytes(b"x")
        assert find_by_id(d, "1234567890123456789") == f
        assert find_by_id(d, "9999999999999999999") is None
