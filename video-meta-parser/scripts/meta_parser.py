#!/usr/bin/env python3
"""
视频元信息解析主控 — 自动识别平台，解析元信息，下载视频，转录音频。

支持抖音 / 快手 / 哔哩哔哩。

输出: <output_dir>/<id>/元信息.json + 视频文件.mp4 + _analysis/audio.wav

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
    transcription 音频转录结果（含语言、置信度、分段文本）
    metadata_success / download_success / audio_extract_success /
                  transcription_success 各处理阶段是否成功
    stage_errors  各阶段的错误详情
    success       元信息阶段是否成功（兼容字段）
    fail_reason   元信息失败原因（成功时为空字符串）
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


_WHISPER_WORKERS = {}
_WHISPER_WORKER_LOCK = threading.Lock()
_WHISPER_TIMEOUT_SECONDS = 300
_SUPPORTED_PLATFORMS = {'douyin', 'kuaishou', 'bilibili'}
_SAFE_OUTPUT_SUBDIR = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,254}')
_RETRY_TRANSACTION_FILENAME = '.network-retry-transaction.json'


def positive_int(value: str) -> int:
    """argparse 正整数参数。"""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError('必须是大于 0 的整数')
    return parsed


def validate_output_subdir(value: str) -> str:
    """只允许单层、可预测的视频目录名。"""
    if not isinstance(value, str) or not _SAFE_OUTPUT_SUBDIR.fullmatch(value):
        raise ValueError(f'不安全的输出子目录: {value!r}')
    return value


def safe_output_dir(output_root: str, output_subdir: str) -> str:
    """返回确认位于 output_root 内的真实目录路径。"""
    subdir = validate_output_subdir(output_subdir)
    root = os.path.realpath(os.path.abspath(output_root))
    candidate = os.path.realpath(os.path.join(root, subdir))
    if os.path.commonpath([root, candidate]) != root:
        raise ValueError(f'输出目录越界: {output_subdir!r}')
    return candidate


def safe_child_dir(parent_dir: str, child_name: str) -> str:
    """返回位于已验证视频目录内的固定子目录，拒绝符号链接越界。"""
    parent = os.path.realpath(os.path.abspath(parent_dir))
    child = os.path.realpath(os.path.join(parent, child_name))
    if os.path.commonpath([parent, child]) != parent:
        raise ValueError(f'子目录越界: {child_name!r}')
    return child


def reject_symlink_tree(root_dir: str) -> None:
    """拒绝产物目录中的符号链接，避免下载或合并时跟随到目录外。"""
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
    """动态加载同目录下的平台脚本模块"""
    path = os.path.join(scripts_dir, f"download_{name}.py")
    spec = importlib.util.spec_from_file_location(f"dl_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def extract_audio(video_path: str, output_path: str) -> bool:
    """提取音频为 16kHz 单声道 WAV"""
    cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le',
        '-ar', '16000', '-ac', '1', output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.returncode == 0 and os.path.exists(output_path)


class _WhisperWorker:
    """复用模型的常驻子进程，同时保留超时和崩溃隔离。"""

    def __init__(self, model_name: str, hf_endpoint: str):
        env = os.environ.copy()
        if hf_endpoint:
            env['HF_ENDPOINT'] = hf_endpoint

        worker_path = os.path.join(os.path.dirname(__file__), 'whisper_worker.py')
        self._process = subprocess.Popen(
            [sys.executable, worker_path, '--model', model_name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._responses = queue.Queue()
        self._request_lock = threading.Lock()
        self._stderr_lines = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        try:
            ready = self._wait_response(_WHISPER_TIMEOUT_SECONDS)
            if not ready.get('ready'):
                raise RuntimeError(ready.get('error') or '转录工作进程启动失败')
        except Exception:
            self.close()
            raise

    def _read_stdout(self) -> None:
        try:
            for line in self._process.stdout:
                try:
                    self._responses.put(json.loads(line))
                except json.JSONDecodeError:
                    self._responses.put({'error': f'转录工作进程输出无效: {line.strip()}'})
        finally:
            self._responses.put({'error': '转录工作进程已退出'})

    def _read_stderr(self) -> None:
        for line in self._process.stderr:
            self._stderr_lines.append(line.strip())
            del self._stderr_lines[:-20]

    def _wait_response(self, timeout: int) -> dict:
        try:
            return self._responses.get(timeout=timeout)
        except queue.Empty as e:
            raise TimeoutError(f'音频转录超过 {timeout} 秒') from e

    def transcribe(self, audio_path: str, timeout: int) -> dict:
        # 单个 worker 顺序处理请求，避免并发读写 JSONL 串线。
        with self._request_lock:
            if self._process.poll() is not None:
                details = self._stderr_lines[-1] if self._stderr_lines else '无错误输出'
                raise RuntimeError(f'转录工作进程已退出: {details}')
            self._process.stdin.write(
                json.dumps({'audio_path': audio_path}, ensure_ascii=False) + '\n'
            )
            self._process.stdin.flush()
            return self._wait_response(timeout)

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()


def _get_whisper_worker(model_name: str, hf_endpoint: str = '') -> _WhisperWorker:
    cache_key = (model_name, hf_endpoint)
    worker = _WHISPER_WORKERS.get(cache_key)
    if worker is not None and worker._process.poll() is None:
        return worker

    with _WHISPER_WORKER_LOCK:
        worker = _WHISPER_WORKERS.get(cache_key)
        if worker is None or worker._process.poll() is not None:
            worker = _WhisperWorker(model_name, hf_endpoint)
            _WHISPER_WORKERS[cache_key] = worker
        return worker


def _close_whisper_workers() -> None:
    for worker in list(_WHISPER_WORKERS.values()):
        worker.close()
    _WHISPER_WORKERS.clear()


atexit.register(_close_whisper_workers)


def transcribe_audio(audio_path: str, model_name: str = 'base',
                     hf_endpoint: str = '') -> dict:
    """通过常驻工作进程使用 faster-whisper 转录音频。"""
    cache_key = (model_name, hf_endpoint)
    try:
        worker = _get_whisper_worker(model_name, hf_endpoint)
        return worker.transcribe(audio_path, _WHISPER_TIMEOUT_SECONDS)
    except Exception as e:
        with _WHISPER_WORKER_LOCK:
            worker = _WHISPER_WORKERS.pop(cache_key, None)
            if worker is not None:
                worker.close()
        return {'error': str(e)}


def build_stage_status(
    metadata_success: bool,
    download_success: bool = False,
    audio_extract_success: bool = False,
    transcription_success: bool = False,
    stage_errors: dict | None = None,
    stage_retryable: dict | None = None,
) -> dict:
    """生成统一的阶段状态字段。"""
    errors = {
        'metadata': '',
        'download': '',
        'audio_extract': '',
        'transcription': '',
    }
    if stage_errors:
        errors.update(stage_errors)
    retryable = {
        'metadata': None,
        'download': None,
        'audio_extract': None,
        'transcription': None,
    }
    if stage_retryable:
        retryable.update(stage_retryable)
    return {
        'metadata_success': metadata_success,
        'download_success': download_success,
        'audio_extract_success': audio_extract_success,
        'transcription_success': transcription_success,
        'stage_errors': errors,
        'stage_retryable': retryable,
    }


def to_unified_meta(
    platform: str,
    info: dict,
    transcription: dict = None,
    *,
    download_success: bool = False,
    audio_extract_success: bool = False,
    transcription_success: bool = False,
    stage_errors: dict | None = None,
    stage_retryable: dict | None = None,
) -> dict:
    """将各平台原始字段映射为统一结构（字段顺序、命名、含义三平台一致）"""
    stage_status = build_stage_status(
        metadata_success=True,
        download_success=download_success,
        audio_extract_success=audio_extract_success,
        transcription_success=transcription_success,
        stage_errors=stage_errors,
        stage_retryable=stage_retryable,
    )
    if platform == 'douyin':
        vid = info['video_id']
        meta = {
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
            'transcription': transcription,
            'success':       True,
            'fail_reason':   '',
        }
        meta.update(stage_status)
        return meta
    if platform == 'kuaishou':
        vid = info['photo_id']
        meta = {
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
            'transcription': transcription,
            'success':       True,
            'fail_reason':   '',
        }
        meta.update(stage_status)
        return meta
    if platform == 'bilibili':
        vid = info['bvid']
        meta = {
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
            'transcription': transcription,
            'success':       True,
            'fail_reason':   '',
        }
        meta.update(stage_status)
        return meta
    raise ValueError(f"未知平台: {platform}")


def to_failed_meta(fail_reason: str, source_url: str = '', video_id: str = '') -> dict:
    """生成失败时的元信息结构，所有字段保持完整但为空。
    如果 resolve 阶段成功拿到了 video_id，可以填充 id 和 source_url。
    """
    meta = {
        'id':            video_id,
        'title':         '',
        'desc':          '',
        'publish_time':  '',
        'play_count':    0,
        'like_count':    0,
        'comment_count': 0,
        'share_count':   0,
        'author':        '',
        'source_url':    source_url,
        'transcription': None,
        'success':       False,
        'fail_reason':   fail_reason,
    }
    meta.update(build_stage_status(
        metadata_success=False,
        stage_errors={
            'metadata': fail_reason,
            'download': 'skipped: metadata failed',
            'audio_extract': 'skipped: download not available',
            'transcription': 'skipped: audio not available',
        },
        stage_retryable={
            'metadata': is_retryable_network_error(fail_reason, 'metadata'),
        },
    ))
    return meta


def parse_meta(platform: str, url: str, scripts_dir: str,
               cookie_file: str | None, output_dir: str,
               whisper_model: str = 'base', hf_endpoint: str = '',
               output_subdir: str | None = None) -> dict:
    """调用对应平台脚本解析元信息，下载视频，转录音频，返回统一结构 dict。
    如果 resolve 成功但 extract 失败，source_url 仍会填充。
    """
    video_id = None
    session = None
    try:
        mod = load_module(scripts_dir, platform)
        session = mod.build_session(cookie_file)
        
        if platform == 'kuaishou':
            video_id = mod.resolve_photo_id(url, session)
            info = mod.extract_video_info(video_id, session)
        elif platform == 'douyin':
            video_id = mod.resolve_video_id(url, session)
            info = mod.extract_video_info(video_id, session)
        elif platform == 'bilibili':
            video_id = mod.resolve_bvid(url, session)
            info = mod.extract_video_info(video_id, session)
        else:
            raise ValueError(f"未知平台: {platform}")
    except Exception as e:
        # resolve 或 extract 阶段失败
        fail_reason = str(e)
        # 如果 resolve 成功拿到了 video_id，仍然可以填充 source_url
        if video_id:
            if platform == 'kuaishou':
                source_url = f"https://www.kuaishou.com/short-video/{video_id}"
            elif platform == 'douyin':
                source_url = f"https://www.douyin.com/video/{video_id}"
            elif platform == 'bilibili':
                source_url = f"https://www.bilibili.com/video/{video_id}"
            else:
                source_url = ''
            return to_failed_meta(fail_reason, source_url=source_url, video_id=video_id)
        else:
            return to_failed_meta(fail_reason)
    finally:
        # 确保 session 被正确关闭
        if session:
            session.close()
    
    # 元信息提取成功，继续下载视频、提取音频和转录。
    transcription = None
    download_success = False
    audio_extract_success = False
    transcription_success = False
    stage_errors = {}
    stage_retryable = {}

    save_dir = safe_output_dir(output_dir, output_subdir or video_id)
    os.makedirs(save_dir, exist_ok=True)
    video_path = os.path.join(save_dir, '视频文件.mp4')

    # 下载视频（使用新 session，因为元信息 session 已关闭）。
    print(f"\n=== 下载视频 ===")
    try:
        reject_symlink_tree(save_dir)
        with mod.build_session(cookie_file) as download_session:
            if platform == 'kuaishou':
                mod.download_video(info['video_url'], video_path, download_session, cookie_file)
            elif platform == 'douyin':
                mod.download_video(info['video_url'], video_path, download_session, cookie_file)
            elif platform == 'bilibili':
                mod.download_video(video_id, info['cid'], video_path, download_session, cookie_file)
        download_success = True
        print(f"视频已保存: {video_path}")
    except Exception as e:
        stage_errors['download'] = str(e)
        stage_retryable['download'] = is_retryable_network_error(str(e), 'download')
        stage_errors['audio_extract'] = 'skipped: download failed'
        stage_errors['transcription'] = 'skipped: audio not available'
        print(f"视频下载失败: {e}")
        return to_unified_meta(
            platform,
            info,
            transcription,
            download_success=download_success,
            audio_extract_success=audio_extract_success,
            transcription_success=transcription_success,
            stage_errors=stage_errors,
            stage_retryable=stage_retryable,
        )

    print(f"\n=== 提取音频 ===")
    try:
        analysis_dir = safe_child_dir(save_dir, '_analysis')
        os.makedirs(analysis_dir, exist_ok=True)
        audio_path = os.path.join(analysis_dir, 'audio.wav')
        if os.path.islink(audio_path):
            raise ValueError(f'拒绝写入符号链接音频文件: {audio_path}')
        audio_extract_success = extract_audio(video_path, audio_path)
        if not audio_extract_success:
            stage_errors['audio_extract'] = 'ffmpeg 音频提取失败'
            stage_retryable['audio_extract'] = False
            stage_errors['transcription'] = 'skipped: audio not available'
            print("音频提取失败，跳过转录")
        else:
            print(f"音频已提取: {audio_path}")
    except Exception as e:
        stage_errors['audio_extract'] = str(e)
        stage_retryable['audio_extract'] = False
        stage_errors['transcription'] = 'skipped: audio not available'
        print(f"音频提取失败，跳过转录: {e}")

    if audio_extract_success:
        print(f"\n=== 转录音频 (model={whisper_model}) ===")
        transcription = transcribe_audio(audio_path, whisper_model, hf_endpoint)
        if 'error' in transcription:
            stage_errors['transcription'] = transcription['error']
            stage_retryable['transcription'] = is_retryable_network_error(
                transcription['error'], 'transcription'
            )
            print(f"转录失败: {transcription['error']}")
            transcription = None
        else:
            transcription_success = True
            print(f"转录完成: {len(transcription.get('segments', []))} 段")

    return to_unified_meta(
        platform,
        info,
        transcription,
        download_success=download_success,
        audio_extract_success=audio_extract_success,
        transcription_success=transcription_success,
        stage_errors=stage_errors,
        stage_retryable=stage_retryable,
    )


def resolve_url_only(url: str, cookies_dir: str, scripts_dir: str) -> dict:
    """仅解析短链接获取 video_id 和 source_url，不下载视频
    
    Returns:
        dict: {
            'url': str,
            'video_id': str | None,
            'source_url': str | None,
            'platform': str | None,
            'error': str | None
        }
    """
    result = {
        'url': url,
        'video_id': None,
        'source_url': None,
        'platform': None,
        'error': None
    }
    
    try:
        platform = detect_platform(url)
        result['platform'] = platform
        cookie_file = find_cookie(cookies_dir, platform)
        
        # 动态加载平台模块
        mod = load_module(scripts_dir, platform)
        
        # 使用 context manager 确保 session 正确关闭
        with mod.build_session(cookie_file) as session:
            # 解析 video_id
            if platform == 'kuaishou':
                video_id = mod.resolve_photo_id(url, session)
                result['source_url'] = f"https://www.kuaishou.com/short-video/{video_id}"
            elif platform == 'douyin':
                video_id = mod.resolve_video_id(url, session)
                result['source_url'] = f"https://www.douyin.com/video/{video_id}"
            elif platform == 'bilibili':
                video_id = mod.resolve_bvid(url, session)
                result['source_url'] = f"https://www.bilibili.com/video/{video_id}"
            
            result['video_id'] = video_id
        
    except Exception as e:
        result['error'] = str(e)
    
    return result


def process_single_url(url: str, output_dir: str, cookies_dir: str, scripts_dir: str,
                       whisper_model: str, hf_endpoint: str,
                       resolved_video_id: str | None = None,
                       resolved_platform: str | None = None,
                       output_subdir: str | None = None) -> dict:
    """处理单个 URL，返回结果摘要
    
    Returns:
        dict: {
            'url': str,
            'video_id': str | None,
            'source_url': str | None,
            'success': bool,
            'meta_path': str | None,
            'error': str | None
        }
    """
    result = {
        'url': url,
        'video_id': None,
        'source_url': None,
        'platform': resolved_platform,
        'output_subdir': output_subdir,
        'success': False,
        'metadata_success': False,
        'download_success': False,
        'audio_extract_success': False,
        'transcription_success': False,
        'stage_errors': {
            'metadata': '',
            'download': '',
            'audio_extract': '',
            'transcription': '',
        },
        'stage_retryable': {
            'metadata': None,
            'download': None,
            'audio_extract': None,
            'transcription': None,
        },
        'meta_path': None,
        'error': None
    }
    
    try:
        # 批量阶段 2 直接使用阶段 1 的 video_id，避免再次访问短链或长链。
        platform = resolved_platform or detect_platform(url)
        result['platform'] = platform
        cookie_file = find_cookie(cookies_dir, platform)
        processing_url = resolved_video_id or url
        
        # 解析元信息（包括下载和转录）
        meta = parse_meta(platform, processing_url, scripts_dir, cookie_file,
                         output_dir, whisper_model, hf_endpoint, output_subdir)

        for field in (
            'metadata_success',
            'download_success',
            'audio_extract_success',
            'transcription_success',
            'stage_errors',
            'stage_retryable',
        ):
            result[field] = meta[field]
        result['success'] = meta['success']
        result['video_id'] = meta['id'] or resolved_video_id
        result['source_url'] = meta.get('source_url')
        
        # 保存元信息
        if meta['success'] or meta['id']:
            save_dir = safe_output_dir(output_dir, output_subdir or meta['id'])
            os.makedirs(save_dir, exist_ok=True)
            meta_path = os.path.join(save_dir, '元信息.json')
            _atomic_write_json(meta_path, meta)
            
            result['meta_path'] = meta_path
            if not meta['success']:
                result['error'] = meta['fail_reason']
        else:
            result['error'] = meta['fail_reason']
            
    except Exception as e:
        result['error'] = str(e)
        result['stage_errors']['metadata'] = str(e)
        result['stage_retryable']['metadata'] = is_retryable_network_error(
            str(e), 'metadata'
        )
    
    return result


def process_all(urls: list[str], output_dir: str, cookies_dir: str, scripts_dir: str,
                whisper_model: str, hf_endpoint: str, concurrent: int, batch_size: int,
                output_subdir_overrides: dict | None = None) -> dict:
    """分批处理所有 URL（两阶段：先解析去重，再处理唯一视频）
    
    产出 3 个文件：
    1. mapping.jsonl - 短链映射关系（JSONL 格式）
    2. batch_summary.json - video_id 去重后的处理结果
    3. progress.json - 汇总进度统计
    
    Returns:
        dict: 进度统计
    """
    if concurrent < 1:
        raise ValueError('concurrent 必须大于 0')
    if batch_size < 1:
        raise ValueError('batch_size 必须大于 0')
    _recover_retry_transaction(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    
    # 基于 URL 字符串去重（保持顺序）
    unique_urls = list(dict.fromkeys(urls))
    if len(unique_urls) < len(urls):
        print(f"URL 去重: {len(urls)} -> {len(unique_urls)} 个")
    
    print(f"\n=== 阶段 1: 解析短链接 (并发 {concurrent}, 分批 {batch_size}) ===")
    
    # 阶段 1: 分批并发解析所有短链接获取 video_id
    resolve_results = []
    phase1_completed = 0
    phase1_success = 0
    
    # 分批处理
    resolve_batches = [unique_urls[i:i+batch_size] for i in range(0, len(unique_urls), batch_size)]
    total_resolve_batches = len(resolve_batches)
    
    for batch_num, batch_urls in enumerate(resolve_batches, 1):
        print(f"\n--- 解析批次 {batch_num}/{total_resolve_batches} ({len(batch_urls)} 个 URL) ---")
        
        batch_results = []
        batch_completed = 0
        
        with ThreadPoolExecutor(max_workers=concurrent) as executor:
            future_to_url = {
                executor.submit(resolve_url_only, url, cookies_dir, scripts_dir): url
                for url in batch_urls
            }
            
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    result = future.result()
                    batch_results.append(result)
                    batch_completed += 1
                    phase1_completed += 1
                    
                    # 打印进度
                    if result['video_id']:
                        phase1_success += 1
                        print(f"  [{batch_completed}/{len(batch_urls)}] ✓ {url} -> {result['video_id']}")
                    else:
                        print(f"  [{batch_completed}/{len(batch_urls)}] ✗ {url} -> 解析失败")
                        if result['error']:
                            print(f"         错误: {result['error'][:80]}")
                except Exception as e:
                    error_result = {
                        'url': url,
                        'video_id': None,
                        'source_url': None,
                        'platform': None,
                        'error': f'Unexpected error: {str(e)}'
                    }
                    batch_results.append(error_result)
                    batch_completed += 1
                    phase1_completed += 1
                    print(f"  [{batch_completed}/{len(batch_urls)}] ✗ {url} -> Unexpected error")
        
        resolve_results.extend(batch_results)
    
    # 保存 mapping.jsonl
    mapping_path = os.path.join(output_dir, 'mapping.jsonl')
    mapping_rows = []
    for result in resolve_results:
        mapping_entry = {
            'short_url': result['url'],
            'video_id': result['video_id'],
            'source_url': result['source_url'],
            'platform': result['platform'],
            'success': result['video_id'] is not None
        }
        if result.get('error'):
            mapping_entry['error'] = result['error']
        mapping_rows.append(mapping_entry)
    _atomic_write_jsonl(mapping_path, mapping_rows)
    
    # 基于 (平台, video_id) 去重，避免不同平台的同名 ID 冲突。
    video_key_to_urls = {}  # (platform, video_id) -> {info}
    failed_urls = []        # 解析失败的 URLs
    
    for result in resolve_results:
        if result['video_id']:
            vid = result['video_id']
            video_key = (result['platform'], vid)
            if video_key not in video_key_to_urls:
                video_key_to_urls[video_key] = {
                    'video_id': vid,
                    'source_url': result['source_url'],
                    'platform': result['platform'],
                    'urls': []
                }
            video_key_to_urls[video_key]['urls'].append(result['url'])
        else:
            failed_urls.append(result)

    output_subdir_overrides = output_subdir_overrides or {}
    output_subdir_overrides = {
        key: validate_output_subdir(subdir)
        for key, subdir in output_subdir_overrides.items()
    }
    known_video_keys = set(output_subdir_overrides) | set(video_key_to_urls)
    video_id_counts = {}
    for _, vid in known_video_keys:
        video_id_counts[vid] = video_id_counts.get(vid, 0) + 1
    for (platform, vid), video_info in video_key_to_urls.items():
        video_info['output_subdir'] = output_subdir_overrides.get(
            (platform, vid),
            vid if video_id_counts[vid] == 1 else f'{platform}_{vid}',
        )
    
    print(f"\n阶段 1 完成:")
    print(f"  总 URL 数: {len(unique_urls)}")
    print(f"  解析成功: {phase1_success}")
    print(f"  解析失败: {len(unique_urls) - phase1_success}")
    print(f"  唯一视频数: {len(video_key_to_urls)}")
    print(f"  映射文件: {mapping_path}")
    
    # 阶段 2: 分批并发处理唯一的视频
    print(f"\n=== 阶段 2: 处理唯一视频 (并发 {concurrent}, 分批 {batch_size}) ===")
    
    unique_video_keys = list(video_key_to_urls.keys())
    process_batches = [unique_video_keys[i:i+batch_size] for i in range(0, len(unique_video_keys), batch_size)]
    total_process_batches = len(process_batches)
    
    phase2_completed = 0
    phase2_success = 0
    stage_success_counts = {
        'metadata': 0,
        'download': 0,
        'audio_extract': 0,
        'transcription': 0,
    }
    all_results = []
    
    for batch_num, batch_video_keys in enumerate(process_batches, 1):
        print(f"\n--- 处理批次 {batch_num}/{total_process_batches} ({len(batch_video_keys)} 个视频) ---")
        
        # 汇报保留原始短链，实际处理使用阶段 1 已解析的 video_id。
        batch_items = []
        for video_key in batch_video_keys:
            video_info = video_key_to_urls[video_key]
            batch_items.append({
                'video_key': video_key,
                'url': video_info['urls'][0],
                'video_id': video_info['video_id'],
                'platform': video_info['platform'],
                'output_subdir': video_info['output_subdir'],
            })
        
        # 并发处理这一批
        batch_results = []
        batch_completed = 0
        
        with ThreadPoolExecutor(max_workers=concurrent) as executor:
            future_to_item = {
                executor.submit(
                    process_single_url, item['url'], output_dir, cookies_dir,
                    scripts_dir, whisper_model, hf_endpoint,
                    item['video_id'], item['platform'], item['output_subdir']
                ): item
                for item in batch_items
            }
            
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                url = item['url']
                try:
                    result = future.result()
                    result['all_urls'] = video_key_to_urls[item['video_key']]['urls']
                    batch_results.append(result)
                    batch_completed += 1
                    phase2_completed += 1
                    
                    # 打印进度
                    status = "✓" if result['metadata_success'] else "✗"
                    vid = result['video_id'] or 'N/A'
                    stage_marks = (
                        f"meta={'✓' if result['metadata_success'] else '✗'} "
                        f"download={'✓' if result['download_success'] else '✗'} "
                        f"audio={'✓' if result['audio_extract_success'] else '✗'} "
                        f"transcribe={'✓' if result['transcription_success'] else '✗'}"
                    )
                    print(
                        f"  [{batch_completed}/{len(batch_items)}] {status} "
                        f"{url} -> {vid} ({stage_marks})"
                    )
                    if not result['success'] and result['error']:
                        print(f"         错误: {result['error'][:80]}")
                    else:
                        for stage in (
                            'metadata',
                            'download',
                            'audio_extract',
                            'transcription',
                        ):
                            stage_error = result['stage_errors'].get(stage, '')
                            if stage_error and not stage_error.startswith('skipped:'):
                                print(f"         {stage} 错误: {stage_error[:80]}")

                    for stage, field in (
                        ('metadata', 'metadata_success'),
                        ('download', 'download_success'),
                        ('audio_extract', 'audio_extract_success'),
                        ('transcription', 'transcription_success'),
                    ):
                        if result[field]:
                            stage_success_counts[stage] += 1
                    
                    if result['success']:
                        phase2_success += 1
                        
                except Exception as e:
                    error_result = {
                        'url': url,
                        'video_id': item['video_id'],
                        'source_url': video_key_to_urls[item['video_key']]['source_url'],
                        'platform': item['platform'],
                        'output_subdir': item['output_subdir'],
                        'success': False,
                        'metadata_success': False,
                        'download_success': False,
                        'audio_extract_success': False,
                        'transcription_success': False,
                        'stage_errors': {
                            'metadata': f'Unexpected error: {str(e)}',
                            'download': '',
                            'audio_extract': '',
                            'transcription': '',
                        },
                        'stage_retryable': {
                            'metadata': is_retryable_network_error(str(e), 'metadata'),
                            'download': None,
                            'audio_extract': None,
                            'transcription': None,
                        },
                        'meta_path': None,
                        'error': f'Unexpected error: {str(e)}',
                        'all_urls': video_key_to_urls[item['video_key']]['urls'],
                    }
                    batch_results.append(error_result)
                    batch_completed += 1
                    phase2_completed += 1
                    print(f"  [{batch_completed}/{len(batch_items)}] ✗ {url} -> Unexpected error")
        
        all_results.extend(batch_results)
    
    # 保存 batch_summary.json（只包含去重后的唯一视频结果）
    summary_path = os.path.join(output_dir, 'batch_summary.json')
    _atomic_write_json(summary_path, all_results)
    
    # 保存 progress.json
    phase2_total = len(video_key_to_urls)

    def stage_progress(attempted: int, succeeded: int) -> dict:
        return {
            'attempted': attempted,
            'success': succeeded,
            'failed': attempted - succeeded,
            'skipped': phase2_total - attempted,
        }

    metadata_succeeded = stage_success_counts['metadata']
    download_succeeded = stage_success_counts['download']
    audio_succeeded = stage_success_counts['audio_extract']
    transcription_succeeded = stage_success_counts['transcription']
    stages = {
        'metadata': stage_progress(phase2_total, metadata_succeeded),
        'download': stage_progress(metadata_succeeded, download_succeeded),
        'audio_extract': stage_progress(download_succeeded, audio_succeeded),
        'transcription': stage_progress(audio_succeeded, transcription_succeeded),
    }

    progress = {
        'phase1_resolve': {
            'total': len(unique_urls),
            'completed': phase1_completed,
            'success': phase1_success,
            'failed': len(unique_urls) - phase1_success,
            'unique_videos': len(video_key_to_urls)
        },
        'phase2_process': {
            'total': phase2_total,
            'completed': phase2_completed,
            'success': phase2_success,
            'failed': phase2_completed - phase2_success,
            'stages': stages,
        }
    }
    
    progress_path = os.path.join(output_dir, 'progress.json')
    _atomic_write_json(progress_path, progress)
    
    # 打印最终汇总
    print(f"\n=== 批量处理完成 ===")
    print(f"\n阶段 1 - 解析短链接:")
    print(f"  总数: {progress['phase1_resolve']['total']}")
    print(f"  完成: {progress['phase1_resolve']['completed']}")
    print(f"  成功: {progress['phase1_resolve']['success']}")
    print(f"  失败: {progress['phase1_resolve']['failed']}")
    print(f"  唯一视频: {progress['phase1_resolve']['unique_videos']}")
    
    print(f"\n阶段 2 - 处理视频:")
    print(f"  总数: {progress['phase2_process']['total']}")
    print(f"  完成: {progress['phase2_process']['completed']}")
    print(f"  成功: {progress['phase2_process']['success']}")
    print(f"  失败: {progress['phase2_process']['failed']}")
    for stage, counts in progress['phase2_process']['stages'].items():
        print(
            f"  {stage}: attempted={counts['attempted']} "
            f"success={counts['success']} failed={counts['failed']} "
            f"skipped={counts['skipped']}"
        )
    
    print(f"\n产出文件:")
    print(f"  映射关系: {mapping_path}")
    print(f"  处理结果: {summary_path}")
    print(f"  进度统计: {progress_path}")
    
    return progress


_NON_RETRYABLE_ERROR_MARKERS = (
    '不支持的平台',
    '无法从链接中提取',
    'unsupported',
    'invalid url',
    'cookie',
    'login required',
    '请登录',
    '权限',
    'private',
    '已删除',
    '下架',
    'not found',
    '404',
    'unauthorized',
    '401',
    'forbidden',
    '403',
    '超过大小上限',
    'exceeds max-filesize',
    'file is larger than max-filesize',
    '直链返回非视频内容',
    '不是可解析的视频',
)

_RETRYABLE_NETWORK_MARKERS = (
    'timeout',
    'timed out',
    'connection',
    'remote disconnected',
    'broken pipe',
    'dns',
    'name resolution',
    'temporary failure',
    'network unreachable',
    'connection refused',
    'proxy error',
    'ssl error',
    'tls error',
    'too many requests',
    'rate limit',
    '429',
    '下载不完整',
    'incomplete download',
    'chunked encoding',
)


def is_retryable_network_error(error: str, stage: str) -> bool:
    """保守判断序列化后的错误是否适合一次网络重试。"""
    normalized = (error or '').strip().lower()
    if not normalized or normalized.startswith('skipped:'):
        return False
    if any(marker in normalized for marker in _NON_RETRYABLE_ERROR_MARKERS):
        return False
    if any(marker in normalized for marker in _RETRYABLE_NETWORK_MARKERS):
        return True
    if re.search(r'\b5\d\d\b', normalized):
        return True
    return False


def _load_json(path: str):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, 'r', encoding='utf-8') as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f'{path}:{line_number} JSON 解析失败: {e}') from e
    return rows


def _atomic_write_json(path: str, data) -> None:
    fd, temp_path = tempfile.mkstemp(
        prefix=f'.{os.path.basename(path)}-',
        suffix='.tmp',
        dir=os.path.dirname(path) or '.',
        text=True,
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        raise


def _atomic_write_jsonl(path: str, rows: list[dict]) -> None:
    fd, temp_path = tempfile.mkstemp(
        prefix=f'.{os.path.basename(path)}-',
        suffix='.tmp',
        dir=os.path.dirname(path) or '.',
        text=True,
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass
        raise


def _remove_path(path: str) -> None:
    """删除文件、链接或目录；目标不存在时忽略。"""
    if not os.path.lexists(path):
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def _validate_transaction_path(output_dir: str, path: str) -> str:
    """校验事务日志中的内部路径，防止被篡改后越界恢复。"""
    root = os.path.realpath(os.path.abspath(output_dir))
    candidate = os.path.realpath(os.path.abspath(path))
    if os.path.commonpath([root, candidate]) != root:
        raise ValueError(f'事务路径越界: {path!r}')
    return candidate


def _recover_retry_transaction(output_dir: str) -> bool:
    """发现未完成的重试合并时，恢复事务前的文件与媒体目录。"""
    output_dir = os.path.realpath(os.path.abspath(output_dir))
    journal_path = os.path.join(output_dir, _RETRY_TRANSACTION_FILENAME)
    if not os.path.exists(journal_path):
        return False

    journal = _load_json(journal_path)
    if not isinstance(journal, dict):
        raise ValueError(f'重试事务日志格式错误: {journal_path}')
    if journal.get('version') != 1:
        raise ValueError(f'不支持的重试事务日志版本: {journal.get("version")!r}')

    allowed_artifacts = {
        os.path.join(output_dir, 'mapping.jsonl'),
        os.path.join(output_dir, 'batch_summary.json'),
        os.path.join(output_dir, 'progress.json'),
    }
    file_entries = journal.get('files') or []
    directory_entries = journal.get('directories') or []

    # 先完整校验，确认日志没有越界路径后再执行任何恢复动作。
    for entry in file_entries:
        target = _validate_transaction_path(output_dir, entry['target'])
        _validate_transaction_path(output_dir, entry['prepared'])
        _validate_transaction_path(output_dir, entry['backup'])
        if target not in allowed_artifacts:
            raise ValueError(f'事务包含未知汇总文件: {target}')
    for entry in directory_entries:
        target = _validate_transaction_path(output_dir, entry['target'])
        _validate_transaction_path(output_dir, entry['prepared'])
        _validate_transaction_path(output_dir, entry['backup'])
        if os.path.dirname(target) != output_dir:
            raise ValueError(f'事务媒体目录不是单层目录: {target}')
        validate_output_subdir(os.path.basename(target))

    for entry in reversed(file_entries):
        target = os.path.realpath(os.path.abspath(entry['target']))
        backup = os.path.realpath(os.path.abspath(entry['backup']))
        if os.path.exists(backup):
            os.replace(backup, target)
        elif not entry.get('existed'):
            _remove_path(target)

    for entry in reversed(directory_entries):
        target = os.path.realpath(os.path.abspath(entry['target']))
        backup = os.path.realpath(os.path.abspath(entry['backup']))
        if os.path.exists(backup):
            _remove_path(target)
            os.replace(backup, target)
        elif not entry.get('existed'):
            _remove_path(target)

    for entry in file_entries + directory_entries:
        _remove_path(os.path.realpath(os.path.abspath(entry['prepared'])))
        _remove_path(os.path.realpath(os.path.abspath(entry['backup'])))
    os.remove(journal_path)
    print('已恢复上次中断的网络重试合并事务')
    return True


def _write_json_temp(path: str, data, *, jsonl: bool = False) -> str:
    """在目标文件同一文件系统准备完整内容，不替换原文件。"""
    fd, temp_path = tempfile.mkstemp(
        prefix=f'.retry-{os.path.basename(path)}-',
        suffix='.tmp',
        dir=os.path.dirname(path),
        text=True,
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            if jsonl:
                for row in data:
                    f.write(json.dumps(row, ensure_ascii=False) + '\n')
            else:
                json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        return temp_path
    except Exception:
        _remove_path(temp_path)
        raise


def _reserve_backup_path(output_dir: str, label: str) -> str:
    """生成同一文件系统中的唯一、尚不存在的备份路径。"""
    path = tempfile.mkdtemp(prefix=f'.retry-backup-{label}-', dir=output_dir)
    os.rmdir(path)
    return path


def _commit_retry_transaction(
    output_dir: str,
    directory_replacements: dict[str, str],
    artifact_values: list[tuple[str, object, bool]],
) -> None:
    """一起提交媒体目录和汇总文件；失败或中断时可整体恢复。"""
    output_dir = os.path.realpath(os.path.abspath(output_dir))
    journal_path = os.path.join(output_dir, _RETRY_TRANSACTION_FILENAME)
    if os.path.exists(journal_path):
        raise RuntimeError('存在未恢复的网络重试事务')

    file_entries = []
    directory_entries = []
    allowed_artifacts = {
        os.path.join(output_dir, 'mapping.jsonl'),
        os.path.join(output_dir, 'batch_summary.json'),
        os.path.join(output_dir, 'progress.json'),
    }
    try:
        for target, data, is_jsonl in artifact_values:
            target = _validate_transaction_path(output_dir, target)
            if target not in allowed_artifacts:
                raise ValueError(f'事务包含未知汇总文件: {target}')
            prepared = _write_json_temp(target, data, jsonl=is_jsonl)
            backup = _reserve_backup_path(output_dir, os.path.basename(target))
            existed = os.path.exists(target)
            if existed:
                shutil.copy2(target, backup)
            file_entries.append({
                'target': target,
                'prepared': prepared,
                'backup': backup,
                'existed': existed,
            })

        for target, prepared in directory_replacements.items():
            target = _validate_transaction_path(output_dir, target)
            prepared = _validate_transaction_path(output_dir, prepared)
            if os.path.dirname(target) != output_dir:
                raise ValueError(f'事务媒体目录不是单层目录: {target}')
            validate_output_subdir(os.path.basename(target))
            if not os.path.isdir(prepared):
                raise ValueError(f'事务待提交媒体目录不存在: {prepared}')
            directory_entries.append({
                'target': target,
                'prepared': prepared,
                'backup': _reserve_backup_path(
                    output_dir, os.path.basename(target)
                ),
                'existed': os.path.exists(target),
            })

        journal = {
            'version': 1,
            'files': file_entries,
            'directories': directory_entries,
        }
        _atomic_write_json(journal_path, journal)

        for entry in directory_entries:
            if entry['existed']:
                os.replace(entry['target'], entry['backup'])
            os.replace(entry['prepared'], entry['target'])
        for entry in file_entries:
            os.replace(entry['prepared'], entry['target'])

        # 所有目标都已替换后先删除日志。之后即使清理备份时中断，
        # 最终产物仍是一致的新版本，不应在下次运行时回滚。
        os.remove(journal_path)
    except Exception:
        if os.path.exists(journal_path):
            _recover_retry_transaction(output_dir)
        else:
            for entry in file_entries + directory_entries:
                _remove_path(entry['prepared'])
                _remove_path(entry['backup'])
        raise
    else:
        for entry in file_entries + directory_entries:
            _remove_path(entry['prepared'])
            _remove_path(entry['backup'])


def _infer_platform(item: dict) -> str | None:
    platform = item.get('platform')
    if platform:
        if platform not in _SUPPORTED_PLATFORMS:
            raise ValueError(f'不支持的产物平台字段: {platform!r}')
        return platform
    for value in (item.get('source_url'), item.get('url'), item.get('short_url')):
        if not value:
            continue
        try:
            return detect_platform(value)
        except ValueError:
            continue
    return None


def _result_key(item: dict) -> tuple[str, str] | None:
    platform = _infer_platform(item)
    video_id = item.get('video_id')
    if platform and video_id:
        return platform, str(video_id)
    return None


def _result_output_subdir(item: dict) -> str | None:
    if item.get('output_subdir'):
        return validate_output_subdir(item['output_subdir'])
    meta_path = item.get('meta_path')
    if meta_path:
        return validate_output_subdir(os.path.basename(os.path.dirname(meta_path)))
    video_id = item.get('video_id')
    return validate_output_subdir(str(video_id)) if video_id else None


def collect_network_retry_candidates(
    mapping_rows: list[dict],
    summary_rows: list[dict],
) -> dict[str, set[str]]:
    """收集需要重试的 URL 及目标阶段。"""
    candidates: dict[str, set[str]] = {}

    for row in mapping_rows:
        if row.get('success'):
            continue
        error = row.get('error', '')
        if is_retryable_network_error(error, 'resolve'):
            candidates.setdefault(row['short_url'], set()).add('resolve')

    stage_fields = (
        ('metadata', 'metadata_success'),
        ('download', 'download_success'),
        ('audio_extract', 'audio_extract_success'),
        ('transcription', 'transcription_success'),
    )
    for row in summary_rows:
        url = row.get('url') or next(iter(row.get('all_urls') or []), None)
        if not url:
            continue
        errors = row.get('stage_errors') or {}
        retryable = row.get('stage_retryable') or {}
        for stage, status_field in stage_fields:
            if row.get(status_field):
                continue
            error = errors.get(stage, '')
            decision = retryable.get(stage)
            if decision is True or (
                decision is None and is_retryable_network_error(error, stage)
            ):
                candidates.setdefault(url, set()).add(stage)

    return candidates


def _target_stage_succeeded(result: dict, target_stages: set[str]) -> bool:
    fields = {
        'metadata': 'metadata_success',
        'download': 'download_success',
        'audio_extract': 'audio_extract_success',
        'transcription': 'transcription_success',
    }
    return any(
        stage == 'resolve' or result.get(fields[stage], False)
        for stage in target_stages
    )


def _result_stage_rank(result: dict) -> int:
    """返回连续成功到达的最深阶段，避免重试结果覆盖更完整的旧记录。"""
    rank = 0
    for field in (
        'metadata_success',
        'download_success',
        'audio_extract_success',
        'transcription_success',
    ):
        if not result.get(field):
            break
        rank += 1
    return rank


def build_progress_from_artifacts(
    mapping_rows: list[dict],
    summary_rows: list[dict],
) -> dict:
    """根据合并后的最终产物重新计算进度，避免重试增量污染统计。"""
    phase2_total = len(summary_rows)
    stage_success = {
        'metadata': sum(bool(row.get('metadata_success')) for row in summary_rows),
        'download': sum(bool(row.get('download_success')) for row in summary_rows),
        'audio_extract': sum(bool(row.get('audio_extract_success')) for row in summary_rows),
        'transcription': sum(bool(row.get('transcription_success')) for row in summary_rows),
    }

    def stage_progress(attempted: int, succeeded: int) -> dict:
        if succeeded > attempted:
            raise ValueError(
                f'阶段状态不一致: success={succeeded} > attempted={attempted}'
            )
        return {
            'attempted': attempted,
            'success': succeeded,
            'failed': attempted - succeeded,
            'skipped': phase2_total - attempted,
        }

    stages = {
        'metadata': stage_progress(phase2_total, stage_success['metadata']),
        'download': stage_progress(stage_success['metadata'], stage_success['download']),
        'audio_extract': stage_progress(stage_success['download'], stage_success['audio_extract']),
        'transcription': stage_progress(
            stage_success['audio_extract'], stage_success['transcription']
        ),
    }
    phase1_success = sum(bool(row.get('success')) for row in mapping_rows)
    return {
        'phase1_resolve': {
            'total': len(mapping_rows),
            'completed': len(mapping_rows),
            'success': phase1_success,
            'failed': len(mapping_rows) - phase1_success,
            'unique_videos': phase2_total,
        },
        'phase2_process': {
            'total': phase2_total,
            'completed': phase2_total,
            'success': stage_success['metadata'],
            'failed': phase2_total - stage_success['metadata'],
            'stages': stages,
        },
    }


def retry_network_failures(
    output_dir: str,
    cookies_dir: str,
    scripts_dir: str,
    whisper_model: str,
    hf_endpoint: str,
    concurrent: int,
    batch_size: int,
) -> dict:
    """隔离重跑网络类失败，仅将成功改善的结果合并回原产物。"""
    output_dir = os.path.abspath(output_dir)
    _recover_retry_transaction(output_dir)
    mapping_path = os.path.join(output_dir, 'mapping.jsonl')
    summary_path = os.path.join(output_dir, 'batch_summary.json')
    progress_path = os.path.join(output_dir, 'progress.json')
    for path in (mapping_path, summary_path, progress_path):
        if not os.path.exists(path):
            raise FileNotFoundError(f'重试所需产物不存在: {path}')

    original_mapping = _load_jsonl(mapping_path)
    original_summary = _load_json(summary_path)
    if not isinstance(original_summary, list):
        raise ValueError(f'batch_summary.json 顶层必须是数组: {summary_path}')

    candidates = collect_network_retry_candidates(original_mapping, original_summary)
    if not candidates:
        print('没有检测到可重试的网络类失败，原产物未修改')
        return {
            'candidates': 0,
            'mapping_updated': 0,
            'summary_updated': 0,
            'output_dir': output_dir,
        }

    print(f'检测到 {len(candidates)} 个网络类失败 URL，开始隔离重试')
    for url, stages in candidates.items():
        print(f"  {url} -> {','.join(sorted(stages))}")

    output_subdir_overrides = {}
    for row in original_summary:
        key = _result_key(row)
        subdir = _result_output_subdir(row)
        if key and subdir:
            output_subdir_overrides[key] = subdir

    mapping_updated = 0
    summary_updated = 0
    with (
        tempfile.TemporaryDirectory(
            prefix='.network-retry-', dir=output_dir
        ) as retry_dir,
        tempfile.TemporaryDirectory(
            prefix='.network-retry-merge-', dir=output_dir
        ) as merge_root,
    ):
        directory_replacements = {}
        process_all(
            list(candidates),
            retry_dir,
            cookies_dir,
            scripts_dir,
            whisper_model,
            hf_endpoint,
            concurrent,
            batch_size,
            output_subdir_overrides,
        )
        retry_mapping = _load_jsonl(os.path.join(retry_dir, 'mapping.jsonl'))
        retry_summary = _load_json(os.path.join(retry_dir, 'batch_summary.json'))

        retry_mapping_by_url = {row['short_url']: row for row in retry_mapping}
        merged_mapping = []
        for row in original_mapping:
            retry_row = retry_mapping_by_url.get(row['short_url'])
            if not row.get('success') and retry_row and retry_row.get('success'):
                merged_mapping.append(retry_row)
                mapping_updated += 1
            else:
                merged_mapping.append(row)

        original_summary_by_key = {
            key: row
            for row in original_summary
            if (key := _result_key(row)) is not None
        }
        merged_summary_by_key = dict(original_summary_by_key)
        summary_order = [
            key for row in original_summary
            if (key := _result_key(row)) is not None
        ]
        replaced_keyless_urls = set()

        for retry_row in retry_summary:
            retry_key = _result_key(retry_row)
            if retry_key is None:
                continue
            retry_urls = retry_row.get('all_urls') or [retry_row.get('url')]
            target_stages = set().union(*(
                candidates.get(url, set()) for url in retry_urls if url
            ))
            if not target_stages or not _target_stage_succeeded(retry_row, target_stages):
                continue

            original_row = merged_summary_by_key.get(retry_key)
            if (
                original_row is not None
                and _result_stage_rank(retry_row) <= _result_stage_rank(original_row)
            ):
                continue
            original_urls = original_row.get('all_urls', []) if original_row else []
            retry_row['all_urls'] = list(dict.fromkeys(original_urls + retry_urls))
            retry_row['platform'] = retry_key[0]
            subdir = _result_output_subdir(retry_row)
            retry_row['output_subdir'] = subdir

            if subdir:
                source_dir = safe_output_dir(retry_dir, subdir)
                target_dir = safe_output_dir(output_dir, subdir)
                if os.path.isdir(source_dir):
                    reject_symlink_tree(source_dir)
                    reject_symlink_tree(target_dir)
                    prepared_dir = safe_output_dir(merge_root, subdir)
                    os.makedirs(prepared_dir, exist_ok=True)
                    if os.path.isdir(target_dir):
                        shutil.copytree(
                            target_dir,
                            prepared_dir,
                            dirs_exist_ok=True,
                            symlinks=True,
                        )
                    shutil.copytree(
                        source_dir,
                        prepared_dir,
                        dirs_exist_ok=True,
                        symlinks=True,
                    )
                    directory_replacements[target_dir] = prepared_dir
                target_meta_path = os.path.join(target_dir, '元信息.json')
                retry_row['meta_path'] = (
                    target_meta_path
                    if os.path.exists(
                        os.path.join(
                            directory_replacements.get(target_dir, target_dir),
                            '元信息.json',
                        )
                    )
                    else None
                )

            merged_summary_by_key[retry_key] = retry_row
            if retry_key not in summary_order:
                summary_order.append(retry_key)
            replaced_keyless_urls.update(url for url in retry_urls if url)
            summary_updated += 1

        # 用最终映射重建 all_urls，并移除已不再被任何输入引用的旧摘要。
        urls_by_key = {}
        for row in merged_mapping:
            if not row.get('success'):
                continue
            key = _result_key(row)
            if key:
                urls_by_key.setdefault(key, []).append(row['short_url'])

        merged_summary = []
        emitted_keys = set()

        def append_keyed_row(key: tuple[str, str]) -> None:
            if key in emitted_keys or key not in urls_by_key:
                return
            row = merged_summary_by_key[key]
            row['all_urls'] = list(dict.fromkeys(urls_by_key[key]))
            row['platform'] = key[0]
            row['output_subdir'] = _result_output_subdir(row)
            merged_summary.append(row)
            emitted_keys.add(key)

        # 保持原摘要顺序和无法建立稳定 key 的旧记录；若某条 keyless
        # 失败已被本次成功重试替代，则只保留新的 keyed 记录。
        for original_row in original_summary:
            key = _result_key(original_row)
            if key is not None:
                append_keyed_row(key)
                continue
            row_urls = set(
                original_row.get('all_urls')
                or ([original_row.get('url')] if original_row.get('url') else [])
            )
            if row_urls & replaced_keyless_urls:
                continue
            merged_summary.append(original_row)

        for key in summary_order:
            append_keyed_row(key)

        progress = build_progress_from_artifacts(merged_mapping, merged_summary)
        _commit_retry_transaction(
            output_dir,
            directory_replacements,
            [
                (mapping_path, merged_mapping, True),
                (summary_path, merged_summary, False),
                (progress_path, progress, False),
            ],
        )

    result = {
        'candidates': len(candidates),
        'mapping_updated': mapping_updated,
        'summary_updated': summary_updated,
        'output_dir': output_dir,
        'progress': progress,
    }
    print('\n=== 网络失败重试完成 ===')
    print(f"候选 URL: {result['candidates']}")
    print(f"映射更新: {result['mapping_updated']}")
    print(f"视频记录更新: {result['summary_updated']}")
    print(f"最终产物目录: {output_dir}")
    return result


def main():
    parser = argparse.ArgumentParser(
        description='视频元信息解析主控 - 支持单 URL 或批量处理',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 单 URL 模式
  python3 meta_parser.py "https://v.douyin.com/xxx"
  
  # 批量模式
  python3 meta_parser.py --input-file urls.txt --concurrent 8 --batch-size 100

  # 一键重试原产物中的网络类失败，并原位合并成功结果
  python3 meta_parser.py --retry-from ./videos --concurrent 8 --batch-size 100
        """)
    
    # 输入参数（互斥）
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('url', nargs='?', help='单个视频短链')
    input_group.add_argument('--input-file', help='批量输入文件（每行一个 URL）')
    input_group.add_argument(
        '--retry-from',
        help='从已有输出目录重试网络类失败，并将成功结果合并回原产物',
    )
    
    # 通用参数
    parser.add_argument(
        '--output-dir',
        default='./videos',
        help='输出根目录（--retry-from 模式使用原产物目录）',
    )
    parser.add_argument('--cookies-dir', default='.', help='cookie 文件所在目录')
    parser.add_argument('--scripts-dir', default=None, help='平台脚本目录（默认与本脚本同目录）')
    parser.add_argument('--whisper-model', default='base', help='Whisper 模型名称（默认: base）')
    parser.add_argument('--hf-endpoint', default='', help='Hugging Face endpoint（可选）')
    
    # 批量模式参数
    parser.add_argument('--concurrent', type=positive_int, default=8, help='并发数（默认: 8）')
    parser.add_argument('--batch-size', type=positive_int, default=100, help='每批大小（默认: 100）')
    
    args = parser.parse_args()
    
    scripts_dir = args.scripts_dir or os.path.dirname(os.path.abspath(__file__))

    if args.retry_from:
        try:
            retry_network_failures(
                args.retry_from,
                args.cookies_dir,
                scripts_dir,
                args.whisper_model,
                args.hf_endpoint,
                args.concurrent,
                args.batch_size,
            )
        except Exception as e:
            print(f'网络失败重试未完成: {e}')
            sys.exit(1)
        return

    # 若上次重试在事务提交中途被强制终止，任何新的正常任务开始前
    # 也先恢复旧产物，避免在半提交状态上继续写入。
    _recover_retry_transaction(args.output_dir)
    
    # 批量模式
    if args.input_file:
        if not os.path.exists(args.input_file):
            print(f"错误: 输入文件不存在: {args.input_file}")
            sys.exit(1)
        
        # 读取 URL 列表
        with open(args.input_file, 'r', encoding='utf-8') as f:
            urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        
        if not urls:
            print("错误: 输入文件为空")
            sys.exit(1)
        
        print(f"=== 批量模式 ===")
        print(f"输入文件: {args.input_file}")
        print(f"URL 数量: {len(urls)}")
        print(f"并发数: {args.concurrent}")
        print(f"批大小: {args.batch_size}")
        
        process_all(urls, args.output_dir, args.cookies_dir, scripts_dir,
                   args.whisper_model, args.hf_endpoint, args.concurrent, args.batch_size)
    
    # 单 URL 模式
    else:
        print("=== 视频元信息解析 ===")
        platform = detect_platform(args.url)
        print(f"平台: {platform}")

        cookie_file = find_cookie(args.cookies_dir, platform)
        if cookie_file:
            print(f"Cookie: {cookie_file}")
        else:
            print(f"Cookie: 未找到 cookies-{platform}.txt，将以匿名方式访问")

        try:
            meta = parse_meta(platform, args.url, scripts_dir, cookie_file,
                             args.output_dir, args.whisper_model, args.hf_endpoint)
        except Exception as e:
            # 解析过程中出现异常（平台识别失败等），直接打印错误，不创建任何文件
            print(f"\n=== 解析失败 ===")
            print(f"失败原因: {str(e)}")
            print(f"\nSUCCESS=false")
            print("METADATA_SUCCESS=false")
            print("DOWNLOAD_SUCCESS=false")
            print("AUDIO_EXTRACT_SUCCESS=false")
            print("TRANSCRIPTION_SUCCESS=false")
            return

        # 解析完成（可能成功或失败）
        if meta['success']:
            # 成功时以 id 为父目录
            save_dir = safe_output_dir(args.output_dir, meta['id'])
            os.makedirs(save_dir, exist_ok=True)
            meta_path = os.path.join(save_dir, '元信息.json')
            _atomic_write_json(meta_path, meta)

            print("\n=== 解析完成 ===")
            print(f"视频ID:   {meta['id']}")
            print(f"标题:     {meta['title']}")
            print(f"作者:     {meta['author']}")
            print(f"发布时间: {meta['publish_time']}")
            print(f"播放/点赞/评论/分享: "
                  f"{meta['play_count']:,} / {meta['like_count']:,} / "
                  f"{meta['comment_count']:,} / {meta['share_count']:,}")
            print(
                "阶段状态: "
                f"metadata={meta['metadata_success']} "
                f"download={meta['download_success']} "
                f"audio={meta['audio_extract_success']} "
                f"transcription={meta['transcription_success']}"
            )
            print(f"元信息:   {meta_path}")
            print(f"\nSUCCESS=true")
            print(f"METADATA_SUCCESS={str(meta['metadata_success']).lower()}")
            print(f"DOWNLOAD_SUCCESS={str(meta['download_success']).lower()}")
            print(f"AUDIO_EXTRACT_SUCCESS={str(meta['audio_extract_success']).lower()}")
            print(f"TRANSCRIPTION_SUCCESS={str(meta['transcription_success']).lower()}")
            print(f"ID={meta['id']}")
            print(f"SOURCE_URL={meta['source_url']}")
            print(f"META_JSON={meta_path}")
        else:
            # 失败时：如果有 id（resolve 成功），创建目录和 JSON；否则只打印错误
            if meta['id']:
                save_dir = safe_output_dir(args.output_dir, meta['id'])
                os.makedirs(save_dir, exist_ok=True)
                meta_path = os.path.join(save_dir, '元信息.json')
                _atomic_write_json(meta_path, meta)

                print(f"\n=== 解析失败 ===")
                print(f"失败原因: {meta['fail_reason']}")
                print(f"视频ID:   {meta['id']}")
                print(f"SOURCE_URL: {meta['source_url']}")
                print(f"元信息:   {meta_path}")
                print(f"\nSUCCESS=false")
                print("METADATA_SUCCESS=false")
                print("DOWNLOAD_SUCCESS=false")
                print("AUDIO_EXTRACT_SUCCESS=false")
                print("TRANSCRIPTION_SUCCESS=false")
                print(f"ID={meta['id']}")
                print(f"META_JSON={meta_path}")
            else:
                # resolve 失败，没有 id，不创建任何文件
                print(f"\n=== 解析失败 ===")
                print(f"失败原因: {meta['fail_reason']}")
                print(f"\nSUCCESS=false")
                print("METADATA_SUCCESS=false")
                print("DOWNLOAD_SUCCESS=false")
                print("AUDIO_EXTRACT_SUCCESS=false")
                print("TRANSCRIPTION_SUCCESS=false")
            return


if __name__ == '__main__':
    main()
