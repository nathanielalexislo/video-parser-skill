---
name: video-meta-parser
description: >
  视频元信息解析技能，支持抖音/快手/哔哩哔哩。输入视频短链，解析元信息、下载视频、转录音频。
  三平台的短链最终转化成含义一致的元信息结构：
  id、title、desc、publish_time、play_count、like_count、comment_count、share_count、author、source_url、
  transcription、success、fail_reason。
  当用户提到 解析视频元信息、获取视频信息、视频作者/点赞/播放数据、下载视频、音频转录，
  或者粘贴了抖音(v.douyin.com)、快手(v.kuaishou.com)、B站(b23.tv/bilibili.com)链接需要提取信息时，
  务必使用本技能。若用户还需要生成视频内容描述，再配合 video-content-parser 技能。
---

# 视频元信息解析

支持抖音、快手、哔哩哔哩三个平台的短视频链接，完成视频短链 → 元信息解析 → 视频下载 → 音频转录的完整流程。
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

收到用户的视频链接后，运行主控脚本：

```bash
python3 <skill-path>/scripts/meta_parser.py "<用户提供的URL>" \
  --output-dir <workspace>/videos \
  --cookies-dir <workspace> \
  --whisper-model base \
  --hf-endpoint https://hf-mirror.com
```

- `<skill-path>` 是本 skill 的安装路径（即 SKILL.md 所在目录）
- `<workspace>` 是用户当前工作区路径
- `--cookies-dir` 指向包含 cookie 文件的目录，脚本会自动查找 `cookies-douyin.txt`、`cookies-kuaishou.txt`、`cookies-bilibili.txt`
- `--whisper-model` 指定 Whisper 模型名称（默认: base，可选: tiny, small, medium, large）
- `--hf-endpoint` 指定 Hugging Face endpoint（可选，用于加速模型下载）

脚本执行后会有三种情况：

**情况一：完全成功**

以 `<id>` 为父目录创建 `<workspace>/videos/<id>/`，包含以下文件：
- `元信息.json` — 完整元信息（含转录数据）
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

**成功时** (`success=true`)：
- 元信息保存位置（`<id>/元信息.json`）
- 视频文件位置（`<id>/视频文件.mp4`）
- 音频文件位置（`<id>/_analysis/audio.wav`）
- 视频基本信息（作者、标题、发布时间、播放/点赞/评论/分享数据表格）
- 转录摘要（如有）：语言、段落数
- 如需生成内容描述，提示可用 `video-content-parser`，并把 `id` 与 `source_url` 传给它

**ID 解析成功但元信息获取失败时** (`success=false` 但输出了 `ID=`)：
- 报告 `fail_reason` 中的错误原因
- 说明已创建 `<id>/元信息.json`，其中包含视频 ID 和规范化长链接
- 本次元信息解析流程中止，不做其他尝试

**ID 解析失败时** (`success=false` 且未输出 `ID=`)：
- 报告 `fail_reason` 中的错误原因
- 说明未创建任何文件
- 本次元信息解析流程中止，不做其他尝试

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
