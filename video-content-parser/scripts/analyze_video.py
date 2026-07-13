#!/usr/bin/env python3
"""
视频内容分析脚本 — 提取关键帧 + 音频转录，输出供 agent 整合的结构化数据。
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile


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
    parser = argparse.ArgumentParser(description='视频内容分析')
    parser.add_argument('video', help='视频文件路径')
    parser.add_argument('--output', required=True, help='描述文件输出路径')
    parser.add_argument('--whisper-model', default='base', help='Whisper 模型名')
    parser.add_argument('--hf-endpoint', default='https://hf-mirror.com',
                        help='HuggingFace 镜像地址')
    parser.add_argument('--frame-interval', type=int, default=2, help='帧提取间隔（秒）')
    parser.add_argument('--skip-whisper', action='store_true', help='跳过语音转录')
    args = parser.parse_args()

    if not os.path.exists(args.video):
        print(f"错误: 视频文件不存在: {args.video}")
        sys.exit(1)

    tmpdir = tempfile.mkdtemp(prefix='video_analysis_')

    # Step 1: 获取视频信息
    print("=== Step 1: 获取视频信息 ===")
    vinfo = get_video_info(args.video)
    print(f"时长: {vinfo['duration']:.1f}s  分辨率: {vinfo['width']}x{vinfo['height']}  "
          f"编码: {vinfo['codec']}  帧率: {vinfo['fps']}")

    # Step 2: 提取关键帧
    print(f"\n=== Step 2: 提取关键帧 (每{args.frame_interval}s) ===")
    frames = extract_frames(args.video, tmpdir, args.frame_interval)
    print(f"提取了 {len(frames)} 帧:")
    for f in frames:
        size_kb = os.path.getsize(f) / 1024
        print(f"  {f}  ({size_kb:.0f} KB)")

    # Step 3: 提取音频
    audio_path = os.path.join(tmpdir, 'audio.wav')
    print(f"\n=== Step 3: 提取音频 ===")
    if extract_audio(args.video, audio_path):
        audio_size_kb = os.path.getsize(audio_path) / 1024
        print(f"音频已提取: {audio_path}  ({audio_size_kb:.0f} KB)")
    else:
        print("音频提取失败")
        audio_path = None

    # Step 4: Whisper 转录
    transcription = None
    if audio_path and not args.skip_whisper:
        print(f"\n=== Step 4: Whisper 转录 (model={args.whisper_model}) ===")
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

    # 输出分析结果 JSON（供 agent 读取后整合为最终描述）
    analysis = {
        'video_info': vinfo,
        'frames': frames,
        'audio_path': audio_path,
        'transcription': transcription,
        'output_path': args.output,
        'temp_dir': tmpdir,
    }

    analysis_file = os.path.join(os.path.dirname(args.output), '_analysis.json')
    with open(analysis_file, 'w', encoding='utf-8') as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)

    print(f"\n=== 分析完成 ===")
    print(f"帧文件目录: {tmpdir}")
    print(f"分析结果:   {analysis_file}")
    print(f"描述输出:   {args.output}")
    print(f"\n请用 Read 工具逐帧阅读以下截图，结合转录文本生成视频内容描述:")
    for i, f in enumerate(frames):
        print(f"  帧{i+1}: {f}")


if __name__ == '__main__':
    main()
