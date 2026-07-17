#!/usr/bin/env python3
"""
视频内容分析脚本 — 提取关键帧，读取转录结果，输出供 agent 整合的结构化数据。
依赖 video-meta-parser 已完成视频下载、音频提取和转录。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from typing import Optional


DEFAULT_FRAME_INTERVAL = 2
DEFAULT_MAX_FRAMES = 60
MAX_ALLOWED_FRAMES = 300
FFPROBE_TIMEOUT_SECONDS = 30
FFMPEG_TIMEOUT_SECONDS = 300


def nonnegative_float(value, default: float = 0.0) -> float:
    """兼容 ffprobe 的 N/A、null、NaN 等非数值字段。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed < 0:
        return default
    return parsed


def safe_analysis_dir(save_dir: str) -> str:
    """生成位于视频目录内的分析目录，拒绝符号链接越界。"""
    root = os.path.realpath(os.path.abspath(save_dir))
    analysis_dir = os.path.realpath(os.path.join(root, '_analysis'))
    if os.path.commonpath([root, analysis_dir]) != root:
        raise RuntimeError(f'分析目录越界: {analysis_dir}')
    return analysis_dir


def get_video_info(video_path: str) -> dict:
    """用 ffprobe 获取视频基本信息"""
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_format', '-show_streams', video_path
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as e:
        raise RuntimeError("ffprobe 不可用，请先安装 ffmpeg") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"ffprobe 超时（{FFPROBE_TIMEOUT_SECONDS}s）: {video_path}"
        ) from e

    # 检查 ffprobe 是否成功
    if result.returncode != 0:
        detail = result.stderr.strip() or f"退出码 {result.returncode}"
        raise RuntimeError(f"ffprobe 失败: {detail}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ffprobe 输出解析失败: {e}")

    fmt = data.get('format', {})
    info = {
        'duration': nonnegative_float(fmt.get('duration')),
        'size_mb': nonnegative_float(fmt.get('size')) / 1048576,
        'width': 0, 'height': 0,
        'codec': '', 'fps': '',
        'audio_codec': '', 'audio_rate': '',
    }
    for s in data.get('streams', []):
        if s.get('codec_type') == 'video':
            info['width'] = s.get('width', 0)
            info['height'] = s.get('height', 0)
            info['codec'] = s.get('codec_name', '')
            info['fps'] = s.get('r_frame_rate', '')
        elif s.get('codec_type') == 'audio':
            info['audio_codec'] = s.get('codec_name', '')
            info['audio_rate'] = s.get('sample_rate', '')
    return info


def positive_int(value: str) -> int:
    """argparse 正整数参数。"""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return parsed


def max_frames_int(value: str) -> int:
    """限制关键帧总数，避免误参数造成过量磁盘和视觉处理开销。"""
    parsed = positive_int(value)
    if parsed > MAX_ALLOWED_FRAMES:
        raise argparse.ArgumentTypeError(
            f"不能超过 {MAX_ALLOWED_FRAMES}"
        )
    return parsed


def calculate_frame_interval(
    duration: float,
    requested_interval: int,
    max_frames: int,
) -> int:
    """根据视频时长增大采样间隔，避免生成过多截图。"""
    if requested_interval < 1:
        raise ValueError('requested_interval 必须大于 0')
    if not 1 <= max_frames <= MAX_ALLOWED_FRAMES:
        raise ValueError(f'max_frames 必须在 1 到 {MAX_ALLOWED_FRAMES} 之间')
    if duration <= 0:
        return requested_interval
    return max(requested_interval, math.ceil(duration / max_frames))


def clear_previous_analysis(output_dir: str) -> None:
    """删除上次生成的帧和 analysis.json，避免失败或重跑时读取旧结果。"""
    for filename in os.listdir(output_dir):
        if not (
            filename.startswith('frame_')
            and filename.endswith('.jpg')
            and filename[6:-4].isdigit()
        ):
            continue
        frame_path = os.path.join(output_dir, filename)
        try:
            os.remove(frame_path)
        except FileNotFoundError:
            pass

    analysis_file = os.path.join(output_dir, 'analysis.json')
    try:
        os.remove(analysis_file)
    except FileNotFoundError:
        pass


def extract_frames(
    video_path: str,
    output_dir: str,
    interval: int = DEFAULT_FRAME_INTERVAL,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> list[str]:
    """每 interval 秒提取一帧关键帧"""
    if interval < 1:
        raise ValueError('interval 必须大于 0')
    if not 1 <= max_frames <= MAX_ALLOWED_FRAMES:
        raise ValueError(f'max_frames 必须在 1 到 {MAX_ALLOWED_FRAMES} 之间')
    pattern = os.path.join(output_dir, 'frame_%03d.jpg')
    cmd = [
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-y', '-i', video_path,
        '-vf', f'fps=1/{interval}',
        '-frames:v', str(max_frames),
        '-q:v', '2', pattern,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as e:
        raise RuntimeError("ffmpeg 不可用，请先安装 ffmpeg") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"ffmpeg 帧提取超时（{FFMPEG_TIMEOUT_SECONDS}s）"
        ) from e

    if result.returncode != 0:
        detail = result.stderr.strip() or f"退出码 {result.returncode}"
        raise RuntimeError(f"ffmpeg 帧提取失败: {detail}")

    frames = sorted([
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.startswith('frame_')
        and f.endswith('.jpg')
        and f[6:-4].isdigit()
    ])
    if not frames:
        raise RuntimeError("ffmpeg 执行成功，但没有生成关键帧")
    return frames


def load_transcription(meta_path: str) -> Optional[dict]:
    """从元信息中读取转录；缺失、无效或读取失败时返回 None。"""
    if not os.path.exists(meta_path):
        print(f"元信息.json 不存在: {meta_path}")
        return None

    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"读取元信息.json 失败: {e}")
        return None

    transcription = meta.get('transcription')
    if not isinstance(transcription, dict):
        print("元信息.json 中没有转录结果")
        return None

    print("从元信息.json 读取转录结果")
    language = transcription.get('language') or 'unknown'
    language_prob = transcription.get('language_prob')
    if isinstance(language_prob, (int, float)):
        print(f"语言: {language}  (概率 {language_prob:.2f})")
    else:
        print(f"语言: {language}")
    print(f"转录段落数: {len(transcription.get('segments') or [])}")
    return transcription


def main():
    parser = argparse.ArgumentParser(description='视频内容分析')
    parser.add_argument('save_dir', help='video-meta-parser 结果目录')
    parser.add_argument(
        '--frame-interval',
        type=positive_int,
        default=DEFAULT_FRAME_INTERVAL,
        help=f'最小帧提取间隔（秒，默认 {DEFAULT_FRAME_INTERVAL}）',
    )
    parser.add_argument(
        '--max-frames',
        type=max_frames_int,
        default=DEFAULT_MAX_FRAMES,
        help=(
            f'最大关键帧数量（默认 {DEFAULT_MAX_FRAMES}，'
            f'上限 {MAX_ALLOWED_FRAMES}）'
        ),
    )
    args = parser.parse_args()
    save_dir = os.path.abspath(args.save_dir)

    # 查找视频文件
    video_path = os.path.join(save_dir, '视频文件.mp4')
    if not os.path.exists(video_path):
        print(f"错误: 视频文件不存在: {video_path}")
        print(f"请先使用 video-meta-parser 下载视频")
        sys.exit(1)

    # 创建 _analysis 子目录
    analysis_dir = safe_analysis_dir(save_dir)
    os.makedirs(analysis_dir, exist_ok=True)
    clear_previous_analysis(analysis_dir)

    # Step 1: 获取视频信息
    print("=== Step 1: 获取视频信息 ===")
    vinfo = get_video_info(video_path)
    print(f"时长: {vinfo['duration']:.1f}s  分辨率: {vinfo['width']}x{vinfo['height']}  "
          f"编码: {vinfo['codec']}  帧率: {vinfo['fps']}")

    # Step 2: 提取关键帧（带时间区间）
    frame_interval = calculate_frame_interval(
        vinfo['duration'],
        args.frame_interval,
        args.max_frames,
    )
    print(f"\n=== Step 2: 提取关键帧 (每{frame_interval}s) ===")
    if frame_interval != args.frame_interval:
        print(
            f"视频较长，已将采样间隔从 {args.frame_interval}s 调整为 "
            f"{frame_interval}s，以限制在约 {args.max_frames} 帧内"
        )
    frames_raw = extract_frames(
        video_path,
        analysis_dir,
        frame_interval,
        args.max_frames,
    )
    # 为每个帧添加 start/end 时间
    frames = []
    for i, frame_path in enumerate(frames_raw):
        start_time = i * frame_interval
        end_time = start_time + frame_interval
        if vinfo['duration'] > 0:
            end_time = min(end_time, vinfo['duration'])
        frames.append({
            'start': start_time,
            'end': end_time,
            'path': frame_path,
        })
    print(f"提取了 {len(frames)} 帧")

    # Step 3: 读取转录结果
    meta_path = os.path.join(save_dir, '元信息.json')
    print(f"\n=== Step 3: 读取转录结果 ===")
    transcription = load_transcription(meta_path)

    # 输出分析结果 JSON
    analysis = {
        'video_info': vinfo,
        'frame_interval': frame_interval,
        'frames': frames,
        'transcription': transcription,
    }

    analysis_file = os.path.join(analysis_dir, 'analysis.json')
    fd, temp_analysis_file = tempfile.mkstemp(
        prefix='.analysis.json-',
        suffix='.tmp',
        dir=analysis_dir,
        text=True,
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(analysis, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_analysis_file, analysis_file)
    except Exception:
        try:
            os.remove(temp_analysis_file)
        except FileNotFoundError:
            pass
        raise

    print(f"\n=== 分析素材准备完成 ===")
    print(f"分析目录: {analysis_dir}")
    print(f"分析结果: {analysis_file}")
    print("请读取 analysis.json 中的最终帧集，结合转录文本生成视频内容描述")


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
