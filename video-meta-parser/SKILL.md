---
name: video-meta-parser
description: >
  视频元信息解析技能，支持抖音/快手/哔哩哔哩。输入视频短链，只解析并输出统一结构的
  视频元信息（不下载视频、不做内容分析）。三平台的短链最终转化成含义一致的元信息结构：
  id、title、desc、publish_time、play_count、like_count、comment_count、share_count、author、source_url、
  success、fail_reason。
  当用户提到 解析视频元信息、获取视频信息、视频作者/点赞/播放数据，
  或者粘贴了抖音(v.douyin.com)、快手(v.kuaishou.com)、B站(b23.tv/bilibili.com)链接需要提取信息时，
  务必使用本技能。若用户还需要下载视频或生成视频内容描述，再配合 video-content-parser 技能。
---

# 视频元信息解析

支持抖音、快手、哔哩哔哩三个平台的短视频链接，**只做**视频短链 → 统一元信息的解析。
本技能不下载视频、不做内容分析（那是 `video-content-parser` 的职责）。

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
| `success` | 是否成功 | true | true | true |
| `fail_reason` | 失败原因（成功时为空字符串） | '' | '' | '' |

## 工作流程

收到用户的视频链接后，运行主控脚本：

```bash
python3 <skill-path>/scripts/meta_parser.py "<用户提供的URL>" \
  --output-dir <workspace>/videos \
  --cookies-dir <workspace>
```

- `<skill-path>` 是本 skill 的安装路径（即 SKILL.md 所在目录）
- `<workspace>` 是用户当前工作区路径
- `--cookies-dir` 指向包含 cookie 文件的目录，脚本会自动查找 `cookies-douyin.txt`、`cookies-kuaishou.txt`、`cookies-bilibili.txt`

脚本会**以 `id` 为父目录**创建 `<workspace>/videos/<id>/`，并写入：

- `元信息.json` — 上表 12 个字段的统一元信息

脚本执行完会在末尾打印，便于串联到 `video-content-parser`：

```
SUCCESS=true
ID=<视频ID>
SOURCE_URL=<source_url>
META_JSON=<元信息.json 路径>
```

**失败时**，脚本会以 `failed_<url_hash>` 为目录名，并打印：

```
SUCCESS=false
META_JSON=<元信息.json 路径>
```

## 汇报结果

完成后向用户报告：
- 元信息保存位置（`<id>/元信息.json` 或 `failed_<hash>/元信息.json`）
- 视频基本信息（作者、标题、发布时间、播放/点赞/评论/分享数据表格）
- 如需下载视频或生成内容描述，提示可用 `video-content-parser`，并把 `id` 与 `source_url` 传给它
- 若 `success` 为 false，报告 `fail_reason` 中的错误信息

## 平台适配说明

| 平台 | URL 特征 | 元信息来源 | cookie 文件 |
|------|----------|-----------|-------------|
| 快手 | `v.kuaishou.com` | 移动端页面提取 | `cookies-kuaishou.txt` |
| 抖音 | `v.douyin.com` | 分享页 `_ROUTER_DATA` | `cookies-douyin.txt` |
| B站 | `b23.tv` / `bilibili.com` | B站 API | `cookies-bilibili.txt` |

## 依赖

- **requests**: HTTP 请求（Python 库）

## 错误处理

- 如果 cookie 文件不存在，脚本会跳过 cookie 加载，以匿名方式访问（可能受限）
- 如果链接已失效或需要登录，脚本会将错误信息写入 `fail_reason` 字段，`success` 设为 false
- 失败时其他字段保持完整但为空（`id`、`title`、`source_url` 等为空字符串，数值字段为 0）
