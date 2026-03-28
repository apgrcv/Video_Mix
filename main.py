import json
import itertools
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import Tk, StringVar, BooleanVar, IntVar, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
SUPPORTED_AUDIO_FILETYPES = [
    ("音频文件", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.wma"),
    ("所有文件", "*.*"),
]
APP_VERSION = "2026-03-10-fast-transition"
RANDOM_ORDER_DISTINCT = "顺序不同算不同结果"
RANDOM_ORDER_IGNORE = "顺序不同算同一结果"
SETTINGS_FILE = ".video_mix_settings.json"

TRANSITION_PROFILES = {
    "极速": {"fade_duration": 0.08, "gap_duration": 0.0},
}
LIGHT_TRANSITION_OPTIONS = ["0.4", "0.6", "0.8", "1.0"]
DEFAULT_LIGHT_TRANSITION_SECONDS = "0.6"
WATERMARK_MODE_IMAGE = "图片水印"
WATERMARK_MODE_TEXT = "文字水印"
SOURCE_MODE_DIRECTORY = "directory"
SOURCE_MODE_FILES = "files"


def list_videos(directory):
    base = Path(directory)
    if not base.exists():
        return []
    return sorted([p for p in base.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS])


def is_video_file(path_obj):
    return path_obj.is_file() and path_obj.suffix.lower() in VIDEO_EXTENSIONS


def find_binary(name):
    ext = ".exe" if os.name == "nt" else ""
    bin_name = f"{name}{ext}"
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent
    candidates = [
        base / bin_name,
        base / "ffmpeg" / bin_name,
        base / "bin" / bin_name,
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    found = shutil.which(name)
    return found


def run_command(cmd):
    extra = {}
    if sys.platform == "win32":
        extra["creationflags"] = subprocess.CREATE_NO_WINDOW
    completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **extra)
    return completed.returncode, completed.stdout, completed.stderr


def probe_resolution(ffprobe, video_path):
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        str(video_path),
    ]
    code, out, _ = run_command(cmd)
    if code != 0:
        return None
    try:
        data = json.loads(out)
        streams = data.get("streams", [])
        if not streams:
            return None
        return streams[0].get("width"), streams[0].get("height")
    except json.JSONDecodeError:
        return None


def probe_has_audio(ffprobe, video_path):
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "json",
        str(video_path),
    ]
    code, out, _ = run_command(cmd)
    if code != 0:
        return False
    try:
        data = json.loads(out)
        streams = data.get("streams", [])
        return len(streams) > 0
    except json.JSONDecodeError:
        return False

def probe_duration(ffprobe, video_path):
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    code, out, _ = run_command(cmd)
    if code != 0:
        return None
    try:
        return float(out.strip())
    except Exception:
        return None


def merge_intervals(intervals):
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda item: item[0])
    merged = [sorted_intervals[0]]
    for start_time, end_time in sorted_intervals[1:]:
        last_start, last_end = merged[-1]
        if start_time <= last_end:
            merged[-1] = (last_start, max(last_end, end_time))
        else:
            merged.append((start_time, end_time))
    return merged


def build_enable_expression(intervals):
    if not intervals:
        return None
    return "+".join(f"between(t,{start_time:.3f},{end_time:.3f})" for start_time, end_time in intervals)


def escape_filter_value(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
    )


def find_drawtext_font():
    candidates = []
    if sys.platform == "darwin":
        candidates = [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    elif os.name == "nt":
        candidates = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyh.ttf",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def probe_stream_signature(ffprobe, video_path):
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,sample_aspect_ratio,sample_rate,channels",
        "-of",
        "json",
        str(video_path),
    ]
    code, out, _ = run_command(cmd)
    if code != 0:
        return None
    try:
        data = json.loads(out)
        streams = data.get("streams", [])
    except json.JSONDecodeError:
        return None
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video_stream:
        return None
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    v_sig = (
        video_stream.get("codec_name"),
        video_stream.get("pix_fmt"),
        video_stream.get("width"),
        video_stream.get("height"),
        video_stream.get("r_frame_rate"),
        video_stream.get("avg_frame_rate"),
        video_stream.get("sample_aspect_ratio"),
    )
    a_sig = None
    if audio_stream:
        a_sig = (
            audio_stream.get("codec_name"),
            audio_stream.get("sample_rate"),
            audio_stream.get("channels"),
        )
    return v_sig, a_sig

def is_copy_compatible(ffprobe, front, back):
    return is_copy_compatible_many(ffprobe, [front, back])


def is_copy_compatible_for_three(ffprobe, first, middle, last):
    return is_copy_compatible_many(ffprobe, [first, middle, last])


def is_copy_compatible_many(ffprobe, video_paths):
    signatures = [probe_stream_signature(ffprobe, video_path) for video_path in video_paths]
    if not signatures or any(not signature for signature in signatures):
        return False
    first_signature = signatures[0]
    return all(signature == first_signature for signature in signatures[1:])


def build_scale_filter(width, height):
    return f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"


def build_transition_scale_filter(width, height):
    return f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1,format=yuv420p"


def resolve_transition_duration(requested_duration, clip_durations):
    if not clip_durations:
        return None
    min_duration = min(clip_durations)
    max_allowed = max(0.0, (min_duration / 2.0) - 0.02)
    if max_allowed <= 0:
        return None
    return min(requested_duration, max_allowed)


def get_transition_profile(profile_name, light_transition_seconds=None):
    if profile_name == "轻量":
        try:
            total_seconds = float(light_transition_seconds or DEFAULT_LIGHT_TRANSITION_SECONDS)
        except Exception:
            total_seconds = float(DEFAULT_LIGHT_TRANSITION_SECONDS)
        total_seconds = max(0.2, total_seconds)
        gap_duration = round(total_seconds * 0.2, 3)
        fade_duration = round(max(0.08, (total_seconds - gap_duration) / 2.0), 3)
        return {"fade_duration": fade_duration, "gap_duration": gap_duration, "total_seconds": round(fade_duration * 2 + gap_duration, 3)}
    profile = TRANSITION_PROFILES.get(profile_name, TRANSITION_PROFILES["极速"]).copy()
    profile["total_seconds"] = round(profile["fade_duration"] * 2 + profile["gap_duration"], 3)
    return profile


def get_transition_variant_params(use_hardware):
    # Transition assets are temporary building blocks; bias toward smaller size while keeping fast encoding.
    if use_hardware:
        return ["-c:v", "h264_videotoolbox", "-b:v", "2200k", "-maxrate", "2600k", "-bufsize", "4400k", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "superfast", "-crf", "30"]


def run_ffmpeg_with_hw_fallback(primary_cmd, fallback_cmd=None):
    code, out, err = run_command(primary_cmd)
    if code == 0:
        return True, out + err
    if fallback_cmd:
        code2, out2, err2 = run_command(fallback_cmd)
        return code2 == 0, out + err + out2 + err2
    return False, out + err


def render_standard_clip(ffmpeg, ffprobe, source_path, output_path, target_resolution, include_audio):
    width, height = target_resolution
    cmd = [ffmpeg, "-y", "-i", str(source_path), "-vf", build_transition_scale_filter(width, height)]
    use_hardware = sys.platform == "darwin"
    cmd += get_transition_variant_params(use_hardware)

    source_has_audio = probe_has_audio(ffprobe, source_path)
    if include_audio and source_has_audio:
        cmd += [
            "-af",
            "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
        ]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(output_path)]

    fallback_cmd = None
    if use_hardware:
        fallback_cmd = [ffmpeg, "-y", "-i", str(source_path), "-vf", build_transition_scale_filter(width, height)]
        fallback_cmd += get_transition_variant_params(False)
        if include_audio and source_has_audio:
            fallback_cmd += [
                "-af",
                "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
            ]
        else:
            fallback_cmd += ["-an"]
        fallback_cmd += ["-movflags", "+faststart", str(output_path)]

    return run_ffmpeg_with_hw_fallback(cmd, fallback_cmd)


def copy_segment_from_standard(ffmpeg, source_path, output_path, start_time, duration):
    if duration <= 0.02:
        return True, ""
    cmd = [ffmpeg, "-y"]
    if start_time > 0:
        cmd += ["-ss", f"{start_time:.3f}"]
    cmd += ["-i", str(source_path), "-t", f"{duration:.3f}", "-c", "copy", "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(output_path)]
    code, out, err = run_command(cmd)
    return code == 0, out + err


def render_faded_segment(ffmpeg, standard_source_path, output_path, start_time, duration, fade_in, fade_out, include_audio):
    if duration <= 0.02:
        return True, ""
    cmd = [ffmpeg, "-y"]
    if start_time > 0:
        cmd += ["-ss", f"{start_time:.3f}"]
    cmd += ["-i", str(standard_source_path), "-t", f"{duration:.3f}"]

    video_filter = []
    if fade_in:
        video_filter.append(f"fade=t=in:st=0:d={duration:.3f}:color=black")
    if fade_out:
        video_filter.append(f"fade=t=out:st=0:d={duration:.3f}:color=black")
    if video_filter:
        cmd += ["-vf", ",".join(video_filter)]

    use_hardware = sys.platform == "darwin"
    cmd += get_transition_variant_params(use_hardware)

    if include_audio:
        audio_filter = ["aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"]
        if fade_in:
            audio_filter.append(f"afade=t=in:st=0:d={duration:.3f}")
        if fade_out:
            audio_filter.append(f"afade=t=out:st=0:d={duration:.3f}")
        cmd += ["-af", ",".join(audio_filter), "-c:a", "aac", "-b:a", "96k"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(output_path)]

    fallback_cmd = None
    if use_hardware:
        fallback_cmd = [ffmpeg, "-y"]
        if start_time > 0:
            fallback_cmd += ["-ss", f"{start_time:.3f}"]
        fallback_cmd += ["-i", str(standard_source_path), "-t", f"{duration:.3f}"]
        if video_filter:
            fallback_cmd += ["-vf", ",".join(video_filter)]
        fallback_cmd += get_transition_variant_params(False)
        if include_audio:
            fallback_cmd += ["-af", ",".join(audio_filter), "-c:a", "aac", "-b:a", "96k"]
        else:
            fallback_cmd += ["-an"]
        fallback_cmd += ["-movflags", "+faststart", str(output_path)]

    return run_ffmpeg_with_hw_fallback(cmd, fallback_cmd)


def render_transition_gap(ffmpeg, output_path, target_resolution, gap_duration, include_audio):
    width, height = target_resolution
    if gap_duration <= 0:
        return False, ""

    def build_gap_cmd(use_hardware):
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}:d={gap_duration:.3f}:r=30",
        ]
        if include_audio:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={gap_duration:.3f}"]
        cmd += get_transition_variant_params(use_hardware)
        if include_audio:
            cmd += ["-c:a", "aac", "-b:a", "96k", "-shortest"]
        else:
            cmd += ["-an"]
        cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output_path)]
        return cmd

    use_hardware = sys.platform == "darwin"
    fallback_cmd = build_gap_cmd(False) if use_hardware else None
    return run_ffmpeg_with_hw_fallback(build_gap_cmd(use_hardware), fallback_cmd)


def prepare_transition_assets(ffmpeg, ffprobe, video_paths, target_resolution, transition_profile, light_transition_seconds, cache_dir, cache, inflight, cache_lock):
    include_audio = all(probe_has_audio(ffprobe, video_path) for video_path in video_paths)
    profile = get_transition_profile(transition_profile, light_transition_seconds)
    fade_duration = profile["fade_duration"]
    gap_duration = profile["gap_duration"]
    durations = [probe_duration(ffprobe, video_path) or 0.0 for video_path in video_paths]
    prepared_paths = []

    def signal_inflight_done(key):
        event = inflight.pop(key, None)
        if event:
            event.set()

    def wait_for_existing_job(key):
        with cache_lock:
            event = inflight.get(key)
        if not event:
            return None
        event.wait()
        with cache_lock:
            cached_path = cache.get(key)
        if cached_path and Path(cached_path).exists():
            return Path(cached_path), True, ""
        return None

    def get_or_create_standard(source_path):
        key = ("standard", str(source_path), target_resolution, include_audio)
        with cache_lock:
            cached_path = cache.get(key)
            if cached_path and Path(cached_path).exists():
                return Path(cached_path), True, ""
            existing_event = inflight.get(key)
            if existing_event is None:
                inflight[key] = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            waited = wait_for_existing_job(key)
            if waited:
                return waited
            return Path(cache_dir) / f"standard_{abs(hash(key))}.mp4", False, "标准化缓存生成失败"

        output_path = Path(cache_dir) / f"standard_{abs(hash(key))}.mp4"
        ok, logtxt = render_standard_clip(ffmpeg, ffprobe, source_path, output_path, target_resolution, include_audio)
        with cache_lock:
            if ok:
                cache[key] = str(output_path)
            signal_inflight_done(key)
        return output_path, ok, logtxt

    def get_or_create_body_segment(standard_path, kind, source_duration):
        if kind == "front_body":
            start_time = 0.0
            duration = source_duration - fade_duration
        elif kind == "middle_body":
            start_time = fade_duration
            duration = source_duration - (fade_duration * 2)
        else:
            start_time = fade_duration
            duration = source_duration - fade_duration

        if duration <= 0.02:
            return None, True, ""

        key = ("body", str(standard_path), kind, round(start_time, 3), round(duration, 3))
        with cache_lock:
            cached_path = cache.get(key)
            if cached_path and Path(cached_path).exists():
                return Path(cached_path), True, ""
            existing_event = inflight.get(key)
            if existing_event is None:
                inflight[key] = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            waited = wait_for_existing_job(key)
            if waited:
                return waited
            return Path(cache_dir) / f"body_{abs(hash(key))}.mp4", False, "主体片段缓存生成失败"

        output_path = Path(cache_dir) / f"body_{abs(hash(key))}.mp4"
        ok, logtxt = copy_segment_from_standard(ffmpeg, standard_path, output_path, start_time, duration)
        with cache_lock:
            if ok:
                cache[key] = str(output_path)
            signal_inflight_done(key)
        return output_path, ok, logtxt

    def get_or_create_fade_segment(standard_path, kind, source_duration):
        start_time = 0.0 if kind == "head_fade" else max(0.0, source_duration - fade_duration)
        key = ("fade", str(standard_path), kind, round(start_time, 3), round(fade_duration, 3), include_audio)
        with cache_lock:
            cached_path = cache.get(key)
            if cached_path and Path(cached_path).exists():
                return Path(cached_path), True, ""
            existing_event = inflight.get(key)
            if existing_event is None:
                inflight[key] = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            waited = wait_for_existing_job(key)
            if waited:
                return waited
            return Path(cache_dir) / f"fade_{abs(hash(key))}.mp4", False, "转场片段缓存生成失败"

        output_path = Path(cache_dir) / f"fade_{abs(hash(key))}.mp4"
        ok, logtxt = render_faded_segment(
            ffmpeg,
            standard_path,
            output_path,
            start_time,
            fade_duration,
            kind == "head_fade",
            kind == "tail_fade",
            include_audio,
        )
        with cache_lock:
            if ok:
                cache[key] = str(output_path)
            signal_inflight_done(key)
        return output_path, ok, logtxt

    def get_or_create_gap():
        key = ("gap", target_resolution, round(gap_duration, 3), include_audio)
        with cache_lock:
            cached_path = cache.get(key)
            if cached_path and Path(cached_path).exists():
                return Path(cached_path), True, ""
            existing_event = inflight.get(key)
            if existing_event is None:
                inflight[key] = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            waited = wait_for_existing_job(key)
            if waited:
                return waited
            return Path(cache_dir) / f"gap_{abs(hash(key))}.mp4", False, "转场缓存生成失败"

        output_path = Path(cache_dir) / f"gap_{abs(hash(key))}.mp4"
        ok, logtxt = render_transition_gap(ffmpeg, output_path, target_resolution, gap_duration, include_audio)
        with cache_lock:
            if ok:
                cache[key] = str(output_path)
            signal_inflight_done(key)
        return output_path, ok, logtxt

    for index, video_path in enumerate(video_paths):
        source_duration = durations[index]
        standard_path, ok, logtxt = get_or_create_standard(video_path)
        if not ok:
            return False, logtxt, []

        if index == 0:
            body_path, ok, logtxt = get_or_create_body_segment(standard_path, "front_body", source_duration)
            if not ok:
                return False, logtxt, []
            if body_path:
                prepared_paths.append(body_path)
            fade_path, ok, logtxt = get_or_create_fade_segment(standard_path, "tail_fade", source_duration)
            if not ok:
                return False, logtxt, []
            prepared_paths.append(fade_path)
        elif index == len(video_paths) - 1:
            fade_path, ok, logtxt = get_or_create_fade_segment(standard_path, "head_fade", source_duration)
            if not ok:
                return False, logtxt, []
            prepared_paths.append(fade_path)
            body_path, ok, logtxt = get_or_create_body_segment(standard_path, "back_body", source_duration)
            if not ok:
                return False, logtxt, []
            if body_path:
                prepared_paths.append(body_path)
        else:
            head_path, ok, logtxt = get_or_create_fade_segment(standard_path, "head_fade", source_duration)
            if not ok:
                return False, logtxt, []
            prepared_paths.append(head_path)
            body_path, ok, logtxt = get_or_create_body_segment(standard_path, "middle_body", source_duration)
            if not ok:
                return False, logtxt, []
            if body_path:
                prepared_paths.append(body_path)
            tail_path, ok, logtxt = get_or_create_fade_segment(standard_path, "tail_fade", source_duration)
            if not ok:
                return False, logtxt, []
            prepared_paths.append(tail_path)

        if gap_duration > 0 and index < len(video_paths) - 1:
            gap_path, ok, logtxt = get_or_create_gap()
            if not ok:
                return False, logtxt, []
            prepared_paths.append(gap_path)

    return True, "", prepared_paths


def concat_copy_many(ffmpeg, video_paths, output, stop_event=None, on_proc=None):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
    try:
        for video_path in video_paths:
            tmp.write(f"file '{str(video_path)}'\n")
        tmp.close()
        cmd = [
            ffmpeg,
            "-y",
            "-fflags",
            "+genpts",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            tmp.name,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]
        try:
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, encoding="utf-8", errors="replace", startupinfo=startupinfo)
            if on_proc:
                on_proc(proc)
            out_lines = []
            while True:
                if stop_event and stop_event.is_set():
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    return False, "Cancelled"
                line = proc.stdout.readline()
                if line:
                    out_lines.append(line.strip())
                    continue
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            code = proc.wait()
            out_text = "\n".join(out_lines)
            if code == 0:
                return True, out_text
            return False, out_text
        except Exception as e:
            return False, str(e)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def concat_copy(ffmpeg, front, back, output, stop_event=None, on_proc=None):
    return concat_copy_many(ffmpeg, [front, back], output, stop_event=stop_event, on_proc=on_proc)


def concat_copy_three(ffmpeg, first, middle, last, output, stop_event=None, on_proc=None):
    return concat_copy_many(ffmpeg, [first, middle, last], output, stop_event=stop_event, on_proc=on_proc)


def concat_copy_four(ffmpeg, first, second, third, fourth, output, stop_event=None, on_proc=None):
    return concat_copy_many(ffmpeg, [first, second, third, fourth], output, stop_event=stop_event, on_proc=on_proc)


def concat_reencode_many(
    ffmpeg,
    ffprobe,
    video_paths,
    output,
    target_resolution,
    progress_total=None,
    on_progress=None,
    on_proc=None,
    transition_name=None,
    transition_duration=0.0,
    clip_durations=None,
):
    width, height = target_resolution
    clip_durations = clip_durations or [probe_duration(ffprobe, video_path) or 0.0 for video_path in video_paths]
    transition_active = bool(transition_name and len(video_paths) > 1)
    effective_transition_duration = 0.0
    if transition_active:
        effective_transition_duration = resolve_transition_duration(transition_duration, clip_durations)
        if not effective_transition_duration:
            return False, "视频时长过短，无法应用转场效果", False

    video_filter = build_transition_scale_filter(width, height) if transition_active else build_scale_filter(width, height)
    has_audio_flags = [probe_has_audio(ffprobe, video_path) for video_path in video_paths]
    all_have_audio = all(has_audio_flags)
    use_videotoolbox = sys.platform == "darwin" and not transition_active
    if use_videotoolbox:
        v_params = ["-c:v", "h264_videotoolbox", "-b:v", "10000k", "-allow_sw", "1"]
    else:
        v_params = ["-c:v", "libx264", "-crf", "23", "-preset", "veryfast"]

    cmd = [ffmpeg, "-y"]
    for video_path in video_paths:
        cmd.extend(["-i", str(video_path)])

    video_parts = [f"[{index}:v]{video_filter}[v{index}]" for index, _ in enumerate(video_paths)]
    filter_parts = list(video_parts)
    current_video_label = "[v0]"

    if transition_active:
        offset = max(0.0, clip_durations[0] - effective_transition_duration)
        for index in range(1, len(video_paths)):
            output_label = f"[vx{index}]"
            filter_parts.append(
                f"{current_video_label}[v{index}]xfade=transition={transition_name}:duration={effective_transition_duration}:offset={offset:.3f}{output_label}"
            )
            current_video_label = output_label
            offset += max(0.0, clip_durations[index] - effective_transition_duration)
    else:
        concat_inputs = "".join(f"[v{index}]" for index, _ in enumerate(video_paths))
        filter_parts.append(f"{concat_inputs}concat=n={len(video_paths)}:v=1:a=0[v]")
        current_video_label = "[v]"

    audio_map = []
    if all_have_audio:
        audio_parts = [
            f"[{index}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{index}]"
            for index, _ in enumerate(video_paths)
        ]
        filter_parts.extend(audio_parts)
        if transition_active:
            current_audio_label = "[a0]"
            for index in range(1, len(video_paths)):
                output_label = f"[ax{index}]"
                filter_parts.append(
                    f"{current_audio_label}[a{index}]acrossfade=d={effective_transition_duration}:c1=tri:c2=tri{output_label}"
                )
                current_audio_label = output_label
            audio_map = ["-map", current_audio_label, "-c:a", "aac", "-b:a", "192k"]
        else:
            concat_inputs = "".join(f"[v{index}][a{index}]" for index, _ in enumerate(video_paths))
            filter_parts[-1] = f"{concat_inputs}concat=n={len(video_paths)}:v=1:a=1[v][a]"
            current_video_label = "[v]"
            audio_map = ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
            filter_parts = video_parts + audio_parts + [filter_parts[-1]]

    if not transition_active and all_have_audio:
        filter_complex = ";".join(filter_parts)
    elif not all_have_audio:
        if transition_active:
            filter_complex = ";".join(filter_parts)
        else:
            filter_complex = ";".join(video_parts + [f"{''.join(f'[v{index}]' for index, _ in enumerate(video_paths))}concat=n={len(video_paths)}:v=1:a=0[v]"])
            current_video_label = "[v]"
    else:
        filter_complex = ";".join(filter_parts)

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        current_video_label,
    ] + audio_map + v_params + [
        "-movflags",
        "+faststart",
        str(output),
        "-progress",
        "pipe:1",
        "-nostats",
    ]

    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, encoding="utf-8", errors="replace", startupinfo=startupinfo)
        if on_proc:
            on_proc(proc)
    except Exception as e:
        return False, str(e), all_have_audio
    out_lines = []
    try:
        while True:
            line = proc.stdout.readline()
            if line:
                s = line.strip()
                if s.startswith("out_time_ms="):
                    try:
                        ms = int(s.split("=", 1)[1])
                        seconds = ms / 1000000.0
                        if progress_total and on_progress:
                            remaining = max(0.0, progress_total - seconds)
                            on_progress(remaining)
                    except Exception:
                        pass
                else:
                    out_lines.append(s)
                continue
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        code = proc.wait()
    except Exception as e:
        out_lines.append(str(e))
        code = proc.returncode
    return code == 0, "\n".join(out_lines), all_have_audio


def concat_reencode(ffmpeg, ffprobe, front, back, output, target_resolution, progress_total=None, on_progress=None, on_proc=None):
    return concat_reencode_many(
        ffmpeg,
        ffprobe,
        [front, back],
        output,
        target_resolution,
        progress_total=progress_total,
        on_progress=on_progress,
        on_proc=on_proc,
    )


def concat_reencode_three(ffmpeg, ffprobe, first, middle, last, output, target_resolution, progress_total=None, on_progress=None, on_proc=None):
    return concat_reencode_many(
        ffmpeg,
        ffprobe,
        [first, middle, last],
        output,
        target_resolution,
        progress_total=progress_total,
        on_progress=on_progress,
        on_proc=on_proc,
    )


def concat_reencode_four(ffmpeg, ffprobe, first, second, third, fourth, output, target_resolution, progress_total=None, on_progress=None, on_proc=None):
    return concat_reencode_many(
        ffmpeg,
        ffprobe,
        [first, second, third, fourth],
        output,
        target_resolution,
        progress_total=progress_total,
        on_progress=on_progress,
        on_proc=on_proc,
    )


def build_watermark_intervals(duration, explicit_enabled, explicit_ranges, tail_enabled, tail_seconds):
    intervals = []
    if explicit_enabled:
        intervals.extend(explicit_ranges)
    if tail_enabled and tail_seconds > 0:
        start_time = max(0.0, duration - tail_seconds)
        intervals.append((start_time, duration))
    merged = merge_intervals(intervals)
    cleaned = []
    for start_time, end_time in merged:
        start_time = max(0.0, min(start_time, duration))
        end_time = max(0.0, min(end_time, duration))
        if end_time > start_time:
            cleaned.append((start_time, end_time))
    return cleaned


def get_watermark_video_params(use_hardware, fast_mode=False):
    if use_hardware:
        if fast_mode:
            return ["-c:v", "h264_videotoolbox", "-b:v", "3500k", "-maxrate", "4500k", "-bufsize", "7000k", "-allow_sw", "1"]
        return ["-c:v", "h264_videotoolbox", "-b:v", "5000k", "-allow_sw", "1"]
    if fast_mode:
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]


def run_ffmpeg_progress_command(cmd, progress_total=None, on_progress=None, on_proc=None, stop_event=None):
    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            startupinfo=startupinfo,
        )
        if on_proc:
            on_proc(proc)
    except Exception as error:
        return False, str(error)

    out_lines = []
    try:
        while True:
            if stop_event and stop_event.is_set():
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
                return False, "Cancelled"
            line = proc.stdout.readline()
            if line:
                stripped = line.strip()
                if stripped.startswith("out_time_ms="):
                    try:
                        milliseconds = int(stripped.split("=", 1)[1])
                        seconds = milliseconds / 1000000.0
                        if progress_total and on_progress:
                            remaining = max(0.0, progress_total - seconds)
                            on_progress(remaining)
                    except Exception:
                        pass
                else:
                    out_lines.append(stripped)
                continue
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        code = proc.wait()
    except Exception as error:
        out_lines.append(str(error))
        code = proc.returncode
    return code == 0, "\n".join(out_lines)


def build_watermark_image_filter(scale_percent, opacity_percent, x_pos, y_pos, enable_expr):
    scale_ratio = max(0.01, scale_percent / 100.0)
    opacity_ratio = max(0.0, min(1.0, opacity_percent / 100.0))
    overlay_args = [str(int(x_pos)), str(int(y_pos))]
    if enable_expr:
        overlay_args.append(f"enable='{enable_expr}'")
    overlay_filter = ":".join(overlay_args)
    return (
        f"[1:v]format=rgba,colorchannelmixer=aa={opacity_ratio:.3f}[wmraw];"
        f"[wmraw][0:v]scale2ref=w=main_w*{scale_ratio:.4f}:h=ow/mdar[wm][base];"
        f"[base][wm]overlay={overlay_filter}[v]"
    )


def apply_image_watermark(
    ffmpeg,
    ffprobe,
    input_video,
    output_video,
    watermark_image,
    scale_percent,
    opacity_percent,
    x_pos,
    y_pos,
    intervals,
    fast_mode=False,
    progress_total=None,
    on_progress=None,
    on_proc=None,
    stop_event=None,
):
    enable_expr = build_enable_expression(intervals)
    filter_complex = build_watermark_image_filter(scale_percent, opacity_percent, x_pos, y_pos, enable_expr)
    use_hardware = sys.platform == "darwin"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(input_video),
        "-i",
        str(watermark_image),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "0:a?",
    ] + get_watermark_video_params(use_hardware, fast_mode=fast_mode) + [
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output_video),
        "-progress",
        "pipe:1",
        "-nostats",
    ]
    success, output_text = run_ffmpeg_progress_command(
        cmd,
        progress_total=progress_total or probe_duration(ffprobe, input_video) or 0.0,
        on_progress=on_progress,
        on_proc=on_proc,
        stop_event=stop_event,
    )
    if success:
        return True, output_text
    if use_hardware:
        fallback_cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(input_video),
            "-i",
            str(watermark_image),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "0:a?",
        ] + get_watermark_video_params(False, fast_mode=fast_mode) + [
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output_video),
            "-progress",
            "pipe:1",
            "-nostats",
        ]
        fallback_success, fallback_output = run_ffmpeg_progress_command(
            fallback_cmd,
            progress_total=progress_total or probe_duration(ffprobe, input_video) or 0.0,
            on_progress=on_progress,
            on_proc=on_proc,
            stop_event=stop_event,
        )
        return fallback_success, output_text + "\n" + fallback_output
    return False, output_text


def apply_text_watermark(
    ffmpeg,
    ffprobe,
    input_video,
    output_video,
    text_content,
    font_size,
    opacity_percent,
    x_pos,
    y_pos,
    intervals,
    fast_mode=False,
    progress_total=None,
    on_progress=None,
    on_proc=None,
    stop_event=None,
):
    enable_expr = build_enable_expression(intervals)
    opacity_ratio = max(0.0, min(1.0, opacity_percent / 100.0))
    fontfile = find_drawtext_font()
    text_file = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
    try:
        text_file.write(text_content)
        text_file.close()
        drawtext_parts = []
        if fontfile:
            drawtext_parts.append(f"fontfile='{escape_filter_value(fontfile)}'")
        drawtext_parts.extend(
            [
                f"textfile='{escape_filter_value(text_file.name)}'",
                f"fontsize={int(font_size)}",
                f"fontcolor=white@{opacity_ratio:.3f}",
                f"x={int(x_pos)}",
                f"y={int(y_pos)}",
                "line_spacing=8",
                "box=0",
            ]
        )
        if enable_expr:
            drawtext_parts.append(f"enable='{enable_expr}'")
        video_filter = "drawtext=" + ":".join(drawtext_parts)
        use_hardware = sys.platform == "darwin"
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(input_video),
            "-vf",
            video_filter,
            "-map",
            "0:v",
            "-map",
            "0:a?",
        ] + get_watermark_video_params(use_hardware, fast_mode=fast_mode) + [
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output_video),
            "-progress",
            "pipe:1",
            "-nostats",
        ]
        success, output_text = run_ffmpeg_progress_command(
            cmd,
            progress_total=progress_total or probe_duration(ffprobe, input_video) or 0.0,
            on_progress=on_progress,
            on_proc=on_proc,
            stop_event=stop_event,
        )
        if success:
            return True, output_text
        if use_hardware:
            fallback_cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(input_video),
                "-vf",
                video_filter,
                "-map",
                "0:v",
                "-map",
                "0:a?",
            ] + get_watermark_video_params(False, fast_mode=fast_mode) + [
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(output_video),
                "-progress",
                "pipe:1",
                "-nostats",
            ]
            fallback_success, fallback_output = run_ffmpeg_progress_command(
                fallback_cmd,
                progress_total=progress_total or probe_duration(ffprobe, input_video) or 0.0,
                on_progress=on_progress,
                on_proc=on_proc,
                stop_event=stop_event,
            )
            return fallback_success, output_text + "\n" + fallback_output
        return False, output_text
    finally:
        try:
            os.unlink(text_file.name)
        except OSError:
            pass


def replace_video_bgm(
    ffmpeg,
    ffprobe,
    input_video,
    output_video,
    bgm_audio,
    progress_total=None,
    on_progress=None,
    on_proc=None,
    stop_event=None,
):
    cmd = [
        ffmpeg,
        "-y",
        "-stream_loop",
        "-1",
        "-i",
        str(bgm_audio),
        "-i",
        str(input_video),
        "-map",
        "1:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output_video),
        "-progress",
        "pipe:1",
        "-nostats",
    ]
    return run_ffmpeg_progress_command(
        cmd,
        progress_total=progress_total or probe_duration(ffprobe, input_video) or 0.0,
        on_progress=on_progress,
        on_proc=on_proc,
        stop_event=stop_event,
    )

class App:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.total_tasks = 0
        self.completed_tasks = 0

        self.tab_mode = StringVar(value="structured")
        self.front_dir = StringVar()
        self.middle_dir = StringVar()
        self.back_dir = StringVar()
        self.random_dir = StringVar()
        self.watermark_dir = StringVar()
        self.replace_bgm_dir = StringVar()
        self.output_dir = StringVar()
        self.replace_bgm_audio_path = StringVar()
        self.random_pick_count = IntVar(value=2)
        self.random_order_mode = StringVar(value=RANDOM_ORDER_DISTINCT)
        self.transition_enabled = BooleanVar(value=False)
        self.transition_profile = StringVar(value="轻量")
        self.light_transition_seconds = StringVar(value=DEFAULT_LIGHT_TRANSITION_SECONDS)
        self.watermark_mode = StringVar(value=WATERMARK_MODE_IMAGE)
        self.watermark_image_path = StringVar()
        self.watermark_image_size_percent = StringVar(value="20")
        self.watermark_image_opacity = StringVar(value="60")
        self.watermark_image_x = StringVar(value="50")
        self.watermark_image_y = StringVar(value="50")
        self.watermark_text_size = StringVar(value="48")
        self.watermark_text_opacity = StringVar(value="60")
        self.watermark_text_x = StringVar(value="50")
        self.watermark_text_y = StringVar(value="50")
        self.watermark_preview_width = StringVar(value="1080")
        self.watermark_preview_height = StringVar(value="1920")
        self.watermark_intervals_enabled = BooleanVar(value=False)
        self.watermark_tail_enabled = BooleanVar(value=False)
        self.watermark_tail_seconds = StringVar(value="3")
        self.watermark_fast_mode = BooleanVar(value=False)
        self.mode = StringVar(value="优先无损（失败自动转兼容）")
        self.resolution_mode = StringVar(value="custom")
        self.custom_width = IntVar(value=1080)
        self.custom_height = IntVar(value=1920)
        self.skip_existing = BooleanVar(value=True)
        self.max_workers = IntVar(value=max(2, min(4, os.cpu_count() or 2)))
        self.front_source_mode = StringVar(value=SOURCE_MODE_DIRECTORY)
        self.middle_source_mode = StringVar(value=SOURCE_MODE_DIRECTORY)
        self.back_source_mode = StringVar(value=SOURCE_MODE_DIRECTORY)
        self.random_source_mode = StringVar(value=SOURCE_MODE_DIRECTORY)
        self.watermark_source_mode = StringVar(value=SOURCE_MODE_DIRECTORY)
        self.replace_bgm_source_mode = StringVar(value=SOURCE_MODE_DIRECTORY)
        self.proc_lock = threading.Lock()
        self.running_procs = set()
        self.running_outputs = set()
        self.last_eta_emit = {}
        self.log_panels = []
        self.thread_slots = None
        self.settings_path = Path(__file__).resolve().parent / SETTINGS_FILE
        self.directory_memory = self.load_directory_memory()
        self.watermark_interval_rows = []
        self.watermark_preview_photo = None
        self.source_fields = {
            "front_dir": {
                "label": "前半段素材",
                "dir_var": self.front_dir,
                "mode_var": self.front_source_mode,
                "files": [],
                "optional": False,
            },
            "middle_dir": {
                "label": "中段素材（可选）",
                "dir_var": self.middle_dir,
                "mode_var": self.middle_source_mode,
                "files": [],
                "optional": True,
            },
            "back_dir": {
                "label": "后半段素材",
                "dir_var": self.back_dir,
                "mode_var": self.back_source_mode,
                "files": [],
                "optional": False,
            },
            "random_dir": {
                "label": "候选素材",
                "dir_var": self.random_dir,
                "mode_var": self.random_source_mode,
                "files": [],
                "optional": False,
            },
            "watermark_dir": {
                "label": "待处理视频",
                "dir_var": self.watermark_dir,
                "mode_var": self.watermark_source_mode,
                "files": [],
                "optional": False,
            },
            "replace_bgm_dir": {
                "label": "待处理视频",
                "dir_var": self.replace_bgm_dir,
                "mode_var": self.replace_bgm_source_mode,
                "files": [],
                "optional": False,
            },
        }

        self.build_ui()
        self.restore_directory_state()
        self.root.after(100, self.process_queue)

    def build_ui(self):
        print(f"App starting with Python {sys.version} and Tk {tk.TkVersion}", flush=True)
        self.root.title(f"视频混剪合并工具 {APP_VERSION}")
        self.root.minsize(880, 640)

        bg_color = "#f6f7fb"
        fg_color = "black"
        font_title = ("Helvetica", 15, "bold")
        font_normal = ("Helvetica", 11)
        font_small = ("Helvetica", 10)
        self.root.option_add("*Font", font_normal)
        self.root.configure(bg=bg_color)

        self.ui_bg = bg_color
        self.ui_fg = fg_color
        self.font_title = font_title
        self.font_normal = font_normal
        self.font_small = font_small

        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_anchor("nw")

        outer = tk.Frame(self.root, bg=bg_color)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.grid_rowconfigure(0, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        self.scroll_canvas = tk.Canvas(outer, bg=bg_color, highlightthickness=0)
        self.scroll_canvas.grid(row=0, column=0, sticky="nsew")
        scroll_bar = tk.Scrollbar(outer, orient="vertical", command=self.scroll_canvas.yview)
        scroll_bar.grid(row=0, column=1, sticky="ns")
        self.scroll_canvas.configure(yscrollcommand=scroll_bar.set)

        self.scroll_frame = tk.Frame(self.scroll_canvas, bg=bg_color)
        self.scroll_window = self.scroll_canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")

        self.scroll_frame.bind("<Configure>", self.on_scroll_frame_configure)
        self.scroll_canvas.bind("<Configure>", self.on_scroll_canvas_configure)
        self.scroll_canvas.bind_all("<MouseWheel>", self.on_mousewheel)
        self.scroll_canvas.bind_all("<Button-4>", self.on_mousewheel_linux)
        self.scroll_canvas.bind_all("<Button-5>", self.on_mousewheel_linux)

        container = tk.Frame(self.scroll_frame, padx=20, pady=20, bg=bg_color)
        container.grid(row=0, column=0, sticky="nsew")
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(5, weight=0)
        container.grid_anchor("nw")

        header = tk.Frame(container, bg=bg_color)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 15))
        header.grid_columnconfigure(0, weight=1)
        title = tk.Label(header, text="视频混剪合并工具", font=font_title, bg=bg_color, fg=fg_color)
        subtitle = tk.Label(header, text="前半段 × 中段 × 后半段 拼接，支持随机排列组合", font=font_small, bg=bg_color, fg="#666666")
        title.grid(row=0, column=0, sticky="w")
        subtitle.grid(row=1, column=0, sticky="w", pady=(2, 0))

        path_section = tk.Frame(container, bg=bg_color)
        path_section.grid(row=1, column=0, sticky="ew", pady=(0, 15))
        path_section.grid_columnconfigure(0, weight=1)
        tk.Label(path_section, text="素材与输出", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=0, sticky="w", pady=(0, 8))

        self.path_tabs = ttk.Notebook(path_section)
        self.path_tabs.grid(row=1, column=0, sticky="ew")
        self.path_tabs.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        self.path_tabs.bind("<MouseWheel>", self.on_notebook_mousewheel)
        self.path_tabs.bind("<Button-4>", self.on_notebook_mousewheel_linux)
        self.path_tabs.bind("<Button-5>", self.on_notebook_mousewheel_linux)
        self.root.bind_class("TNotebook", "<MouseWheel>", self.on_notebook_mousewheel)
        self.root.bind_class("TNotebook", "<Button-4>", self.on_notebook_mousewheel_linux)
        self.root.bind_class("TNotebook", "<Button-5>", self.on_notebook_mousewheel_linux)

        structured_tab = tk.Frame(self.path_tabs, bg=bg_color, padx=10, pady=10)
        structured_tab.grid_columnconfigure(0, weight=1)
        self.path_tabs.add(structured_tab, text="前中后拼接")

        random_tab = tk.Frame(self.path_tabs, bg=bg_color, padx=10, pady=10)
        random_tab.grid_columnconfigure(0, weight=1)
        self.path_tabs.add(random_tab, text="随机排列组合")

        watermark_tab = tk.Frame(self.path_tabs, bg=bg_color, padx=10, pady=10)
        watermark_tab.grid_columnconfigure(0, weight=1)
        self.path_tabs.add(watermark_tab, text="加水印")

        replace_bgm_tab = tk.Frame(self.path_tabs, bg=bg_color, padx=10, pady=10)
        replace_bgm_tab.grid_columnconfigure(0, weight=1)
        self.path_tabs.add(replace_bgm_tab, text="替换BGM")

        self.build_source_row(structured_tab, "front_dir")
        self.build_source_row(structured_tab, "middle_dir")
        self.build_source_row(structured_tab, "back_dir")

        self.build_source_row(random_tab, "random_dir")

        random_pick_row = tk.Frame(random_tab, bg=bg_color)
        random_pick_row.grid(row=1, column=0, sticky="ew", pady=4)
        random_pick_row.grid_columnconfigure(1, weight=1)
        tk.Label(random_pick_row, text="拼接数量", font=font_normal, bg=bg_color, fg=fg_color, width=12, anchor="w").grid(row=0, column=0, sticky="w")
        pick_menu = tk.OptionMenu(random_pick_row, self.random_pick_count, 2, 3, 4)
        pick_menu.config(width=8, font=font_normal, bg="white", fg=fg_color, highlightthickness=0)
        pick_menu["menu"].config(bg="white", fg=fg_color, font=font_normal)
        pick_menu.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.random_pick_info_var = StringVar(value="当前拼接数量：2 段")
        tk.Label(random_pick_row, textvariable=self.random_pick_info_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))

        random_order_row = tk.Frame(random_tab, bg=bg_color)
        random_order_row.grid(row=2, column=0, sticky="ew", pady=4)
        random_order_row.grid_columnconfigure(1, weight=1)
        tk.Label(random_order_row, text="拼接顺序", font=font_normal, bg=bg_color, fg=fg_color, width=12, anchor="w").grid(row=0, column=0, sticky="w")
        random_order_menu = tk.OptionMenu(random_order_row, self.random_order_mode, RANDOM_ORDER_DISTINCT, RANDOM_ORDER_IGNORE)
        random_order_menu.config(width=18, font=font_normal, bg="white", fg=fg_color, highlightthickness=0)
        random_order_menu["menu"].config(bg="white", fg=fg_color, font=font_normal)
        random_order_menu.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.build_source_row(watermark_tab, "watermark_dir")

        watermark_help_row = tk.Frame(watermark_tab, bg=bg_color)
        watermark_help_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        tk.Label(
            watermark_help_row,
            text="可处理整个目录内的视频，也可只处理手动选择的部分视频，结果输出到下方的输出目录。",
            font=font_small,
            bg=bg_color,
            fg="#666666",
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self.build_source_row(replace_bgm_tab, "replace_bgm_dir")

        replace_bgm_help_row = tk.Frame(replace_bgm_tab, bg=bg_color)
        replace_bgm_help_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        tk.Label(
            replace_bgm_help_row,
            text="可处理整个目录内的视频，也可只处理手动选择的部分视频；会完全移除原音频并替换为新的 BGM。",
            font=font_small,
            bg=bg_color,
            fg="#666666",
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        shared_path_content = tk.Frame(path_section, bg=bg_color)
        shared_path_content.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        shared_path_content.grid_columnconfigure(0, weight=1)
        out_row = self.build_path_row(shared_path_content, "输出目录", self.output_dir, self.pick_output, button_text="选择目录")
        self.estimated_var = StringVar(value="预计输出数量：0")
        tk.Label(out_row, textvariable=self.estimated_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))

        for field_name, source in self.source_fields.items():
            source["dir_var"].trace_add("write", lambda *args, field_name=field_name: self.handle_source_dir_change(field_name))
            source["mode_var"].trace_add("write", lambda *args, field_name=field_name: self.handle_source_mode_change(field_name))
        self.random_pick_count.trace_add("write", lambda *args: self.update_counts())
        self.random_order_mode.trace_add("write", lambda *args: self.update_counts())
        self.output_dir.trace_add("write", lambda *args: self.handle_directory_var_change("output_dir", self.output_dir))
        self.replace_bgm_audio_path.trace_add("write", lambda *args: self.handle_replace_bgm_audio_change())
        self.update_counts()

        options_section = tk.Frame(container, bg=bg_color)
        options_section.grid(row=2, column=0, sticky="ew", pady=(0, 15))
        options_section.grid_columnconfigure(0, weight=1)
        tk.Label(options_section, text="合并选项", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=0, sticky="w", pady=(0, 8))
        options_frame = tk.Frame(options_section, bg=bg_color)
        options_frame.grid(row=1, column=0, sticky="ew")
        options_frame.grid_columnconfigure(0, weight=1)
        self.merge_options_frame = options_frame

        row1 = tk.Frame(options_frame, bg=bg_color)
        row1.grid(row=0, column=0, sticky="ew", pady=4)
        row1.grid_columnconfigure(1, weight=1)
        tk.Label(row1, text="合并模式", font=font_normal, bg=bg_color, fg=fg_color, width=10, anchor="w").grid(row=0, column=0, sticky="w")
        mode_menu = tk.OptionMenu(
            row1,
            self.mode,
            "优先无损（失败自动转兼容）",
            "强制兼容（统一分辨率）",
        )
        mode_menu.config(width=26, font=font_normal, bg="white", fg=fg_color, highlightthickness=0)
        mode_menu["menu"].config(bg="white", fg=fg_color, font=font_normal)
        mode_menu.grid(row=0, column=1, sticky="w", padx=(10, 0))

        row2 = tk.Frame(options_frame, bg=bg_color)
        row2.grid(row=1, column=0, sticky="ew", pady=4)
        tk.Label(row2, text="统一分辨率", font=font_normal, bg=bg_color, fg=fg_color, width=10, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Radiobutton(
            row2,
            text="自动（以前当前任务首段为准）",
            variable=self.resolution_mode,
            value="auto",
            command=self.refresh_resolution_state,
            font=font_normal,
            bg=bg_color,
            fg=fg_color,
            activebackground=bg_color,
            activeforeground=fg_color,
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Radiobutton(
            row2,
            text="自定义",
            variable=self.resolution_mode,
            value="custom",
            command=self.refresh_resolution_state,
            font=font_normal,
            bg=bg_color,
            fg=fg_color,
            activebackground=bg_color,
            activeforeground=fg_color,
        ).grid(row=0, column=2, sticky="w", padx=(10, 0))

        tk.Label(row2, text="宽", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=3, sticky="w", padx=(10, 0))
        width_entry = tk.Entry(row2, textvariable=self.custom_width, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db")
        width_entry.grid(row=0, column=4, sticky="w", padx=(4, 0))
        tk.Label(row2, text="高", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=5, sticky="w", padx=(10, 0))
        height_entry = tk.Entry(row2, textvariable=self.custom_height, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db")
        height_entry.grid(row=0, column=6, sticky="w", padx=(4, 0))

        self.width_entry = width_entry
        self.height_entry = height_entry

        row3 = tk.Frame(options_frame, bg=bg_color)
        row3.grid(row=2, column=0, sticky="ew", pady=4)
        tk.Label(row3, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(row3, text="跳过已存在文件", variable=self.skip_existing, font=font_normal, bg=bg_color, fg=fg_color, activebackground=bg_color, activeforeground=fg_color).grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )

        transition_title_row = tk.Frame(options_frame, bg=bg_color)
        transition_title_row.grid(row=3, column=0, sticky="ew", pady=(10, 2))
        tk.Label(transition_title_row, text="转场效果", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=0, sticky="w")

        row4 = tk.Frame(options_frame, bg=bg_color)
        row4.grid(row=4, column=0, sticky="ew", pady=4)
        tk.Label(row4, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(
            row4,
            text="启用转场效果",
            variable=self.transition_enabled,
            command=self.refresh_transition_state,
            font=font_normal,
            bg=bg_color,
            fg=fg_color,
            activebackground=bg_color,
            activeforeground=fg_color,
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))

        row5 = tk.Frame(options_frame, bg=bg_color)
        row5.grid(row=5, column=0, sticky="ew", pady=4)
        tk.Label(row5, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        tk.Label(row5, text="轻量时长", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=1, sticky="w", padx=(10, 0))
        light_duration_menu = tk.OptionMenu(row5, self.light_transition_seconds, *LIGHT_TRANSITION_OPTIONS)
        light_duration_menu.config(width=8, font=font_normal, bg="white", fg=fg_color, highlightthickness=0)
        light_duration_menu["menu"].config(bg="white", fg=fg_color, font=font_normal)
        light_duration_menu.grid(row=0, column=2, sticky="w", padx=(10, 0))
        tk.Label(row5, text="秒", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=3, sticky="w", padx=(6, 0))

        row6 = tk.Frame(options_frame, bg=bg_color)
        row6.grid(row=6, column=0, sticky="ew", pady=(0, 4))
        tk.Label(row6, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        self.transition_hint_label = tk.Label(
            row6,
            text="提示：当前仅保留轻量转场；时长越长，过渡越明显，但处理会更慢。",
            font=font_small,
            bg=bg_color,
            fg="#666666",
        )
        self.transition_hint_label.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.light_transition_row = row5
        self.light_duration_menu = light_duration_menu
        self.transition_enabled.trace_add("write", lambda *args: self.refresh_transition_state())

        watermark_options_frame = tk.Frame(options_section, bg=bg_color)
        watermark_options_frame.grid(row=1, column=0, sticky="ew")
        watermark_options_frame.grid_columnconfigure(0, weight=1)
        self.watermark_options_frame = watermark_options_frame

        watermark_type_row = tk.Frame(watermark_options_frame, bg=bg_color)
        watermark_type_row.grid(row=0, column=0, sticky="ew", pady=4)
        tk.Label(watermark_type_row, text="水印类型", font=font_normal, bg=bg_color, fg=fg_color, width=10, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Radiobutton(
            watermark_type_row,
            text=WATERMARK_MODE_IMAGE,
            variable=self.watermark_mode,
            value=WATERMARK_MODE_IMAGE,
            command=self.refresh_watermark_mode_state,
            font=font_normal,
            bg=bg_color,
            fg=fg_color,
            activebackground=bg_color,
            activeforeground=fg_color,
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Radiobutton(
            watermark_type_row,
            text=WATERMARK_MODE_TEXT,
            variable=self.watermark_mode,
            value=WATERMARK_MODE_TEXT,
            command=self.refresh_watermark_mode_state,
            font=font_normal,
            bg=bg_color,
            fg=fg_color,
            activebackground=bg_color,
            activeforeground=fg_color,
        ).grid(row=0, column=2, sticky="w", padx=(10, 0))

        speed_row = tk.Frame(watermark_options_frame, bg=bg_color)
        speed_row.grid(row=1, column=0, sticky="ew", pady=4)
        tk.Label(speed_row, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(
            speed_row,
            text="极速模式",
            variable=self.watermark_fast_mode,
            font=font_normal,
            bg=bg_color,
            fg=fg_color,
            activebackground=bg_color,
            activeforeground=fg_color,
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Label(speed_row, text="更激进的编码参数，速度更快，画质和体积控制会更偏向速度。", font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(2, 0))

        image_frame = tk.Frame(watermark_options_frame, bg=bg_color)
        image_frame.grid(row=2, column=0, sticky="ew", pady=4)
        image_frame.grid_columnconfigure(1, weight=1)
        tk.Label(image_frame, text="PNG 水印", font=font_normal, bg=bg_color, fg=fg_color, width=10, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Entry(image_frame, textvariable=self.watermark_image_path, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=1, sticky="ew", padx=(10, 10))
        tk.Button(image_frame, text="选择图片", command=self.pick_watermark_image, font=self.font_normal, highlightbackground=bg_color).grid(row=0, column=2, sticky="e")
        self.watermark_image_meta_var = StringVar(value="图片尺寸：未选择")
        tk.Label(image_frame, textvariable=self.watermark_image_meta_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(4, 0))

        image_param_row = tk.Frame(watermark_options_frame, bg=bg_color)
        image_param_row.grid(row=3, column=0, sticky="ew", pady=4)
        tk.Label(image_param_row, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        tk.Label(image_param_row, text="大小%", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Entry(image_param_row, textvariable=self.watermark_image_size_percent, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=2, sticky="w", padx=(4, 0))
        tk.Label(image_param_row, text="透明度%", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=3, sticky="w", padx=(12, 0))
        tk.Entry(image_param_row, textvariable=self.watermark_image_opacity, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=4, sticky="w", padx=(4, 0))
        tk.Label(image_param_row, text="X", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=5, sticky="w", padx=(12, 0))
        tk.Entry(image_param_row, textvariable=self.watermark_image_x, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=6, sticky="w", padx=(4, 0))
        tk.Label(image_param_row, text="Y", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=7, sticky="w", padx=(12, 0))
        tk.Entry(image_param_row, textvariable=self.watermark_image_y, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=8, sticky="w", padx=(4, 0))

        text_frame = tk.Frame(watermark_options_frame, bg=bg_color)
        text_frame.grid(row=4, column=0, sticky="ew", pady=4)
        text_frame.grid_columnconfigure(1, weight=1)
        tk.Label(text_frame, text="文字水印", font=font_normal, bg=bg_color, fg=fg_color, width=10, anchor="nw").grid(row=0, column=0, sticky="nw")
        self.watermark_text_widget = ScrolledText(text_frame, height=4, font=("Helvetica", 11), bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db")
        self.watermark_text_widget.grid(row=0, column=1, sticky="ew", padx=(10, 0))

        text_param_row = tk.Frame(watermark_options_frame, bg=bg_color)
        text_param_row.grid(row=5, column=0, sticky="ew", pady=4)
        tk.Label(text_param_row, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        tk.Label(text_param_row, text="字号", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Entry(text_param_row, textvariable=self.watermark_text_size, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=2, sticky="w", padx=(4, 0))
        tk.Label(text_param_row, text="透明度%", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=3, sticky="w", padx=(12, 0))
        tk.Entry(text_param_row, textvariable=self.watermark_text_opacity, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=4, sticky="w", padx=(4, 0))
        tk.Label(text_param_row, text="X", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=5, sticky="w", padx=(12, 0))
        tk.Entry(text_param_row, textvariable=self.watermark_text_x, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=6, sticky="w", padx=(4, 0))
        tk.Label(text_param_row, text="Y", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=7, sticky="w", padx=(12, 0))
        tk.Entry(text_param_row, textvariable=self.watermark_text_y, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=8, sticky="w", padx=(4, 0))

        preview_row = tk.Frame(watermark_options_frame, bg=bg_color)
        preview_row.grid(row=6, column=0, sticky="ew", pady=4)
        tk.Label(preview_row, text="预览尺寸", font=font_normal, bg=bg_color, fg=fg_color, width=10, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(preview_row, text="宽", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Entry(preview_row, textvariable=self.watermark_preview_width, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=2, sticky="w", padx=(4, 0))
        tk.Label(preview_row, text="高", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=3, sticky="w", padx=(10, 0))
        tk.Entry(preview_row, textvariable=self.watermark_preview_height, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=4, sticky="w", padx=(4, 0))
        tk.Button(preview_row, text="预览", command=self.render_watermark_preview, font=self.font_normal, highlightbackground=bg_color).grid(row=0, column=5, sticky="w", padx=(12, 0))

        preview_canvas_row = tk.Frame(watermark_options_frame, bg=bg_color)
        preview_canvas_row.grid(row=7, column=0, sticky="ew", pady=(4, 8))
        tk.Label(preview_canvas_row, text="预览画布", font=font_normal, bg=bg_color, fg=fg_color, width=10, anchor="nw").grid(row=0, column=0, sticky="nw")
        self.watermark_preview_canvas = tk.Canvas(preview_canvas_row, width=360, height=240, bg="white", highlightthickness=1, highlightbackground="#d1d5db")
        self.watermark_preview_canvas.grid(row=0, column=1, sticky="w", padx=(10, 0))

        schedule_title_row = tk.Frame(watermark_options_frame, bg=bg_color)
        schedule_title_row.grid(row=8, column=0, sticky="ew", pady=(8, 2))
        tk.Label(schedule_title_row, text="出现时间配置", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=0, sticky="w")

        explicit_toggle_row = tk.Frame(watermark_options_frame, bg=bg_color)
        explicit_toggle_row.grid(row=9, column=0, sticky="ew", pady=4)
        tk.Label(explicit_toggle_row, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(
            explicit_toggle_row,
            text="启用指定秒段",
            variable=self.watermark_intervals_enabled,
            command=self.refresh_watermark_schedule_state,
            font=font_normal,
            bg=bg_color,
            fg=fg_color,
            activebackground=bg_color,
            activeforeground=fg_color,
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.watermark_add_interval_button = tk.Button(explicit_toggle_row, text="新增时间段", command=self.add_watermark_interval_row, font=self.font_normal, highlightbackground=bg_color)
        self.watermark_add_interval_button.grid(row=0, column=2, sticky="w", padx=(12, 0))

        self.watermark_intervals_frame = tk.Frame(watermark_options_frame, bg=bg_color)
        self.watermark_intervals_frame.grid(row=10, column=0, sticky="ew", pady=(0, 4))

        tail_row = tk.Frame(watermark_options_frame, bg=bg_color)
        tail_row.grid(row=11, column=0, sticky="ew", pady=4)
        tk.Label(tail_row, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        tk.Checkbutton(
            tail_row,
            text="启用结尾倒数秒",
            variable=self.watermark_tail_enabled,
            command=self.refresh_watermark_schedule_state,
            font=font_normal,
            bg=bg_color,
            fg=fg_color,
            activebackground=bg_color,
            activeforeground=fg_color,
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        tk.Entry(tail_row, textvariable=self.watermark_tail_seconds, width=6, font=font_normal, bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db").grid(row=0, column=2, sticky="w", padx=(8, 0))
        tk.Label(tail_row, text="秒", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=3, sticky="w", padx=(6, 0))

        self.watermark_image_frame = image_frame
        self.watermark_image_param_row = image_param_row
        self.watermark_text_frame = text_frame
        self.watermark_text_param_row = text_param_row
        self.watermark_tail_row = tail_row
        self.watermark_mode.trace_add("write", lambda *args: self.refresh_watermark_mode_state())
        self.watermark_intervals_enabled.trace_add("write", lambda *args: self.refresh_watermark_schedule_state())
        self.watermark_tail_enabled.trace_add("write", lambda *args: self.refresh_watermark_schedule_state())
        self.add_watermark_interval_row()

        self.watermark_options_frame.grid_remove()

        replace_bgm_options_frame = tk.Frame(options_section, bg=bg_color)
        replace_bgm_options_frame.grid(row=1, column=0, sticky="ew")
        replace_bgm_options_frame.grid_columnconfigure(0, weight=1)
        self.replace_bgm_options_frame = replace_bgm_options_frame

        bgm_audio_row = tk.Frame(replace_bgm_options_frame, bg=bg_color)
        bgm_audio_row.grid(row=0, column=0, sticky="ew", pady=4)
        bgm_audio_row.grid_columnconfigure(1, weight=1)
        tk.Label(bgm_audio_row, text="BGM 文件", font=font_normal, bg=bg_color, fg=fg_color, width=10, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Entry(
            bgm_audio_row,
            textvariable=self.replace_bgm_audio_path,
            font=font_normal,
            bg="white",
            fg=fg_color,
            highlightthickness=1,
            highlightbackground="#d1d5db",
        ).grid(row=0, column=1, sticky="ew", padx=(10, 10))
        tk.Button(
            bgm_audio_row,
            text="选择音频",
            command=self.pick_replace_bgm_audio,
            font=self.font_normal,
            highlightbackground=bg_color,
        ).grid(row=0, column=2, sticky="e")

        bgm_hint_row = tk.Frame(replace_bgm_options_frame, bg=bg_color)
        bgm_hint_row.grid(row=1, column=0, sticky="ew", pady=(2, 4))
        tk.Label(bgm_hint_row, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        tk.Label(
            bgm_hint_row,
            text="视频流直接 copy，不重编码视频；原音频会被完全移除。BGM 不足时自动循环，超出视频时自动截断。",
            font=font_small,
            bg=bg_color,
            fg="#666666",
            justify="left",
            anchor="w",
        ).grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.replace_bgm_options_frame.grid_remove()

        progress_section = tk.Frame(container, bg=bg_color)
        progress_section.grid(row=3, column=0, sticky="ew", pady=(0, 15))
        tk.Label(progress_section, text="任务进度", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=0, sticky="w", pady=(0, 8))
        progress_section.grid_columnconfigure(0, weight=1)

        self.progress_canvas = tk.Canvas(progress_section, height=20, highlightthickness=0, bg="#e5e7eb")
        self.progress_canvas.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.progress_bar_rect = self.progress_canvas.create_rectangle(0, 0, 0, 20, fill="#3b82f6", width=0)
        self.progress_label = tk.Label(progress_section, text="0 / 0", font=font_normal, bg=bg_color, fg=fg_color)
        self.progress_label.grid(row=2, column=0, sticky="w")
        self.eta_label = tk.Label(progress_section, text="剩余时间估算：--:--", font=font_small, bg=bg_color, fg="#666666")
        self.eta_label.grid(row=3, column=0, sticky="w")

        buttons_frame = tk.Frame(container, bg=bg_color)
        buttons_frame.grid(row=4, column=0, sticky="ew", pady=(0, 15))
        buttons_frame.grid_columnconfigure(3, weight=1)
        btn_font = ("Helvetica", 12, "bold")
        self.start_button = tk.Button(buttons_frame, text="开始任务", command=self.start_merge, font=btn_font, bg="#3b82f6", fg="black", activebackground="#2563eb", activeforeground="black", highlightbackground=bg_color)
        self.stop_button = tk.Button(buttons_frame, text="停止任务", command=self.stop_merge, font=btn_font, state="disabled", highlightbackground=bg_color)
        self.open_button = tk.Button(buttons_frame, text="打开输出目录", command=self.open_output, font=btn_font, highlightbackground=bg_color)
        tk.Label(buttons_frame, text="并发数", font=font_small, bg=bg_color, fg="#666666").grid(row=0, column=3, sticky="e", padx=(10, 4))
        tk.Spinbox(buttons_frame, from_=1, to=max(1, (os.cpu_count() or 4)), textvariable=self.max_workers, width=4).grid(row=0, column=4, sticky="e")
        self.start_button.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(0, 10))
        self.open_button.grid(row=0, column=2, sticky="w")

        log_section = tk.Frame(container, bg=bg_color)
        log_section.grid(row=5, column=0, sticky="nsew")
        log_section.grid_columnconfigure(0, weight=1)
        log_section.grid_rowconfigure(1, weight=1)
        tk.Label(log_section, text="运行日志", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=0, sticky="w", pady=(0, 8))

        self.log_container = tk.Frame(log_section, bg=bg_color)
        self.log_container.grid(row=1, column=0, sticky="nsew")
        self.log_container.grid_columnconfigure(0, weight=1)
        self.log_container.grid_rowconfigure(0, weight=1)

        self.log_text = ScrolledText(self.log_container, height=10, font=("Menlo", 11), state="disabled", bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self.refresh_resolution_state()
        self.refresh_transition_state()
        self.refresh_watermark_mode_state()
        self.refresh_watermark_schedule_state()
        self.on_tab_changed()

        if os.environ.get("VIDEO_MIX_DEBUG_UI") == "1":
            self.root.after(800, self.dump_ui_state)

    def on_scroll_frame_configure(self, event):
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def on_scroll_canvas_configure(self, event):
        self.scroll_canvas.itemconfig(self.scroll_window, width=event.width)

    def _normalize_mousewheel_delta(self, delta):
        if delta == 0:
            return 0
        direction = -1 if delta > 0 else 1
        magnitude = abs(delta)
        if sys.platform == "darwin":
            # macOS often reports large wheel/trackpad deltas; clamp to small steps.
            steps = max(1, min(4, int(magnitude / 40) or 1))
        else:
            steps = max(1, int(magnitude / 120) or 1)
        return direction * steps

    def on_mousewheel(self, event):
        delta = self._normalize_mousewheel_delta(event.delta)
        if delta != 0:
            self.scroll_canvas.yview_scroll(delta, "units")
        return "break"

    def on_mousewheel_linux(self, event):
        if event.num == 4:
            self.scroll_canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.scroll_canvas.yview_scroll(1, "units")
        return "break"

    def on_tab_changed(self, event=None):
        current_index = self.path_tabs.index(self.path_tabs.select())
        if current_index == 0:
            self.tab_mode.set("structured")
        elif current_index == 1:
            self.tab_mode.set("random")
        elif current_index == 2:
            self.tab_mode.set("watermark")
        else:
            self.tab_mode.set("replace_bgm")
        if self.tab_mode.get() == "watermark":
            self.merge_options_frame.grid_remove()
            self.replace_bgm_options_frame.grid_remove()
            self.watermark_options_frame.grid()
        elif self.tab_mode.get() == "replace_bgm":
            self.merge_options_frame.grid_remove()
            self.watermark_options_frame.grid_remove()
            self.replace_bgm_options_frame.grid()
        else:
            self.watermark_options_frame.grid_remove()
            self.replace_bgm_options_frame.grid_remove()
            self.merge_options_frame.grid()
        self.update_counts()

    def on_notebook_mousewheel(self, event):
        self.on_mousewheel(event)
        return "break"

    def on_notebook_mousewheel_linux(self, event):
        self.on_mousewheel_linux(event)
        return "break"

    def build_source_row(self, parent, field_name):
        source = self.source_fields[field_name]
        row = parent.grid_size()[1]
        row_frame = tk.Frame(parent, bg=self.ui_bg, padx=10, pady=10, highlightthickness=1, highlightbackground="#e5e7eb")
        row_frame.grid(row=row, column=0, sticky="ew", pady=6)
        row_frame.grid_columnconfigure(0, weight=1)

        header = tk.Frame(row_frame, bg=self.ui_bg)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        tk.Label(header, text=source["label"], font=self.font_normal, bg=self.ui_bg, fg=self.ui_fg, anchor="w").grid(row=0, column=0, sticky="w")

        mode_frame = tk.Frame(header, bg=self.ui_bg)
        mode_frame.grid(row=0, column=1, sticky="e")
        dir_toggle = tk.Radiobutton(
            mode_frame,
            text="目录",
            variable=source["mode_var"],
            value=SOURCE_MODE_DIRECTORY,
            indicatoron=False,
            width=8,
            font=self.font_small,
            bg="white",
            fg=self.ui_fg,
            selectcolor="#dbeafe",
            activebackground="#eff6ff",
            activeforeground=self.ui_fg,
            relief="flat",
            bd=1,
            highlightthickness=1,
            highlightbackground="#cbd5e1",
        )
        dir_toggle.grid(row=0, column=0, sticky="e")
        files_toggle = tk.Radiobutton(
            mode_frame,
            text="指定文件",
            variable=source["mode_var"],
            value=SOURCE_MODE_FILES,
            indicatoron=False,
            width=8,
            font=self.font_small,
            bg="white",
            fg=self.ui_fg,
            selectcolor="#dbeafe",
            activebackground="#eff6ff",
            activeforeground=self.ui_fg,
            relief="flat",
            bd=1,
            highlightthickness=1,
            highlightbackground="#cbd5e1",
        )
        files_toggle.grid(row=0, column=1, sticky="e", padx=(6, 0))

        action_row = tk.Frame(row_frame, bg=self.ui_bg)
        action_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        action_row.grid_columnconfigure(0, weight=1)

        source["display_var"] = StringVar(value="未选择目录")
        display_entry = tk.Entry(
            action_row,
            textvariable=source["display_var"],
            font=self.font_normal,
            bg="white",
            fg=self.ui_fg,
            state="readonly",
            readonlybackground="white",
            highlightthickness=1,
            highlightbackground="#d1d5db",
        )
        display_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        select_dir_button = tk.Button(
            action_row,
            text="选择目录",
            command=lambda field_name=field_name: self.pick_source_directory(field_name),
            font=self.font_normal,
            highlightbackground=self.ui_bg,
        )
        select_dir_button.grid(row=0, column=1, sticky="e")

        select_files_button = tk.Button(
            action_row,
            text="选择文件",
            command=lambda field_name=field_name: self.pick_source_files(field_name),
            font=self.font_normal,
            highlightbackground=self.ui_bg,
        )
        select_files_button.grid(row=0, column=2, sticky="e", padx=(8, 0))

        view_files_button = tk.Button(
            action_row,
            text="查看",
            command=lambda field_name=field_name: self.open_selected_files_dialog(field_name),
            font=self.font_normal,
            highlightbackground=self.ui_bg,
        )
        view_files_button.grid(row=0, column=3, sticky="e", padx=(8, 0))

        clear_files_button = tk.Button(
            action_row,
            text="清空",
            command=lambda field_name=field_name: self.clear_source_files(field_name),
            font=self.font_normal,
            highlightbackground=self.ui_bg,
        )
        clear_files_button.grid(row=0, column=4, sticky="e", padx=(8, 0))

        source["count_var"] = StringVar(value="未选择目录")
        tk.Label(row_frame, textvariable=source["count_var"], font=self.font_small, bg=self.ui_bg, fg="#666666").grid(row=2, column=0, sticky="w", pady=(6, 0))

        source["select_dir_button"] = select_dir_button
        source["select_files_button"] = select_files_button
        source["view_files_button"] = view_files_button
        source["clear_files_button"] = clear_files_button
        source["display_entry"] = display_entry
        self.refresh_source_widgets(field_name)
        return row_frame

    def build_path_row(self, parent, label, var, command, button_text="选择"):
        row = parent.grid_size()[1]
        row_frame = tk.Frame(parent, bg=self.ui_bg)
        row_frame.grid(row=row, column=0, sticky="ew", pady=4)
        row_frame.grid_columnconfigure(1, weight=1, minsize=360)

        tk.Label(row_frame, text=label, font=self.font_normal, bg=self.ui_bg, fg=self.ui_fg, width=12, anchor="w").grid(row=0, column=0, sticky="w")
        entry = tk.Entry(row_frame, textvariable=var, font=self.font_normal, bg="white", fg=self.ui_fg, highlightthickness=1, highlightbackground="#d1d5db")
        entry.grid(row=0, column=1, sticky="ew", padx=(10, 10))
        tk.Button(row_frame, text=button_text, command=command, font=self.font_normal, highlightbackground=self.ui_bg).grid(row=0, column=2, sticky="e")
        return row_frame

    def get_source_files_key(self, field_name):
        return f"{field_name}_files"

    def get_source_mode_key(self, field_name):
        return f"{field_name}_mode"

    def normalize_source_file_paths(self, paths):
        normalized = []
        seen = set()
        for raw_path in paths:
            path_str = str(raw_path).strip()
            if not path_str:
                continue
            path_obj = Path(path_str)
            resolved = str(path_obj)
            if resolved in seen:
                continue
            seen.add(resolved)
            normalized.append(resolved)
        return normalized

    def get_source_file_paths(self, field_name):
        return list(self.source_fields[field_name]["files"])

    def set_source_file_paths(self, field_name, paths):
        normalized = self.normalize_source_file_paths(paths)
        self.source_fields[field_name]["files"] = normalized
        self.directory_memory[self.get_source_files_key(field_name)] = normalized
        if normalized:
            self.directory_memory["last_browsed_dir"] = str(Path(normalized[0]).parent)
        self.save_directory_memory()

    def get_source_file_stats(self, field_name):
        valid_paths = []
        invalid_count = 0
        for raw_path in self.get_source_file_paths(field_name):
            path_obj = Path(raw_path)
            if is_video_file(path_obj):
                valid_paths.append(path_obj)
            else:
                invalid_count += 1
        folder_count = len({str(path.parent) for path in valid_paths})
        return valid_paths, invalid_count, folder_count

    def get_source_videos(self, field_name):
        source = self.source_fields[field_name]
        if source["mode_var"].get() == SOURCE_MODE_FILES:
            valid_paths, _invalid_count, _folder_count = self.get_source_file_stats(field_name)
            return valid_paths
        directory = source["dir_var"].get().strip()
        return list_videos(directory) if directory else []

    def source_has_selection(self, field_name):
        source = self.source_fields[field_name]
        if source["mode_var"].get() == SOURCE_MODE_FILES:
            return len(self.get_source_file_paths(field_name)) > 0
        return bool(source["dir_var"].get().strip())

    def get_initial_source_browse_dir(self, field_name):
        source = self.source_fields[field_name]
        current_dir = source["dir_var"].get().strip()
        if current_dir and Path(current_dir).is_dir():
            return current_dir
        valid_paths, _invalid_count, _folder_count = self.get_source_file_stats(field_name)
        if valid_paths:
            return str(valid_paths[0].parent)
        remembered = self.directory_memory.get(field_name)
        if remembered and Path(remembered).is_dir():
            return remembered
        last_browsed = self.directory_memory.get("last_browsed_dir")
        if last_browsed and Path(last_browsed).is_dir():
            return last_browsed
        return str(Path(__file__).resolve().parent)

    def build_source_summary(self, field_name):
        source = self.source_fields[field_name]
        mode = source["mode_var"].get()
        if mode == SOURCE_MODE_FILES:
            raw_paths = self.get_source_file_paths(field_name)
            valid_paths, invalid_count, folder_count = self.get_source_file_stats(field_name)
            if raw_paths:
                display_text = f"已选择 {len(raw_paths)} 个文件"
            else:
                display_text = "未选择文件"
            if valid_paths:
                summary = f"已选择 {len(valid_paths)} 个视频"
                if folder_count:
                    summary += f"，来自 {folder_count} 个文件夹"
                if invalid_count:
                    summary += f"；{invalid_count} 个文件已失效"
            else:
                summary = "请选择一个或多个视频文件"
                if invalid_count:
                    summary = f"当前没有有效视频；{invalid_count} 个文件已失效"
            return display_text, summary

        directory = source["dir_var"].get().strip()
        if directory:
            videos = list_videos(directory)
            display_text = directory
            if videos:
                summary = f"共 {len(videos)} 个视频"
            else:
                summary = "目录中未找到可用视频"
        else:
            display_text = "未选择目录"
            summary = "未选择目录"
            if source.get("optional"):
                summary = "未选择素材，将跳过这一段"
        return display_text, summary

    def refresh_source_widgets(self, field_name):
        source = self.source_fields[field_name]
        display_text, summary = self.build_source_summary(field_name)
        source["display_var"].set(display_text)
        source["count_var"].set(summary)
        is_files_mode = source["mode_var"].get() == SOURCE_MODE_FILES
        has_selected_files = len(self.get_source_file_paths(field_name)) > 0
        if is_files_mode:
            source["select_dir_button"].grid_remove()
            source["select_files_button"].grid()
            source["view_files_button"].grid()
            source["clear_files_button"].grid()
            source["view_files_button"].configure(state="normal" if has_selected_files else "disabled")
            source["clear_files_button"].configure(state="normal" if has_selected_files else "disabled")
        else:
            source["select_dir_button"].grid()
            source["select_files_button"].grid_remove()
            source["view_files_button"].grid_remove()
            source["clear_files_button"].grid_remove()

    def handle_source_dir_change(self, field_name):
        source = self.source_fields[field_name]
        self.handle_directory_var_change(field_name, source["dir_var"])
        self.refresh_source_widgets(field_name)
        self.update_counts()

    def handle_source_mode_change(self, field_name):
        self.directory_memory[self.get_source_mode_key(field_name)] = self.source_fields[field_name]["mode_var"].get()
        self.save_directory_memory()
        self.refresh_source_widgets(field_name)
        self.update_counts()

    def pick_source_directory(self, field_name):
        source = self.source_fields[field_name]
        initial_dir = self.get_initial_source_browse_dir(field_name)
        path = filedialog.askdirectory(initialdir=initial_dir)
        if path:
            source["dir_var"].set(path)
            self.remember_directory(field_name, path)
            self.refresh_source_widgets(field_name)
            self.update_counts()

    def pick_source_files(self, field_name, append=False, parent=None):
        initial_dir = self.get_initial_source_browse_dir(field_name)
        paths = filedialog.askopenfilenames(
            parent=parent,
            initialdir=initial_dir,
            title=f"选择{self.source_fields[field_name]['label']}",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.m4v"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        existing_paths = self.get_source_file_paths(field_name) if append else []
        self.set_source_file_paths(field_name, existing_paths + list(paths))
        self.refresh_source_widgets(field_name)
        self.update_counts()

    def clear_source_files(self, field_name):
        self.set_source_file_paths(field_name, [])
        self.refresh_source_widgets(field_name)
        self.update_counts()

    def open_selected_files_dialog(self, field_name):
        source = self.source_fields[field_name]
        dialog = tk.Toplevel(self.root)
        dialog.title(f"{source['label']} - 已选文件")
        dialog.transient(self.root)
        dialog.geometry("760x440")
        dialog.minsize(680, 360)
        dialog.configure(bg=self.ui_bg)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)

        summary_var = StringVar()
        tk.Label(dialog, text=source["label"], font=self.font_normal, bg=self.ui_bg, fg=self.ui_fg).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 4))
        tk.Label(dialog, textvariable=summary_var, font=self.font_small, bg=self.ui_bg, fg="#666666").grid(row=0, column=0, sticky="e", padx=16, pady=(16, 4))

        list_frame = tk.Frame(dialog, bg=self.ui_bg)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 12))
        list_frame.grid_columnconfigure(0, weight=1)
        list_frame.grid_rowconfigure(0, weight=1)

        listbox = tk.Listbox(
            list_frame,
            selectmode=tk.EXTENDED,
            font=("Menlo", 11),
            bg="white",
            fg=self.ui_fg,
            highlightthickness=1,
            highlightbackground="#d1d5db",
        )
        listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        listbox.configure(yscrollcommand=scrollbar.set)

        button_row = tk.Frame(dialog, bg=self.ui_bg)
        button_row.grid(row=2, column=0, sticky="e", padx=16, pady=(0, 16))

        def refresh_list():
            listbox.delete(0, "end")
            raw_paths = self.get_source_file_paths(field_name)
            valid_paths, invalid_count, folder_count = self.get_source_file_stats(field_name)
            for raw_path in raw_paths:
                path_obj = Path(raw_path)
                prefix = "[失效] " if not is_video_file(path_obj) else ""
                listbox.insert("end", f"{prefix}{path_obj.name}    {path_obj.parent}")
            summary_text = f"共 {len(valid_paths)} 个有效视频"
            if folder_count:
                summary_text += f"，来自 {folder_count} 个文件夹"
            if invalid_count:
                summary_text += f"；{invalid_count} 个已失效"
            if not raw_paths:
                summary_text = "当前未选择文件"
            summary_var.set(summary_text)

        def append_files():
            self.pick_source_files(field_name, append=True, parent=dialog)
            refresh_list()

        def remove_selected():
            selected = listbox.curselection()
            if not selected:
                return
            raw_paths = self.get_source_file_paths(field_name)
            remaining = [path for index, path in enumerate(raw_paths) if index not in selected]
            self.set_source_file_paths(field_name, remaining)
            self.refresh_source_widgets(field_name)
            self.update_counts()
            refresh_list()

        def clear_all():
            self.clear_source_files(field_name)
            refresh_list()

        tk.Button(button_row, text="追加选择", command=append_files, font=self.font_normal, highlightbackground=self.ui_bg).grid(row=0, column=0, padx=(0, 8))
        tk.Button(button_row, text="移除选中", command=remove_selected, font=self.font_normal, highlightbackground=self.ui_bg).grid(row=0, column=1, padx=(0, 8))
        tk.Button(button_row, text="清空全部", command=clear_all, font=self.font_normal, highlightbackground=self.ui_bg).grid(row=0, column=2, padx=(0, 8))
        tk.Button(button_row, text="关闭", command=dialog.destroy, font=self.font_normal, highlightbackground=self.ui_bg).grid(row=0, column=3)
        refresh_list()

    def dump_ui_state(self):
        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)

        widgets = list(walk(self.root))
        mapped = [w for w in widgets if w.winfo_ismapped()]
        labels = [w for w in widgets if w.winfo_class() in {"Label", "TLabel"}]
        entries = [w for w in widgets if w.winfo_class() in {"Entry", "TEntry"}]
        buttons = [w for w in widgets if w.winfo_class() in {"Button", "TButton"}]

        def info(widget):
            try:
                text = widget.cget("text")
            except Exception:
                text = ""
            return f"{widget.winfo_class():7} mapped={int(widget.winfo_ismapped())} geom={widget.winfo_geometry():>12} manager={widget.winfo_manager():>5} text={text!r}"

        print("=== UI DUMP BEGIN ===", flush=True)
        print(f"Python {sys.version}")
        print(f"Tkinter {tk.TkVersion}")
        try:
            print(f"Tcl/Tk {self.root.tk.call('info', 'patchlevel')}")
        except Exception:
            pass
        print(f"total={len(widgets)} mapped={len(mapped)} labels={len(labels)} entries={len(entries)} buttons={len(buttons)}", flush=True)
        for widget in labels[:40]:
            print(info(widget), flush=True)
        for widget in entries[:20]:
            print(info(widget), flush=True)
        print("=== UI DUMP END ===", flush=True)

    def refresh_resolution_state(self):
        if self.resolution_mode.get() == "custom":
            self.width_entry.configure(state="normal")
            self.height_entry.configure(state="normal")
        else:
            self.width_entry.configure(state="disabled")
            self.height_entry.configure(state="disabled")

    def refresh_transition_state(self):
        enabled = self.transition_enabled.get()
        if enabled:
            self.light_transition_row.grid()
            self.light_duration_menu.configure(state="normal")
        else:
            self.light_transition_row.grid_remove()
            self.light_duration_menu.configure(state="disabled")
        self.transition_hint_label.configure(fg="#666666" if enabled else "#b0b0b0")

    def refresh_watermark_mode_state(self):
        image_mode = self.watermark_mode.get() == WATERMARK_MODE_IMAGE
        if image_mode:
            self.watermark_image_frame.grid()
            self.watermark_image_param_row.grid()
            self.watermark_text_frame.grid_remove()
            self.watermark_text_param_row.grid_remove()
        else:
            self.watermark_image_frame.grid_remove()
            self.watermark_image_param_row.grid_remove()
            self.watermark_text_frame.grid()
            self.watermark_text_param_row.grid()

    def refresh_watermark_schedule_state(self):
        interval_state = "normal" if self.watermark_intervals_enabled.get() else "disabled"
        self.watermark_add_interval_button.configure(state=interval_state)
        for row in self.watermark_interval_rows:
            row["start_entry"].configure(state=interval_state)
            row["end_entry"].configure(state=interval_state)
            row["remove_button"].configure(state=interval_state)
        tail_state = "normal" if self.watermark_tail_enabled.get() else "disabled"
        if hasattr(self, "watermark_tail_row"):
            for child in self.watermark_tail_row.winfo_children():
                if isinstance(child, tk.Entry):
                    child.configure(state=tail_state)

    def add_watermark_interval_row(self, start_value="", end_value=""):
        row_index = len(self.watermark_interval_rows)
        row_frame = tk.Frame(self.watermark_intervals_frame, bg=self.ui_bg)
        row_frame.grid(row=row_index, column=0, sticky="ew", pady=2)
        tk.Label(row_frame, text="", font=self.font_normal, bg=self.ui_bg, width=10).grid(row=0, column=0, sticky="w")
        tk.Label(row_frame, text="开始秒", font=self.font_normal, bg=self.ui_bg, fg=self.ui_fg).grid(row=0, column=1, sticky="w", padx=(10, 0))
        start_var = StringVar(value=start_value)
        start_entry = tk.Entry(row_frame, textvariable=start_var, width=8, font=self.font_normal, bg="white", fg=self.ui_fg, highlightthickness=1, highlightbackground="#d1d5db")
        start_entry.grid(row=0, column=2, sticky="w", padx=(4, 0))
        tk.Label(row_frame, text="结束秒", font=self.font_normal, bg=self.ui_bg, fg=self.ui_fg).grid(row=0, column=3, sticky="w", padx=(10, 0))
        end_var = StringVar(value=end_value)
        end_entry = tk.Entry(row_frame, textvariable=end_var, width=8, font=self.font_normal, bg="white", fg=self.ui_fg, highlightthickness=1, highlightbackground="#d1d5db")
        end_entry.grid(row=0, column=4, sticky="w", padx=(4, 0))
        remove_button = tk.Button(row_frame, text="删除", command=lambda: self.remove_watermark_interval_row(row_frame), font=self.font_normal, highlightbackground=self.ui_bg)
        remove_button.grid(row=0, column=5, sticky="w", padx=(12, 0))
        self.watermark_interval_rows.append(
            {
                "frame": row_frame,
                "start_var": start_var,
                "end_var": end_var,
                "start_entry": start_entry,
                "end_entry": end_entry,
                "remove_button": remove_button,
            }
        )
        self.refresh_watermark_schedule_state()

    def remove_watermark_interval_row(self, row_frame):
        if len(self.watermark_interval_rows) <= 1:
            for row in self.watermark_interval_rows:
                if row["frame"] == row_frame:
                    row["start_var"].set("")
                    row["end_var"].set("")
            return
        self.watermark_interval_rows = [row for row in self.watermark_interval_rows if row["frame"] != row_frame]
        row_frame.destroy()
        for index, row in enumerate(self.watermark_interval_rows):
            row["frame"].grid_configure(row=index)

    def pick_watermark_image(self):
        initial_dir = self.directory_memory.get("last_browsed_dir", str(Path(__file__).resolve().parent))
        current_path = self.watermark_image_path.get().strip()
        if current_path and Path(current_path).exists():
            initial_dir = str(Path(current_path).parent)
        path = filedialog.askopenfilename(
            initialdir=initial_dir,
            filetypes=[("PNG 图片", "*.png")],
        )
        if path:
            self.watermark_image_path.set(path)
            self.directory_memory["last_browsed_dir"] = str(Path(path).parent)
            self.save_directory_memory()
            try:
                image = tk.PhotoImage(file=path)
                self.watermark_image_meta_var.set(f"图片尺寸：{image.width()} × {image.height()}")
            except Exception:
                self.watermark_image_meta_var.set("图片尺寸：读取失败")

    def get_initial_audio_browse_dir(self):
        current_path = self.replace_bgm_audio_path.get().strip()
        if current_path and Path(current_path).exists():
            return str(Path(current_path).parent)
        remembered = self.directory_memory.get("replace_bgm_audio_dir")
        if remembered and Path(remembered).is_dir():
            return remembered
        last_browsed = self.directory_memory.get("last_browsed_dir")
        if last_browsed and Path(last_browsed).is_dir():
            return last_browsed
        return str(Path(__file__).resolve().parent)

    def handle_replace_bgm_audio_change(self):
        current_path = self.replace_bgm_audio_path.get().strip()
        if not current_path:
            return
        path_obj = Path(current_path)
        if not path_obj.exists():
            return
        self.directory_memory["replace_bgm_audio_path"] = str(path_obj)
        self.directory_memory["replace_bgm_audio_dir"] = str(path_obj.parent)
        self.directory_memory["last_browsed_dir"] = str(path_obj.parent)
        self.save_directory_memory()

    def pick_replace_bgm_audio(self):
        initial_dir = self.get_initial_audio_browse_dir()
        path = filedialog.askopenfilename(
            initialdir=initial_dir,
            title="选择 BGM 音频文件",
            filetypes=SUPPORTED_AUDIO_FILETYPES,
        )
        if path:
            self.replace_bgm_audio_path.set(path)

    def get_watermark_text_content(self):
        return self.watermark_text_widget.get("1.0", "end").rstrip("\n")

    def render_watermark_preview(self):
        canvas = self.watermark_preview_canvas
        canvas.delete("all")
        canvas_width = int(canvas.cget("width"))
        canvas_height = int(canvas.cget("height"))
        try:
            preview_width = max(1, int(float(self.watermark_preview_width.get())))
            preview_height = max(1, int(float(self.watermark_preview_height.get())))
        except Exception:
            messagebox.showerror("错误", "预览宽高必须是有效数字")
            return

        scale = min((canvas_width - 20) / preview_width, (canvas_height - 20) / preview_height)
        scale = max(scale, 0.1)
        video_width = preview_width * scale
        video_height = preview_height * scale
        left = (canvas_width - video_width) / 2
        top = (canvas_height - video_height) / 2
        right = left + video_width
        bottom = top + video_height

        canvas.create_rectangle(left, top, right, bottom, outline="#3b82f6", width=2, fill="#f8fafc")
        canvas.create_text(left + 8, top + 8, text=f"{preview_width}×{preview_height}", anchor="nw", fill="#64748b", font=("Helvetica", 9))

        if self.watermark_mode.get() == WATERMARK_MODE_IMAGE:
            image_path = self.watermark_image_path.get().strip()
            if not image_path or not Path(image_path).exists():
                canvas.create_text(canvas_width / 2, canvas_height / 2, text="请先选择 PNG 水印", fill="#999999", font=("Helvetica", 11))
                return
            try:
                image = tk.PhotoImage(file=image_path)
                image_width = image.width()
                image_height = image.height()
            except Exception:
                canvas.create_text(canvas_width / 2, canvas_height / 2, text="PNG 读取失败", fill="#999999", font=("Helvetica", 11))
                return
            try:
                scale_percent = max(1.0, float(self.watermark_image_size_percent.get()))
                x_pos = float(self.watermark_image_x.get())
                y_pos = float(self.watermark_image_y.get())
            except Exception:
                messagebox.showerror("错误", "图片水印的位置和大小必须是有效数字")
                return
            display_width = preview_width * (scale_percent / 100.0) * scale
            display_height = display_width * (image_height / max(1, image_width))
            wm_left = left + x_pos * scale
            wm_top = top + y_pos * scale
            wm_right = wm_left + display_width
            wm_bottom = wm_top + display_height
            canvas.create_rectangle(wm_left, wm_top, wm_right, wm_bottom, outline="#ef4444", width=2, fill="#fecaca")
            canvas.create_text((wm_left + wm_right) / 2, (wm_top + wm_bottom) / 2, text="PNG", fill="#7f1d1d", font=("Helvetica", 10, "bold"))
        else:
            text_content = self.get_watermark_text_content()
            if not text_content.strip():
                canvas.create_text(canvas_width / 2, canvas_height / 2, text="请输入文字水印内容", fill="#999999", font=("Helvetica", 11))
                return
            try:
                font_size = max(1, int(float(self.watermark_text_size.get())))
                x_pos = float(self.watermark_text_x.get())
                y_pos = float(self.watermark_text_y.get())
            except Exception:
                messagebox.showerror("错误", "文字水印的位置和字号必须是有效数字")
                return
            preview_font_size = max(8, int(font_size * scale))
            preview_font = tkfont.Font(family="Helvetica", size=preview_font_size)
            wm_left = left + x_pos * scale
            wm_top = top + y_pos * scale
            canvas.create_text(
                wm_left,
                wm_top,
                text=text_content,
                anchor="nw",
                fill="#1d4ed8",
                font=preview_font,
            )

    def parse_watermark_intervals(self):
        parsed = []
        if not self.watermark_intervals_enabled.get():
            return parsed
        for row in self.watermark_interval_rows:
            start_value = row["start_var"].get().strip()
            end_value = row["end_var"].get().strip()
            if not start_value and not end_value:
                continue
            try:
                start_time = float(start_value)
                end_time = float(end_value)
            except Exception:
                raise ValueError("指定秒段中的开始秒和结束秒必须是数字")
            if start_time < 0 or end_time < 0 or end_time <= start_time:
                raise ValueError("指定秒段必须满足：开始秒 >= 0，结束秒 > 开始秒")
            parsed.append((start_time, end_time))
        if self.watermark_intervals_enabled.get() and not parsed:
            raise ValueError("请至少填写一个有效的指定秒段")
        return parsed

    def get_watermark_config(self):
        config = {
            "mode": self.watermark_mode.get(),
            "preview_size": (
                int(float(self.watermark_preview_width.get())),
                int(float(self.watermark_preview_height.get())),
            ),
            "fast_mode": self.watermark_fast_mode.get(),
            "explicit_enabled": self.watermark_intervals_enabled.get(),
            "explicit_ranges": self.parse_watermark_intervals(),
            "tail_enabled": self.watermark_tail_enabled.get(),
            "tail_seconds": float(self.watermark_tail_seconds.get() or 0),
        }
        if config["tail_enabled"] and config["tail_seconds"] <= 0:
            raise ValueError("结尾倒数秒数必须大于 0")

        if config["mode"] == WATERMARK_MODE_IMAGE:
            image_path = self.watermark_image_path.get().strip()
            if not image_path or not Path(image_path).exists():
                raise ValueError("请先选择 PNG 水印图片")
            config.update(
                {
                    "image_path": image_path,
                    "scale_percent": float(self.watermark_image_size_percent.get()),
                    "opacity_percent": float(self.watermark_image_opacity.get()),
                    "x_pos": float(self.watermark_image_x.get()),
                    "y_pos": float(self.watermark_image_y.get()),
                }
            )
        else:
            text_content = self.get_watermark_text_content()
            if not text_content.strip():
                raise ValueError("请输入文字水印内容")
            config.update(
                {
                    "text_content": text_content,
                    "font_size": int(float(self.watermark_text_size.get())),
                    "opacity_percent": float(self.watermark_text_opacity.get()),
                    "x_pos": float(self.watermark_text_x.get()),
                    "y_pos": float(self.watermark_text_y.get()),
                }
            )

        if not (0 < config["opacity_percent"] <= 100):
            raise ValueError("透明度必须在 1 到 100 之间")
        if config["mode"] == WATERMARK_MODE_IMAGE and config["scale_percent"] <= 0:
            raise ValueError("图片水印大小百分比必须大于 0")
        return config

    def get_replace_bgm_config(self):
        audio_path = self.replace_bgm_audio_path.get().strip()
        if not audio_path or not Path(audio_path).exists():
            raise ValueError("请先选择有效的 BGM 音频文件")
        return {
            "audio_path": audio_path,
        }

    def load_directory_memory(self):
        if not self.settings_path.exists():
            return {}
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_directory_memory(self):
        try:
            self.settings_path.write_text(
                json.dumps(self.directory_memory, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def restore_directory_state(self):
        for field_name, target_var in (("output_dir", self.output_dir),):
            remembered = self.directory_memory.get(field_name)
            if remembered and Path(remembered).is_dir():
                target_var.set(remembered)
        remembered_audio = self.directory_memory.get("replace_bgm_audio_path")
        if remembered_audio and Path(remembered_audio).exists():
            self.replace_bgm_audio_path.set(remembered_audio)
        for field_name, source in self.source_fields.items():
            remembered = self.directory_memory.get(field_name)
            if remembered and Path(remembered).is_dir():
                source["dir_var"].set(remembered)
            remembered_files = self.directory_memory.get(self.get_source_files_key(field_name), [])
            if isinstance(remembered_files, list):
                source["files"] = self.normalize_source_file_paths(remembered_files)
            remembered_mode = self.directory_memory.get(self.get_source_mode_key(field_name), SOURCE_MODE_DIRECTORY)
            if remembered_mode in {SOURCE_MODE_DIRECTORY, SOURCE_MODE_FILES}:
                source["mode_var"].set(remembered_mode)
            self.refresh_source_widgets(field_name)
        self.update_counts()

    def remember_directory(self, field_name, directory):
        if not directory:
            return
        directory_path = Path(directory)
        if not directory_path.is_dir():
            return
        directory_str = str(directory_path)
        self.directory_memory[field_name] = directory_str
        self.directory_memory["last_browsed_dir"] = directory_str
        self.save_directory_memory()

    def handle_directory_var_change(self, field_name, target_var):
        current_value = target_var.get().strip()
        if current_value:
            self.remember_directory(field_name, current_value)

    def get_initial_browse_dir(self, field_name, target_var):
        current_value = target_var.get().strip()
        if current_value and Path(current_value).is_dir():
            return current_value
        remembered = self.directory_memory.get(field_name)
        if remembered and Path(remembered).is_dir():
            return remembered
        last_browsed = self.directory_memory.get("last_browsed_dir")
        if last_browsed and Path(last_browsed).is_dir():
            return last_browsed
        return str(Path(__file__).resolve().parent)

    def browse_directory(self, field_name, target_var):
        initial_dir = self.get_initial_browse_dir(field_name, target_var)
        path = filedialog.askdirectory(initialdir=initial_dir)
        if path:
            target_var.set(path)
            self.remember_directory(field_name, path)
            self.update_counts()

    def pick_output(self):
        self.browse_directory("output_dir", self.output_dir)

    def open_output(self):
        out_dir = self.output_dir.get()
        if not out_dir:
            messagebox.showinfo("提示", "请先选择输出目录")
            return
        out_dir = str(Path(out_dir))
        try:
            if os.name == "nt":
                os.startfile(out_dir)
            elif sys.platform == "darwin":
                subprocess.run(["open", out_dir], check=False)
            else:
                subprocess.run(["xdg-open", out_dir], check=False)
        except Exception:
            try:
                if os.name == "nt":
                    subprocess.run(["explorer", out_dir], check=False)
                elif sys.platform == "darwin":
                    subprocess.run(["open", out_dir], check=False)
                else:
                    subprocess.run(["xdg-open", out_dir], check=False)
            except Exception as e:
                messagebox.showerror("错误", f"无法打开输出目录：{e}")

    def log(self, text):
        self.queue.put(("log", text))

    def update_progress(self):
        self.queue.put(("progress", self.completed_tasks, self.total_tasks))

    def count_random_outputs(self, video_count, pick_count, order_mode):
        if video_count < pick_count:
            return 0
        if order_mode == RANDOM_ORDER_IGNORE:
            return math.comb(video_count, pick_count)
        return math.perm(video_count, pick_count)

    def build_output_name(self, video_paths):
        return "_".join(video_path.stem for video_path in video_paths) + "_merged.mp4"

    def start_merge(self):
        if self.worker and self.worker.is_alive():
            return

        strategy = self.tab_mode.get()
        output_dir = self.output_dir.get()
        if not output_dir:
            messagebox.showerror("错误", "请先选择输出目录")
            return

        config = {
            "strategy": strategy,
            "output_dir": Path(output_dir),
            "mode": "auto" if self.mode.get() == "优先无损（失败自动转兼容）" else "compat",
            "resolution_mode": self.resolution_mode.get(),
            "custom_resolution": (self.custom_width.get(), self.custom_height.get()),
            "skip_existing": self.skip_existing.get(),
            "transition_enabled": self.transition_enabled.get(),
            "transition_profile": self.transition_profile.get(),
            "light_transition_seconds": self.light_transition_seconds.get(),
        }

        if strategy == "structured":
            if not self.source_has_selection("front_dir") or not self.source_has_selection("back_dir"):
                messagebox.showerror("错误", "请先选择前半段、后半段和输出目录")
                return
            fronts = self.get_source_videos("front_dir")
            middles = self.get_source_videos("middle_dir")
            backs = self.get_source_videos("back_dir")
            if not fronts or not backs:
                messagebox.showerror("错误", "未找到可用的视频文件")
                return
            config.update({
                "fronts": fronts,
                "middles": middles,
                "backs": backs,
            })
            self.total_tasks = len(fronts) * len(backs) if not middles else len(fronts) * len(middles) * len(backs)
        elif strategy == "random":
            if not self.source_has_selection("random_dir"):
                messagebox.showerror("错误", "请先选择候选素材")
                return
            random_videos = self.get_source_videos("random_dir")
            pick_count = int(self.random_pick_count.get() or 2)
            if len(random_videos) < pick_count:
                messagebox.showerror("错误", f"候选素材数量不足，至少需要 {pick_count} 个视频")
                return
            config.update({
                "random_videos": random_videos,
                "pick_count": pick_count,
                "random_order_mode": self.random_order_mode.get(),
            })
            self.total_tasks = self.count_random_outputs(len(random_videos), pick_count, self.random_order_mode.get())
        elif strategy == "watermark":
            if not self.source_has_selection("watermark_dir"):
                messagebox.showerror("错误", "请先选择待处理视频")
                return
            watermark_videos = self.get_source_videos("watermark_dir")
            if not watermark_videos:
                messagebox.showerror("错误", "未找到可用的视频文件")
                return
            try:
                watermark_config = self.get_watermark_config()
            except ValueError as error:
                messagebox.showerror("错误", str(error))
                return
            config.update({
                "watermark_videos": watermark_videos,
                "watermark_config": watermark_config,
            })
            self.total_tasks = len(watermark_videos)
        else:
            if not self.source_has_selection("replace_bgm_dir"):
                messagebox.showerror("错误", "请先选择待处理视频")
                return
            replace_bgm_videos = self.get_source_videos("replace_bgm_dir")
            if not replace_bgm_videos:
                messagebox.showerror("错误", "未找到可用的视频文件")
                return
            try:
                replace_bgm_config = self.get_replace_bgm_config()
            except ValueError as error:
                messagebox.showerror("错误", str(error))
                return
            config.update({
                "replace_bgm_videos": replace_bgm_videos,
                "replace_bgm_config": replace_bgm_config,
            })
            self.total_tasks = len(replace_bgm_videos)

        ffmpeg = find_binary("ffmpeg")
        ffprobe = find_binary("ffprobe")
        if not ffmpeg or not ffprobe:
            messagebox.showerror("错误", "未找到 FFmpeg/FFprobe，请将可执行文件放在程序目录或 ffmpeg 文件夹中")
            return
        if strategy == "replace_bgm":
            if not probe_has_audio(ffprobe, Path(config["replace_bgm_config"]["audio_path"])):
                messagebox.showerror("错误", "所选 BGM 文件中未检测到可用音频流")
                return

        self.stop_event.clear()
        self.completed_tasks = 0
        self.progress_canvas.update_idletasks()
        self.progress_canvas.coords(self.progress_bar_rect, 0, 0, 0, 16)
        self.progress_label.configure(text=f"0 / {self.total_tasks}")
        self.eta_label.configure(text="剩余时间估算：--:--")

        try:
            max_workers = int(self.max_workers.get() or 1)
            max_workers = max(1, max_workers)
        except Exception:
            max_workers = 1

        for widget in self.log_container.winfo_children():
            widget.destroy()
        self.log_panels = []

        cols = math.ceil(math.sqrt(max_workers))
        rows = math.ceil(max_workers / cols)

        for row in range(rows):
            self.log_container.grid_rowconfigure(row, weight=1)
        for col in range(cols):
            self.log_container.grid_columnconfigure(col, weight=1)

        panel_height = 180
        for index in range(max_workers):
            row = index // cols
            col = index % cols
            frame = tk.Frame(self.log_container, bg="white", highlightthickness=1, highlightbackground="#d1d5db", height=panel_height)
            frame.grid(row=row, column=col, sticky="nsew", padx=2, pady=2)
            frame.grid_propagate(False)
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(1, weight=1)

            label = tk.Label(frame, text=f"线程 #{index+1}: 等待任务...", font=("Helvetica", 9, "bold"), bg="#f0f0f0", anchor="w", padx=5)
            label.grid(row=0, column=0, sticky="ew")

            text = ScrolledText(frame, height=7, font=("Menlo", 10), state="disabled", bg="white", fg="#333333", bd=0)
            text.grid(row=1, column=0, sticky="nsew")

            self.log_panels.append({"frame": frame, "label": label, "text": text})

        if self.log_panels:
            self.log_text = self.log_panels[0]["text"]

        self.log("开始合并任务")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(text="停止任务")
        self.stop_button.configure(state="normal")

        config.update({
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
            "max_workers": max_workers,
        })
        self.worker = threading.Thread(target=self.run_merge, args=(config,), daemon=True)
        self.worker.start()

    def stop_merge(self):
        if self.worker and self.worker.is_alive():
            if not self.stop_event.is_set():
                self.stop_event.set()
                self.log("正在停止任务...")
            else:
                self.log("仍在尝试停止任务...")
            self.stop_button.configure(text="正在停止...")
            self.cancel_current()
            self.stop_button.configure(state="normal")

    def run_merge(self, config):
        strategy = config["strategy"]
        output_dir = config["output_dir"]
        mode = config["mode"]
        resolution_mode = config["resolution_mode"]
        custom_resolution = config["custom_resolution"]
        skip_existing = config["skip_existing"]
        transition_enabled = config.get("transition_enabled", False)
        transition_profile = config.get("transition_profile", "极速")
        light_transition_seconds = config.get("light_transition_seconds", DEFAULT_LIGHT_TRANSITION_SECONDS)
        ffmpeg = config["ffmpeg"]
        ffprobe = config["ffprobe"]
        max_workers = config.get("max_workers", 1)

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log("创建输出目录失败")
            self.log(str(e))
            self.finish()
            return

        self.thread_slots = queue.Queue()
        for index in range(max_workers):
            self.thread_slots.put(index)

        transition_cache_dir = tempfile.mkdtemp(prefix="video_mix_transition_cache_")
        transition_cache = {}
        transition_inflight = {}
        transition_cache_lock = threading.Lock()

        combinations = []
        if strategy == "structured":
            fronts = config["fronts"]
            middles = config.get("middles", [])
            backs = config["backs"]
            if middles:
                for front in fronts:
                    for middle in middles:
                        for back in backs:
                            video_paths = (front, middle, back)
                            output_name = self.build_output_name(video_paths)
                            output_path = output_dir / output_name
                            if skip_existing and output_path.exists():
                                self.log(f"跳过已存在：{output_name}")
                                self.completed_tasks += 1
                                self.update_progress()
                            else:
                                combinations.append((video_paths, output_name, output_path))
            else:
                for front in fronts:
                    for back in backs:
                        video_paths = (front, back)
                        output_name = self.build_output_name(video_paths)
                        output_path = output_dir / output_name
                        if skip_existing and output_path.exists():
                            self.log(f"跳过已存在：{output_name}")
                            self.completed_tasks += 1
                            self.update_progress()
                        else:
                            combinations.append((video_paths, output_name, output_path))
        elif strategy == "random":
            random_videos = config["random_videos"]
            pick_count = config["pick_count"]
            random_order_mode = config.get("random_order_mode", RANDOM_ORDER_DISTINCT)
            random_iterator = itertools.combinations(random_videos, pick_count) if random_order_mode == RANDOM_ORDER_IGNORE else itertools.permutations(random_videos, pick_count)
            for video_paths in random_iterator:
                output_name = self.build_output_name(video_paths)
                output_path = output_dir / output_name
                if skip_existing and output_path.exists():
                    self.log(f"跳过已存在：{output_name}")
                    self.completed_tasks += 1
                    self.update_progress()
                else:
                    combinations.append((video_paths, output_name, output_path))
        elif strategy == "watermark":
            for input_video in config["watermark_videos"]:
                output_name = f"{input_video.stem}_watermark.mp4"
                output_path = output_dir / output_name
                if skip_existing and output_path.exists():
                    self.log(f"跳过已存在：{output_name}")
                    self.completed_tasks += 1
                    self.update_progress()
                else:
                    combinations.append(((input_video,), output_name, output_path))
        else:
            for input_video in config["replace_bgm_videos"]:
                output_name = f"{input_video.stem}_replace_bgm{input_video.suffix.lower()}"
                output_path = output_dir / output_name
                if skip_existing and output_path.exists():
                    self.log(f"跳过已存在：{output_name}")
                    self.completed_tasks += 1
                    self.update_progress()
                else:
                    combinations.append(((input_video,), output_name, output_path))

        if not combinations:
            self.finish()
            return

        def merge_combination(video_paths, output_name, output_path):
            if self.stop_event.is_set():
                return False, "Cancelled"

            slot_idx = self.thread_slots.get()
            try:
                def safe_remove(path_obj):
                    if not path_obj:
                        return
                    try:
                        if Path(path_obj).exists():
                            os.remove(path_obj)
                    except Exception:
                        pass

                self.queue.put(("title_slot", slot_idx, f"正在处理: {output_name}"))
                self.queue.put(("log_slot", slot_idx, f"开始: {output_name}"))

                on_proc = lambda proc: self.register_proc(proc, output_path)
                video_list = list(video_paths)
                clip_durations = [probe_duration(ffprobe, video_path) or 0.0 for video_path in video_list]

                if strategy == "watermark":
                    watermark_config = config["watermark_config"]
                    input_video = video_list[0]
                    duration = clip_durations[0]
                    intervals = build_watermark_intervals(
                        duration,
                        watermark_config["explicit_enabled"],
                        watermark_config["explicit_ranges"],
                        watermark_config["tail_enabled"],
                        watermark_config["tail_seconds"],
                    )
                    self.queue.put(("log_slot", slot_idx, f"水印模式：{watermark_config['mode']}"))
                    if watermark_config.get("fast_mode"):
                        self.queue.put(("log_slot", slot_idx, "编码策略：极速模式"))
                    if intervals:
                        self.queue.put(("log_slot", slot_idx, f"命中时间段数：{len(intervals)}"))

                    def on_progress_task(remaining):
                        minutes = int(remaining // 60)
                        seconds = int(remaining % 60)
                        self.queue.put(("title_slot", slot_idx, f"{output_name} (ETA: {minutes:02d}:{seconds:02d})"))
                        self.queue.put(("eta", remaining))

                    if watermark_config["mode"] == WATERMARK_MODE_IMAGE:
                        ok, logtxt = apply_image_watermark(
                            ffmpeg,
                            ffprobe,
                            input_video,
                            output_path,
                            watermark_config["image_path"],
                            watermark_config["scale_percent"],
                            watermark_config["opacity_percent"],
                            watermark_config["x_pos"],
                            watermark_config["y_pos"],
                            intervals,
                            fast_mode=watermark_config.get("fast_mode", False),
                            progress_total=duration,
                            on_progress=on_progress_task,
                            on_proc=on_proc,
                            stop_event=self.stop_event,
                        )
                    else:
                        ok, logtxt = apply_text_watermark(
                            ffmpeg,
                            ffprobe,
                            input_video,
                            output_path,
                            watermark_config["text_content"],
                            watermark_config["font_size"],
                            watermark_config["opacity_percent"],
                            watermark_config["x_pos"],
                            watermark_config["y_pos"],
                            intervals,
                            fast_mode=watermark_config.get("fast_mode", False),
                            progress_total=duration,
                            on_progress=on_progress_task,
                            on_proc=on_proc,
                            stop_event=self.stop_event,
                        )

                    if ok:
                        self.queue.put(("log_slot", slot_idx, "加水印成功"))
                        return True, "watermark"
                    safe_remove(output_path)
                    self.queue.put(("log_slot", slot_idx, f"失败: {logtxt}"))
                    return False, logtxt

                if strategy == "replace_bgm":
                    replace_bgm_config = config["replace_bgm_config"]
                    input_video = video_list[0]
                    duration = clip_durations[0]
                    bgm_audio = Path(replace_bgm_config["audio_path"])
                    bgm_duration = probe_duration(ffprobe, bgm_audio) or 0.0
                    self.queue.put(("log_slot", slot_idx, "处理模式：完全替换原音频"))
                    self.queue.put(("log_slot", slot_idx, "视频策略：视频流 copy，仅编码新的音频流"))
                    if bgm_duration > 0:
                        if duration > bgm_duration:
                            self.queue.put(("log_slot", slot_idx, f"BGM 时长 {bgm_duration:.2f}s，小于视频时长 {duration:.2f}s，处理时将自动循环"))
                        elif duration < bgm_duration:
                            self.queue.put(("log_slot", slot_idx, f"BGM 时长 {bgm_duration:.2f}s，大于视频时长 {duration:.2f}s，处理时将自动截断"))

                    def on_progress_task(remaining):
                        minutes = int(remaining // 60)
                        seconds = int(remaining % 60)
                        self.queue.put(("title_slot", slot_idx, f"{output_name} (ETA: {minutes:02d}:{seconds:02d})"))
                        self.queue.put(("eta", remaining))

                    ok, logtxt = replace_video_bgm(
                        ffmpeg,
                        ffprobe,
                        input_video,
                        output_path,
                        bgm_audio,
                        progress_total=duration,
                        on_progress=on_progress_task,
                        on_proc=on_proc,
                        stop_event=self.stop_event,
                    )
                    if ok:
                        self.queue.put(("log_slot", slot_idx, "替换 BGM 成功"))
                        return True, "replace_bgm"
                    safe_remove(output_path)
                    self.queue.put(("log_slot", slot_idx, f"失败: {logtxt}"))
                    return False, logtxt

                if resolution_mode == "custom":
                    target_resolution = custom_resolution
                else:
                    target_resolution = probe_resolution(ffprobe, video_list[0])
                    if not target_resolution:
                        return False, f"无法获取分辨率: {video_list[0].name}"

                total_duration = sum(clip_durations)

                if transition_enabled and len(video_list) > 1:
                    profile = get_transition_profile(transition_profile, light_transition_seconds)
                    self.queue.put(("log_slot", slot_idx, f"启用转场：轻量转场 {profile['total_seconds']:.1f}秒"))
                    ok, logtxt, prepared_paths = prepare_transition_assets(
                        ffmpeg,
                        ffprobe,
                        video_list,
                        target_resolution,
                        transition_profile,
                        light_transition_seconds,
                        transition_cache_dir,
                        transition_cache,
                        transition_inflight,
                        transition_cache_lock,
                    )
                    if not ok:
                        safe_remove(output_path)
                        self.queue.put(("log_slot", slot_idx, f"转场素材生成失败: {logtxt}"))
                        return False, logtxt
                    total_duration = max(0.0, total_duration + profile["gap_duration"] * (len(video_list) - 1))
                    ok, logtxt = concat_copy_many(ffmpeg, prepared_paths, output_path, stop_event=self.stop_event, on_proc=on_proc)
                    if ok:
                        self.queue.put(("log_slot", slot_idx, "转场合并成功"))
                        return True, "transition"
                    safe_remove(output_path)
                    self.queue.put(("log_slot", slot_idx, f"失败: {logtxt}"))
                    return False, logtxt

                if mode == "auto" and is_copy_compatible_many(ffprobe, video_list):
                    ok, logtxt = concat_copy_many(ffmpeg, video_list, output_path, stop_event=self.stop_event, on_proc=on_proc)
                    if ok:
                        self.queue.put(("log_slot", slot_idx, "无损合并成功"))
                        return True, "copy"
                    self.queue.put(("log_slot", slot_idx, "无损失败，转兼容..."))

                def on_progress_task(remaining):
                    minutes = int(remaining // 60)
                    seconds = int(remaining % 60)
                    self.queue.put(("title_slot", slot_idx, f"{output_name} (ETA: {minutes:02d}:{seconds:02d})"))
                    self.queue.put(("eta", remaining))

                ok, logtxt, _audio_kept = concat_reencode_many(
                    ffmpeg,
                    ffprobe,
                    video_list,
                    output_path,
                    target_resolution,
                    progress_total=total_duration,
                    on_progress=on_progress_task,
                    on_proc=on_proc,
                    clip_durations=clip_durations,
                )

                if ok:
                    self.queue.put(("log_slot", slot_idx, "兼容合并成功"))
                else:
                    safe_remove(output_path)
                    self.queue.put(("log_slot", slot_idx, f"失败: {logtxt}"))
                return ok, "reencode" if ok else logtxt
            finally:
                self.queue.put(("title_slot", slot_idx, f"线程 #{slot_idx+1}: 空闲"))
                self.thread_slots.put(slot_idx)

        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {}
                for video_paths, output_name, output_path in combinations:
                    if self.stop_event.is_set():
                        break
                    future = executor.submit(merge_combination, video_paths, output_name, output_path)
                    future_map[future] = (output_name, output_path)
                for future in as_completed(future_map):
                    output_name, output_path = future_map[future]
                    try:
                        ok, mode_used = future.result()
                        if ok:
                            if mode_used == "copy":
                                note = "无损"
                            elif mode_used == "watermark":
                                note = "水印"
                            elif mode_used == "replace_bgm":
                                note = "替换BGM"
                            else:
                                note = "兼容"
                            self.log(f"{note}处理完成: {output_name}")
                        else:
                            self.log(f"处理失败: {output_name}")
                            self.log(str(mode_used))
                    except Exception as e:
                        self.log(f"处理异常: {output_name}")
                        self.log(str(e))
                    finally:
                        with self.proc_lock:
                            self.running_outputs.discard(str(output_path))
                    self.completed_tasks += 1
                    self.update_progress()
                    if self.stop_event.is_set():
                        break
        except Exception as e:
            self.log("并发执行异常")
            self.log(str(e))
        finally:
            try:
                shutil.rmtree(transition_cache_dir, ignore_errors=True)
            except Exception:
                pass
            self.finish()

    def finish(self):
        self.queue.put(("done", None))

    def process_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                action = item[0]
                if action == "log":
                    if self.log_text:
                        self.log_text.configure(state="normal")
                        self.log_text.insert("end", item[1] + "\n")
                        self.log_text.configure(state="disabled")
                        self.log_text.see("end")
                elif action == "log_slot":
                    slot_idx, text = item[1], item[2]
                    if 0 <= slot_idx < len(self.log_panels):
                        text_widget = self.log_panels[slot_idx]["text"]
                        text_widget.configure(state="normal")
                        text_widget.insert("end", text + "\n")
                        text_widget.configure(state="disabled")
                        text_widget.see("end")
                elif action == "title_slot":
                    slot_idx, text = item[1], item[2]
                    if 0 <= slot_idx < len(self.log_panels):
                        self.log_panels[slot_idx]["label"].configure(text=text)
                elif action == "progress":
                    current, total = item[1], item[2]
                    self.progress_label.configure(text=f"{current} / {total}")
                    self.progress_canvas.update_idletasks()
                    width = max(1, self.progress_canvas.winfo_width())
                    fill_width = int(width * (current / max(1, total)))
                    self.progress_canvas.coords(self.progress_bar_rect, 0, 0, fill_width, 16)
                elif action == "eta":
                    remaining = float(item[1])
                    minutes = int(remaining // 60)
                    seconds = int(remaining % 60)
                    self.eta_label.configure(text=f"剩余时间估算：{minutes:02d}:{seconds:02d}")
                elif action == "done":
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(text="停止任务")
                    self.stop_button.configure(state="disabled")
                    self.log("任务完成")
        except queue.Empty:
            pass
        self.root.after(100, self.process_queue)

    def register_proc(self, proc, output_path):
        with self.proc_lock:
            self.running_procs.add(proc)
            if output_path:
                self.running_outputs.add(str(output_path))

    def cancel_current(self):
        try:
            with self.proc_lock:
                procs = list(self.running_procs)
                outs = list(self.running_outputs)
                self.running_procs.clear()
                self.running_outputs.clear()
            for proc in procs:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.kill()
                except Exception:
                    pass
            for output in outs:
                try:
                    if Path(output).exists():
                        os.remove(output)
                        self.log(f"已删除未完成文件: {Path(output).name}")
                except Exception:
                    pass
        finally:
            self.current_output_path = None

    def update_counts(self):
        try:
            front_count = len(self.get_source_videos("front_dir"))
            middle_count = len(self.get_source_videos("middle_dir"))
            back_count = len(self.get_source_videos("back_dir"))
            random_count = len(self.get_source_videos("random_dir"))
            watermark_count = len(self.get_source_videos("watermark_dir"))
            replace_bgm_count = len(self.get_source_videos("replace_bgm_dir"))
            pick_count = int(self.random_pick_count.get() or 2)
            random_order_mode = self.random_order_mode.get()

            for field_name in self.source_fields:
                self.refresh_source_widgets(field_name)
            self.random_pick_info_var.set(f"当前拼接数量：{pick_count} 段")

            if self.tab_mode.get() == "structured":
                total = front_count * back_count if middle_count == 0 else front_count * middle_count * back_count
            elif self.tab_mode.get() == "random":
                total = self.count_random_outputs(random_count, pick_count, random_order_mode)
            elif self.tab_mode.get() == "watermark":
                total = watermark_count
            else:
                total = replace_bgm_count
            self.estimated_var.set(f"预计输出数量：{total}")
        except Exception:
            pass


def main():
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
