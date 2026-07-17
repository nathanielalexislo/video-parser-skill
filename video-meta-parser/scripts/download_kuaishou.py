#!/usr/bin/env python3
"""
快手视频下载脚本
用法:
    python3 download_kuaishou.py <快手视频链接> [保存目录] [--cookies cookies.txt]
    python3 download_kuaishou.py https://v.kuaishou.com/xxxxx
    python3 download_kuaishou.py https://www.kuaishou.com/short-video/xxxxx ./videos
    python3 download_kuaishou.py https://v.kuaishou.com/xxxxx --cookies cookies-kuaishou.txt

功能:
    - 提取并下载视频直链（mp4）
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


MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)
REQUEST_TIMEOUT = (10, 30)
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024
DOWNLOAD_DEADLINE_SECONDS = 30 * 60



def build_session(cookie_file: str | None = None) -> requests.Session:
    """构建带移动端 UA 的 requests Session，可选加载 cookie 文件"""
    session = requests.Session()
    session.headers.update({"User-Agent": MOBILE_UA})
    if cookie_file and os.path.exists(cookie_file):
        jar = http.cookiejar.MozillaCookieJar(cookie_file)
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies.update(jar)
        print(f"      已加载 cookie: {cookie_file}  ({len(jar)} 条)")
    return session


def resolve_photo_id(url: str, session: requests.Session) -> str:
    """从各种快手链接格式中提取 photoId（使用移动端 UA）"""

    # 已经是 photoId 格式（纯字母数字）
    if re.fullmatch(r'[a-zA-Z0-9]+', url):
        return url

    # 短链 -> 跟随重定向拿到最终 URL
    if 'v.kuaishou.com' in url or 'v.m.chenzhongtech.com' in url:
        resp = session.get(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        url = resp.url

    # 从 URL 路径中提取 photoId
    # 格式: /short-video/3xxuq837um73cqe?...
    # 格式: /fw/photo/3xxuq837um73cqe?...
    m = re.search(r'/(?:short-video|fw/photo)/([a-zA-Z0-9]+)', url)
    if m:
        return m.group(1)

    # 兜底：从 query 参数中取 photoId
    m = re.search(r'photoId=([a-zA-Z0-9]+)', url)
    if m:
        return m.group(1)

    raise ValueError(f"无法从链接中提取 photoId: {url}")


def extract_video_info(photo_id: str, session: requests.Session) -> dict:
    """
    通过移动端页面提取视频直链及完整元信息。
    返回 dict:
        video_url, title, author, publish_time, caption,
        like_count, comment_count, view_count, share_count, head_url
    """
    page_url = f"https://v.m.chenzhongtech.com/fw/photo/{photo_id}"
    resp = session.get(page_url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    html = resp.text

    def _first(pattern: str, default: str = '') -> str:
        m = re.search(pattern, html)
        return m.group(1) if m else default

    # ── 视频直链 ──────────────────────────────────────────
    m = re.search(r'"url"\s*:\s*"(https?://[^"]*\.mp4[^"]*)"', html)
    if not m:
        m = re.search(
            r'(https?://[a-z0-9\-]+\.kwaicdn\.com/upic/[^\'"\s]+\.mp4[^\'"\s]*)',
            html
        )
    if not m:
        raise RuntimeError("未能从页面中提取视频直链，可能链接已失效或需要登录")
    video_url = m.group(1).replace('\\u002F', '/').replace('\\/', '/')

    # ── 标题 / 内容描述 ──────────────────────────────────
    caption = _first(r'"caption"\s*:\s*"([^"]*)"')
    caption_clean = re.sub(r'[\\/:*?"<>|\n\r]', '_', caption).strip()[:60] if caption else ''
    title = caption_clean or f"kuaishou_{photo_id}"

    # ── 作者名称 ──────────────────────────────────────────
    author = _first(r'"userName"\s*:\s*"([^"]*)"')

    # ── 发布时间（毫秒时间戳 → 北京时间字符串）──────────────
    ts_raw = _first(r'"timestamp"\s*:\s*(\d+)', '0')
    try:
        ts_sec = int(ts_raw) / 1000
        bj_tz = timezone(timedelta(hours=8))
        publish_time = datetime.fromtimestamp(ts_sec, tz=bj_tz).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError):
        publish_time = ts_raw

    # ── 互动数据 ──────────────────────────────────────────
    like_count    = int(_first(r'"likeCount"\s*:\s*(\d+)',    '0'))
    comment_count = int(_first(r'"commentCount"\s*:\s*(\d+)', '0'))
    view_count    = int(_first(r'"viewCount"\s*:\s*(\d+)',    '0'))
    share_count   = int(_first(r'"shareCount"\s*:\s*(\d+)',   '0'))

    # ── 作者头像 ──────────────────────────────────────────
    head_url = _first(r'"headUrl"\s*:\s*"([^"]*)"')

    return {
        'photo_id':      photo_id,
        'video_url':     video_url,
        'title':         title,
        'author':        author,
        'publish_time':  publish_time,
        'caption':       caption,
        'like_count':    like_count,
        'comment_count': comment_count,
        'view_count':    view_count,
        'share_count':   share_count,
        'head_url':      head_url,
    }


def download_with_ytdlp(video_url: str, save_path: str,
                        cookie_file: str | None = None) -> tuple[bool, str]:
    """尝试用 yt-dlp 下载，并保留失败原因供上层判断是否可重试。"""
    ytdlp = shutil.which('yt-dlp')
    if not ytdlp:
        print("      yt-dlp 未安装，跳过")
        return False, 'yt-dlp 未安装'

    cmd = [
        ytdlp,
        '--no-warnings',
        '--no-check-certificates',
        '--socket-timeout', '60',
        '--max-filesize', str(MAX_DOWNLOAD_BYTES),
        '-o', save_path,
    ]
    if cookie_file and os.path.exists(cookie_file):
        cmd += ['--cookies', cookie_file]
    # 直链是普通 mp4 URL，强制用 generic 提取器
    cmd += ['--force-generic-extractor', video_url]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0 and os.path.exists(save_path):
            if os.path.getsize(save_path) > MAX_DOWNLOAD_BYTES:
                os.remove(save_path)
                return False, f'yt-dlp 文件超过大小上限 {MAX_DOWNLOAD_BYTES} 字节'
            size_mb = os.path.getsize(save_path) / 1048576
            print(f"✓ yt-dlp 下载完成: {save_path}  ({size_mb:.1f} MB)")
            return True, ''
        # yt-dlp 可能输出到带扩展名的文件，检查一下
        alt = save_path + '.mp4'
        if result.returncode == 0 and os.path.exists(alt):
            if os.path.getsize(alt) > MAX_DOWNLOAD_BYTES:
                os.remove(alt)
                return False, f'yt-dlp 文件超过大小上限 {MAX_DOWNLOAD_BYTES} 字节'
            os.rename(alt, save_path)
            size_mb = os.path.getsize(save_path) / 1048576
            print(f"✓ yt-dlp 下载完成: {save_path}  ({size_mb:.1f} MB)")
            return True, ''
        err = result.stderr.strip().split('\n')[-1] if result.stderr else 'unknown error'
        print(f"      yt-dlp 失败: {err}")
        return False, f'yt-dlp 失败: {err}'
    except subprocess.TimeoutExpired:
        print("      yt-dlp 超时（120s）")
        return False, 'yt-dlp 超时（120s）'
    except Exception as e:
        print(f"      yt-dlp 异常: {e}")
        return False, f'yt-dlp 异常: {e}'


def download_with_requests(video_url: str, save_path: str,
                           session: requests.Session) -> None:
    """使用 requests 流式下载"""
    session.headers.update({"Referer": "https://v.m.chenzhongtech.com/"})
    resp = session.get(video_url, stream=True, timeout=60)
    resp.raise_for_status()

    content_type = resp.headers.get('content-type', '').split(';', 1)[0].lower()
    if content_type.startswith('text/') or content_type in {
        'application/json', 'application/xml'
    }:
        raise RuntimeError(f'直链返回非视频内容: {content_type}')

    total = int(resp.headers.get('content-length', 0))
    if total > MAX_DOWNLOAD_BYTES:
        raise RuntimeError(
            f'直链文件超过大小上限: {total} > {MAX_DOWNLOAD_BYTES} 字节'
        )
    downloaded = 0
    chunk_size = 1024 * 64  # 64KB
    started_at = time.monotonic()

    with open(save_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            if time.monotonic() - started_at > DOWNLOAD_DEADLINE_SECONDS:
                raise TimeoutError(
                    f'直链下载总时限超过 {DOWNLOAD_DEADLINE_SECONDS} 秒'
                )
            if downloaded + len(chunk) > MAX_DOWNLOAD_BYTES:
                raise RuntimeError(
                    f'直链文件超过大小上限 {MAX_DOWNLOAD_BYTES} 字节'
                )
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded / total * 100
                bar = '█' * int(pct // 2.5) + '░' * (40 - int(pct // 2.5))
                print(f'\r下载中 [{bar}] {pct:5.1f}%  '
                      f'{downloaded/1048576:.1f}/{total/1048576:.1f} MB',
                      end='', flush=True)

    if downloaded == 0:
        raise RuntimeError('直链返回空文件')
    if total and downloaded != total:
        raise RuntimeError(f'下载不完整: 期望 {total} 字节，实际 {downloaded} 字节')

    ffprobe = shutil.which('ffprobe')
    if ffprobe:
        probe = subprocess.run(
            [
                ffprobe, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', save_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0 or 'video' not in probe.stdout:
            raise RuntimeError('下载内容不是可解析的视频')

    print(f'\n✓ requests 下载完成: {save_path}  '
          f'({os.path.getsize(save_path)/1048576:.1f} MB)')


def download_video(video_url: str, save_path: str,
                   session: requests.Session,
                   cookie_file: str | None = None) -> None:
    """优先直连下载 MP4，失败时回退到 yt-dlp"""
    print("      尝试 requests 直链下载 ...")
    direct_error = ''
    try:
        download_with_requests(video_url, save_path, session)
        return
    except Exception as e:
        direct_error = str(e)
        # 直链可能因防盗链、过期或网络波动失败，保留原有 yt-dlp 容错。
        print(f"      requests 直链下载失败: {e}")
        if os.path.exists(save_path):
            os.remove(save_path)

    print("      回退 yt-dlp 下载 ...")
    success, ytdlp_error = download_with_ytdlp(video_url, save_path, cookie_file)
    if not success:
        raise RuntimeError(
            f"requests 直链失败: {direct_error}; {ytdlp_error}"
        )


def print_info(info: dict) -> None:
    """格式化打印视频元信息"""
    print(f"      作者    = {info['author']}")
    print(f"      发布时间= {info['publish_time']}")
    print(f"      内容    = {info['caption'][:80]}{'...' if len(info['caption']) > 80 else ''}")
    print(f"      点赞    = {info['like_count']:,}")
    print(f"      评论    = {info['comment_count']:,}")
    print(f"      播放    = {info['view_count']:,}")
    print(f"      分享    = {info['share_count']:,}")
    print(f"      直链    = {info['video_url'][:90]}...")


def save_meta(info: dict, save_dir: str) -> None:
    """将元信息保存为 元信息.json"""
    meta_path = os.path.join(save_dir, '元信息.json')
    meta = {k: v for k, v in info.items() if k != 'video_url'}
    meta['source_url'] = f"https://www.kuaishou.com/short-video/{info['photo_id']}"
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"      元信息  -> {meta_path}")


def main():
    parser = argparse.ArgumentParser(description='快手视频下载脚本')
    parser.add_argument('url', help='快手视频链接或 photoId')
    parser.add_argument('save_dir', nargs='?', default='.', help='保存目录（默认当前目录）')
    parser.add_argument('--cookies', '-c', default=None,
                        help='Netscape cookie 文件路径（可选，传入登录态）')
    args = parser.parse_args()

    url = args.url.strip()

    print(f"[1/4] 解析 photoId ...")
    session = build_session(args.cookies)
    photo_id = resolve_photo_id(url, session)
    print(f"      photoId = {photo_id}")

    print(f"[2/4] 提取视频信息 ...")
    info = extract_video_info(photo_id, session)
    print_info(info)

    save_path = os.path.join(args.save_dir, '视频文件.mp4')
    os.makedirs(args.save_dir, exist_ok=True)
    print(f"[3/4] 下载视频 -> {save_path}")
    download_video(info['video_url'], save_path, session, args.cookies)

    print(f"[4/4] 保存元信息 ...")
    save_meta(info, args.save_dir)
    print("全部完成！")


if __name__ == '__main__':
    main()
