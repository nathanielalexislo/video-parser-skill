#!/usr/bin/env python3
"""
抖音视频下载脚本
用法:
    python3 download_douyin.py <抖音视频链接> [保存目录] [--cookies cookies.txt]
    python3 download_douyin.py https://v.douyin.com/xxxxx
    python3 download_douyin.py https://www.douyin.com/video/xxxxx ./videos
    python3 download_douyin.py https://v.douyin.com/xxxxx --cookies cookies-douyin.txt

功能:
    - 提取并下载视频直链（mp4，无水印）
    - 提取作者名称、发布时间、内容描述、点赞数、评论数、分享数
    - 将元信息保存为同名 .json 文件
    - 支持通过 Netscape cookie 文件传入登录态
"""

import re
import sys
import os
import json
import argparse
import shutil
import subprocess
import http.cookiejar
from datetime import datetime, timezone, timedelta
import requests
from urllib.parse import unquote


MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)

BJ_TZ = timezone(timedelta(hours=8))


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


def resolve_video_id(url: str, session: requests.Session) -> str:
    """从各种抖音链接格式中提取视频 ID（aweme_id）"""

    # 已经是纯数字 ID
    if re.fullmatch(r'\d{10,}', url):
        return url

    # 短链 / 长链 -> 跟随重定向拿到最终 URL
    resp = session.get(url, allow_redirects=True)
    final_url = resp.url

    # 从 URL 中提取: /video/7631022935200921779 或 /note/xxx
    m = re.search(r'/(?:video|note)/(\d+)', final_url)
    if m:
        return m.group(1)

    # query 参数 modal_id
    m = re.search(r'modal_id=(\d+)', final_url)
    if m:
        return m.group(1)

    # item_id
    m = re.search(r'item_id=(\d+)', final_url)
    if m:
        return m.group(1)

    raise ValueError(f"无法从链接中提取视频 ID: {final_url}")


def extract_video_info(video_id: str, session: requests.Session) -> dict:
    """
    通过移动端分享页面提取视频直链及完整元信息。
    返回 dict:
        video_url, title, author, publish_time, desc,
        like_count, comment_count, share_count
    """
    page_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    resp = session.get(page_url, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text

    def _first(pattern: str, default: str = '') -> str:
        m = re.search(pattern, html)
        return m.group(1) if m else default

    # ── 解析 _ROUTER_DATA JSON ───────────────────────────
    router_data = None
    m = re.search(r'_ROUTER_DATA\s*=\s*(\{.+?\})\s*</script>', html, re.DOTALL)
    if m:
        try:
            raw = m.group(1)
            # 只替换 \uXXXX 转义序列，避免 unicode_escape 对 UTF-8 中文双重编码
            raw = re.sub(
                r'\\u([0-9a-fA-F]{4})',
                lambda x: chr(int(x.group(1), 16)),
                raw
            )
            router_data = json.loads(raw, strict=False)
        except (json.JSONDecodeError, UnicodeDecodeError):
            router_data = None

    # 递归搜索 JSON 中第一个匹配 key 的值
    def _deep(obj, key, depth=8):
        if depth <= 0:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == key:
                    return v
                r = _deep(v, key, depth - 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = _deep(item, key, depth - 1)
                if r is not None:
                    return r
        return None

    # ── 视频直链（去水印：playwm → play）──────────────────
    search_in = json.dumps(router_data) if router_data else html
    play_m = re.search(r'playwm/\?video_id=([a-zA-Z0-9_\-]+)', search_in)
    if not play_m:
        play_m = re.search(r'play/\?video_id=([a-zA-Z0-9_\-]+)', search_in)
    if not play_m:
        raise RuntimeError("未能提取视频直链，可能链接已失效或需要登录")

    video_inner_id = play_m.group(1)
    video_url = (
        f"https://aweme.snssdk.com/aweme/v1/play/"
        f"?video_id={video_inner_id}&ratio=720p&line=0"
    )

    # ── 从解析后的 JSON 对象中提取字段 ────────────────────
    desc = ''
    author = ''
    ts_raw = '0'
    like_count = comment_count = share_count = play_count = 0

    if router_data:
        desc   = str(_deep(router_data, 'desc') or '')
        ts_raw = str(_deep(router_data, 'create_time') or '0')
        # 作者
        author_obj = _deep(router_data, 'author')
        if isinstance(author_obj, dict):
            author = str(author_obj.get('nickname', ''))
        if not author:
            author = str(_deep(router_data, 'nickname') or '')
        # 互动数据
        stats = _deep(router_data, 'statistics')
        if isinstance(stats, dict):
            like_count    = int(stats.get('digg_count', 0))
            comment_count = int(stats.get('comment_count', 0))
            share_count   = int(stats.get('share_count', 0))
            play_count    = int(stats.get('play_count', 0))
        else:
            like_count    = int(_deep(router_data, 'digg_count') or 0)
            comment_count = int(_deep(router_data, 'comment_count') or 0)
            share_count   = int(_deep(router_data, 'share_count') or 0)
            play_count    = int(_deep(router_data, 'play_count') or 0)
    else:
        desc   = _first(r'"desc"\s*:\s*"([^"]*)"')
        author = _first(r'"nickname"\s*:\s*"([^"]*)"')
        ts_raw = _first(r'"create_time"\s*:\s*(\d+)', '0')
        for key in ('digg_count', 'comment_count', 'share_count', 'play_count'):
            m2 = re.search(rf'"{key}"\s*:\s*(\d+)', html)
            val = int(m2.group(1)) if m2 else 0
            if key == 'digg_count':
                like_count = val
            elif key == 'comment_count':
                comment_count = val
            elif key == 'share_count':
                share_count = val
            elif key == 'play_count':
                play_count = val

    desc_clean = re.sub(r'[\\/:*?"<>|\n\r]', '_', desc).strip()[:60] if desc else ''
    title = desc_clean or f"douyin_{video_id}"

    # ── 发布时间（秒时间戳 → 北京时间字符串）────────────────
    try:
        publish_time = datetime.fromtimestamp(
            int(ts_raw), tz=BJ_TZ
        ).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError):
        publish_time = ts_raw

    return {
        'video_id':      video_id,
        'video_url':     video_url,
        'title':         title,
        'author':        author,
        'publish_time':  publish_time,
        'desc':          desc,
        'like_count':    like_count,
        'comment_count': comment_count,
        'share_count':   share_count,
        'play_count':    play_count,
    }


def download_with_ytdlp(url: str, save_path: str,
                        cookie_file: str | None = None) -> bool:
    """尝试用 yt-dlp 下载，成功返回 True"""
    ytdlp = shutil.which('yt-dlp')
    if not ytdlp:
        print("      yt-dlp 未安装，跳过")
        return False

    cmd = [
        ytdlp,
        '--no-warnings',
        '--no-check-certificates',
        '--force-generic-extractor',
        '-o', save_path,
    ]
    if cookie_file and os.path.exists(cookie_file):
        cmd += ['--cookies', cookie_file]
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0 and os.path.exists(save_path):
            size_mb = os.path.getsize(save_path) / 1048576
            print(f"✓ yt-dlp 下载完成: {save_path}  ({size_mb:.1f} MB)")
            return True
        alt = save_path + '.mp4'
        if os.path.exists(alt):
            os.rename(alt, save_path)
            size_mb = os.path.getsize(save_path) / 1048576
            print(f"✓ yt-dlp 下载完成: {save_path}  ({size_mb:.1f} MB)")
            return True
        err = result.stderr.strip().split('\n')[-1] if result.stderr else 'unknown'
        print(f"      yt-dlp 失败: {err}")
    except subprocess.TimeoutExpired:
        print("      yt-dlp 超时（120s）")
    except Exception as e:
        print(f"      yt-dlp 异常: {e}")
    return False


def download_with_requests(video_url: str, save_path: str,
                           session: requests.Session) -> None:
    """使用 requests 流式下载（备用方案）"""
    resp = session.get(video_url, stream=True, timeout=60, allow_redirects=True)
    resp.raise_for_status()

    total = int(resp.headers.get('content-length', 0))
    downloaded = 0
    chunk_size = 1024 * 64

    with open(save_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded / total * 100
                bar = '█' * int(pct // 2.5) + '░' * (40 - int(pct // 2.5))
                print(f'\r下载中 [{bar}] {pct:5.1f}%  '
                      f'{downloaded/1048576:.1f}/{total/1048576:.1f} MB',
                      end='', flush=True)

    size_mb = os.path.getsize(save_path) / 1048576
    print(f'\n✓ requests 下载完成: {save_path}  ({size_mb:.1f} MB)')


def download_video(video_url: str, save_path: str,
                   session: requests.Session,
                   cookie_file: str | None = None) -> None:
    """优先用 yt-dlp 下载，失败则回退到 requests 流式下载"""
    print("      尝试 yt-dlp ...")
    if download_with_ytdlp(video_url, save_path, cookie_file):
        return
    print("      回退 requests 下载 ...")
    download_with_requests(video_url, save_path, session)


def print_info(info: dict) -> None:
    """格式化打印视频元信息"""
    print(f"      作者    = {info['author']}")
    print(f"      发布时间= {info['publish_time']}")
    print(f"      内容    = {info['desc'][:80]}{'...' if len(info['desc']) > 80 else ''}")
    print(f"      点赞    = {info['like_count']:,}")
    print(f"      评论    = {info['comment_count']:,}")
    print(f"      分享    = {info['share_count']:,}")
    print(f"      播放    = {info['play_count']:,}")
    print(f"      直链    = {info['video_url'][:90]}...")


def save_meta(info: dict, save_path: str) -> None:
    """将元信息保存为同名 JSON 文件"""
    meta_path = os.path.splitext(save_path)[0] + '.json'
    meta = {k: v for k, v in info.items() if k != 'video_url'}
    meta['source_url'] = f"https://www.douyin.com/video/{info['video_id']}"
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"      元信息  -> {meta_path}")


def main():
    parser = argparse.ArgumentParser(description='抖音视频下载脚本')
    parser.add_argument('url', help='抖音视频链接或视频 ID')
    parser.add_argument('save_dir', nargs='?', default='.', help='保存目录（默认当前目录）')
    parser.add_argument('--cookies', '-c', default=None,
                        help='Netscape cookie 文件路径（可选，传入登录态）')
    args = parser.parse_args()

    url = args.url.strip()

    print(f"[1/4] 解析视频 ID ...")
    session = build_session(args.cookies)
    video_id = resolve_video_id(url, session)
    print(f"      videoId = {video_id}")

    print(f"[2/4] 提取视频信息 ...")
    info = extract_video_info(video_id, session)
    print_info(info)

    save_path = os.path.join(args.save_dir, f"{info['title']}.mp4")
    os.makedirs(args.save_dir, exist_ok=True)
    print(f"[3/4] 下载视频 -> {save_path}")
    download_video(info['video_url'], save_path, session, args.cookies)

    print(f"[4/4] 保存元信息 ...")
    save_meta(info, save_path)
    print("全部完成！")


if __name__ == '__main__':
    main()
