---
name: video-meta-parser
description: >
  视频元信息解析技能，支持抖音/快手/哔哩哔哩。支持单 URL 或批量处理模式。
  输入视频短链（或包含多个短链的文件），解析元信息、下载视频、转录音频。
  三平台的短链最终转化成含义一致的元信息结构：
  id、title、desc、publish_time、play_count、like_count、comment_count、share_count、author、source_url、
  transcription、success、fail_reason。
  当用户提到 解析视频元信息、获取视频信息、视频作者/点赞/播放数据、下载视频、音频转录，
  或者粘贴了抖音(v.douyin.com)、快手(v.kuaishou.com)、B站(b23.tv/bilibili.com)链接需要提取信息时，
  务必使用本技能。若用户还需要生成视频内容描述，再配合 video-content-parser 技能。
---

# 视频元信息解析

支持抖音、快手、哔哩哔哩三个平台的短视频链接，支持单 URL 或批量处理模式，完成视频短链 → 元信息解析 → 视频下载 → 音频转录的完整流程。
本技能会下载视频并转录音频，但不生成内容描述（那是 `video-content-parser` 的职责）。

## 统一元信息结构

三平台不同的短链，最终转化成同一套字段（命名、顺序、含义完全一致）：

| 字段 | 含义 | 抖音来源 | 快手来源 | B站来源 |
|------|------|----------|----------|---------|
| `id` | 视频唯一 ID | aweme_id | photoId | BVID |
| `title` | 标题 | 标题 | 标题 | 标题 |
| `desc` | 内容描述 | desc | caption | desc |
| `publish_time` | 发布时间（北京时间） | create_time | timestamp | pubdate |
| `play_count` | 播放次数 | play_count | viewCount | view |
| `like_count` | 点赞数 | digg_count | likeCount | like |
| `comment_count` | 评论数 | comment_count | commentCount | reply |
| `share_count` | 分享数 | share_count | shareCount | share |
| `author` | 作者名称 | nickname | userName | owner.name |
| `source_url` | 规范化长链接 | douyin.com/video/{id} | kuaishou.com/short-video/{id} | bilibili.com/video/{id} |
| `transcription` | 音频转录结果（含语言、置信度、分段文本） | whisper | whisper | whisper |
| `success` | 是否成功 | true | true | true |
| `fail_reason` | 失败原因（成功时为空字符串） | '' | '' | '' |

## 工作流程

脚本支持两种模式：单 URL 模式和批量处理模式。

### 单 URL 模式

处理单个视频链接：

```bash
python3 <skill-path>/scripts/meta_parser.py "<用户提供的URL>" \
  --output-dir <workspace>/videos \
  --cookies-dir <workspace> \
  --whisper-model base \
  --hf-endpoint https://hf-mirror.com
```

### 批量处理模式

处理多个视频链接（从文件中读取）：

```bash
python3 <skill-path>/scripts/meta_parser.py --input-file <URL文件路径> \
  --output-dir <workspace>/videos \
  --cookies-dir <workspace> \
  --concurrent 8 \
  --batch-size 100 \
  --whisper-model base \
  --hf-endpoint https://hf-mirror.com
```

**参数说明：**
- `<skill-path>` 是本 skill 的安装路径（即 SKILL.md 所在目录）
- `<workspace>` 是用户当前工作区路径
- `--input-file` 批量输入文件路径（每行一个 URL，支持 `#` 开头的注释行）
- `--concurrent` 并发数，默认 8（可根据机器性能调整）
- `--batch-size` 每批大小，默认 100
- `--cookies-dir` 指向包含 cookie 文件的目录，脚本会自动查找 `cookies-douyin.txt`、`cookies-kuaishou.txt`、`cookies-bilibili.txt`
- `--whisper-model` 指定 Whisper 模型名称（默认: base，可选: tiny, small, medium, large）
- `--hf-endpoint` 指定 Hugging Face endpoint（可选，用于加速模型下载）

**批量处理特性：**
- **两阶段处理**（两阶段都有并发和分批能力）：
  - 阶段 1：分批并发解析所有短链接，获取 `video_id` 和 `source_url`
  - 阶段 2：基于 `video_id` 去重，分批并发处理唯一视频（元信息解析、下载、转录）
- 自动去重：基于 `video_id` 去重，多个短链指向同一视频时只处理一次
- 进度显示：实时显示每个批次的处理状态（✓ 成功 / ✗ 失败）
- 错误隔离：单个 URL 失败不影响其他 URL 的处理
- **3 个产出文件**：
  1. `mapping.jsonl` — 短链映射关系（JSONL 格式，每行一个映射）
  2. `batch_summary.json` — video_id 去重后的处理结果
  3. `progress.json` — 两个阶段的进度统计

**产出文件 1: mapping.jsonl**

JSONL 格式（每行一个 JSON 对象），记录所有短链到 video_id 的映射关系：

```jsonl
{"short_url": "https://v.douyin.com/xxx", "video_id": "123456", "source_url": "https://www.douyin.com/video/123456", "success": true}
{"short_url": "https://v.douyin.com/aaa", "video_id": "123456", "source_url": "https://www.douyin.com/video/123456", "success": true}
{"short_url": "https://v.douyin.com/bbb", "video_id": "123456", "source_url": "https://www.douyin.com/video/123456", "success": true}
{"short_url": "https://v.kuaishou.com/yyy", "video_id": null, "source_url": null, "success": false, "error": "视频不存在"}
```

**产出文件 2: batch_summary.json**

video_id 去重后的处理结果（只包含唯一视频，不包含解析失败的短链）：

```json
[
  {
    "url": "https://v.douyin.com/xxx",
    "video_id": "123456",
    "source_url": "https://www.douyin.com/video/123456",
    "success": true,
    "meta_path": "/path/to/元信息.json",
    "error": null,
    "all_urls": [
      "https://v.douyin.com/xxx",
      "https://v.douyin.com/aaa",
      "https://v.douyin.com/bbb"
    ]
  },
  {
    "url": "https://v.kuaishou.com/zzz",
    "video_id": "789012",
    "source_url": "https://www.kuaishou.com/short-video/789012",
    "success": false,
    "meta_path": null,
    "error": "视频已删除",
    "all_urls": [
      "https://v.kuaishou.com/zzz"
    ]
  }
]
```

**产出文件 3: progress.json**

两个阶段的详细进度统计：

```json
{
  "phase1_resolve": {
    "total": 150,
    "completed": 150,
    "success": 148,
    "failed": 2,
    "unique_videos": 142
  },
  "phase2_process": {
    "total": 142,
    "completed": 142,
    "success": 134,
    "failed": 8
  }
}
```

**字段说明：**
- `phase1_resolve.total` — 去重后的短链总数（输入）
- `phase1_resolve.success` — 成功解析出 video_id 的短链数
- `phase1_resolve.failed` — 解析失败的短链数（无法获取 video_id）
- `phase1_resolve.unique_videos` — 去重后的唯一视频数
- `phase2_process.total` — 需要处理的唯一视频数（等于 phase1_resolve.unique_videos）
- `phase2_process.success` — 成功处理的视频数（元信息+下载+转录）
- `phase2_process.failed` — 处理失败的视频数

**注意：** 
- `mapping.jsonl` 包含所有短链（包括解析失败的）
- `batch_summary.json` 只包含唯一视频的处理结果（不包含解析失败的短链）
- `all_urls` 字段记录了所有指向该视频的短链
- 只有唯一的视频会被下载和处理，避免重复工作

### 单 URL 模式的输出

脚本执行后会有三种情况：

**情况一：元信息解析成功** (`success=true`)

以 `<id>` 为父目录创建 `<workspace>/videos/<id>/`，至少包含：
- `元信息.json` — 完整元信息（`transcription` 字段可能为 null，如果下载或转录失败）

如果视频下载和音频转录也成功，还会包含：
- `视频文件.mp4` — 下载的视频
- `_analysis/audio.wav` — 提取的音频

末尾打印：

```
SUCCESS=true
ID=<视频ID>
SOURCE_URL=<source_url>
META_JSON=<元信息.json 路径>
```

**情况二：ID 解析成功，但元信息获取失败**

仍以 `<id>` 为父目录创建 `<workspace>/videos/<id>/元信息.json`，其中 `id` 和 `source_url` 会被填充，其他字段为空（字符串字段为空字符串，数值字段为 0），`success` 为 false。末尾打印：

```
SUCCESS=false
ID=<视频ID>
SOURCE_URL=<source_url>
META_JSON=<元信息.json 路径>
```

**情况三：ID 解析失败**

不创建任何目录或文件，只打印错误信息：

```
SUCCESS=false
```

## 汇报结果

根据 `success` 字段和输出信息向用户报告：

**元信息解析成功时** (`success=true`)：
- 元信息保存位置（`<id>/元信息.json`）
- 视频基本信息（作者、标题、发布时间、播放/点赞/评论/分享数据表格）
- 如果视频文件存在，报告视频文件位置（`<id>/视频文件.mp4`）
- 如果音频文件存在，报告音频文件位置（`<id>/_analysis/audio.wav`）
- 如果 `transcription` 不为 null，报告转录摘要：语言、段落数
- 如需生成内容描述，提示可用 `video-content-parser`，并把 `SAVE_DIR`（即 `<workspace>/videos/<id>/`）传给它

**ID 解析成功但元信息获取失败时** (`success=false` 但输出了 `ID=`)：
- 报告 `fail_reason` 中的错误原因
- 说明已创建 `<id>/元信息.json`，其中包含视频 ID 和规范化长链接
- 本次元信息解析流程中止，不做其他尝试

**ID 解析失败时** (`success=false` 且未输出 `ID=`)：
- 报告 `fail_reason` 中的错误原因
- 说明未创建任何文件
- 本次元信息解析流程中止，不做其他尝试

### 批量模式的输出

批量处理完成后，会生成以下文件和目录结构：

```
<output-dir>/
├── batch_summary.json          # 汇总报告（含所有 URL 的处理结果和映射关系）
├── <video_id_1>/
│   ├── 元信息.json
│   ├── 视频文件.mp4
│   └── _analysis/
│       └── audio.wav
├── <video_id_2>/
│   └── ...
└── <video_id_N>/
    └── ...
```

**注意：** 多个短链可能映射到同一个 `video_id`，因此不会重复下载，多个短链会在 `batch_summary.json` 的 `results` 数组中指向同一个目录。

### 批量模式的汇报

批量处理完成后，向用户报告：
- 总计处理的 URL 数量（去重后）
- 成功/失败的 URL 数量
- 汇总报告文件位置（`batch_summary.json`）
- 如有失败的 URL，列出前几个失败原因（可参考 `batch_summary.json` 中的 `results` 数组）
- 如需生成内容描述，提示可用 `video-content-parser`，并说明需要对每个成功的 `<id>` 目录分别运行

### 批量模式的错误处理

- 批量模式下，单个 URL 失败不会中断整个批次，其他 URL 会继续处理
- 失败的 URL 会被记录在 `batch_summary.json` 的 `results` 数组中，包含 `error` 字段
- 即使部分 URL 失败，成功的 URL 仍会生成完整的输出文件（元信息、视频、音频）
- 如果所有 URL 都失败，仍会生成 `batch_summary.json`，但不会创建任何视频目录

## 平台适配说明

| 平台 | URL 特征 | 元信息来源 | cookie 文件 |
|------|----------|-----------|-------------|
| 快手 | `v.kuaishou.com`、`kuaishou.com/short-video`、`chenzhongtech.com` | 移动端页面提取 | `cookies-kuaishou.txt` |
| 抖音 | `v.douyin.com`、`douyin.com/video`、`iesdouyin.com` | 分享页 `_ROUTER_DATA` | `cookies-douyin.txt` |
| B站 | `b23.tv`、`bilibili.com/video`、`BV` 开头的 ID | B站 API | `cookies-bilibili.txt` |

## 依赖

- **requests**: HTTP 请求（Python 库）
- **ffmpeg**: 音频提取（将视频中的音频转为 WAV 格式）
- **faster-whisper**: 语音转录（Python 库，`pip install faster-whisper`）

## 错误处理

- 如果 cookie 文件不存在，脚本会跳过 cookie 加载，以匿名方式访问（可能受限）
- 如果 ID 解析成功但元信息获取失败（如视频已删除、需要登录），`id` 和 `source_url` 仍会被填充，其他字段为空，`success` 为 false
- 如果 ID 解析失败（如短链跳转被 412 拦截、无法从 URL 提取 ID），不会创建任何文件，只打印错误信息
- 如果平台无法识别，直接抛出异常，不创建任何文件
- 如果遇到网络异常（连接超时、DNS 解析失败等），脚本会抛出异常并打印错误信息，流程中止
- 如果视频下载失败（如链接失效、需要登录），元信息仍会保存，但 `transcription` 字段为 null
- 如果音频提取失败（如 ffmpeg 不可用），`transcription` 字段为 null
- 如果语音转录失败（如 faster-whisper 模型下载失败），`transcription` 字段为 null，但会打印错误信息
