# 一条龙流水线（douyin_auto.py）设计文档

- 日期：2026-09-01
- 状态：已获用户批准并实现
- 组合复用：douyin_search（搜索筛选）+ douyin_dl（无水印下载）+ watermark_filter（AI 验水印）

## 1. 目标

一条命令产出 N 条"通过筛选器 且 AI 判定无作者水印"的干净视频；多次执行自动跳过
已处理视频，直至凑够或候选耗尽。

## 2. CLI

```
run_auto.bat "AI 短剧,AI 动画短片,ai漫剧" --limit 50 \
    --max-followers 10000 --max-duration 120 --max-likes 500000
```
（--limit 含义 = 最终干净视频数；其余参数同 douyin_search；需 DASHSCOPE_API_KEY）

## 3. 执行流程

```
① 纳管历史:
   - 疑似水印\ 目录中的文件 → 直接记为 watermarked（含手动 run_wm 移入的）
   - 主目录中未判定的 mp4 → 逐个 AI 判定（验旧）
② 循环直到 干净数 ≥ limit:
   - collect_many(keywords, 缺口, filters, seen=已处理集) 拉新候选
   - 逐候选: 下载 → 抽帧 → Qwen-VL 判定
     有水印 → move 到 疑似水印\；干净 → 计入
   - 本轮无进展 / 关键词翻尽 → 结束
③ 汇总: 干净 X/limit、水印移走 Y、已处理 Z
```

## 4. 幂等与去重（跨执行持久）

- **状态文件** `out_dir/auto_state.json`：`{aweme_id: {verdict, desc}}`
  （clean / watermarked / skip；error 与识图失败不落状态，下轮自动重试）
- **文件名兜底**：状态丢失时按目录内 `*_{id}.mp4` 文件名重建已处理集合
  （douyin_dl 命名规则保证尾部含 ID）
- 下载失败/识图失败：文件与状态不动，重跑命令自动重试

## 5. 错误处理

| 情况 | 行为 |
|---|---|
| 缺 DASHSCOPE_API_KEY | 退出(1) 提示 set |
| 搜索全程失败（验证码未滑等） | 结束本轮并提示重跑 |
| 图集（ParseError） | 记 skip，不重试 |
| 下载网络失败 | 不记状态，下轮重试 |
| 识图失败 | 文件保留，下轮重判 |
| 疑似水印目录重名 | unique_dest 加 -N 后缀 |

## 7. 剧集全集 + 作者黑名单（2026-09-02 追加，已获用户批准）

- **剧集判定**：视频数据携带官方合集 `mix_info`（mix_id/mix_name/集数）；
  同一 mix_id = 同一部剧集。`parse_search_response` 提取 mix_id 等字段。
- **拉全集**：`collect_mix(video_id)` 打开视频页，拦截
  `/aweme/v1/web/mix/aweme/` 响应收集全部集（滚动触发面板分页），按集数排序。
- **auto 集成**：候选命中剧集（有 mix_id）→ 自动拉全部集 → 每集独立
  下载+验水印+计数（用户决策：**每集算 1 条**）；剧集内**豁免三条件筛选**
  （选定即整部拿全，可能超出 limit）；全局去重照常防重。
- **手动模式**：`python run.py mix "<任意一集链接>"`（有 Key 验水印，无 Key 纯下载）。
- **作者黑名单**：`--block-keywords`（search/auto 均支持），默认词表
  `搬运/侵权/联系删除/如有侵权/仅供欣赏/仅供学习/转载/免责`，
  匹配作者简介/昵称/视频标题，命中即跳过（过滤搬运/侵权类账号）；传空串禁用。
- selftest 16/16（新增 mix 解析/黑名单判定/字段提取 3 项）。

## 6. 验证记录（2026-09-01）

selftest 2/2（ID 提取、状态读写/按 ID 找文件）；四脚本 selftest 全绿
（auto 2/2、search 10/10、wm 4/4、dl 20/20）；真实目录冒烟：搜索批量 26 个
文件全部解析出视频 ID。端到端（含 API 识图与断点续跑）由用户启动后验收。
