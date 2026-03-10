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
from tkinter import Tk, StringVar, BooleanVar, IntVar, filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
APP_VERSION = "2026-03-10-fast-transition"

TRANSITION_PROFILES = {
    "极速": {"fade_duration": 0.08, "gap_duration": 0.0},
}
LIGHT_TRANSITION_OPTIONS = ["0.4", "0.6", "0.8", "1.0"]
DEFAULT_LIGHT_TRANSITION_SECONDS = "0.6"


def list_videos(directory):
    base = Path(directory)
    if not base.exists():
        return []
    return sorted([p for p in base.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS])


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
    if use_hardware:
        return ["-c:v", "h264_videotoolbox", "-b:v", "5000k", "-allow_sw", "1"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]


def render_transition_variant(ffmpeg, ffprobe, source_path, output_path, target_resolution, fade_in, fade_out, fade_duration, include_audio):
    width, height = target_resolution
    source_duration = probe_duration(ffprobe, source_path) or 0.0
    actual_fade = resolve_transition_duration(fade_duration, [source_duration]) or 0.0
    video_filters = [build_transition_scale_filter(width, height)]
    if fade_in and actual_fade > 0:
        video_filters.append(f"fade=t=in:st=0:d={actual_fade:.3f}:color=black")
    if fade_out and actual_fade > 0:
        fade_start = max(0.0, source_duration - actual_fade)
        video_filters.append(f"fade=t=out:st={fade_start:.3f}:d={actual_fade:.3f}:color=black")

    cmd = [ffmpeg, "-y", "-i", str(source_path), "-vf", ",".join(video_filters)]
    use_hardware = sys.platform == "darwin"
    cmd += get_transition_variant_params(use_hardware)

    source_has_audio = probe_has_audio(ffprobe, source_path)
    if include_audio and source_has_audio:
        audio_filter = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
        if fade_in and actual_fade > 0:
            audio_filter += f",afade=t=in:st=0:d={actual_fade:.3f}"
        if fade_out and actual_fade > 0:
            fade_start = max(0.0, source_duration - actual_fade)
            audio_filter += f",afade=t=out:st={fade_start:.3f}:d={actual_fade:.3f}"
        cmd += ["-af", audio_filter, "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]

    cmd += ["-movflags", "+faststart", str(output_path)]
    code, out, err = run_command(cmd)
    if code == 0:
        return True, out + err
    if use_hardware:
        fallback_cmd = [ffmpeg, "-y", "-i", str(source_path), "-vf", ",".join(video_filters)] + get_transition_variant_params(False)
        if include_audio and source_has_audio:
            audio_filter = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
            if fade_in and actual_fade > 0:
                audio_filter += f",afade=t=in:st=0:d={actual_fade:.3f}"
            if fade_out and actual_fade > 0:
                fade_start = max(0.0, source_duration - actual_fade)
                audio_filter += f",afade=t=out:st={fade_start:.3f}:d={actual_fade:.3f}"
            fallback_cmd += ["-af", audio_filter, "-c:a", "aac", "-b:a", "128k"]
        else:
            fallback_cmd += ["-an"]
        fallback_cmd += ["-movflags", "+faststart", str(output_path)]
        code, out2, err2 = run_command(fallback_cmd)
        return code == 0, out + err + out2 + err2
    return False, out + err


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
            cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
        else:
            cmd += ["-an"]
        cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output_path)]
        return cmd

    use_hardware = sys.platform == "darwin"
    cmd = build_gap_cmd(use_hardware)
    code, out, err = run_command(cmd)
    if code == 0:
        return True, out + err
    if use_hardware:
        fallback_cmd = build_gap_cmd(False)
        code, out2, err2 = run_command(fallback_cmd)
        return code == 0, out + err + out2 + err2
    return False, out + err


def prepare_transition_assets(ffmpeg, ffprobe, video_paths, target_resolution, transition_profile, light_transition_seconds, cache_dir, cache, cache_lock):
    include_audio = all(probe_has_audio(ffprobe, video_path) for video_path in video_paths)
    profile = get_transition_profile(transition_profile, light_transition_seconds)
    fade_duration = profile["fade_duration"]
    gap_duration = profile["gap_duration"]
    prepared_paths = []

    def get_or_create_variant(source_path, fade_in, fade_out):
        key = (str(source_path), target_resolution, transition_profile, light_transition_seconds, include_audio, fade_in, fade_out)
        with cache_lock:
            cached_path = cache.get(key)
            if cached_path and Path(cached_path).exists():
                return Path(cached_path), True, ""
            output_path = Path(cache_dir) / f"variant_{abs(hash(key))}.mp4"
            ok, logtxt = render_transition_variant(
                ffmpeg,
                ffprobe,
                source_path,
                output_path,
                target_resolution,
                fade_in,
                fade_out,
                fade_duration,
                include_audio,
            )
            if ok:
                cache[key] = str(output_path)
            return output_path, ok, logtxt

    def get_or_create_gap():
        key = ("gap", target_resolution, transition_profile, light_transition_seconds, include_audio)
        with cache_lock:
            cached_path = cache.get(key)
            if cached_path and Path(cached_path).exists():
                return Path(cached_path), True, ""
            output_path = Path(cache_dir) / f"gap_{abs(hash(key))}.mp4"
            ok, logtxt = render_transition_gap(ffmpeg, output_path, target_resolution, gap_duration, include_audio)
            if ok:
                cache[key] = str(output_path)
            return output_path, ok, logtxt

    for index, video_path in enumerate(video_paths):
        fade_in = index > 0
        fade_out = index < len(video_paths) - 1
        prepared_path, ok, logtxt = get_or_create_variant(video_path, fade_in, fade_out)
        if not ok:
            return False, logtxt, []
        prepared_paths.append(prepared_path)
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
        self.output_dir = StringVar()
        self.random_pick_count = IntVar(value=2)
        self.transition_enabled = BooleanVar(value=False)
        self.transition_profile = StringVar(value="极速")
        self.light_transition_seconds = StringVar(value=DEFAULT_LIGHT_TRANSITION_SECONDS)
        self.mode = StringVar(value="优先无损（失败自动转兼容）")
        self.resolution_mode = StringVar(value="custom")
        self.custom_width = IntVar(value=1080)
        self.custom_height = IntVar(value=1920)
        self.skip_existing = BooleanVar(value=True)
        self.max_workers = IntVar(value=max(2, min(4, os.cpu_count() or 2)))
        self.proc_lock = threading.Lock()
        self.running_procs = set()
        self.running_outputs = set()
        self.last_eta_emit = {}
        self.log_panels = []
        self.thread_slots = None

        self.build_ui()
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

        structured_tab = tk.Frame(self.path_tabs, bg=bg_color, padx=10, pady=10)
        structured_tab.grid_columnconfigure(0, weight=1)
        self.path_tabs.add(structured_tab, text="前中后拼接")

        random_tab = tk.Frame(self.path_tabs, bg=bg_color, padx=10, pady=10)
        random_tab.grid_columnconfigure(0, weight=1)
        self.path_tabs.add(random_tab, text="随机排列组合")

        front_row = self.build_path_row(structured_tab, "前半段目录", self.front_dir, self.pick_front)
        middle_row = self.build_path_row(structured_tab, "中段目录 (可选)", self.middle_dir, self.pick_middle)
        back_row = self.build_path_row(structured_tab, "后半段目录", self.back_dir, self.pick_back)
        self.front_count_var = StringVar(value="共 0 个视频")
        self.middle_count_var = StringVar(value="共 0 个视频")
        self.back_count_var = StringVar(value="共 0 个视频")
        tk.Label(front_row, textvariable=self.front_count_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))
        tk.Label(middle_row, textvariable=self.middle_count_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))
        tk.Label(back_row, textvariable=self.back_count_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))

        random_dir_row = self.build_path_row(random_tab, "文件目录", self.random_dir, self.pick_random_dir)
        self.random_dir_count_var = StringVar(value="共 0 个视频")
        tk.Label(random_dir_row, textvariable=self.random_dir_count_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))

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

        shared_path_content = tk.Frame(path_section, bg=bg_color)
        shared_path_content.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        shared_path_content.grid_columnconfigure(0, weight=1)
        out_row = self.build_path_row(shared_path_content, "输出目录", self.output_dir, self.pick_output)
        self.estimated_var = StringVar(value="预计输出数量：0")
        tk.Label(out_row, textvariable=self.estimated_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))

        self.front_dir.trace_add("write", lambda *args: self.update_counts())
        self.middle_dir.trace_add("write", lambda *args: self.update_counts())
        self.back_dir.trace_add("write", lambda *args: self.update_counts())
        self.random_dir.trace_add("write", lambda *args: self.update_counts())
        self.random_pick_count.trace_add("write", lambda *args: self.update_counts())
        self.update_counts()

        options_section = tk.Frame(container, bg=bg_color)
        options_section.grid(row=2, column=0, sticky="ew", pady=(0, 15))
        options_section.grid_columnconfigure(0, weight=1)
        tk.Label(options_section, text="合并选项", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=0, sticky="w", pady=(0, 8))
        options_frame = tk.Frame(options_section, bg=bg_color)
        options_frame.grid(row=1, column=0, sticky="ew")
        options_frame.grid_columnconfigure(0, weight=1)

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
        tk.Label(row5, text="转场模式", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=1, sticky="w", padx=(10, 0))
        fast_radio = tk.Radiobutton(
            row5,
            text="极速转场",
            variable=self.transition_profile,
            value="极速",
            command=self.refresh_transition_state,
            font=font_normal,
            bg=bg_color,
            fg=fg_color,
            activebackground=bg_color,
            activeforeground=fg_color,
        )
        fast_radio.grid(row=0, column=2, sticky="w", padx=(10, 0))
        light_radio = tk.Radiobutton(
            row5,
            text="轻量转场",
            variable=self.transition_profile,
            value="轻量",
            command=self.refresh_transition_state,
            font=font_normal,
            bg=bg_color,
            fg=fg_color,
            activebackground=bg_color,
            activeforeground=fg_color,
        )
        light_radio.grid(row=0, column=3, sticky="w", padx=(12, 0))

        row6 = tk.Frame(options_frame, bg=bg_color)
        row6.grid(row=6, column=0, sticky="ew", pady=4)
        tk.Label(row6, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        tk.Label(row6, text="轻量时长", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=1, sticky="w", padx=(10, 0))
        light_duration_menu = tk.OptionMenu(row6, self.light_transition_seconds, *LIGHT_TRANSITION_OPTIONS)
        light_duration_menu.config(width=8, font=font_normal, bg="white", fg=fg_color, highlightthickness=0)
        light_duration_menu["menu"].config(bg="white", fg=fg_color, font=font_normal)
        light_duration_menu.grid(row=0, column=2, sticky="w", padx=(10, 0))
        tk.Label(row6, text="秒", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=3, sticky="w", padx=(6, 0))

        row7 = tk.Frame(options_frame, bg=bg_color)
        row7.grid(row=7, column=0, sticky="ew", pady=(0, 4))
        tk.Label(row7, text="", font=font_normal, bg=bg_color, width=10).grid(row=0, column=0, sticky="w")
        self.transition_hint_label = tk.Label(
            row7,
            text="提示：极速转场速度最快；轻量转场支持更长时长，效果更明显但会更慢。",
            font=font_small,
            bg=bg_color,
            fg="#666666",
        )
        self.transition_hint_label.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.light_transition_row = row6
        self.light_duration_menu = light_duration_menu
        self.transition_profile_radios = [fast_radio, light_radio]
        self.transition_enabled.trace_add("write", lambda *args: self.refresh_transition_state())
        self.transition_profile.trace_add("write", lambda *args: self.refresh_transition_state())
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
        self.start_button = tk.Button(buttons_frame, text="开始合并", command=self.start_merge, font=btn_font, bg="#3b82f6", fg="black", activebackground="#2563eb", activeforeground="black", highlightbackground=bg_color)
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

        if os.environ.get("VIDEO_MIX_DEBUG_UI") == "1":
            self.root.after(800, self.dump_ui_state)

    def on_scroll_frame_configure(self, event):
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def on_scroll_canvas_configure(self, event):
        self.scroll_canvas.itemconfig(self.scroll_window, width=event.width)

    def on_mousewheel(self, event):
        if os.name == "nt":
            delta = -1 * int(event.delta / 120)
        elif sys.platform == "darwin":
            delta = -1 * int(event.delta)
        else:
            delta = -1 * int(event.delta / 120)
        self.scroll_canvas.yview_scroll(delta, "units")

    def on_mousewheel_linux(self, event):
        if event.num == 4:
            self.scroll_canvas.yview_scroll(-1, "units")
        elif event.num == 5:
            self.scroll_canvas.yview_scroll(1, "units")

    def on_tab_changed(self, event=None):
        current_index = self.path_tabs.index(self.path_tabs.select())
        self.tab_mode.set("structured" if current_index == 0 else "random")
        self.update_counts()

    def build_path_row(self, parent, label, var, command):
        row = parent.grid_size()[1]
        row_frame = tk.Frame(parent, bg=self.ui_bg)
        row_frame.grid(row=row, column=0, sticky="ew", pady=4)
        row_frame.grid_columnconfigure(1, weight=1, minsize=360)

        tk.Label(row_frame, text=label, font=self.font_normal, bg=self.ui_bg, fg=self.ui_fg, width=12, anchor="w").grid(row=0, column=0, sticky="w")
        entry = tk.Entry(row_frame, textvariable=var, font=self.font_normal, bg="white", fg=self.ui_fg, highlightthickness=1, highlightbackground="#d1d5db")
        entry.grid(row=0, column=1, sticky="ew", padx=(10, 10))
        tk.Button(row_frame, text="选择", command=command, font=self.font_normal, highlightbackground=self.ui_bg).grid(row=0, column=2, sticky="e")
        return row_frame

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
        state = "normal" if enabled else "disabled"
        for radio in self.transition_profile_radios:
            radio.configure(state=state)
        show_light_options = enabled and self.transition_profile.get() == "轻量"
        if show_light_options:
            self.light_transition_row.grid()
            self.light_duration_menu.configure(state="normal")
        else:
            self.light_transition_row.grid_remove()
            self.light_duration_menu.configure(state="disabled")
        self.transition_hint_label.configure(fg="#666666" if enabled else "#b0b0b0")

    def pick_front(self):
        path = filedialog.askdirectory()
        if path:
            self.front_dir.set(path)
            self.update_counts()

    def pick_middle(self):
        path = filedialog.askdirectory()
        if path:
            self.middle_dir.set(path)
            self.update_counts()

    def pick_back(self):
        path = filedialog.askdirectory()
        if path:
            self.back_dir.set(path)
            self.update_counts()

    def pick_random_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.random_dir.set(path)
            self.update_counts()

    def pick_output(self):
        path = filedialog.askdirectory()
        if path:
            self.output_dir.set(path)

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

    def count_random_outputs(self, video_count, pick_count):
        if video_count < pick_count:
            return 0
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
            front_dir = self.front_dir.get()
            back_dir = self.back_dir.get()
            middle_dir = self.middle_dir.get()
            if not front_dir or not back_dir:
                messagebox.showerror("错误", "请先选择前半段、后半段和输出目录")
                return
            fronts = list_videos(front_dir)
            middles = list_videos(middle_dir) if middle_dir else []
            backs = list_videos(back_dir)
            if not fronts or not backs:
                messagebox.showerror("错误", "未找到可用的视频文件")
                return
            config.update({
                "fronts": fronts,
                "middles": middles,
                "backs": backs,
            })
            self.total_tasks = len(fronts) * len(backs) if not middles else len(fronts) * len(middles) * len(backs)
        else:
            random_dir = self.random_dir.get()
            if not random_dir:
                messagebox.showerror("错误", "请先选择文件目录")
                return
            random_videos = list_videos(random_dir)
            pick_count = int(self.random_pick_count.get() or 2)
            if len(random_videos) < pick_count:
                messagebox.showerror("错误", f"文件目录中的视频数量不足，至少需要 {pick_count} 个视频")
                return
            config.update({
                "random_videos": random_videos,
                "pick_count": pick_count,
            })
            self.total_tasks = self.count_random_outputs(len(random_videos), pick_count)

        ffmpeg = find_binary("ffmpeg")
        ffprobe = find_binary("ffprobe")
        if not ffmpeg or not ffprobe:
            messagebox.showerror("错误", "未找到 FFmpeg/FFprobe，请将可执行文件放在程序目录或 ffmpeg 文件夹中")
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
            self.stop_event.set()
            self.log("正在停止任务...")
            self.cancel_current()
        self.stop_button.configure(state="disabled")

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
        else:
            random_videos = config["random_videos"]
            pick_count = config["pick_count"]
            for video_paths in itertools.permutations(random_videos, pick_count):
                output_name = self.build_output_name(video_paths)
                output_path = output_dir / output_name
                if skip_existing and output_path.exists():
                    self.log(f"跳过已存在：{output_name}")
                    self.completed_tasks += 1
                    self.update_progress()
                else:
                    combinations.append((video_paths, output_name, output_path))

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

                if resolution_mode == "custom":
                    target_resolution = custom_resolution
                else:
                    target_resolution = probe_resolution(ffprobe, video_list[0])
                    if not target_resolution:
                        return False, f"无法获取分辨率: {video_list[0].name}"

                total_duration = sum(clip_durations)

                if transition_enabled and len(video_list) > 1:
                    profile = get_transition_profile(transition_profile, light_transition_seconds)
                    if transition_profile == "轻量":
                        self.queue.put(("log_slot", slot_idx, f"启用转场：轻量转场 {profile['total_seconds']:.1f}秒"))
                    else:
                        self.queue.put(("log_slot", slot_idx, "启用转场：极速转场"))
                    ok, logtxt, prepared_paths = prepare_transition_assets(
                        ffmpeg,
                        ffprobe,
                        video_list,
                        target_resolution,
                        transition_profile,
                        light_transition_seconds,
                        transition_cache_dir,
                        transition_cache,
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
                            note = "无损" if mode_used == "copy" else "兼容"
                            self.log(f"{note}合并完成: {output_name}")
                        else:
                            self.log(f"合并失败: {output_name}")
                            self.log(str(mode_used))
                    except Exception as e:
                        self.log(f"合并异常: {output_name}")
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
            front_count = len(list_videos(self.front_dir.get())) if self.front_dir.get() else 0
            middle_count = len(list_videos(self.middle_dir.get())) if self.middle_dir.get() else 0
            back_count = len(list_videos(self.back_dir.get())) if self.back_dir.get() else 0
            random_count = len(list_videos(self.random_dir.get())) if self.random_dir.get() else 0
            pick_count = int(self.random_pick_count.get() or 2)

            self.front_count_var.set(f"共 {front_count} 个视频")
            self.middle_count_var.set(f"共 {middle_count} 个视频")
            self.back_count_var.set(f"共 {back_count} 个视频")
            self.random_dir_count_var.set(f"共 {random_count} 个视频")
            self.random_pick_info_var.set(f"当前拼接数量：{pick_count} 段")

            if self.tab_mode.get() == "structured":
                total = front_count * back_count if middle_count == 0 else front_count * middle_count * back_count
            else:
                total = self.count_random_outputs(random_count, pick_count)
            self.estimated_var.set(f"预计输出数量：{total}")
        except Exception:
            pass


def main():
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
