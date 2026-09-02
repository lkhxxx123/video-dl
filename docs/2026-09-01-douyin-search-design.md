# 抖音关键词搜索批量下载器（douyin_search.py）设计文档

- 日期：2026-09-01
- 状态：设计已获用户批准
- 关联：`2026-09-01-douyin-downloader-design.md`（单条下载器，本设计零改动复用它）

## 1. 背景与目标

用户希望按关键词（如"AI 短剧"）搜索抖音视频并逐条批量下载无水印 mp4。

**硬约束（已核实）**：抖音 web 搜索接口 `aweme/v1/web/search/item` 必须携带
a_bogus 签名（字节 JSVMP 混淆、与 UA 绑定）+ `msToken`/`ttwid` cookie，且需登录态。
参考：[a_bogus 算法还原](https://www.wuyunai.com/forum-post/878.html)、
[2025-08 最新版参数构造](https://haloowhite.com/2025/08/18/dy-vmp-tutorial/)。

**选定路线（方案 A）**：Playwright 真浏览器自动化 —— 签名由浏览器原生计算，
完全绕开逆向；登录扫码一次，登录态持久化。备选 B（纯接口+自带签名，维护成本最高）、
C（借用 TikTokDownloader，重依赖）已评估并否决。

## 2. 范围

**做：**
- `python douyin_search.py "关键词" --limit N`：搜索 → 逐条无水印下载
- 首次运行自动引导扫码登录；`--login` 单独登录/续期
- 结果幂等去重（同名文件已存在即跳过）

**不做（YAGNI）：**
- 排序/时间/时长筛选（默认综合排序，`type=video`）
- 图集下载（单条跳过并提示）
- GUI、代理池

## 3. 架构与数据流

新文件 `douyin_search.py`（约 150 行），**单向依赖** `douyin_dl.py`（零改动）：

```
douyin_search.py
├── collect_ids(keyword, limit) -> list[(aweme_id, title)]
│   Playwright Chromium 持久化上下文（.browser-profile/），统一有头模式
│   ① 登录检测：context cookies 中无 sessionid → 弹出抖音首页并提示扫码，
│      轮询等待 sessionid 出现，超时 120s
│   ② page.goto("https://www.douyin.com/search/{quote(keyword)}?type=video")
│   ③ page.on("response") 拦截 URL 匹配 /aweme/v1/web/search/item/ 的 XHR 响应
│      → json 解析：遍历 data[]，取 aweme_info.aweme_id 与 aweme_info.desc；
│        无 aweme_id 的条目（广告/直播卡）丢弃；按 aweme_id 去重
│   ④ 收够 limit 条停止；或连续 3 次滚动（每次滚动后等 1.5s 收响应）无新增即停
└── 下载编排：
    for aweme_id → douyin_dl.run(f"https://www.douyin.com/video/{aweme_id}",
                                 downloads/{关键词}/)
    单条 ParseError（图集等）/网络失败 → 打印"跳过: 原因"并继续
    条间隔随机 1~2s；结束打印汇总：成功 X / 跳过 Y / 失败 Z
```

## 4. CLI 接口

```
python douyin_search.py "AI 短剧"            # --limit 默认 10
python douyin_search.py "AI 短剧" --limit 20
python douyin_search.py --login              # 只登录不搜索
python douyin_search.py --selftest           # 内置断言（不联网、不开浏览器）
```

- 输出目录：`downloads/{关键词}/`；文件名沿用 douyin_dl 规则（`标题_ID.mp4`）
- 退出码：0 成功（含部分跳过）/ 1 致命错误（未登录超时、无搜索结果等）
- 控制台输出 UTF-8

## 5. 错误处理

| 情况 | 行为 |
|---|---|
| playwright 未安装 / Chromium 未下载 | 打印确切安装命令（pip install playwright; playwright install chromium）后退出(1) |
| 扫码超时（120s 无 sessionid） | 报错退出(1)，提示重跑 `--login` |
| 30s 内未拦截到任何搜索响应 | 报错退出(1)：页面可能改版或触发风控 |
| 搜索结果 < limit | 下载已拿到的条目，提示实际数量 |
| 单条为图集 / 下载失败 | 打印跳过原因，继续下一条，末尾汇总 |
| 已下载过的视频 | douyin_dl 幂等跳过 |

## 6. 测试与验收

- `--selftest`（离线，fixture 用真实响应结构样本）：搜索响应 JSON → (id, title)
  列表解析（含广告条目丢弃、去重）；登录检测的 cookie 判定逻辑
- 端到端验收（真实浏览器 + 真实账号扫码）：
  1. `--login` 扫码成功，`.browser-profile/` 持久化
  2. `douyin_search.py "AI 短剧" --limit 5` → 下载 ≥3 个可播放 mp4（>1MB），
     汇总正确
  3. 重复运行同一命令 → 全部"已存在，跳过"
  4. 画面无水印由用户目视最终确认

## 7. 依赖与文件

- 新依赖：`playwright`（`pip install playwright && playwright install chromium`）
- 文件：`d:\templet\app\douyin-dl\douyin_search.py`；登录态目录
  `.browser-profile/`（**含账号 cookie，敏感，勿外传/勿提交任何仓库**）
- Python 命令一律 `python`；requests 沿用

## 8. 结果筛选参数（2026-09-01 追加，已获批准）

```
python douyin_search.py "关键词" --limit N \
    --max-followers 10000 \   # 作者粉丝数 < N（严格小于）
    --max-duration 120 \      # 视频时长 < S 秒
    --max-likes 1000          # 点赞数 < N
```

- 三参数默认 `None` = 不过滤（行为与旧版完全一致）
- 数据来源（2026-09 实测/社区核实）：点赞 `statistics.digg_count`、
  时长 `video.duration`（毫秒，兼容顶层 `duration`）、
  粉丝取值链 `author.follower_count` → `author.mplatform_followers_count` → 未知
- `parse_search_response` 返回 `dict(aweme_id, title, digg, duration_ms, followers)`
- 不合格项打印一行跳过原因（如 `跳过: xxx（粉丝 12345 >= 10000）`）；
  收集循环持续滚动直到**合格数**达 limit 或连续 3 次滚动无新结果
- 字段未知的处理：该筛选条件启用时，未知即跳过并计数；若整批均为
  "粉丝数未知"，汇总明确提示接口缺字段（届时另行确认兜底方案：
  浏览器内顺带访问作者主页拦截粉丝数）
- `--selftest` 覆盖筛选函数与解析扩展的离线断言

## 10. 多关键词聚合批量（2026-09-01 追加，已实现）

- `keyword` 位置参数支持**中英文逗号分隔多关键词**（`split_keywords`）
- **开一次浏览器**逐词搜索（同页签跳转，避免每词重新开窗+验证码）：
  全局按 aweme_id 去重合并，**合格数凑够 limit 即停**；单词验证超时/无数据
  只告警并跳到下一词，不中断整批；词间随机降温 3~5s
- 输出目录：单词 = `downloads/{词}/`；多词 = `downloads/搜索批量/`
- **下载失败自动补漏**：首轮失败的条目在整轮结束后等 10s 统一重试一轮，
  汇总报告"补漏救回 N"；仍失败的靠幂等重跑续传
- selftest 10/10（新增 split_keywords 断言）

**筛选功能验收记录（2026-09-01，已通过）**：selftest 9/9；真实搜索三轮实测：
① `赞<1000` 翻尽 440 条全部按真实点赞数淘汰（最低 1494），空结果优雅收尾；
② `赞<50万+时长<120s+粉丝<1W` —— 三个筛选器全部以真实数据触发
（淘汰原因含真实时长 153~1052s、真实粉丝 14021 等），最终下载 2 条合格视频
（9~10MB），单条分享页风控被优雅跳过不影响批次。**搜索响应确认携带
follower_count 字段**（取值链首选项有效）。

## 9. 维护策略

最脆弱两点均收敛在 `collect_ids` 内：搜索接口 URL 匹配（一个正则）与登录检测
（sessionid 判定）；抖音改版只需修该函数。批量下载他人作品仅限个人保存，
尊重创作者版权，勿二次上传。
