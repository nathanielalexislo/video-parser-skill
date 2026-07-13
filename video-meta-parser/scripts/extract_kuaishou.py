#!/usr/bin/env python3
"""
快手视频元信息提取（纯提取，不下载视频）。
被 video-meta-parser/scripts/meta_parser.py 动态加载。
"""

import re
import os
import http.cookiejar
from datetime import datetime, timezone, timedelta
import requests


MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.0 Mobile/15E148 Safari/604.1"
)


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


def _is_error_page(html: str) -> str:
    """检查页面是否为视频不存在/已失效等错误页，返回错误原因；正常则返回空字符串"""
    markers = [
        (r'视频不存在', '视频不存在'),
        (r'作品不存在', '作品不存在'),
        (r'视频已失效', '视频已失效'),
        (r'视频已删除', '视频已删除'),
        (r'视频不见了', '视频不存在'),
        (r'已失效', '链接已失效'),
        (r'404', '页面不存在（404）'),
    ]
    for pattern, reason in markers:
        if re.search(pattern, html):
            return reason
    return ''


def resolve_photo_id(url: str, session: requests.Session) -> str:
    """从各种快手链接格式中提取 photoId（使用移动端 UA）"""
    if re.fullmatch(r'[a-zA-Z0-9]+', url):
        return url

    if 'v.kuaishou.com' in url or 'v.m.chenzhongtech.com' in url:
        resp = session.get(url, allow_redirects=True)
        if resp.status_code >= 400:
            raise RuntimeError(f"短链访问失败，状态码: {resp.status_code}")
        error_reason = _is_error_page(resp.text)
        if error_reason:
            raise RuntimeError(f"短链指向的视频已失效：{error_reason}")
        url = resp.url

    m = re.search(r'/(?:short-video|fw/photo)/([a-zA-Z0-9]+)', url)
    if m:
        return m.group(1)

    m = re.search(r'photoId=([a-zA-Z0-9]+)', url)
    if m:
        return m.group(1)

    raise ValueError(f"无法从链接中提取 photoId: {url}")


def extract_video_info(photo_id: str, session: requests.Session) -> dict:
    """通过移动端页面提取视频直链及完整元信息"""
    page_url = f"https://v.m.chenzhongtech.com/fw/photo/{photo_id}"
    resp = session.get(page_url, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text

    error_reason = _is_error_page(html)
    if error_reason:
        raise RuntimeError(f"视频不存在或已失效：{error_reason}")

    def _first(pattern: str, default: str = '') -> str:
        m = re.search(pattern, html)
        return m.group(1) if m else default

    m = re.search(r'"url"\s*:\s*"(https?://[^"]*\.mp4[^"]*)"', html)
    if not m:
        m = re.search(
            r'(https?://[a-z0-9\-]+\.kwaicdn\.com/upic/[^\'"\s]+\.mp4[^\'"\s]*)',
            html
        )
    if not m:
        raise RuntimeError("未能从页面中提取视频直链，可能链接已失效或需要登录")
    video_url = m.group(1).replace('\\u002F', '/').replace('\\/', '/')

    caption = _first(r'"caption"\s*:\s*"([^"]*)"')
    caption_clean = re.sub(r'[\\/:*?"<>|\n\r]', '_', caption).strip()[:60] if caption else ''
    title = caption_clean or f"kuaishou_{photo_id}"

    author = _first(r'"userName"\s*:\s*"([^"]*)"')

    ts_raw = _first(r'"timestamp"\s*:\s*(\d+)', '0')
    try:
        ts_sec = int(ts_raw) / 1000
        bj_tz = timezone(timedelta(hours=8))
        publish_time = datetime.fromtimestamp(ts_sec, tz=bj_tz).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError):
        publish_time = ts_raw

    like_count = int(_first(r'"likeCount"\s*:\s*(\d+)', '0'))
    comment_count = int(_first(r'"commentCount"\s*:\s*(\d+)', '0'))
    view_count = int(_first(r'"viewCount"\s*:\s*(\d+)', '0'))
    share_count = int(_first(r'"shareCount"\s*:\s*(\d+)', '0'))

    return {
        'photo_id': photo_id,
        'video_url': video_url,
        'title': title,
        'author': author,
        'publish_time': publish_time,
        'caption': caption,
        'like_count': like_count,
        'comment_count': comment_count,
        'view_count': view_count,
        'share_count': share_count,
    }


def print_info(info: dict) -> None:
    """格式化打印视频元信息"""
    print(f"      作者    = {info['author']}")
    print(f"      发布时间= {info['publish_time']}")
    print(f"      内容    = {info['caption'][:80]}{'...' if len(info['caption']) > 80 else ''}")
    print(f"      点赞    = {info['like_count']:,}")
    print(f"      评论    = {info['comment_count']:,}")
    print(f"      播放    = {info['view_count']:,}")
    print(f"      分享    = {info['share_count']:,}")
