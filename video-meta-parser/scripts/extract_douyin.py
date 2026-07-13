#!/usr/bin/env python3
"""
抖音视频元信息提取（纯提取，不下载视频）。
被 video-meta-parser/scripts/meta_parser.py 动态加载。
"""

import re
import os
import json
import http.cookiejar
from datetime import datetime, timezone, timedelta
import requests


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
        (r'内容无法查看', '内容无法查看'),
    ]
    for pattern, reason in markers:
        if re.search(pattern, html):
            return reason
    return ''


def resolve_video_id(url: str, session: requests.Session) -> str:
    """从各种抖音链接格式中提取视频 ID（aweme_id）"""
    if re.fullmatch(r'\d{10,}', url):
        return url

    resp = session.get(url, allow_redirects=True)
    if resp.status_code >= 400:
        raise RuntimeError(f"短链访问失败，状态码: {resp.status_code}")
    error_reason = _is_error_page(resp.text)
    if error_reason:
        raise RuntimeError(f"短链指向的视频已失效：{error_reason}")
    final_url = resp.url

    m = re.search(r'/(?:video|note)/(\d+)', final_url)
    if m:
        return m.group(1)

    m = re.search(r'modal_id=(\d+)', final_url)
    if m:
        return m.group(1)

    m = re.search(r'item_id=(\d+)', final_url)
    if m:
        return m.group(1)

    raise ValueError(f"无法从链接中提取视频 ID: {final_url}")


def _deep(obj, key, depth=8):
    """递归搜索 JSON 中第一个匹配 key 的值"""
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


def extract_video_info(video_id: str, session: requests.Session) -> dict:
    """通过移动端分享页面提取视频直链及完整元信息"""
    page_url = f"https://www.iesdouyin.com/share/video/{video_id}/"
    resp = session.get(page_url, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text

    error_reason = _is_error_page(html)
    if error_reason:
        raise RuntimeError(f"视频不存在或已失效：{error_reason}")

    def _first(pattern: str, default: str = '') -> str:
        m = re.search(pattern, html)
        return m.group(1) if m else default

    router_data = None
    m = re.search(r'_ROUTER_DATA\s*=\s*(\{.+?\})\s*</script>', html, re.DOTALL)
    if m:
        try:
            raw = m.group(1)
            raw = re.sub(
                r'\\u([0-9a-fA-F]{4})',
                lambda x: chr(int(x.group(1), 16)),
                raw
            )
            router_data = json.loads(raw, strict=False)
        except (json.JSONDecodeError, UnicodeDecodeError):
            router_data = None

    # 视频直链（去水印：playwm → play）
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

    desc = ''
    author = ''
    ts_raw = '0'
    like_count = comment_count = share_count = play_count = 0

    if router_data:
        desc = str(_deep(router_data, 'desc') or '')
        ts_raw = str(_deep(router_data, 'create_time') or '0')
        author_obj = _deep(router_data, 'author')
        if isinstance(author_obj, dict):
            author = str(author_obj.get('nickname', ''))
        if not author:
            author = str(_deep(router_data, 'nickname') or '')
        stats = _deep(router_data, 'statistics')
        if isinstance(stats, dict):
            like_count = int(stats.get('digg_count', 0))
            comment_count = int(stats.get('comment_count', 0))
            share_count = int(stats.get('share_count', 0))
            play_count = int(stats.get('play_count', 0))
        else:
            like_count = int(_deep(router_data, 'digg_count') or 0)
            comment_count = int(_deep(router_data, 'comment_count') or 0)
            share_count = int(_deep(router_data, 'share_count') or 0)
            play_count = int(_deep(router_data, 'play_count') or 0)
    else:
        desc = _first(r'"desc"\s*:\s*"([^"]*)"')
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

    try:
        publish_time = datetime.fromtimestamp(int(ts_raw), tz=BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError):
        publish_time = ts_raw

    return {
        'video_id': video_id,
        'video_url': video_url,
        'title': title,
        'author': author,
        'publish_time': publish_time,
        'desc': desc,
        'like_count': like_count,
        'comment_count': comment_count,
        'share_count': share_count,
        'play_count': play_count,
    }


def print_info(info: dict) -> None:
    """格式化打印视频元信息"""
    print(f"      作者    = {info['author']}")
    print(f"      发布时间= {info['publish_time']}")
    print(f"      内容    = {info['desc'][:80]}{'...' if len(info['desc']) > 80 else ''}")
    print(f"      点赞    = {info['like_count']:,}")
    print(f"      评论    = {info['comment_count']:,}")
    print(f"      分享    = {info['share_count']:,}")
    print(f"      播放    = {info['play_count']:,}")
