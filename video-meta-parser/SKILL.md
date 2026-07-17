---
name: video-meta-parser
description: >
  解析抖音、快手、哔哩哔哩的单个或批量视频链接，统一提取元信息、下载视频并转录音频。
  当用户提供或明确提及抖音（v.douyin.com/douyin.com）、快手（v.kuaishou.com/kuaishou.com）、
  B站（b23.tv/bilibili.com/BV 号）的链接，或者可直接识别的 BVID，并要求解析元信息、作者、播放/点赞数据、
  下载视频或音频转录时使用。不要因为泛化的“下载视频”或“音频转录”请求而对其他平台或本地文件触发本技能。
  如果还需要生成视频画面和内容描述，再配合 video-content-parser。
---

# 视频元信息解析

使用 `scripts/meta_parser.py` 完成视频 ID 解析、元信息提取、视频下载和音频转录。本技能不生成视频画面内容描述。

## 运行

处理单个链接：

```bash
python3 <skill-path>/scripts/meta_parser.py "<URL>" \
  --output-dir <workspace>/videos \
  --cookies-dir <workspace> \
  --whisper-model base \
  --hf-endpoint https://hf-mirror.com
```

处理批量链接：

```bash
python3 <skill-path>/scripts/meta_parser.py --input-file <URL文件> \
  --output-dir <workspace>/videos \
  --cookies-dir <workspace> \
  --concurrent 8 \
  --batch-size 100 \
  --whisper-model base \
  --hf-endpoint https://hf-mirror.com
```

一键重试已有产物中的网络类失败，并把成功改善的记录合并回原产物：

```bash
python3 <skill-path>/scripts/meta_parser.py \
  --retry-from <output-dir> \
  --cookies-dir <workspace> \
  --concurrent 8 \
  --batch-size 100 \
  --whisper-model base \
  --hf-endpoint https://hf-mirror.com
```

- URL 文件每行一个链接，忽略空行和以 `#` 开头的注释。
- `--cookies-dir` 中可放置 `cookies-douyin.txt`、`cookies-kuaishou.txt`、`cookies-bilibili.txt`；缺失时匿名访问。
- `--whisper-model` 默认为 `base`，可使用 `tiny`、`small`、`medium`、`large`。
- `--concurrent` 默认为 8。大模型或 CPU/内存受限时降低并发数。

## 批量流程

1. 并发解析输入链接，生成 `video_id` 和规范化 `source_url`。
2. 按“平台 + `video_id`”去重，处理每个唯一视频。不要在外部预解析或重复调用短链。
3. 保留所有原始链接到唯一视频的映射。单个链接失败不中断其他任务。

## 统一元信息

每个 `meta_path` 指向的 `元信息.json` 使用以下字段：

| 字段 | 含义 |
|---|---|
| `id` | 抖音 aweme_id、快手 photoId 或 BVID |
| `title` / `desc` | 标题和内容描述 |
| `publish_time` | 北京时间 |
| `play_count` / `like_count` / `comment_count` / `share_count` | 统一的互动数据 |
| `author` | 作者名称 |
| `source_url` | 规范化长链 |
| `transcription` | 语言、置信度和分段文本；下载、音频提取或转录失败时为 `null` |
| `metadata_success` / `download_success` | 元信息、视频下载阶段是否成功 |
| `audio_extract_success` / `transcription_success` | 音频提取、音频转录阶段是否成功 |
| `stage_errors` | `metadata`、`download`、`audio_extract`、`transcription` 各阶段错误；无错误时为空字符串 |
| `stage_retryable` | 各阶段是否属于可重试网络错误；`true/false` 为已判定，`null` 为没有明确判定 |
| `success` | 只表示视频 ID 和平台元信息解析成功，不代表下载和转录都成功 |
| `fail_reason` | 元信息解析失败原因；成功时为空字符串 |

## 产出

单 URL 模式：

```text
<output-dir>/<id>/
├── 元信息.json
├── 视频文件.mp4           # 下载成功时
└── _analysis/audio.wav  # 音频提取成功时
```

批量模式：

```text
<output-dir>/
├── mapping.jsonl
├── batch_summary.json
├── progress.json
└── <video_id>/...          # 跨平台 ID 冲突时为 <platform>_<video_id>/
```

- `mapping.jsonl`：每个去重后的输入 URL 到 `video_id/source_url` 的映射，包含阶段 1 失败项。
- `batch_summary.json`：顶层为 JSON 数组，每个唯一视频一项；包含 URL、路径、四个阶段状态、`stage_errors`、`stage_retryable` 和 `all_urls`。不包含阶段 1 失败项。
- `progress.json`：包含 `phase1_resolve` 和 `phase2_process` 计数；`phase2_process.stages` 按阶段提供 `attempted/success/failed/skipped`。

## 错误语义

- 如果 ID 和元信息解析成功，始终保存 `元信息.json`。后续下载或转录失败不改变 `success=true`，但 `transcription=null`，且相应媒体文件可能缺失。
- 如果已解析 ID 但元信息失败，保存含 `id`、`source_url` 和 `fail_reason` 的失败元信息文件。
- 如果 ID 解析失败，不创建视频目录；批量模式将失败记入 `mapping.jsonl` 并继续。
- 网络、平台访问或单个视频错误不中断批量中的其他视频。
- 单个直连流保留 60 秒无数据超时，并限制为最长 30 分钟、最大 4 GiB；触发边界后记录下载阶段失败并继续下一个视频。

## 失败重试

- 任务结束后，先查看 `progress.json` 判断失败量级；确认需要重试时运行 `--retry-from <output-dir>`。
- 重试命令优先读取 `stage_retryable`，仅选择超时、连接中断、限流、5xx、不完整下载等网络类错误；旧产物没有该字段时才保守分析错误文本。跳过权限、Cookie、内容下架、平台不支持、文件超限或其他确定性错误。
- 重试先写入隔离目录。只有目标阶段成功且结果比原记录更完整时，才用可恢复事务一起替换媒体目录、`mapping.jsonl`、`batch_summary.json` 和 `progress.json`；失败或中断时恢复旧产物，失败重试不覆盖原记录。
- 合并后重新计算所有阶段统计，并保持跨平台同 ID 的原目录映射。

## 汇报

- 先按阶段报告成功、失败和跳过数量及产出路径。
- 元信息成功时，报告作者、标题、发布时间和播放/点赞/评论/分享数据。
- 仅在对应文件真实存在时报告视频和音频路径。
- 仅在 `transcription` 非 `null` 时报告语言和分段数。
- 列出失败原因，不要把 `success=true` 误报为下载或转录成功。
- 如果用户还需要画面内容描述，只对 `download_success=true`、`meta_path` 有效且其父目录中 `视频文件.mp4` 真实存在的项目调用 `video-content-parser`。

## 依赖

- 必需：`requests`、`ffmpeg`、`faster-whisper`
- 建议：`ffprobe`（用于验证下载的媒体文件，通常随 `ffmpeg` 安装）
- 可选兜底：`yt-dlp`
