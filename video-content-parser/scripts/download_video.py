#!/usr/bin/env python3
"""
视频下载主控 — 输入 video-meta-parser 产出的 id 与 source_url，
自动识别平台并下载视频。

用法:
    python3 download_video.py --id <视频ID> --source-url <source_url> \
        --output-dir <workspace>/videos --cookies-dir <workspace>

输出: <output_dir>/<id>/视频文件.mp4
（与 video-meta-parser 产出的 <output_dir>/<id>/元信息.json 同目录）
"""

import argparse
import importlib.util
import json
import os


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


def run_download(platform: str, video_id: str, scripts_dir: str,
                 output_dir: str, cookie_file: str | None) -> dict:
    """按平台下载视频，返回 {video_id, save_dir, mp4_path}"""
    save_dir = os.path.join(output_dir, video_id)
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, '视频文件.mp4')

    if platform == 'kuaishou':
        mod = load_module(scripts_dir, 'kuaishou')
        session = mod.build_session(cookie_file)
        info = mod.extract_video_info(video_id, session)
        mod.download_video(info['video_url'], save_path, session, cookie_file)

    elif platform == 'douyin':
        mod = load_module(scripts_dir, 'douyin')
        session = mod.build_session(cookie_file)
        info = mod.extract_video_info(video_id, session)
        mod.download_video(info['video_url'], save_path, session, cookie_file)

    elif platform == 'bilibili':
        mod = load_module(scripts_dir, 'bilibili')
        session = mod.build_session(cookie_file)
        info = mod.extract_video_info(video_id, session)
        mod.download_video(video_id, info['cid'], save_path, session, cookie_file)

    else:
        raise ValueError(f"未知平台: {platform}")

    return {'video_id': video_id, 'save_dir': save_dir, 'mp4_path': save_path}


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

    result = run_download(platform, args.id, scripts_dir,
                          args.output_dir, cookie_file)

    print("\n=== 下载完成 ===")
    print(f"视频ID:   {result['video_id']}")
    print(f"保存目录: {result['save_dir']}")
    print(f"视频文件: {result['mp4_path']}")

    # 写 result.json 供 analyze 步骤读取
    result_file = os.path.join(result['save_dir'], '_download_result.json')
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\nMP4_PATH={result['mp4_path']}")
    print(f"SAVE_DIR={result['save_dir']}")
    print(f"RESULT_JSON={result_file}")


if __name__ == '__main__':
    main()
