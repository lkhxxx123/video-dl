# AI 水印检测分流器（watermark_filter.py）设计文档

- 日期：2026-09-01
- 状态：设计已获用户批准（路线 B：阿里云百炼 Qwen-VL，OpenAI 兼容接口）
- 关联：检测对象是 douyin_dl/douyin_search 下载的视频

## 1. 背景与目标

部分视频作者会自行叠加"移动水印"（账号 ID、引流文字、半透明 logo，位置随时间漂移）。
目标：AI 识图检测这类水印，把有水印的视频**转移**到隔离目录（不删除，可人工复核）。

## 2. 技术方案

- 抽帧：`ffprobe` 取时长 → 均匀采 N 帧（默认 6，跳过首尾 3%）→ `ffmpeg -ss {t}`
  各抽 1 帧，缩放 `scale=640:-2` 输出 jpg（控制 Base64/Token 体积）
- 识图：阿里云百炼 OpenAI 兼容 `chat/completions`，**一条消息多图**
  （`content` 数组：N 个 `image_url`(Base64 Data URL) + 1 个 text 提示词）
- 提示词要求模型区分：
  - **算**作者水印：账号名/抖音号、公众号/引流文字、半透明 logo、跑马灯；
    位置可能在帧间**移动**（移动水印判定性特征）
  - **不算**：抖音平台角标/UI、底部居中硬字幕、画面场景内自然文字（招牌/标题动画）
- 输出 JSON：`{"has_author_watermark": bool, "moving": bool,
  "desc": "水印内容与位置简述", "frames_with_watermark": [帧序号]}`
  —— 解析做防御式处理（剥离 ```json 围栏、截取首个 `{...}`）
- 有水印 → `shutil.move` 到 `<视频目录>/疑似水印/`；全部结果写
  `watermark_report.json` + 控制台逐条打印

## 3. CLI

```
python watermark_filter.py <目录>                # 检测并转移
python watermark_filter.py <目录> --dry-run      # 只检测不移动
python watermark_filter.py <目录> --frames 8 --model qwen-vl-plus
```

| 配置 | 默认 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` 环境变量 | 必填（--dry-run 除外） | 百炼 API Key |
| `--model` | `qwen-vl-max` | 可换 `qwen-vl-plus` 等 |
| `--base-url` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 百炼兼容端点；可传 WorkspaceId 专属域名 |
| `--frames` | 6 | 每视频抽帧数（越多漏检越少、成本越高） |

## 4. 错误处理

| 情况 | 行为 |
|---|---|
| 缺 DASHSCOPE_API_KEY（非 dry-run） | 报错退出(1)，提示 set |
| ffprobe/ffmpeg 失败或时长为 0 | 该视频标记 error，继续下一个 |
| API 调用失败（网络/4xx/5xx） | 重试 2 次（间隔 3s），仍失败标记 error |
| 模型返回非 JSON | 防御解析；仍失败标记 error（不误转移） |
| 目标目录已有同名文件 | 移动时加时间戳后缀，不覆盖 |

## 5. 测试与验收

- `--selftest`（离线）：采样时间点计算、防御式 JSON 解析、重名后缀生成
- 端到端：① `--dry-run` 对已下载 10 个视频跑通抽帧（不调 API）；
  ② 设 API key 后真实检测，人工抽查判定质量（误判/漏检）可接受后转正式用
- 诚实预期：判定非 100% 准确，故用隔离目录 + dry-run 预览；帧数可调

## 6. 依赖与文件

- 脚本：`d:\templet\app\douyin-dl\watermark_filter.py`（单文件）
- 依赖：ffmpeg/ffprobe（本机已有 8.0）、requests（已有）；**不装 openai SDK**
- 报告：`<视频目录>/watermark_report.json`
