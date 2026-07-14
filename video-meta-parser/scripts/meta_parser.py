#!/usr/bin/env python3
"""
视频元信息解析主控 — 自动识别平台，将视频短链解析为统一结构的元信息。

支持抖音 / 快手 / 哔哩哔哩。只做元信息解析，不下载视频。

输出: <output_dir>/<id>/元信息.json

统一元信息字段（三平台含义一致）:
    id            视频唯一 ID（抖音 aweme_id / 快手 photoId / B站 BVID）
    title         标题
    desc          内容描述
    publish_time  发布时间（北京时间字符串）
    play_count    播放次数
    like_count    点赞数
    comment_count 评论数
    share_count   分享数
    author        作者名称
    source_url    视频规范化长链接
    success       是否成功（true/false）
    fail_reason   失败原因（成功时为空字符串）
"""

import argparse
import importlib.util
import json
import os


def detect_platform(url: str) -> str:
    """根据 URL 识别平台"""
    if any(x in url for x in ['v.kuaishou.com', 'kuaishou.com/short-video', 'chenzhongtech.com']):
        return 'kuaishou'
    if any(x in url for x in ['v.douyin.com', 'douyin.com/video', 'iesdouyin.com']):
        return 'douyin'
    if any(x in url for x in ['b23.tv', 'bilibili.com/video', 'BV']):
        return 'bilibili'
    raise ValueError(f"不支持的平台，URL: {url}")


def find_cookie(cookies_dir: str, platform: str) -> str | None:
    """查找 cookie 文件"""
    path = os.path.join(cookies_dir, f"cookies-{platform}.txt")
    return path if os.path.exists(path) else None


def load_module(scripts_dir: str, name: str):
    """动态加载同目录下的平台元信息提取脚本模块"""
    path = os.path.join(scripts_dir, f"extract_{name}.py")
    spec = importlib.util.spec_from_file_location(f"dl_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def to_unified_meta(platform: str, info: dict) -> dict:
    """将各平台原始字段映射为统一结构（字段顺序、命名、含义三平台一致）"""
    if platform == 'douyin':
        vid = info['video_id']
        return {
            'id':            vid,
            'title':         info.get('title', ''),
            'desc':          info.get('desc', ''),
            'publish_time':  info.get('publish_time', ''),
            'play_count':    info.get('play_count', 0),
            'like_count':    info.get('like_count', 0),
            'comment_count': info.get('comment_count', 0),
            'share_count':   info.get('share_count', 0),
            'author':        info.get('author', ''),
            'source_url':    f"https://www.douyin.com/video/{vid}",
            'success':       True,
            'fail_reason':   '',
        }
    if platform == 'kuaishou':
        vid = info['photo_id']
        return {
            'id':            vid,
            'title':         info.get('title', ''),
            'desc':          info.get('caption', ''),
            'publish_time':  info.get('publish_time', ''),
            'play_count':    info.get('view_count', 0),
            'like_count':    info.get('like_count', 0),
            'comment_count': info.get('comment_count', 0),
            'share_count':   info.get('share_count', 0),
            'author':        info.get('author', ''),
            'source_url':    f"https://www.kuaishou.com/short-video/{vid}",
            'success':       True,
            'fail_reason':   '',
        }
    if platform == 'bilibili':
        vid = info['bvid']
        return {
            'id':            vid,
            'title':         info.get('title', ''),
            'desc':          info.get('desc', ''),
            'publish_time':  info.get('publish_time', ''),
            'play_count':    info.get('view_count', 0),
            'like_count':    info.get('like_count', 0),
            'comment_count': info.get('reply_count', 0),
            'share_count':   info.get('share_count', 0),
            'author':        info.get('author', ''),
            'source_url':    f"https://www.bilibili.com/video/{vid}",
            'success':       True,
            'fail_reason':   '',
        }
    raise ValueError(f"未知平台: {platform}")


def to_failed_meta(fail_reason: str) -> dict:
    """生成失败时的元信息结构，所有字段保持完整但为空"""
    return {
        'id':            '',
        'title':         '',
        'desc':          '',
        'publish_time':  '',
        'play_count':    0,
        'like_count':    0,
        'comment_count': 0,
        'share_count':   0,
        'author':        '',
        'source_url':    '',
        'success':       False,
        'fail_reason':   fail_reason,
    }


def parse_meta(platform: str, url: str, scripts_dir: str,
               cookie_file: str | None) -> dict:
    """调用对应平台脚本解析元信息，返回统一结构 dict"""
    if platform == 'kuaishou':
        mod = load_module(scripts_dir, 'kuaishou')
        session = mod.build_session(cookie_file)
        pid = mod.resolve_photo_id(url, session)
        info = mod.extract_video_info(pid, session)
    elif platform == 'douyin':
        mod = load_module(scripts_dir, 'douyin')
        session = mod.build_session(cookie_file)
        vid = mod.resolve_video_id(url, session)
        info = mod.extract_video_info(vid, session)
    elif platform == 'bilibili':
        mod = load_module(scripts_dir, 'bilibili')
        session = mod.build_session(cookie_file)
        bvid = mod.resolve_bvid(url, session)
        info = mod.extract_video_info(bvid, session)
    else:
        raise ValueError(f"未知平台: {platform}")
    return to_unified_meta(platform, info)


def main():
    parser = argparse.ArgumentParser(description='视频元信息解析主控')
    parser.add_argument('url', help='视频短链')
    parser.add_argument('--output-dir', default='./videos', help='输出根目录')
    parser.add_argument('--cookies-dir', default='.', help='cookie 文件所在目录')
    parser.add_argument('--scripts-dir', default=None, help='平台脚本目录（默认与本脚本同目录）')
    args = parser.parse_args()

    scripts_dir = args.scripts_dir or os.path.dirname(os.path.abspath(__file__))

    print("=== 视频元信息解析 ===")
    platform = detect_platform(args.url)
    print(f"平台: {platform}")

    cookie_file = find_cookie(args.cookies_dir, platform)
    if cookie_file:
        print(f"Cookie: {cookie_file}")
    else:
        print(f"Cookie: 未找到 cookies-{platform}.txt，将以匿名方式访问")

    try:
        meta = parse_meta(platform, args.url, scripts_dir, cookie_file)
    except (RuntimeError, ValueError, Exception) as e:
        meta = to_failed_meta(str(e))
        # 失败时用 URL hash 作为目录名，避免空目录
        import hashlib
        dir_name = hashlib.md5(args.url.encode()).hexdigest()[:12]
        save_dir = os.path.join(args.output_dir, f"failed_{dir_name}")
        os.makedirs(save_dir, exist_ok=True)
        meta_path = os.path.join(save_dir, '元信息.json')
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        print(f"\n=== 解析失败 ===")
        print(f"失败原因: {meta['fail_reason']}")
        print(f"元信息:   {meta_path}")
        print(f"\nSUCCESS=false")
        print(f"META_JSON={meta_path}")
        return

    # 成功时以 id 为父目录存储元信息
    save_dir = os.path.join(args.output_dir, meta['id'])
    os.makedirs(save_dir, exist_ok=True)
    meta_path = os.path.join(save_dir, '元信息.json')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print("\n=== 解析完成 ===")
    print(f"视频ID:   {meta['id']}")
    print(f"标题:     {meta['title']}")
    print(f"作者:     {meta['author']}")
    print(f"发布时间: {meta['publish_time']}")
    print(f"播放/点赞/评论/分享: "
          f"{meta['play_count']:,} / {meta['like_count']:,} / "
          f"{meta['comment_count']:,} / {meta['share_count']:,}")
    print(f"元信息:   {meta_path}")

    # 供 video-content-parser 串联使用
    print(f"\nSUCCESS=true")
    print(f"ID={meta['id']}")
    print(f"SOURCE_URL={meta['source_url']}")
    print(f"META_JSON={meta_path}")


if __name__ == '__main__':
    main()
