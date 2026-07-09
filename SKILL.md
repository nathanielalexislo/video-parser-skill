---
name: video-parser
description: >
  短视频内容解析技能，支持抖音/快手/哔哩哔哩。输入视频短链，自动下载视频、提取元信息、
  通过关键帧分析+Whisper语音转录生成视频内容描述。
  当用户提到 解析视频、下载视频、分析视频内容、视频摘要，
  或者粘贴了抖音(v.douyin.com)、快手(v.kuaishou.com)、B站(b23.tv/bilibili.com)链接时，
  务必使用本技能。即使用户只是发了一个链接没说话，只要链接来自这三个平台，也应该触发。
---

# 短视频内容解析

支持抖音、快手、哔哩哔哩三个平台的短视频链接，一键完成：下载视频 → 提取元信息 → 生成内容描述。

## 工作流程

收到用户的视频链接后，按以下步骤执行：

### Step 1: 识别平台 & 下载视频

运行主控脚本，它会自动识别平台、下载视频、提取元信息：

```bash
python3 <skill-path>/scripts/video_parser.py "<用户提供的URL>" \
  --output-dir <workspace>/videos \
  --cookies-dir <workspace>
```

- `<skill-path>` 是本 skill 的安装路径（即 SKILL.md 所在目录）
- `<workspace>` 是用户当前工作区路径
- `--cookies-dir` 指向包含 cookie 文件的目录，脚本会自动查找 `cookies-douyin.txt`、`cookies-kuaishou.txt`、`cookies-bilibili.txt`

脚本会自动创建 `<workspace>/videos/<视频ID>/` 目录，并输出：
- `视频文件.mp4` — 视频文件
- `元信息.json` — 元信息（作者、标题、点赞、评论等）

### Step 2: 生成视频内容描述

视频下载完成后，运行内容分析脚本：

```bash
python3 <skill-path>/scripts/analyze_video.py "<视频文件路径>" \
  --output "<视频目录>/视频内容描述.txt" \
  --whisper-model base \
  --hf-endpoint https://hf-mirror.com
```

该脚本会：
1. 用 ffmpeg 每2秒提取一帧关键帧截图（保存到临时目录）
2. 用 ffmpeg 提取音频为 WAV 格式
3. 用 faster-whisper（base模型）转录音频为带时间戳的文本
4. 由你逐帧阅读截图（用 Read 工具读取 jpg），结合音频转录文本，生成完整的视频内容描述

**关于逐帧分析的说明：**
分析脚本会输出帧文件列表和音频转录文本。你需要用 Read 工具逐个读取帧截图来理解画面内容，然后将视觉信息和音频文本整合为一份结构化的描述文档。

描述文档格式参考（保存为 `视频内容描述.txt`）：

```
视频内容详细描述
==================
视频来源: <URL>
作者: <作者名>
时长: <时长> | 分辨率: <WxH> | 格式: MP4
标题: <标题>

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
- 文件保存位置
- 视频基本信息（作者、标题、互动数据表格）
- 视频内容概要（一段话）

## 平台适配说明

| 平台 | URL 特征 | 下载策略 | cookie 文件 |
|------|----------|----------|-------------|
| 快手 | `v.kuaishou.com` | 移动端页面提取 mp4 直链 → yt-dlp → requests | `cookies-kuaishou.txt` |
| 抖音 | `v.douyin.com` | 分享页 `_ROUTER_DATA` → playwm→play 去水印 → yt-dlp → requests | `cookies-douyin.txt` |
| B站 | `b23.tv` / `bilibili.com` | B站 API → yt-dlp（DASH自动合并）→ API+ffmpeg | `cookies-bilibili.txt` |

## 依赖

- **ffmpeg / ffprobe**: 视频处理（帧提取、音频提取、DASH合并）
- **yt-dlp**: 优先下载工具
- **faster-whisper**: 语音转录（Python 库，`pip install faster-whisper`）
- **requests**: HTTP 请求（Python 库）

## 错误处理

- 如果 cookie 文件不存在，脚本会跳过 cookie 加载，以匿名方式访问（可能受限）
- 如果 yt-dlp 失败，自动回退到平台专用 API 或 requests 直接下载
- 如果 faster-whisper 模型未下载，设置 `HF_ENDPOINT=https://hf-mirror.com` 使用镜像
- 如果 ffmpeg 不可用，跳过帧提取和音频转录，仅输出视频文件和元信息
