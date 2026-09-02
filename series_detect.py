#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""series_detect.py — 作者主页剧集识别（合集/系列接口都拿不到时的兜底）

两层判定：
1. episode_hint(): 标题集数标记正则（便宜，只做"是否值得深入看主页"的门控）；
2. judge_series(): 把主页全部作品标题(+主页截图)交给 qwen 判定哪些属于目标视频
   的同一部系列，返回 ID 子集（只下载子集，防止把作者日常视频误全下）。
"""
import base64
import json
import re
import sys
import time

import requests

RETRIES = 2

# ---------- 纯逻辑：标题集数预检 ----------

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(s: str):
    """中文数字(≤三位, 如 十四/二十三/一百零五/一百二十三) → int；非法 None。"""
    if not s or any(ch not in CN_NUM and ch != "十" and ch != "百" for ch in s):
        return None
    if "百" in s:
        head, _, rest = s.partition("百")
        base = (CN_NUM.get(head, 1) if head else 1) * 100
        rest = rest.lstrip("零")
        if not rest:
            return base
        sub = cn_to_int(rest)  # 余数递归（不含百，必收敛）
        return None if sub is None else base + sub
    if "十" in s:
        tens, _, ones = s.partition("十")
        return (CN_NUM.get(tens, 1) if tens else 1) * 10 + CN_NUM.get(ones, 0)
    return CN_NUM.get(s)


EP_PATTERNS = [
    re.compile(r"第\s*(\d{1,4})\s*[集期话回卷章篇]"),
    re.compile(r"第\s*([零一二两三四五六七八九十百]+)\s*[集期话回卷章篇]"),
    re.compile(r"(?:^|[^A-Za-z0-9])[Ee][Pp]\s*\.?\s*(\d{1,4})"),
    re.compile(r"(?:^|[^0-9])(\d{1,3})\s*/\s*\d{1,3}(?:$|[^0-9])"),
    re.compile(r"(?:^|[^0-9])(\d{1,3})\s*集"),
    re.compile(r"-\s*0*(\d{1,3})\s*(?:[-#]|$)"),
]


def episode_hint(title: str):
    """标题含集数标记 → 返回集号 int；否则 None。只做门控，允许误报。"""
    for pat in EP_PATTERNS:
        m = pat.search(title or "")
        if not m:
            continue
        g = m.group(1)
        num = cn_to_int(g) if not g.isdigit() else int(g)
        if num is not None and 0 < num < 10000:
            return num
    return None


# ---------- 纯逻辑：判定结果校验 ----------

def validate_series_verdict(verdict: dict, items: list, target_id: str) -> dict:
    """清洗 VLM 输出：丢弃未知 ID、去重；有效分集<2 视为非系列。

    目标视频允许不在主页作品列表里（主页可能只加载到部分作品），
    此时信任 VLM 按标题前缀给出的 ID 子集。
    """
    if not isinstance(verdict, dict):
        return {"is_series": False, "name": "", "ids": [],
                "reason": "输出格式异常"}
    known_order = [it["aweme_id"] for it in items]
    seen, ids = set(), []
    for i in verdict.get("ids") or []:
        if i in known_order and i not in seen:
            seen.add(i)
            ids.append(i)
    if target_id in known_order and target_id not in seen:
        ids.append(target_id)  # 目标在列表中时必须属于结果
    ids.sort(key=known_order.index)          # 恢复发布时间升序
    is_series = bool(verdict.get("is_series")) and len(ids) >= 2
    return {"is_series": is_series,
            "name": (verdict.get("name") or "").strip(),
            "ids": ids if is_series else [],
            "reason": verdict.get("reason", "")}


# ---------- Qwen 调用 ----------

SERIES_PROMPT = """你是短视频编目助手。用户从抖音选中了一条目标视频，下面给出目标视频信息
和该作者主页的作品列表(按发布时间升序，每行格式: 序号|发布日期|标题|视频ID，
目标可能在列表中被【目标】标记；主页可能只加载了部分作品，目标未必在列表里)。
任务: 找出与目标视频属于"同一部剧集/系列"的分集，依据目标标题的系列名前缀判断。
判定要点:
- 同系列通常共享系列名前缀(如【XX回忆录】、《XX》、XX笑传)，或带集数标记
  (第X集/第X期/EP X/-01-/上中下/01-04)
- 同一作者的其他系列(不同系列名前缀)、日常视频、花絮、共创作品都【不算】
- 宁可漏判不可错判: 拿不准的视频不要放进 ids
严格只输出 JSON(不要任何多余文字):
{{"is_series": true或false, "name": "系列名(如【XX回忆录】，没有则空串)", "ids": [属于该系列的全部视频ID], "reason": "简述依据"}}"""


def parse_series_verdict(text: str) -> dict:
    """防御式解析: 剥围栏、截首个 {...}、校验字段。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                  flags=re.IGNORECASE)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"输出中无 JSON: {text[:80]}")
    data = json.loads(m.group(0))
    if not isinstance(data.get("is_series"), bool):
        raise ValueError("缺少 is_series 布尔字段")
    if not isinstance(data.get("ids"), list):
        data["ids"] = []
    return data


def _fmt_items(items: list, target_id: str, target_title: str) -> str:
    lines = [f"目标视频: {target_title or '(无标题)'} | ID: {target_id}",
             "作品列表:"]
    for i, it in enumerate(items, 1):
        ct = it.get("create_time") or 0
        day = time.strftime("%y%m%d", time.localtime(ct)) if ct else "?"
        mark = "【目标】" if it["aweme_id"] == target_id else ""
        lines.append(f"{i}|{day}|{mark}{it.get('title', '')}|{it['aweme_id']}")
    return "\n".join(lines)


def judge_series(target_id: str, target_title: str, items: list, screenshot,
                 api_key: str, base_url: str, model: str) -> dict:
    """主页作品列表(+可选截图) → qwen 判定同系列 ID 子集。

    items: [{aweme_id,title,create_time}]；screenshot: 截图 Path 或 None。
    返回经 validate_series_verdict 清洗后的判定 dict。
    """
    content = []
    if screenshot:
        b64 = base64.b64encode(screenshot.read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {
            "url": f"data:image/jpeg;base64,{b64}"}})
    content.append({"type": "text", "text": SERIES_PROMPT + "\n\n"
                    + _fmt_items(items, target_id, target_title)})
    body = {"model": model,
            "messages": [{"role": "user", "content": content}]}
    last_exc = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body, timeout=120)
            r.raise_for_status()
            raw = parse_series_verdict(
                r.json()["choices"][0]["message"]["content"])
            return validate_series_verdict(raw, items, target_id)
        except Exception as e:  # noqa: BLE001 - 统一重试
            last_exc = e
            if attempt < RETRIES:
                time.sleep(3)
    raise last_exc


# ---------- selftest ----------

def _collect_selftests():
    return sorted(
        (name, fn) for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    )


def run_selftests():
    tests = _collect_selftests()
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
    print(f"selftest: {len(tests) - failed}/{len(tests)} 项通过")
    return failed == 0


def test_cn_to_int():
    assert cn_to_int("十四") == 14
    assert cn_to_int("十") == 10
    assert cn_to_int("二十三") == 23
    assert cn_to_int("一百零五") == 105
    assert cn_to_int("九") == 9
    assert cn_to_int("x十") is None
    assert cn_to_int("") is None


def test_episode_hint_arabic_and_cn():
    assert episode_hint("黄皮子 第14集 超清") == 14
    assert episode_hint("短剧 第十四期：重逢") == 14
    assert episode_hint("XX的日常 第3话") == 3
    assert episode_hint("圣血天使-巴尔的毁灭-02- #战锤40k") == 2
    assert episode_hint("穿越异世界 EP.7") == 7
    assert episode_hint("爱在黎明 3/10") == 3


def test_episode_hint_negative():
    assert episode_hint("【黄皮子回忆录】-初见- #战锤40k") is None
    assert episode_hint("当克里格死亡军团遇到后室 #战锤40k") is None
    assert episode_hint("") is None


def test_parse_series_verdict():
    v = parse_series_verdict(
        '结论：```json\n{"is_series": true, "name": "【回忆录】", '
        '"ids": ["1", "2"], "reason": "同前缀"}\n``` 完')
    assert v["is_series"] is True and v["ids"] == ["1", "2"]
    for bad in ('不是json', '{"ids": []}'):
        try:
            parse_series_verdict(bad)
            assert False, f"应拒绝: {bad}"
        except ValueError:
            pass


def test_validate_series_verdict():
    items = [{"aweme_id": "A", "title": "t1", "create_time": 1},
             {"aweme_id": "B", "title": "t2", "create_time": 2},
             {"aweme_id": "C", "title": "t3", "create_time": 3}]
    v = validate_series_verdict(
        {"is_series": True, "name": "系列X", "ids": ["C", "B", "B", "Z"],
         "reason": "r"}, items, "B")
    assert v["is_series"] and v["ids"] == ["B", "C"]  # 未知剔除+时间序
    # 目标不在 ids → 补进去；只有目标一条 → 不算系列
    v2 = validate_series_verdict(
        {"is_series": True, "ids": [], "reason": "r"}, items, "A")
    assert v2["is_series"] is False
    # 判否 → ids 清空
    v3 = validate_series_verdict(
        {"is_series": False, "ids": ["A", "B"], "reason": "r"}, items, "A")
    assert v3["is_series"] is False and v3["ids"] == []
    # 异常输入
    assert validate_series_verdict("junk", items, "A")["is_series"] is False


def test_fmt_items_marks_target():
    out = _fmt_items([{"aweme_id": "X", "title": "甲", "create_time": 0},
                      {"aweme_id": "Y", "title": "乙", "create_time": 0}],
                     "Y", "乙")
    assert out.startswith("目标视频: 乙 | ID: Y")
    assert "【目标】乙|Y" in out and "甲|X" in out


def test_validate_target_absent_from_items():
    # 主页只加载部分作品、目标不在列表 → 信任 VLM 给的 ID 子集
    items = [{"aweme_id": "A", "title": "【回忆录】一", "create_time": 1},
             {"aweme_id": "B", "title": "【回忆录】二", "create_time": 2}]
    v = validate_series_verdict(
        {"is_series": True, "name": "【回忆录】", "ids": ["A", "B"],
         "reason": "同前缀"}, items, "Z")
    assert v["is_series"] and v["ids"] == ["A", "B"]


def main(argv=None):  # pragma: no cover - 手动自测入口
    if argv is None:
        argv = sys.argv[1:]
    if "--selftest" in argv:
        sys.exit(0 if run_selftests() else 1)
    print(__doc__)


if __name__ == "__main__":
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
