#!/usr/bin/env python3
"""
B站视频下载脚本
用法:
    python3 download_bilibili.py <B站视频链接> [保存目录] [--cookies cookies.txt]
    python3 download_bilibili.py https://b23.tv/xxxxx
    python3 download_bilibili.py https://www.bilibili.com/video/BVxxxxx ./videos
    python3 download_bilibili.py https://b23.tv/xxxxx --cookies cookies-bilibili.txt

功能:
    - 通过 B站 API + ffmpeg 下载并合并 DASH 音视频流
    - API 下载失败时使用 yt-dlp 兜底
    - 提取作者名称、发布时间、内容描述、点赞数、评论数、播放数、分享数
    - 将元信息保存为同名 .json 文件
    - 支持通过 Netscape cookie 文件传入登录态
"""

from __future__ import annotations

import re
import sys
import os
import json
import argparse
import shutil
import subprocess
import time
import http.cookiejar
from datetime import datetime, timezone, timedelta
import requests


PC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

BJ_TZ = timezone(timedelta(hours=8))

BILIBILI_API_VIEW = "https://api.bilibili.com/x/web-interface/view"
BILIBILI_API_PLAY = "https://api.bilibili.com/x/player/playurl"
REQUEST_TIMEOUT = (10, 30)
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024
DOWNLOAD_DEADLINE_SECONDS = 30 * 60


def build_session(cookie_file: str | None = None) -> requests.Session:
    """构建 Session，设置 B站 Referer + UA，可选加载 cookie"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": PC_UA,
        "Referer": "https://www.bilibili.com",
    })
    if cookie_file and os.path.exists(cookie_file):
        jar = http.cookiejar.MozillaCookieJar(cookie_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
        print(f"      已加载 cookie: {cookie_file}  ({len(jar)} 条)")
    return session


def resolve_bvid(url: str, session: requests.Session) -> str:
    """从各种 B站链接格式中提取 BVID"""

    # 已经是 BVID
    if re.fullmatch(r'BV[a-zA-Z0-9]+', url):
        return url

    # 短链 -> 跟随重定向
    if 'b23.tv' in url:
        resp = session.get(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        url = resp.url

    # 从 URL 提取 BV 号
    m = re.search(r'(BV[a-zA-Z0-9]+)', url)
    if m:
        return m.group(1)

    raise ValueError(f"无法从链接中提取 BVID: {url}")


def extract_video_info(bvid: str, session: requests.Session) -> dict:
    """通过 B站 API 提取视频元信息"""
    resp = session.get(
        BILIBILI_API_VIEW, params={"bvid": bvid}, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get('code') != 0:
        raise RuntimeError(f"B站 API 错误: {data.get('message', 'unknown')}")

    v = data['data']
    stat = v.get('stat', {})
    owner = v.get('owner', {})

    # 发布时间
    pubdate = v.get('pubdate', 0)
    try:
        publish_time = datetime.fromtimestamp(
            pubdate, tz=BJ_TZ
        ).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError):
        publish_time = str(pubdate)

    title = v.get('title', f'bilibili_{bvid}')
    title_clean = re.sub(r'[\\/:*?"<>|\n\r]', '_', title).strip()[:60] or f'bilibili_{bvid}'

    return {
        'bvid':          bvid,
        'aid':           v.get('aid', 0),
        'cid':           v.get('cid', 0),
        'title':         title,
        'title_clean':   title_clean,
        'author':        owner.get('name', ''),
        'author_mid':    owner.get('mid', 0),
        'publish_time':  publish_time,
        'desc':          v.get('desc', ''),
        'duration':      v.get('duration', 0),
        'view_count':    stat.get('view', 0),
        'like_count':    stat.get('like', 0),
        'reply_count':   stat.get('reply', 0),
        'share_count':   stat.get('share', 0),
        'danmaku_count': stat.get('danmaku', 0),
    }


def download_with_ytdlp(bvid: str, save_path: str,
                        cookie_file: str | None = None) -> tuple[bool, str]:
    """尝试用 yt-dlp 下载，并保留失败原因供上层判断是否可重试。"""
    ytdlp = shutil.which('yt-dlp')
    if not ytdlp:
        print("      yt-dlp 未安装，跳过")
        return False, 'yt-dlp 未安装'

    url = f"https://www.bilibili.com/video/{bvid}"
    # yt-dlp 模板：去掉扩展名中间部分，最终合并为 .mp4
    output_template = os.path.splitext(save_path)[0] + '.%(ext)s'

    cmd = [
        ytdlp,
        '--no-warnings',
        '--no-check-certificates',
        '--socket-timeout', '60',
        '--max-filesize', str(MAX_DOWNLOAD_BYTES),
        '-o', output_template,
        '--merge-output-format', 'mp4',
    ]
    if cookie_file and os.path.exists(cookie_file):
        cmd += ['--cookies', cookie_file]
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and os.path.exists(save_path):
            if os.path.getsize(save_path) > MAX_DOWNLOAD_BYTES:
                os.remove(save_path)
                return False, f'yt-dlp 文件超过大小上限 {MAX_DOWNLOAD_BYTES} 字节'
            size_mb = os.path.getsize(save_path) / 1048576
            print(f"✓ yt-dlp 下载完成: {save_path}  ({size_mb:.1f} MB)")
            return True, ''
        err = result.stderr.strip().split('\n')[-1] if result.stderr else 'unknown'
        print(f"      yt-dlp 失败: {err}")
        return False, f'yt-dlp 失败: {err}'
    except subprocess.TimeoutExpired:
        print("      yt-dlp 超时（300s）")
        return False, 'yt-dlp 超时（300s）'
    except Exception as e:
        print(f"      yt-dlp 异常: {e}")
        return False, f'yt-dlp 异常: {e}'


def download_with_api(bvid: str, cid: int, save_path: str,
                      session: requests.Session) -> tuple[bool, str]:
    """通过 B站 API 获取 DASH 流 + ffmpeg 合并，并保留失败原因。"""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        print("      ffmpeg 未安装，无法使用 API 下载")
        return False, 'ffmpeg 未安装'

    # 获取 DASH 流地址
    resp = session.get(
        BILIBILI_API_PLAY,
        params={
            "bvid": bvid, "cid": cid,
            "qn": 80, "fnval": 16, "fourk": 1,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') != 0:
        error = f"playurl API 错误: {data.get('message')} (code={data.get('code')})"
        print(f"      {error}")
        return False, error

    dash = data.get('data', {}).get('dash')
    if not dash:
        print("      无 DASH 流")
        return False, 'playurl API 未返回 DASH 流'

    videos = dash.get('video', [])
    audios = dash.get('audio', [])
    if not videos or not audios:
        print("      DASH 流不完整")
        return False, 'DASH 流不完整'

    best_v = max(videos, key=lambda x: x.get('bandwidth', 0))
    best_a = max(audios, key=lambda x: x.get('bandwidth', 0))

    video_url = best_v['baseUrl']
    audio_url = best_a['baseUrl']

    base = os.path.splitext(save_path)[0]
    video_tmp = base + '.video.m4s'
    audio_tmp = base + '.audio.m4s'

    headers = {
        "User-Agent": PC_UA,
        "Referer": "https://www.bilibili.com",
    }
    deadline_at = time.monotonic() + DOWNLOAD_DEADLINE_SECONDS

    print("      下载视频流 ...")
    _download_stream(
        video_url, video_tmp, headers, MAX_DOWNLOAD_BYTES, deadline_at
    )
    print("      下载音频流 ...")
    remaining_bytes = MAX_DOWNLOAD_BYTES - os.path.getsize(video_tmp)
    _download_stream(
        audio_url, audio_tmp, headers, remaining_bytes, deadline_at
    )

    print("      ffmpeg 合并中 ...")
    merge_cmd = [
        ffmpeg, '-y',
        '-i', video_tmp,
        '-i', audio_tmp,
        '-c', 'copy',
        '-movflags', '+faststart',
        save_path,
    ]
    result = subprocess.run(merge_cmd, capture_output=True, text=True, timeout=120)

    # 清理临时文件
    for tmp in [video_tmp, audio_tmp]:
        if os.path.exists(tmp):
            os.remove(tmp)

    if result.returncode == 0 and os.path.exists(save_path):
        size_mb = os.path.getsize(save_path) / 1048576
        print(f"✓ API+ffmpeg 下载完成: {save_path}  ({size_mb:.1f} MB)")
        return True, ''

    detail = result.stderr.strip().split('\n')[-1] if result.stderr else 'unknown'
    error = f'ffmpeg 合并失败: {detail}'
    print(f"      {error}")
    return False, error


def _download_stream(url: str, save_path: str, headers: dict,
                     max_bytes: int = MAX_DOWNLOAD_BYTES,
                     deadline_at: float | None = None) -> None:
    """下载单个 DASH 流（带进度条）"""
    if max_bytes < 1:
        raise RuntimeError(f'DASH 文件超过大小上限 {MAX_DOWNLOAD_BYTES} 字节')
    resp = requests.get(url, headers=headers, stream=True, timeout=60)
    resp.raise_for_status()

    total = int(resp.headers.get('content-length', 0))
    if total > max_bytes:
        raise RuntimeError(
            f'DASH 文件超过大小上限: {total} > {max_bytes} 字节'
        )
    downloaded = 0
    chunk_size = 1024 * 64
    deadline_at = deadline_at or (
        time.monotonic() + DOWNLOAD_DEADLINE_SECONDS
    )

    with open(save_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            if time.monotonic() > deadline_at:
                raise TimeoutError(
                    f'DASH 下载总时限超过 {DOWNLOAD_DEADLINE_SECONDS} 秒'
                )
            if downloaded + len(chunk) > max_bytes:
                raise RuntimeError(
                    f'DASH 文件超过大小上限 {MAX_DOWNLOAD_BYTES} 字节'
                )
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded / total * 100
                bar = '█' * int(pct // 2.5) + '░' * (40 - int(pct // 2.5))
                print(f'\r      [{bar}] {pct:5.1f}%  '
                      f'{downloaded/1048576:.1f}/{total/1048576:.1f} MB',
                      end='', flush=True)
    if total:
        print()
    if downloaded == 0:
        raise RuntimeError('DASH 直链返回空文件')
    if total and downloaded != total:
        raise RuntimeError(f'下载不完整: 期望 {total} 字节，实际 {downloaded} 字节')


def download_video(bvid: str, cid: int, save_path: str,
                   session: requests.Session,
                   cookie_file: str | None = None) -> None:
    """优先 API+ffmpeg，失败时回退到 yt-dlp"""
    print("      尝试 API+ffmpeg ...")
    api_error = ''
    try:
        success, api_error = download_with_api(bvid, cid, save_path, session)
        if success:
            return
    except Exception as e:
        api_error = str(e)
        print(f"      API+ffmpeg 下载失败: {e}")

    base = os.path.splitext(save_path)[0]
    for partial_path in [save_path, base + '.video.m4s', base + '.audio.m4s']:
        if os.path.exists(partial_path):
            os.remove(partial_path)

    print("      回退 yt-dlp 下载 ...")
    success, ytdlp_error = download_with_ytdlp(bvid, save_path, cookie_file)
    if success:
        return
    raise RuntimeError(
        f"API+ffmpeg 失败: {api_error}; {ytdlp_error}"
    )


def print_info(info: dict) -> None:
    """格式化打印视频元信息"""
    print(f"      作者    = {info['author']}")
    print(f"      发布时间= {info['publish_time']}")
    print(f"      标题    = {info['title'][:80]}{'...' if len(info['title']) > 80 else ''}")
    print(f"      时长    = {info['duration']}s")
    print(f"      播放    = {info['view_count']:,}")
    print(f"      点赞    = {info['like_count']:,}")
    print(f"      评论    = {info['reply_count']:,}")
    print(f"      分享    = {info['share_count']:,}")
    print(f"      弹幕    = {info['danmaku_count']:,}")


def save_meta(info: dict, save_dir: str) -> None:
    """将元信息保存为 元信息.json"""
    meta_path = os.path.join(save_dir, '元信息.json')
    meta = {k: v for k, v in info.items() if k != 'title_clean'}
    meta['source_url'] = f"https://www.bilibili.com/video/{info['bvid']}"
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"      元信息  -> {meta_path}")


def main():
    parser = argparse.ArgumentParser(description='B站视频下载脚本')
    parser.add_argument('url', help='B站视频链接或 BVID')
    parser.add_argument('save_dir', nargs='?', default='.', help='保存目录（默认当前目录）')
    parser.add_argument('--cookies', '-c', default=None,
                        help='Netscape cookie 文件路径（可选，传入登录态）')
    args = parser.parse_args()

    url = args.url.strip()

    print(f"[1/4] 解析 BVID ...")
    session = build_session(args.cookies)
    bvid = resolve_bvid(url, session)
    print(f"      BVID    = {bvid}")

    print(f"[2/4] 提取视频信息 ...")
    info = extract_video_info(bvid, session)
    print_info(info)

    save_path = os.path.join(args.save_dir, '视频文件.mp4')
    os.makedirs(args.save_dir, exist_ok=True)
    print(f"[3/4] 下载视频 -> {save_path}")
    download_video(bvid, info['cid'], save_path, session, args.cookies)

    print(f"[4/4] 保存元信息 ...")
    save_meta(info, args.save_dir)
    print("全部完成！")


if __name__ == '__main__':
    main()
