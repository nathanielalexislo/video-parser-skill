#!/usr/bin/env python3
"""
视频下载主控脚本 — 自动识别平台并调用对应的下载脚本。
输出: <output_dir>/<视频ID>/<视频ID>.mp4 + <视频ID>.json
"""

import argparse
import json
import os
import re
import sys


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
    name = f"cookies-{platform}.txt"
    path = os.path.join(cookies_dir, name)
    return path if os.path.exists(path) else None


def run_download(platform: str, url: str, scripts_dir: str,
                 output_dir: str, cookie_file: str | None) -> dict:
    """调用对应平台的下载逻辑，返回 {video_id, save_dir, mp4_path, json_path}"""
    import importlib.util
    import requests

    # 公共 session 构建
    def load_module(name):
        path = os.path.join(scripts_dir, f"download_{name}.py")
        spec = importlib.util.spec_from_file_location(f"dl_{name}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    if platform == 'kuaishou':
        mod = load_module('kuaishou')
        session = mod.build_session(cookie_file)
        video_id = mod.resolve_photo_id(url, session)
        info = mod.extract_video_info(video_id, session)
        save_dir = os.path.join(output_dir, video_id)
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"{info['title']}.mp4")
        mod.download_video(info['video_url'], save_path, session, cookie_file)
        mod.save_meta(info, save_path)
        return {
            'video_id': video_id,
            'save_dir': save_dir,
            'mp4_path': save_path,
            'json_path': os.path.splitext(save_path)[0] + '.json',
            'title': info['title'],
        }

    elif platform == 'douyin':
        mod = load_module('douyin')
        session = mod.build_session(cookie_file)
        video_id = mod.resolve_video_id(url, session)
        info = mod.extract_video_info(video_id, session)
        save_dir = os.path.join(output_dir, video_id)
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"{info['title']}.mp4")
        mod.download_video(info['video_url'], save_path, session, cookie_file)
        mod.save_meta(info, save_path)
        return {
            'video_id': video_id,
            'save_dir': save_dir,
            'mp4_path': save_path,
            'json_path': os.path.splitext(save_path)[0] + '.json',
            'title': info['title'],
        }

    elif platform == 'bilibili':
        mod = load_module('bilibili')
        session = mod.build_session(cookie_file)
        bvid = mod.resolve_bvid(url, session)
        info = mod.extract_video_info(bvid, session)
        save_dir = os.path.join(output_dir, bvid)
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"{info['title_clean']}.mp4")
        mod.download_video(bvid, info['cid'], save_path, session, cookie_file)
        mod.save_meta(info, save_path)
        return {
            'video_id': bvid,
            'save_dir': save_dir,
            'mp4_path': save_path,
            'json_path': os.path.splitext(save_path)[0] + '.json',
            'title': info['title'],
        }


def main():
    parser = argparse.ArgumentParser(description='短视频下载主控')
    parser.add_argument('url', help='视频链接')
    parser.add_argument('--output-dir', default='./videos', help='输出根目录')
    parser.add_argument('--cookies-dir', default='.', help='cookie 文件所在目录')
    parser.add_argument('--scripts-dir', default=None, help='下载脚本目录（默认与本脚本同目录）')
    args = parser.parse_args()

    scripts_dir = args.scripts_dir or os.path.dirname(os.path.abspath(__file__))

    print(f"=== 短视频下载 ===")
    platform = detect_platform(args.url)
    print(f"平台: {platform}")

    cookie_file = find_cookie(args.cookies_dir, platform)
    if cookie_file:
        print(f"Cookie: {cookie_file}")
    else:
        print(f"Cookie: 未找到 cookies-{platform}.txt，将以匿名方式访问")

    result = run_download(platform, args.url, scripts_dir,
                         args.output_dir, cookie_file)

    # 输出结果 JSON 供后续步骤使用
    print(f"\n=== 下载完成 ===")
    print(f"视频ID: {result['video_id']}")
    print(f"保存目录: {result['save_dir']}")
    print(f"视频文件: {result['mp4_path']}")
    print(f"元信息:   {result['json_path']}")

    # 写 result.json 供 analyze 脚本读取
    result_file = os.path.join(result['save_dir'], '_download_result.json')
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\nRESULT_JSON={result_file}")


if __name__ == '__main__':
    main()
