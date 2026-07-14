#!/usr/bin/env python3
"""
B站视频元信息提取（纯提取，不下载视频）。
被 video-meta-parser/scripts/meta_parser.py 动态加载。
"""

import re
import os
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


def _is_error_page(html: str) -> str:
    """检查页面是否为视频不存在/已失效等错误页，返回错误原因；正常则返回空字符串"""
    markers = [
        (r'视频不见了', '视频不见了'),
    ]
    for pattern, reason in markers:
        if re.search(pattern, html):
            return reason
    return ''


def resolve_bvid(url: str, session: requests.Session) -> str:
    """从各种 B站链接格式中提取 BVID"""
    if re.fullmatch(r'BV[a-zA-Z0-9]+', url):
        return url

    if 'b23.tv' in url:
        resp = session.get(url, allow_redirects=True)
        if resp.status_code >= 400:
            raise RuntimeError(f"短链访问失败，状态码: {resp.status_code}")
        url = resp.url

    m = re.search(r'(BV[a-zA-Z0-9]+)', url)
    if m:
        return m.group(1)

    raise ValueError(f"无法从链接中提取 BVID: {url}")


def extract_video_info(bvid: str, session: requests.Session) -> dict:
    """通过 B站 API 提取视频元信息"""
    resp = session.get(BILIBILI_API_VIEW, params={"bvid": bvid})
    resp.raise_for_status()
    data = resp.json()

    code = data.get('code')
    if code != 0:
        message = data.get('message', 'unknown')
        if code == -404 or '不存在' in message or '不可见' in message or '木有' in message:
            raise RuntimeError(f"B站视频不存在或不可见（code={code}）：{message}")
        raise RuntimeError(f"B站 API 错误（code={code}）：{message}")

    v = data['data']
    stat = v.get('stat', {})
    owner = v.get('owner', {})

    pubdate = v.get('pubdate', 0)
    try:
        publish_time = datetime.fromtimestamp(pubdate, tz=BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError):
        publish_time = str(pubdate)

    title = v.get('title', f'bilibili_{bvid}')
    title_clean = re.sub(r'[\\/:*?"<>|\n\r]', '_', title).strip()[:60] or f'bilibili_{bvid}'

    return {
        'bvid': bvid,
        'aid': v.get('aid', 0),
        'cid': v.get('cid', 0),
        'title': title,
        'title_clean': title_clean,
        'author': owner.get('name', ''),
        'author_mid': owner.get('mid', 0),
        'publish_time': publish_time,
        'desc': v.get('desc', ''),
        'duration': v.get('duration', 0),
        'view_count': stat.get('view', 0),
        'like_count': stat.get('like', 0),
        'reply_count': stat.get('reply', 0),
        'share_count': stat.get('share', 0),
        'danmaku_count': stat.get('danmaku', 0),
    }


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
