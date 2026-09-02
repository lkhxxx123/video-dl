# douyin_search.py 结果筛选参数实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为搜索批量下载增加三个筛选启动参数：`--max-followers N`、`--max-duration S`、`--max-likes N`（严格小于才保留，默认不限）。

**Architecture:** `parse_search_response` 从返回 `(id, title)` 元组改为返回含 `digg/duration_ms/followers` 的 dict；新增纯函数 `passes_filter` 做筛选判定；`collect_ids` 在拦截回调里逐条过滤、按"合格数"控制滚动停止；`download_all`/`main` 适配 dict 与新参数。

**Tech Stack:** 不变（Python 3.10 `python` 命令、playwright、requests）

**Spec:** `douyin-search-design.md` §8（2026-09-01 追加）

## Global Constraints

- Python 命令一律 `python`；非 git 仓库无 commit 步骤
- 三参数默认 None=不过滤，不传时行为与旧版一致
- 筛选语义：严格小于（`值 >= 阈值` 即拒绝）；启用的筛选条件遇字段未知即拒绝并注明原因
- 文件：只改 `d:\templet\app\douyin-dl\douyin_search.py`
- `--selftest` 全部离线

---

### Task 1: parse_search_response 返回 dict（含筛选字段）

**Files:** Modify `douyin_search.py`

**Interfaces:**
- Produces: `parse_search_response(payload: dict, seen: set) -> list[dict]`，
  元素为 `{"aweme_id": str, "title": str, "digg": int|None,
  "duration_ms": int|None, "followers": int|None}`；
  粉丝取值链 `author.follower_count` → `author.mplatform_followers_count` → None；
  时长 `video.duration` → 顶层 `duration` → None

- [ ] **Step 1: 改造既有 3 个解析测试到 dict 形态（先失败）**

用以下内容**替换**现有 `test_parse_search_response_aweme_list_shape`、
`test_parse_search_response_legacy_data_shape`（`test_parse_search_response_empty_or_broken` 不变）：

```python
def test_parse_search_response_aweme_list_shape():
    payload = {"status_code": 0, "aweme_list": [
        {"aweme_id": "111", "desc": "标题A",
         "statistics": {"digg_count": 5},
         "video": {"duration": 90000},
         "author": {"follower_count": 200}},
        {"aweme_id": "111", "desc": "重复"},   # 重复 ID 去重
        {"aweme_id": "222", "desc": ""},        # 无统计字段的条目
        {"log_pb": {}},                          # 无 ID 条目丢弃
    ]}
    seen = set()
    got = parse_search_response(payload, seen)
    assert got == [{"aweme_id": "111", "title": "标题A", "digg": 5,
                    "duration_ms": 90000, "followers": 200}]
    assert parse_search_response(payload, seen) == []


def test_parse_search_response_legacy_data_shape():
    # 兼容旧形态 data[].aweme_info；粉丝回退 mplatform_followers_count
    payload = {"data": [{"aweme_info": {
        "aweme_id": "333", "desc": "标题C",
        "statistics": {"digg_count": 7},
        "author": {"mplatform_followers_count": 999}}}]}
    got = parse_search_response(payload, set())
    assert got[0]["aweme_id"] == "333" and got[0]["digg"] == 7
    assert got[0]["followers"] == 999 and got[0]["duration_ms"] is None
```

- [ ] **Step 2:** Run `python douyin_search.py --selftest` → 上述 2 项 FAIL
- [ ] **Step 3: 实现新解析**

```python
def parse_search_response(payload: dict, seen: set):
    """从搜索响应提取新增条目 dict；去重，丢弃无 ID 条目。

    实测(2026-09)：条目在顶层 aweme_list（元素即 aweme 对象）；
    兼容旧形态 data[].aweme_info。返回 dict 含筛选所需字段。
    """
    results = []
    entries = list(payload.get("aweme_list") or []) + \
        list(payload.get("data") or [])
    for entry in entries:
        info = entry.get("aweme_info") or entry
        aweme_id = info.get("aweme_id")
        if not aweme_id or aweme_id in seen:
            continue
        seen.add(aweme_id)
        author = info.get("author") or {}
        followers = author.get("follower_count")
        if followers is None:
            followers = author.get("mplatform_followers_count")
        duration_ms = (info.get("video") or {}).get("duration")
        if duration_ms is None:
            duration_ms = info.get("duration")
        results.append({
            "aweme_id": aweme_id,
            "title": info.get("desc") or "",
            "digg": (info.get("statistics") or {}).get("digg_count"),
            "duration_ms": duration_ms,
            "followers": followers,
        })
    return results
```

- [ ] **Step 4:** Run → selftest 7/7 通过

### Task 2: passes_filter 筛选函数

**Files:** Modify `douyin_search.py`

**Interfaces:**
- Produces: `passes_filter(item: dict, max_followers=None, max_duration=None,
  max_likes=None) -> tuple[bool, str]`（通过时 `(True, "")`；拒绝时原因如
  `"粉丝 10000 >= 10000"` / `"时长 120s >= 120s"` / `"点赞 1000 >= 1000"` /
  `"粉丝数未知"` 等）

- [ ] **Step 1: 写失败测试**

```python
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


def test_passes_filter_unknown_rejects_only_when_active():
    assert passes_filter(_item(followers=None), max_followers=100)[0] is False
    assert passes_filter(_item(digg=None), max_likes=100)[0] is False
    assert passes_filter(_item(duration_ms=None), max_duration=60)[0] is False
    assert passes_filter(_item(followers=None, digg=None,
                               duration_ms=None))[0] is True
```

- [ ] **Step 2:** Run → 新增 3 项 FAIL（8/11 之类）
- [ ] **Step 3: 实现（追加到 is_verify_block 之后）**

```python
def passes_filter(item: dict, max_followers=None, max_duration=None,
                  max_likes=None):
    """筛选判定：严格小于才保留；启用的条件遇字段未知即拒绝。"""
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
```

- [ ] **Step 4:** Run → 10/10 通过

### Task 3: collect_ids 集成 + download_all/main 适配

**Files:** Modify `douyin_search.py`（浏览器层与编排、CLI）

**Interfaces:**
- `collect_ids(keyword, limit, max_followers=None, max_duration=None,
  max_likes=None) -> list[dict]`：拦截回调内逐条 `passes_filter`，不合格打印
  `  跳过: {标题前24字}（{原因}）`；初始等待以"收到任一新条目(raw)"为准；
  滚动停止以 raw 增量判 idle、以 `len(kept) >= limit` 判完成；结束时打印
  `筛选后合格 X / 共 Y 条`
- `download_all(items, out_dir)`：解包改为 `it["aweme_id"], it["title"]`
- `main`：新增三个 argparse 参数并透传；任一启用时打印 `筛选: 粉丝<N 时长<Ns 赞<N>`

- [ ] **Step 1: 实现（collect_ids 的 seen/items 部分替换 + download_all 解包 + main 参数）**
- [ ] **Step 2:** Run `python douyin_search.py --selftest` → 10/10；
  Run `python douyin_search.py --help` → 帮助含三个新参数

### Task 4: 端到端验收（需用户滑块）

- [ ] Run: `python douyin_search.py "AI 短剧" --limit 3 --max-followers 10000 --max-duration 120 --max-likes 1000`
- 预期：滑块验证后出现若干"跳过: …（原因）"行；最终下载 ≤3 个满足条件的视频；
  若全部"粉丝数未知"→ 明确汇总，回报用户再定兜底
- 幂等复跑一次：全部"已存在，跳过"

---

## Self-Review 记录

1. **Spec 覆盖**：三参数与默认不限(T3)、取值链与 dict 字段(T1)、严格小于与未知拒绝(T2)、
   跳过原因打印与合格数停止(T3)、整批未知提示(T4 观察)、selftest 覆盖(T1/T2)——齐全。
2. **占位符**：Task 3 Step 1 为多处小改动的集合，具体代码在执行时按 Interfaces 精确给出
   （涉及 collect_ids/download_all/main 三处的替换块，均为已定稿代码的搬运）。
3. **命名一致**：`passes_filter`/`parse_search_response`/`collect_ids`/`download_all`
   签名前后一致；dict 键 `aweme_id/title/digg/duration_ms/followers` 全程统一。
