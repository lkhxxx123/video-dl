# 抖音无水印批量下载工具集

仅限个人离线保存；请尊重创作者版权，勿去水印二次上传。

## 环境

- Python 3.10+（命令一律 `python`）
- `pip install playwright && playwright install chromium`（搜索/剧集需要）
- ffmpeg / ffprobe（水印检测抽帧，需在 PATH）
- `key.txt`：**只放一行 Key 本体**（任何 OpenAI 兼容服务商）
- 识图默认：MiniMax `MiniMax-M3`（Anthropic 协议端点，与订阅 Key 配套）；
  换百炼时加 `--model qwen3.8-flash --base-url <百炼兼容端点>` 并换 key

统一入口：`python run.py <模式>`，所有模式共用：滑块验证需人在场、
风控自动切浏览器兜底、全局去重（文件名尾部视频 ID）、断点续跑。

---

## ① 合集下载（douyin_jx.py）

按用户手动流程：精选搜索 → 作者主页合集页 → 点进具体合集滑动拉全 → 逐集下载。

```cmd
python run.py jx "奇闻故事,睡前故事,神话故事,聊斋故事,历史故事,人间真实,剧情演绎,沉浸式剧情,脑洞故事,反转剧情" --limit 100 --max-duration 1000 --max-ep-duration 1500 --max-likes 50000 --frames 6 --model MiniMax-M3 --base-url https://api.minimaxi.com/anthropic
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `"关键词"` | — | 逗号分隔多个 |
| `--limit N` | 10 | **成功保留的剧数**（弃用/已完整不占配额） |
| `--max-likes / --max-followers / --max-duration` | 不限 | 严格小于 |
| `--block-keywords` | 内置词表 | 搬运/侵权号黑名单（简介/昵称/标题） |
| `--frames N` | 6 | 每条抽帧数 |
| `--sample N` | 2 | 采样提速：前 N + 后 N 集，`0` 关闭 |
| `--max-ep-duration S` | 600 | **合并总集拦截**：单集超 S 秒的占比 ≥60% 判弃（"一口气/1-N集合集"类）；`0` 关闭。与入口筛选 `--max-duration` 独立 |

**行为要点**：
- 采样 4 集（前2+后2）任一有水印 → **弃剧**（识图 ≤4 次）
- 全干净 → 中间集只下载不判定
- 弃剧双标记：目录改名 `有水印弃用-剧名(N集)` + `watermark_skip.json`
  名单（重跑 0 成本跳过；删条目可重新判）
- 成功保留目录名 `剧名(N集)`，集文件 `01_标题_@作者_ID.mp4`（按名排序即观看顺序）
- 输出：`downloads\日期\剧集\`

## ② 散片下载（douyin_clips.py）

非剧集短视频，独立流程：root 搜索 → 按小时分桶 → 每条 3 帧验水印。

```cmd
python run.py clips "奇闻异事,白天刷不到晚上逃不掉系列,凌哈哈故事会,灵异故事,恐怖故事,民间故事正能量,胆小慎入,都市传说,志怪故事" --limit 1000 --max-duration 1000 --max-likes 50000 --model MiniMax-M3 --base-url https://api.minimaxi.com/anthropic
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `"关键词"` | — | 搜索入口 `www.douyin.com/root/search/{关键词}?type=general` |
| `--limit N` | 10 | 目标**干净散片数**（水印的不计数） |
| `--max-likes / --max-followers / --max-duration` | 不限 | 同上 |
| `--block-keywords` | 内置词表 | 同上 |

**行为要点**：
- 输出 `downloads\日期\散片\HH点MM分\`（与"剧集"同级；按下载时刻
  **10 分钟分桶**，每小时 6 个目录）
- 每条验水印**只抽 3 帧**（`douyin_clips.py` 顶部 `FRAMES=3` 可改）
- 有水印（作者水印 **或抖音平台标识**）→ 该 10 分钟目录下 `疑似水印\`
- 文件名 `标题_@作者_ID.mp4`（散片无集数前缀）

---

## 依赖库（勿删，两个脚本共用）

`douyin_dl.py`(下载引擎/风控兜底) `douyin_search.py`(浏览器/搜索/筛选)
`douyin_auto.py`(状态/判定) `watermark_filter.py`(识图)
`series_detect.py`(集数标记识别)

## 常见问题

- **滑块/扫码**：首次运行会弹浏览器自动引导扫码登录（登录态存
  `.browser-profile\`，一般 1~2 周有效，过期后下次运行自动再弹）；
  搜索过程中的滑块需人在场完成
- **换识图服务商**：`--model` + `--base-url` 成套换（base-url 含
  `/anthropic` 自动走 Anthropic 协议，如 MiniMax M3），key.txt 同步换
- **删除目录后重跑**：状态自动清理，可重新下载
- **断点续跑**：任何模式中断后重跑同命令自动续
