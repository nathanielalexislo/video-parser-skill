#!/usr/bin/env python3
"""
视频内容分析脚本 — 提取关键帧，读取转录结果，输出供 agent 整合的结构化数据。
依赖 video-meta-parser 已完成视频下载、音频提取和转录。
"""

import argparse
import json
import os
import subprocess
import sys


def get_video_info(video_path: str) -> dict:
    """用 ffprobe 获取视频基本信息"""
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_format', '-show_streams', video_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)

    fmt = data.get('format', {})
    info = {
        'duration': float(fmt.get('duration', 0)),
        'size_mb': int(fmt.get('size', 0)) / 1048576,
        'width': 0, 'height': 0,
        'codec': '', 'fps': '',
        'audio_codec': '', 'audio_rate': '',
    }
    for s in data.get('streams', []):
        if s['codec_type'] == 'video':
            info['width'] = s.get('width', 0)
            info['height'] = s.get('height', 0)
            info['codec'] = s.get('codec_name', '')
            info['fps'] = s.get('r_frame_rate', '')
        elif s['codec_type'] == 'audio':
            info['audio_codec'] = s.get('codec_name', '')
            info['audio_rate'] = s.get('sample_rate', '')
    return info


def extract_frames(video_path: str, output_dir: str, interval: int = 2) -> list[str]:
    """每 interval 秒提取一帧关键帧"""
    pattern = os.path.join(output_dir, 'frame_%03d.jpg')
    cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-vf', f'fps=1/{interval}',
        '-q:v', '2', pattern
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    frames = sorted([
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if f.startswith('frame_') and f.endswith('.jpg')
    ])
    return frames


def main():
    parser = argparse.ArgumentParser(description='视频内容分析')
    parser.add_argument('save_dir', help='视频保存目录（如 <workspace>/videos/<id>/）')
    parser.add_argument('--frame-interval', type=int, default=2, help='帧提取间隔（秒）')
    args = parser.parse_args()

    # 查找视频文件
    video_path = os.path.join(args.save_dir, '视频文件.mp4')
    if not os.path.exists(video_path):
        print(f"错误: 视频文件不存在: {video_path}")
        print(f"请先使用 video-meta-parser 下载视频")
        sys.exit(1)

    # 创建 _analysis 子目录
    analysis_dir = os.path.join(args.save_dir, '_analysis')
    os.makedirs(analysis_dir, exist_ok=True)

    # Step 1: 获取视频信息
    print("=== Step 1: 获取视频信息 ===")
    vinfo = get_video_info(video_path)
    print(f"时长: {vinfo['duration']:.1f}s  分辨率: {vinfo['width']}x{vinfo['height']}  "
          f"编码: {vinfo['codec']}  帧率: {vinfo['fps']}")

    # Step 2: 提取关键帧（带时间区间）
    print(f"\n=== Step 2: 提取关键帧 (每{args.frame_interval}s) ===")
    frames_raw = extract_frames(video_path, analysis_dir, args.frame_interval)
    # 为每个帧添加 start/end 时间
    frames = []
    for i, frame_path in enumerate(frames_raw):
        start_time = i * args.frame_interval
        end_time = start_time + args.frame_interval
        frames.append({
            'start': start_time,
            'end': end_time,
            'path': frame_path,
        })
    print(f"提取了 {len(frames)} 帧:")
    for f in frames:
        size_kb = os.path.getsize(f['path']) / 1024
        print(f"  [{f['start']}s - {f['end']}s] {f['path']}  ({size_kb:.0f} KB)")

    # Step 3: 读取转录结果
    transcription = None
    meta_path = os.path.join(args.save_dir, '元信息.json')
    print(f"\n=== Step 3: 读取转录结果 ===")
    
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            if meta.get('transcription'):
                transcription = meta['transcription']
                print(f"从元信息.json 读取转录结果")
                print(f"语言: {transcription['language']}  "
                      f"(概率 {transcription['language_prob']:.2f})")
                print(f"转录段落数: {len(transcription.get('segments', []))}")
            else:
                print("元信息.json 中没有转录结果")
        except Exception as e:
            print(f"读取元信息.json 失败: {e}")
    else:
        print(f"元信息.json 不存在: {meta_path}")

    # 输出分析结果 JSON
    analysis = {
        'video_info': vinfo,
        'frames': frames,
        'transcription': transcription,
    }

    analysis_file = os.path.join(analysis_dir, 'analysis.json')
    with open(analysis_file, 'w', encoding='utf-8') as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)

    print(f"\n=== 分析素材准备完成 ===")
    print(f"分析目录: {analysis_dir}")
    print(f"分析结果: {analysis_file}")
    print(f"\n请用 Read 工具逐帧阅读以下截图，结合转录文本生成视频内容描述:")
    for f in frames:
        print(f"  [{f['start']}s - {f['end']}s] {f['path']}")


if __name__ == '__main__':
    main()
