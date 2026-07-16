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
    success       是否成功（true/false）
    fail_reason   失败原因（成功时为空字符串）
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional


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


def transcribe_audio(audio_path: str, model_name: str = 'base',
                     hf_endpoint: str = '') -> dict:
    """用 faster-whisper 转录音频"""
    env = os.environ.copy()
    if hf_endpoint:
        env['HF_ENDPOINT'] = hf_endpoint
    # 通过环境变量传递路径，避免 f-string 嵌套引号问题
    env['AUDIO_PATH'] = audio_path
    env['MODEL_NAME'] = model_name

    code = '''
import json, sys, os
from faster_whisper import WhisperModel
audio_path = os.environ['AUDIO_PATH']
model_name = os.environ['MODEL_NAME']
model = WhisperModel(model_name, device="cpu", compute_type="int8")
segments, info = model.transcribe(audio_path, language="zh", beam_size=5, vad_filter=True)
results = []
for seg in segments:
    results.append({"start": round(seg.start, 1), "end": round(seg.end, 1), "text": seg.text.strip()})
output = {"language": info.language, "language_prob": info.language_probability, "segments": results}
print(json.dumps(output, ensure_ascii=False))
'''
    result = subprocess.run(
        [sys.executable, '-c', code],
        capture_output=True, text=True, timeout=300, env=env
    )
    if result.returncode != 0:
        return {'error': result.stderr.strip().split('\n')[-1]}
    return json.loads(result.stdout)


def to_unified_meta(platform: str, info: dict, transcription: dict = None) -> dict:
    """将各平台原始字段映射为统一结构（字段顺序、命名、含义三平台一致）"""
    if platform == 'douyin':
        vid = info['video_id']
        return {
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
    if platform == 'kuaishou':
        vid = info['photo_id']
        return {
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
    if platform == 'bilibili':
        vid = info['bvid']
        return {
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
    raise ValueError(f"未知平台: {platform}")


def to_failed_meta(fail_reason: str, source_url: str = '', video_id: str = '') -> dict:
    """生成失败时的元信息结构，所有字段保持完整但为空。
    如果 resolve 阶段成功拿到了 video_id，可以填充 id 和 source_url。
    """
    return {
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


def parse_meta(platform: str, url: str, scripts_dir: str,
               cookie_file: str | None, output_dir: str,
               whisper_model: str = 'base', hf_endpoint: str = '') -> dict:
    """调用对应平台脚本解析元信息，下载视频，转录音频，返回统一结构 dict。
    如果 resolve 成功但 extract 失败，source_url 仍会填充。
    """
    video_id = None
    try:
        if platform == 'kuaishou':
            mod = load_module(scripts_dir, 'kuaishou')
            session = mod.build_session(cookie_file)
            video_id = mod.resolve_photo_id(url, session)
            info = mod.extract_video_info(video_id, session)
        elif platform == 'douyin':
            mod = load_module(scripts_dir, 'douyin')
            session = mod.build_session(cookie_file)
            video_id = mod.resolve_video_id(url, session)
            info = mod.extract_video_info(video_id, session)
        elif platform == 'bilibili':
            mod = load_module(scripts_dir, 'bilibili')
            session = mod.build_session(cookie_file)
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
    
    # 元信息提取成功，继续下载视频和转录音频
    transcription = None
    try:
        # 下载视频
        save_dir = os.path.join(output_dir, video_id)
        os.makedirs(save_dir, exist_ok=True)
        video_path = os.path.join(save_dir, '视频文件.mp4')
        
        print(f"\n=== 下载视频 ===")
        if platform == 'kuaishou':
            mod.download_video(info['video_url'], video_path, session, cookie_file)
        elif platform == 'douyin':
            mod.download_video(info['video_url'], video_path, session, cookie_file)
        elif platform == 'bilibili':
            mod.download_video(video_id, info['cid'], video_path, session, cookie_file)
        print(f"视频已保存: {video_path}")
        
        # 提取音频
        analysis_dir = os.path.join(save_dir, '_analysis')
        os.makedirs(analysis_dir, exist_ok=True)
        audio_path = os.path.join(analysis_dir, 'audio.wav')
        
        print(f"\n=== 提取音频 ===")
        if extract_audio(video_path, audio_path):
            print(f"音频已提取: {audio_path}")
            
            # 转录音频
            print(f"\n=== 转录音频 (model={whisper_model}) ===")
            transcription = transcribe_audio(audio_path, whisper_model, hf_endpoint)
            if 'error' in transcription:
                print(f"转录失败: {transcription['error']}")
                transcription = None
            else:
                print(f"转录完成: {len(transcription.get('segments', []))} 段")
        else:
            print("音频提取失败，跳过转录")
    except Exception as e:
        print(f"下载或转录过程中出错: {e}")
        # 不影响元信息返回，只是 transcription 为 None
    
    return to_unified_meta(platform, info, transcription)


def process_single_url(url: str, output_dir: str, cookies_dir: str, scripts_dir: str,
                       whisper_model: str, hf_endpoint: str) -> dict:
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
        'success': False,
        'meta_path': None,
        'error': None
    }
    
    try:
        # 检测平台
        platform = detect_platform(url)
        cookie_file = find_cookie(cookies_dir, platform)
        
        # 解析元信息（包括下载和转录）
        meta = parse_meta(platform, url, scripts_dir, cookie_file,
                         output_dir, whisper_model, hf_endpoint)
        
        # 保存元信息
        if meta['success'] or meta['id']:
            save_dir = os.path.join(output_dir, meta['id'])
            os.makedirs(save_dir, exist_ok=True)
            meta_path = os.path.join(save_dir, '元信息.json')
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            
            result['video_id'] = meta['id']
            result['source_url'] = meta.get('source_url')
            result['meta_path'] = meta_path
            result['success'] = meta['success']
            if not meta['success']:
                result['error'] = meta['fail_reason']
        else:
            result['error'] = meta['fail_reason']
            
    except Exception as e:
        result['error'] = str(e)
    
    return result


def process_batch(urls: list[str], output_dir: str, cookies_dir: str, scripts_dir: str,
                  whisper_model: str, hf_endpoint: str, concurrent: int,
                  batch_num: int, total_batches: int) -> list[dict]:
    """并发处理一批 URL
    
    Args:
        urls: URL 列表
        concurrent: 并发数
        batch_num: 当前批次号（从 1 开始）
        total_batches: 总批次数
        
    Returns:
        list[dict]: 每个 URL 的处理结果
    """
    print(f"\n=== 处理批次 {batch_num}/{total_batches} ({len(urls)} 个 URL，并发 {concurrent}) ===")
    
    results = []
    completed = 0
    
    with ThreadPoolExecutor(max_workers=concurrent) as executor:
        # 提交所有任务
        future_to_url = {
            executor.submit(process_single_url, url, output_dir, cookies_dir, 
                          scripts_dir, whisper_model, hf_endpoint): url
            for url in urls
        }
        
        # 等待完成并收集结果
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                result = future.result()
                results.append(result)
                completed += 1
                
                # 打印进度
                status = "✓" if result['success'] else "✗"
                vid = result['video_id'] or 'N/A'
                print(f"  [{completed}/{len(urls)}] {status} {url} -> {vid}")
                if not result['success'] and result['error']:
                    print(f"         错误: {result['error'][:80]}")
                    
            except Exception as e:
                # 不应该发生，因为 process_single_url 已经捕获了异常
                results.append({
                    'url': url,
                    'video_id': None,
                    'success': False,
                    'meta_path': None,
                    'error': f'Unexpected error: {str(e)}'
                })
                completed += 1
                print(f"  [{completed}/{len(urls)}] ✗ {url} -> Unexpected error")
    
    return results


def process_all(urls: list[str], output_dir: str, cookies_dir: str, scripts_dir: str,
                whisper_model: str, hf_endpoint: str, concurrent: int, batch_size: int) -> dict:
    """分批处理所有 URL
    
    Returns:
        dict: 汇总报告 {
            'total': int,
            'success': int,
            'failed': int,
            'results': list[dict]
        }
    """
    # 去重
    unique_urls = list(dict.fromkeys(urls))  # 保持顺序的去重
    if len(unique_urls) < len(urls):
        print(f"去重: {len(urls)} -> {len(unique_urls)} 个 URL")
    
    # 分批
    batches = [unique_urls[i:i+batch_size] for i in range(0, len(unique_urls), batch_size)]
    total_batches = len(batches)
    
    print(f"\n总计: {len(unique_urls)} 个 URL，分 {total_batches} 批处理")
    
    # 处理所有批次
    all_results = []
    for i, batch in enumerate(batches, 1):
        batch_results = process_batch(batch, output_dir, cookies_dir, scripts_dir,
                                     whisper_model, hf_endpoint, concurrent,
                                     i, total_batches)
        all_results.extend(batch_results)
    
    # 生成汇总报告
    success_count = sum(1 for r in all_results if r['success'])
    failed_count = len(all_results) - success_count
    
    summary = {
        'total': len(all_results),
        'success': success_count,
        'failed': failed_count,
        'results': all_results
    }
    
    # 保存汇总报告
    summary_path = os.path.join(output_dir, 'batch_summary.json')
    os.makedirs(output_dir, exist_ok=True)
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    
    # 生成短链到 source_url 的映射表
    mapping_path = os.path.join(output_dir, 'url_mapping.json')
    mapping_data = {
        'total': len(all_results),
        'mappings': []
    }
    
    for result in all_results:
        mapping_entry = {
            'short_url': result['url'],
            'source_url': result['source_url'],
            'video_id': result['video_id'],
            'success': result['success'],
            'error': result['error']
        }
        mapping_data['mappings'].append(mapping_entry)
    
    with open(mapping_path, 'w', encoding='utf-8') as f:
        json.dump(mapping_data, f, ensure_ascii=False, indent=2)
    
    # 生成 CSV 格式的映射表（便于查看）
    csv_path = os.path.join(output_dir, 'url_mapping.csv')
    with open(csv_path, 'w', encoding='utf-8') as f:
        # 写入表头
        f.write('short_url,source_url,video_id,success,error\n')
        # 写入数据
        for result in all_results:
            # CSV 转义：如果字段包含逗号或引号，用引号包裹
            def csv_escape(value):
                if value is None:
                    return ''
                s = str(value)
                if ',' in s or '"' in s or '\n' in s:
                    return '"' + s.replace('"', '""') + '"'
                return s
            
            row = [
                csv_escape(result['url']),
                csv_escape(result['source_url']),
                csv_escape(result['video_id']),
                csv_escape(result['success']),
                csv_escape(result['error'])
            ]
            f.write(','.join(row) + '\n')
    
    # 打印最终汇总
    print(f"\n=== 批量处理完成 ===")
    print(f"总计: {summary['total']} 个 URL")
    print(f"成功: {summary['success']}")
    print(f"失败: {summary['failed']}")
    print(f"汇总报告: {summary_path}")
    print(f"URL 映射表 (JSON): {mapping_path}")
    print(f"URL 映射表 (CSV): {csv_path}")
    
    return summary


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
        """)
    
    # 输入参数（互斥）
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('url', nargs='?', help='单个视频短链')
    input_group.add_argument('--input-file', help='批量输入文件（每行一个 URL）')
    
    # 通用参数
    parser.add_argument('--output-dir', default='./videos', help='输出根目录')
    parser.add_argument('--cookies-dir', default='.', help='cookie 文件所在目录')
    parser.add_argument('--scripts-dir', default=None, help='平台脚本目录（默认与本脚本同目录）')
    parser.add_argument('--whisper-model', default='base', help='Whisper 模型名称（默认: base）')
    parser.add_argument('--hf-endpoint', default='', help='Hugging Face endpoint（可选）')
    
    # 批量模式参数
    parser.add_argument('--concurrent', type=int, default=8, help='并发数（默认: 8）')
    parser.add_argument('--batch-size', type=int, default=100, help='每批大小（默认: 100）')
    
    args = parser.parse_args()
    
    scripts_dir = args.scripts_dir or os.path.dirname(os.path.abspath(__file__))
    
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
            return

        # 解析完成（可能成功或失败）
        if meta['success']:
            # 成功时以 id 为父目录
            save_dir = os.path.join(args.output_dir, meta['id'])
            os.makedirs(save_dir, exist_ok=True)
            meta_path = os.path.join(save_dir, '元信息.json')
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            print("\n=== 解析完成 ===")
            print(f"视频ID:   {meta['id']}")
            print(f"标题:     {meta['title']}")
            print(f"作者:     {meta['author']}")
            print(f"发布时间: {meta['publish_time']}")
            print(f"播放/点赞/评论/分享: "
                  f"{meta['play_count']:,} / {meta['like_count']:,} / "
                  f"{meta['comment_count']:,} / {meta['share_count']:,}")
            print(f"元信息:   {meta_path}")
            print(f"\nSUCCESS=true")
            print(f"ID={meta['id']}")
            print(f"SOURCE_URL={meta['source_url']}")
            print(f"META_JSON={meta_path}")
        else:
            # 失败时：如果有 id（resolve 成功），创建目录和 JSON；否则只打印错误
            if meta['id']:
                save_dir = os.path.join(args.output_dir, meta['id'])
                os.makedirs(save_dir, exist_ok=True)
                meta_path = os.path.join(save_dir, '元信息.json')
                with open(meta_path, 'w', encoding='utf-8') as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)

                print(f"\n=== 解析失败 ===")
                print(f"失败原因: {meta['fail_reason']}")
                print(f"视频ID:   {meta['id']}")
                print(f"SOURCE_URL: {meta['source_url']}")
                print(f"元信息:   {meta_path}")
                print(f"\nSUCCESS=false")
                print(f"ID={meta['id']}")
                print(f"META_JSON={meta_path}")
            else:
                # resolve 失败，没有 id，不创建任何文件
                print(f"\n=== 解析失败 ===")
                print(f"失败原因: {meta['fail_reason']}")
                print(f"\nSUCCESS=false")
            return


if __name__ == '__main__':
    main()
