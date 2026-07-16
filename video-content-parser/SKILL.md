---
name: video-content-parser
description: >
  视频内容描述生成技能，支持抖音/快手/哔哩哔哩。基于 video-meta-parser 已下载的视频和转录结果，
  通过关键帧分析生成视频内容描述。
  当用户提到 分析视频内容、视频摘要、生成视频内容描述，
  或已经通过 video-meta-parser 完成视频下载后需要进一步处理时，务必使用本技能。
  若用户只给了视频短链还没有下载视频，应先用 video-meta-parser 解析元信息并下载视频。
---

# 视频内容描述生成

支持抖音、快手、哔哩哔哩三个平台。基于 `video-meta-parser` 已下载的视频和转录结果，
提取关键帧并生成内容描述。

> 前置：本技能依赖 `video-meta-parser` 先完成元信息解析、视频下载和音频转录。
> `video-meta-parser` 会在 `<output-dir>/<id>/` 下生成视频文件、音频文件和转录结果。
> 若用户只提供了原始短链，请先运行 `video-meta-parser`。

## 工作流程

### Step 1: 准备分析素材

运行分析脚本，输入 `video-meta-parser` 创建的保存目录：

```bash
python3 <skill-path>/scripts/analyze_video.py "<SAVE_DIR>"
```

- `<skill-path>` 是本 skill 的安装路径（即 SKILL.md 所在目录）
- `<SAVE_DIR>` 是 `video-meta-parser` 创建的保存目录（如 `<workspace>/videos/<id>/`）

可选参数：
- `--frame-interval <秒数>`：帧提取间隔，默认 2 秒

该脚本会：
1. 查找 `<SAVE_DIR>/视频文件.mp4`（由 `video-meta-parser` 下载）
2. 用 ffprobe 获取视频时长、分辨率等基本信息
3. 用 ffmpeg 每 N 秒提取一帧关键帧截图（默认每 2 秒）
4. 读取 `<SAVE_DIR>/元信息.json` 中的 `transcription` 字段（由 `video-meta-parser` 转录）
5. 在 `<SAVE_DIR>/_analysis/` 目录下保存关键帧截图和 `analysis.json`

`analysis.json` 结构：

```json
{
  "video_info": {
    "duration": 120.5,
    "width": 1920,
    "height": 1080,
    ...
  },
  "frames": [
    {"start": 0, "end": 2, "path": "<SAVE_DIR>/_analysis/frame_001.jpg"},
    {"start": 2, "end": 4, "path": "<SAVE_DIR>/_analysis/frame_002.jpg"},
    ...
  ],
  "transcription": {
    "language": "zh",
    "language_prob": 0.99,
    "segments": [
      {"start": 0.0, "end": 3.5, "text": "大家好"},
      {"start": 3.5, "end": 6.2, "text": "今天我们来讲讲"},
      ...
    ]
  }
}
```

### Step 2: 生成视频内容描述

基于 Step 1 准备的分析素材，按固定结构生成视频内容描述：

1. 用 Read 工具逐个读取 `_analysis/` 目录下的帧截图（路径在 `analysis.json` 的 `frames` 字段中）
2. 为每个关键帧生成描述，包括时间段和画面内容，形成**关键帧转录**
3. 保留 `analysis.json` 中的 `transcription` 数据作为**音频转录**
4. 基于关键帧转录 + 音频转录的内容（忽略时间区间），生成一段**概述**
5. 按以下结构整合为最终描述文档，保存为 `<SAVE_DIR>/视频内容描述.txt`：

```
视频内容详细描述
==================

【概述】
<一段话概括视频内容，基于关键帧转录 + 音频转录总结，可忽略时间区间>

【关键帧转录】
▸ [<start>s - <end>s]
  画面：<画面描述>
  文字叠加：<画面上的文字>
  ...

【音频转录】
  [<start>s - <end>s] <转录文本>
  ...
```

**注意：**
- 关键帧转录的时间段与 `analysis.json` 中 `frames` 的 `start`/`end` 对应
- 音频转录的时间段与 `analysis.json` 中 `transcription.segments` 的 `start`/`end` 对应
- **不要读取 `元信息.json`**，所有需要的信息都已包含在 `analysis.json` 中

## 依赖

- **ffmpeg / ffprobe**: 视频处理（帧提取）

## 错误处理

- 如果 `<SAVE_DIR>/视频文件.mp4` 不存在，脚本会提示先使用 `video-meta-parser` 下载视频，流程中止
- 如果 `<SAVE_DIR>/元信息.json` 不存在或读取失败，`transcription` 字段为 null
- 如果 `<SAVE_DIR>/元信息.json` 中 `transcription` 字段为 null，脚本会打印提示信息
- 如果视频文件损坏或无法读取，脚本会在获取视频信息时失败并抛出异常，流程中止
- 如果 ffmpeg 不可用或执行失败，脚本会在帧提取步骤失败，但脚本仍会继续执行，`frames` 字段为空列表
