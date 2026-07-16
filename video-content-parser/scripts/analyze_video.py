#!/usr/bin/env python3
"""
视频内容分析脚本 — 提取关键帧 + 音频转录，输出供 agent 整合的结构化数据。
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


def extract_audio(video_path: str, output_path: str) -> bool:
    """提取音频为 16kHz 单声道 WAV"""
    cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-acodec', 'pcm_s16le',
        '-ar', '16000', '-ac', '1', output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return result.returncode == 0 and os.path.exists(output_path)


def transcribe_audio(audio_path: str, model_name: str = 'base',
                     hf_endpoint: str = '') -> dict:
    """用 faster-whisper 转录音频"""
    env = os.environ.copy()
    if hf_endpoint:
        env['HF_ENDPOINT'] = hf_endpoint

    # 在子进程中运行转录，避免模型加载问题
    code = f'''
import json, sys
from faster_whisper import WhisperModel
model = WhisperModel("{model_name}", device="cpu", compute_type="int8")
segments, info = model.transcribe("{audio_path}", language="zh", beam_size=5, vad_filter=True)
results = []
for seg in segments:
    results.append({{"start": round(seg.start, 1), "end": round(seg.end, 1), "text": seg.text.strip()}})
output = {{"language": info.language, "language_prob": info.language_probability, "segments": results}}
print(json.dumps(output, ensure_ascii=False))
'''
    result = subprocess.run(
        [sys.executable, '-c', code],
        capture_output=True, text=True, timeout=300, env=env
    )
    if result.returncode != 0:
        return {'error': result.stderr.strip().split('\n')[-1]}
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description='视频分析素材准备')
    parser.add_argument('save_dir', help='视频保存目录（如 <workspace>/videos/<id>/）')
    parser.add_argument('--whisper-model', default='base', help='Whisper 模型名')
    parser.add_argument('--hf-endpoint', default='https://hf-mirror.com',
                        help='HuggingFace 镜像地址')
    parser.add_argument('--frame-interval', type=int, default=2, help='帧提取间隔（秒）')
    parser.add_argument('--skip-whisper', action='store_true', help='跳过语音转录')
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

    # Step 3: 查找或提取音频
    audio_path = os.path.join(analysis_dir, 'audio.wav')
    print(f"\n=== Step 3: 查找/提取音频 ===")
    if os.path.exists(audio_path):
        print(f"音频已存在: {audio_path}")
        audio_extracted = True
    else:
        print("音频不存在，开始提取...")
        audio_extracted = extract_audio(video_path, audio_path)
        if audio_extracted:
            audio_size_kb = os.path.getsize(audio_path) / 1024
            print(f"音频已提取: {audio_path}  ({audio_size_kb:.0f} KB)")
        else:
            print("音频提取失败")
            audio_path = None

    # Step 4: 查找或转录音频
    transcription = None
    meta_path = os.path.join(args.save_dir, '元信息.json')
    print(f"\n=== Step 4: 查找/转录音频 ===")
    
    # 先尝试从元信息.json 读取转录
    if os.path.exists(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
            if meta.get('transcription'):
                print(f"从元信息.json 读取转录结果")
                transcription = meta['transcription']
                print(f"语言: {transcription['language']}  "
                      f"(概率 {transcription['language_prob']:.2f})")
                print(f"转录段落数: {len(transcription.get('segments', []))}")
        except Exception as e:
            print(f"读取元信息.json 失败: {e}")
    
    # 如果没有转录结果且音频存在且未跳过转录
    if transcription is None and audio_path and not args.skip_whisper:
        print(f"开始 Whisper 转录 (model={args.whisper_model})...")
        transcription = transcribe_audio(audio_path, args.whisper_model, args.hf_endpoint)
        if 'error' in transcription:
            print(f"转录失败: {transcription['error']}")
            transcription = None
        else:
            print(f"语言: {transcription['language']}  "
                  f"(概率 {transcription['language_prob']:.2f})")
            print("转录结果:")
            for seg in transcription['segments']:
                print(f"  [{seg['start']}s - {seg['end']}s] {seg['text']}")
    elif transcription is not None:
        print("已有转录结果，跳过转录")
    elif args.skip_whisper:
        print("跳过转录 (--skip-whisper)")
    else:
        print("无音频文件，跳过转录")

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
