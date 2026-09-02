# 抖音单条无水印视频下载器（douyin_dl.py）设计文档

- 日期：2026-09-01
- 状态：设计已获用户批准
- 参考实现依据：[利用 Python+Requests 实现抖音无水印视频下载（腾讯云开发者社区，2025-07-02）](https://cloud.tencent.com/developer/article/2536872)

## 1. 背景与目标

用户希望粘贴一段抖音分享口令/链接，下载对应视频的无水印 mp4，仅用于个人离线保存。

## 2. 范围

**做：**
- 从任意粘贴文本中提取抖音视频链接（`v.douyin.com` 短链、`www.douyin.com/video/{id}` 网页链接）
- 下载单条视频的无水印 mp4
- 幂等：同名文件已存在则跳过

**不做：**
- 批量抓取（作者主页/合集）——需对抗 a_bogus/X-Bogus 签名，复杂度不值
- 图集（检测到即明确报错提示）
- 封面/背景音乐/文案落盘（标题与作者仅打印）
- GUI

## 3. 技术方案（已选：方案 A —— 轻量分享页路由）

备选方案 B（复用 TikTokDownloader 等开源项目，过重）、方案 C（yt-dlp，抖音支持不稳定）已评估并否决。

解析流水线 4 步：

1. **提取链接**：正则从粘贴文本匹配
   `https?://v\.douyin\.com/[A-Za-z0-9_-]+/?`、
   `https?://www\.douyin\.com/video/\d+` 或
   `https?://(?:www\.)?douyin\.com/…[?&]modal_id=\d+`（网页版弹窗形态）；
   多个匹配时取第一个。
2. **拿 aweme_id**：
   - `v.douyin.com` 短链 → GET（iPhone UA，跟随 302 重定向）→ 从最终 URL 提取纯数字 ID；
   - `www.douyin.com/video/{id}` → 直接提取路径 ID；
   - 含 `modal_id={id}` 查询参数 → 提取参数 ID。
3. **拿无水印地址**：GET `https://www.iesdouyin.com/share/video/{id}`（同 UA）
   → **实测(2026-09)：首次请求常只种 `ttwid` cookie 返回无数据壳页面，且有随机性，
   需最多 5 次、间隔 0.8s 重试直到拿到数据**
   → 正则（DOTALL）提取页面内嵌 `window._ROUTER_DATA = {...}` JSON
   → **遍历 loaderData 找含 `videoInfoRes` 的分支**（不做字面 key 匹配，抖音改 key 名不致失效）
   → `item_list[0].video.play_addr.url_list[0]`（主机可能是 `aweme.snssdk.com`），
   将 `playwm` 替换为 `play`。
   **图集判定：实测部分真实视频 item 也带 `images` 字段，故"有 `play_addr` 即视频；
   无 `play_addr` 且有 `images` 才判图集"**。
4. **下载**：GET 无水印地址（同 UA，`stream=True`），8KB 块写盘。

## 4. CLI 接口

```
python douyin_dl.py "<任意含链接的文本>"   # 位置参数 text，可选
python douyin_dl.py                        # 无参数 → 交互式 input() 提示粘贴
python douyin_dl.py ... -o <目录>          # 输出目录，默认见下
python douyin_dl.py --selftest             # 内置断言自测（不联网、不下载）
```

行为细节：
- 默认输出目录 = 脚本所在目录下的 `downloads/`（即 `app\douyin-dl\downloads\`），可用 `-o` 覆盖；从任意 cwd 运行都落在同一处
- 文件名：`{标题清洗后前50字符}_{aweme_id}.mp4`；清洗顺序 = 去换行 → 替换 Windows 非法字符 `\/:*?"<>|` 为空格 → strip → 截前 50 字符；空标题回退 `{aweme_id}.mp4`
- 过程打印：标题、作者昵称、保存路径（不落盘）
- 同名文件已存在 → 打印提示并跳过下载
- 退出码：0 成功 / 1 失败
- 输出统一 UTF-8（必要时 `sys.stdout.reconfigure(encoding="utf-8")`），避免 Windows 控制台中文乱码

## 5. 错误处理

| 情况 | 行为 |
|---|---|
| 文本中无可识别链接 | 报错退出(1)，提示粘贴完整口令 |
| 短链重定向后拿不到数字 ID | 报错退出(1)：视频可能已删除/私密 |
| `_ROUTER_DATA` 缺失 / JSON 解析失败 / 5 次请求均无数据 | 报错退出(1)：分享页结构可能变更或触发风控，建议稍后重试 |
| item 无 `play_addr` 且含 `images` 字段 | 明确提示"图集不支持"，退出(1) |
| HTTP 非 200 / 超时（10s） | 自动重试 2 次（带间隔），仍失败则报错退出(1) |

## 6. 测试与验收

- `--selftest`（纯逻辑内置断言，不联网）：口令提取（含两种链接格式、混排文本、无链接文本）、文件名清洗、loaderData 模糊匹配、图集检测
- 端到端：实现完成后由用户提供一条真实分享链接实测
- 验收标准：
  1. `--selftest` 全部通过
  2. 真实链接 → 产出可播放 mp4，>1MB，画面无抖音水印
  3. 重复运行同一链接 → 跳过，不重复下载

**验收记录（2026-09-01，已通过）**：`--selftest` 20/20；真实视频
（aweme_id=7673851043746221352，modal_id 与 /video/ 两种链接形态）下载成功，
9.9MB、mp4 魔数合法（ftyp/isom）、重复运行正确跳过。画面水印由用户目视最终确认。

## 7. 依赖与环境

- Python：本机 `python`（3.10.10）可用；注意 `python3` 在本机是坏 stub，一律用 `python`
- 第三方依赖仅 `requests`（编码前检查是否已安装）

## 7.5 浏览器兜底路线（2026-09-01 追加，应对分享页 IP 风控）

实测：高强度批量后 iesdouyin 分享页会被 IP 级硬限流（递增退避 35s 仍全壳页）。
对策 —— 双路线自动切换：

- **主路线**：免登录分享页（快、零依赖），保持不变
- **兜底路线**（主路线 5 次重试仍无数据时自动启用）：
  1. 无头 Playwright 持久化浏览器（`.browser-profile/`，需已 `--login`）打开
     `douyin.com/video/{id}`
  2. 拦截 `/aweme/v1/web/aweme/detail/` 响应 → 取 `play_addr.url_list`
     （仅保留 http 开头条目——列表混有 `width=...` 参数串）
  3. 直链（douyinvod CDN，web 接口本身无水印）逐个尝试下载，
     请求头必须带 `Referer: https://www.douyin.com/`（否则 403）
- 下载中途失败**清理半成品**（任意大小），避免下轮幂等误判"已存在"
- 兜底依赖 playwright + 登录态；两者缺失时给出明确安装/登录指引
- 验证记录：分享页限流期间，兜底路线实测下载 17.5MB 合法 mp4；
  douyin_search / douyin_auto 经由 douyin_dl.run 自动受益

## 8. 文件位置

- 脚本：`d:\templet\app\douyin-dl\douyin_dl.py`（单文件）
- 本文档：`d:\templet\app\douyin-dl\docs\2026-09-01-douyin-downloader-design.md`
- 输出目录默认：`d:\templet\app\douyin-dl\downloads\`
- d:\templet 不是 git 仓库，本文档不做 git 提交

## 9. 维护策略

唯一易失效点是第 3 步解析逻辑（收敛在单个函数内）；若抖音改版，仅需修该函数。脚本头部注释注明：仅限个人保存，请尊重创作者版权，勿去水印二传。
