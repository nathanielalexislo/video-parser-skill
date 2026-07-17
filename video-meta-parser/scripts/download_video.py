#!/usr/bin/env python3
"""
视频下载主控 — 输入 video-meta-parser 产出的 id 与 source_url，
自动识别平台并下载视频。

用法:
    python3 download_video.py --id <视频ID> --source-url <source_url> \
        --output-dir <workspace>/videos --cookies-dir <workspace>

输出: <output_dir>/<id>/视频文件.mp4 + _download_result.json（含 source_url）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import tempfile


SAFE_VIDEO_ID = re.compile(r'[A-Za-z0-9]+')


def safe_save_dir(output_dir: str, video_id: str) -> str:
    """只允许平台 ID 作为单层目录名，并阻止符号链接越界。"""
    if not SAFE_VIDEO_ID.fullmatch(video_id or ''):
        raise ValueError(f'不安全的视频 ID: {video_id!r}')
    root = os.path.realpath(os.path.abspath(output_dir))
    save_dir = os.path.realpath(os.path.join(root, video_id))
    if os.path.commonpath([root, save_dir]) != root:
        raise ValueError(f'视频目录越界: {video_id!r}')
    return save_dir


def reject_symlink_tree(root_dir: str) -> None:
    """拒绝已有产物中的符号链接，避免下载器跟随到目录外。"""
    if not os.path.isdir(root_dir):
        return
    for current_dir, dirnames, filenames in os.walk(
        root_dir, followlinks=False
    ):
        for name in dirnames + filenames:
            path = os.path.join(current_dir, name)
            if os.path.islink(path):
                raise ValueError(f'产物目录包含符号链接: {path}')


def detect_platform(url: str) -> str:
    """根据 source_url 识别平台"""
    if any(x in url for x in ['v.kuaishou.com', 'kuaishou.com/short-video', 'chenzhongtech.com']):
        return 'kuaishou'
    if any(x in url for x in ['v.douyin.com', 'douyin.com/video', 'iesdouyin.com']):
        return 'douyin'
    if any(x in url for x in ['b23.tv', 'bilibili.com/video', 'BV']):
        return 'bilibili'
    raise ValueError(f"不支持的平台，source_url: {url}")


def find_cookie(cookies_dir: str, platform: str) -> str | None:
    """查找 cookie 文件"""
    path = os.path.join(cookies_dir, f"cookies-{platform}.txt")
    return path if os.path.exists(path) else None


def load_module(scripts_dir: str, name: str):
    """动态加载同目录下的平台脚本模块"""
    path = os.path.join(scripts_dir, f"download_{name}.py")
    spec = importlib.util.spec_from_file_location(f"dl_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_download(platform: str, video_id: str, source_url: str, scripts_dir: str,
                 output_dir: str, cookie_file: str | None) -> dict:
    """按平台下载视频，返回 {video_id, source_url, save_dir, mp4_path}"""
    save_dir = safe_save_dir(output_dir, video_id)
    os.makedirs(save_dir, exist_ok=True)
    reject_symlink_tree(save_dir)
    save_path = os.path.join(save_dir, '视频文件.mp4')

    if platform == 'kuaishou':
        mod = load_module(scripts_dir, 'kuaishou')
        with mod.build_session(cookie_file) as session:
            info = mod.extract_video_info(video_id, session)
            mod.download_video(
                info['video_url'], save_path, session, cookie_file
            )

    elif platform == 'douyin':
        mod = load_module(scripts_dir, 'douyin')
        with mod.build_session(cookie_file) as session:
            info = mod.extract_video_info(video_id, session)
            mod.download_video(
                info['video_url'], save_path, session, cookie_file
            )

    elif platform == 'bilibili':
        mod = load_module(scripts_dir, 'bilibili')
        with mod.build_session(cookie_file) as session:
            info = mod.extract_video_info(video_id, session)
            mod.download_video(
                video_id, info['cid'], save_path, session, cookie_file
            )

    else:
        raise ValueError(f"未知平台: {platform}")

    return {
        'video_id': video_id,
        'source_url': source_url,
        'save_dir': save_dir,
        'mp4_path': save_path,
    }


def main():
    parser = argparse.ArgumentParser(description='视频下载主控（输入 id + source_url）')
    parser.add_argument('--id', required=True, help='video-meta-parser 产出的视频 ID')
    parser.add_argument('--source-url', required=True, help='video-meta-parser 产出的 source_url')
    parser.add_argument('--output-dir', default='./videos', help='输出根目录')
    parser.add_argument('--cookies-dir', default='.', help='cookie 文件所在目录')
    parser.add_argument('--scripts-dir', default=None, help='平台脚本目录（默认与本脚本同目录）')
    args = parser.parse_args()

    scripts_dir = args.scripts_dir or os.path.dirname(os.path.abspath(__file__))

    print("=== 视频下载 ===")
    platform = detect_platform(args.source_url)
    print(f"平台: {platform}")
    print(f"视频ID: {args.id}")

    cookie_file = find_cookie(args.cookies_dir, platform)
    if cookie_file:
        print(f"Cookie: {cookie_file}")
    else:
        print(f"Cookie: 未找到 cookies-{platform}.txt，将以匿名方式访问")

    result = run_download(platform, args.id, args.source_url, scripts_dir,
                          args.output_dir, cookie_file)

    print("\n=== 下载完成 ===")
    print(f"视频ID:   {result['video_id']}")
    print(f"保存目录: {result['save_dir']}")
    print(f"视频文件: {result['mp4_path']}")

    # 写 result.json 供 analyze 步骤读取
    result_file = os.path.join(result['save_dir'], '_download_result.json')
    fd, temp_result_file = tempfile.mkstemp(
        prefix='._download_result.json-',
        suffix='.tmp',
        dir=result['save_dir'],
        text=True,
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_result_file, result_file)
    except Exception:
        try:
            os.remove(temp_result_file)
        except FileNotFoundError:
            pass
        raise
    print(f"\nMP4_PATH={result['mp4_path']}")
    print(f"SAVE_DIR={result['save_dir']}")
    print(f"RESULT_JSON={result_file}")


if __name__ == '__main__':
    main()
