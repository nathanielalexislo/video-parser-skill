---
name: video-content-parser
description: >
  视频下载与内容描述生成技能，支持抖音/快手/哔哩哔哩。输入 video-meta-parser 产出的
  id 与 source_url，自动下载视频，再通过关键帧分析 + Whisper 语音转录生成视频内容描述。
  当用户提到 下载视频、分析视频内容、视频摘要、生成视频内容描述，
  或已经通过 video-meta-parser 拿到 id/source_url 需要进一步处理时，务必使用本技能。
  若用户只给了视频短链还没有 id/source_url，应先用 video-meta-parser 解析元信息。
---

# 视频下载与内容描述生成

支持抖音、快手、哔哩哔哩三个平台。以 `video-meta-parser` 产出的 `id` 和 `source_url`
作为输入，完成：下载视频 → 生成内容描述。

> 前置：本技能依赖 `video-meta-parser` 先解析出 `id` 与 `source_url`。
> `id` 决定下载到哪个目录（`<output-dir>/<id>/`）、`source_url` 决定平台与下载对象。
> 若用户只提供了原始短链，请先运行 `video-meta-parser`。

## 工作流程

### Step 1: 下载视频

运行下载主控脚本，输入第一个技能产出的 `id` 与 `source_url`：

```bash
python3 <skill-path>/scripts/download_video.py \
  --id "<video-meta-parser 产出的 id>" \
  --source-url "<video-meta-parser 产出的 source_url>" \
  --output-dir <workspace>/videos \
  --cookies-dir <workspace>
```

- `<skill-path>` 是本 skill 的安装路径（即 SKILL.md 所在目录）
- `<workspace>` 是用户当前工作区路径
- `--output-dir` 应与 `video-meta-parser` 使用的相同，这样视频会下载到 `<output-dir>/<id>/`
- `--cookies-dir` 指向包含 cookie 文件的目录，脚本会自动查找 `cookies-douyin.txt`、`cookies-kuaishou.txt`、`cookies-bilibili.txt`

脚本会在 `<output-dir>/<id>/` 下生成：
- `视频文件.mp4` — 视频文件
- `_download_result.json` — 含 `video_id`、`source_url`、`mp4_path`、`save_dir`
- 末尾打印：
  ```
  MP4_PATH=<视频文件路径>
  SAVE_DIR=<保存目录>
  RESULT_JSON=<_download_result.json 路径>
  ```

**下载失败时**，脚本会抛出异常，不创建 `视频文件.mp4` 和 `_download_result.json`，流程中止。

### Step 2: 生成视频内容描述

视频下载完成后，运行内容分析脚本（使用 Step 1 输出的 `MP4_PATH` 和 `SAVE_DIR`）：

```bash
python3 <skill-path>/scripts/analyze_video.py "<MP4_PATH>" \
  --output "<SAVE_DIR>/视频内容描述.txt" \
  --whisper-model base \
  --hf-endpoint https://hf-mirror.com
```

可选参数：
- `--frame-interval <秒数>`：帧提取间隔，默认 2 秒
- `--skip-whisper`：跳过语音转录（当 whisper 失败或不需要时使用）

该脚本会：
1. 用 ffprobe 获取视频时长、分辨率等基本信息
2. 用 ffmpeg 每 N 秒提取一帧关键帧截图（默认每 2 秒，保存到临时目录）
3. 用 ffmpeg 提取音频为 16kHz 单声道 WAV 格式
4. 用 faster-whisper 转录音频为带时间戳的文本
5. 在 `<SAVE_DIR>/` 下生成 `_analysis.json`，包含视频信息、帧文件列表、音频路径、转录结果
6. 末尾打印帧文件列表和分析结果路径，提示你逐帧阅读截图

**关于逐帧分析的说明：**
你需要用 Read 工具逐个读取帧截图（路径在 `_analysis.json` 的 `frames` 字段中，或从脚本输出中复制），理解画面内容，然后将视觉信息和音频转录文本（在 `_analysis.json` 的 `transcription` 字段中）整合为一份结构化的描述文档。描述中的 `视频来源` 取自 `_download_result.json` 中的 `source_url`，**不要读取 `元信息.json`**。

描述文档格式参考（保存为 `视频内容描述.txt`）：

```
视频内容详细描述
==================
视频来源: <source_url>
时长: <时长> | 分辨率: <WxH> | 格式: MP4

【概述】
<一段话概括视频内容>

【音频转录（faster-whisper base模型）】
  [<时间>] <转录文本>
  ...

【逐段描述】
▸ <时间段> — <段落标题>
  画面：<画面描述>
  文字叠加：<画面上的文字>
  旁白：<对应时间段的旁白内容>
  ...

【音效/音频分析】
  - 语音类型：<描述>
  - 背景音乐：<描述>
  - 转录工具：faster-whisper base模型

【关键信息提取】
  - <要点列表>
```

### Step 3: 汇报结果

完成后向用户报告：
- 文件保存位置（`<id>/` 目录下的视频与描述文件）
- 视频内容概要（一段话）

## 平台适配说明

| 平台 | source_url 特征 | 下载策略 | cookie 文件 |
|------|----------|----------|-------------|
| 快手 | `v.kuaishou.com`、`kuaishou.com/short-video`、`chenzhongtech.com` | 移动端页面提取 mp4 直链 → yt-dlp → requests | `cookies-kuaishou.txt` |
| 抖音 | `v.douyin.com`、`douyin.com/video`、`iesdouyin.com` | 分享页 `_ROUTER_DATA` → playwm→play 去水印 → yt-dlp → requests | `cookies-douyin.txt` |
| B站 | `b23.tv`、`bilibili.com/video`、`BV` 开头的 ID | B站 API → yt-dlp（DASH自动合并）→ API+ffmpeg | `cookies-bilibili.txt` |

## 依赖

- **ffmpeg / ffprobe**: 视频处理（帧提取、音频提取、DASH合并）
- **yt-dlp**: 优先下载工具
- **faster-whisper**: 语音转录（Python 库，`pip install faster-whisper`）
- **requests**: HTTP 请求（Python 库）

## 错误处理

- 如果 cookie 文件不存在，脚本会跳过 cookie 加载，以匿名方式访问（可能受限）
- 如果 yt-dlp 下载失败，自动回退到平台专用 API 或 requests 直接下载
- 如果所有下载方式均失败，`download_video.py` 会抛出异常，流程中止
- 如果 ffmpeg 不可用，`analyze_video.py` 会在帧提取或音频提取步骤失败，脚本仍会继续执行，但相关功能会被跳过
- 如果 faster-whisper 转录失败，脚本会打印错误信息，`transcription` 字段为 null，但仍会生成 `_analysis.json` 和提示你逐帧阅读
