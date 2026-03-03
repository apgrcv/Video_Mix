import json
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
from tkinter import Tk, StringVar, BooleanVar, IntVar, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
APP_VERSION = "2026-02-21-middle"


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
    front_sig = probe_stream_signature(ffprobe, front)
    back_sig = probe_stream_signature(ffprobe, back)
    if not front_sig or not back_sig:
        return False
    return front_sig == back_sig


def is_copy_compatible_for_three(ffprobe, first, middle, last):
    """检查三个视频是否可以无损拼接（编码格式、分辨率、帧率等完全一致）"""
    first_sig = probe_stream_signature(ffprobe, first)
    middle_sig = probe_stream_signature(ffprobe, middle)
    last_sig = probe_stream_signature(ffprobe, last)
    if not first_sig or not middle_sig or not last_sig:
        return False
    return first_sig == middle_sig == last_sig


def build_scale_filter(width, height):
    return f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"


def concat_copy(ffmpeg, front, back, output, stop_event=None, on_proc=None):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
    try:
        tmp.write(f"file '{str(front)}'\n")
        tmp.write(f"file '{str(back)}'\n")
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
        ts1 = tempfile.NamedTemporaryFile(delete=False, suffix=".ts")
        ts2 = tempfile.NamedTemporaryFile(delete=False, suffix=".ts")
        ts1.close()
        ts2.close()
        cmd1 = [
            ffmpeg,
            "-y",
            "-i",
            str(front),
            "-c",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
            "-f",
            "mpegts",
            ts1.name,
        ]
        cmd2 = [
            ffmpeg,
            "-y",
            "-i",
            str(back),
            "-c",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
            "-f",
            "mpegts",
            ts2.name,
        ]
        if stop_event and stop_event.is_set():
            return False, "Cancelled"
        code1, out1, err1 = run_command(cmd1)
        if stop_event and stop_event.is_set():
            return False, "Cancelled"
        code2, out2, err2 = run_command(cmd2)
        if code1 == 0 and code2 == 0:
            cmd3 = [
                ffmpeg,
                "-y",
                "-i",
                f"concat:{ts1.name}|{ts2.name}",
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-movflags",
                "+faststart",
                str(output),
            ]
            if stop_event and stop_event.is_set():
                return False, "Cancelled"
            code3, out3, err3 = run_command(cmd3)
            os.unlink(ts1.name)
            os.unlink(ts2.name)
            if code3 == 0:
                return True, out1 + err1 + out2 + err2 + out3 + err3
            return False, out1 + err1 + out2 + err2 + out3 + err3
        else:
            try:
                os.unlink(ts1.name)
            except OSError:
                pass
            try:
                os.unlink(ts2.name)
            except OSError:
                pass
            return False, out1 + err1 + out2 + err2
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def concat_copy_three(ffmpeg, first, middle, last, output, stop_event=None, on_proc=None):
    """三段视频无损拼接（copy stream）"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
    try:
        tmp.write(f"file '{str(first)}'\n")
        tmp.write(f"file '{str(middle)}'\n")
        tmp.write(f"file '{str(last)}'\n")
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
        # Fallback: 使用 TS 容器转换 + concat 协议
        ts1 = tempfile.NamedTemporaryFile(delete=False, suffix=".ts")
        ts2 = tempfile.NamedTemporaryFile(delete=False, suffix=".ts")
        ts3 = tempfile.NamedTemporaryFile(delete=False, suffix=".ts")
        ts1.close()
        ts2.close()
        ts3.close()
        cmd1 = [
            ffmpeg,
            "-y",
            "-i",
            str(first),
            "-c",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
            "-f",
            "mpegts",
            ts1.name,
        ]
        cmd2 = [
            ffmpeg,
            "-y",
            "-i",
            str(middle),
            "-c",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
            "-f",
            "mpegts",
            ts2.name,
        ]
        cmd3 = [
            ffmpeg,
            "-y",
            "-i",
            str(last),
            "-c",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
            "-f",
            "mpegts",
            ts3.name,
        ]
        if stop_event and stop_event.is_set():
            return False, "Cancelled"
        code1, out1, err1 = run_command(cmd1)
        if stop_event and stop_event.is_set():
            return False, "Cancelled"
        code2, out2, err2 = run_command(cmd2)
        if stop_event and stop_event.is_set():
            return False, "Cancelled"
        code3, out3, err3 = run_command(cmd3)
        if code1 == 0 and code2 == 0 and code3 == 0:
            cmd_concat = [
                ffmpeg,
                "-y",
                "-i",
                f"concat:{ts1.name}|{ts2.name}|{ts3.name}",
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                "-movflags",
                "+faststart",
                str(output),
            ]
            if stop_event and stop_event.is_set():
                return False, "Cancelled"
            code_concat, out_concat, err_concat = run_command(cmd_concat)
            os.unlink(ts1.name)
            os.unlink(ts2.name)
            os.unlink(ts3.name)
            if code_concat == 0:
                return True, out1 + err1 + out2 + err2 + out3 + err3 + out_concat + err_concat
            return False, out1 + err1 + out2 + err2 + out3 + err3 + out_concat + err_concat
        else:
            for ts_file in [ts1.name, ts2.name, ts3.name]:
                try:
                    os.unlink(ts_file)
                except OSError:
                    pass
            return False, out1 + err1 + out2 + err2 + out3 + err3
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def concat_reencode(ffmpeg, ffprobe, front, back, output, target_resolution, progress_total=None, on_progress=None, on_proc=None):
    width, height = target_resolution
    video_filter = build_scale_filter(width, height)
    front_has_audio = probe_has_audio(ffprobe, front)
    back_has_audio = probe_has_audio(ffprobe, back)
    use_videotoolbox = sys.platform == "darwin"
    if use_videotoolbox:
        # macOS 硬件加速，10Mbps 码率保证画质，速度极快
        v_params = ["-c:v", "h264_videotoolbox", "-b:v", "10000k", "-allow_sw", "1"]
    else:
        # 标准 x264 优化：veryfast 提升速度，crf 23 平衡画质与体积
        v_params = ["-c:v", "libx264", "-crf", "23", "-preset", "veryfast"]

    if front_has_audio and back_has_audio:
        filter_complex = (
            f"[0:v]{video_filter}[v0];"
            f"[1:v]{video_filter}[v1];"
            "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a0];"
            "[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a1];"
            "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
        )
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(front),
            "-i",
            str(back),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
        ] + v_params + [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    else:
        filter_complex = (
            f"[0:v]{video_filter}[v0];"
            f"[1:v]{video_filter}[v1];"
            "[v0][v1]concat=n=2:v=1:a=0[v]"
        )
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(front),
            "-i",
            str(back),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
        ] + v_params + [
            "-movflags",
            "+faststart",
            str(output),
        ]
    cmd = cmd + ["-progress", "pipe:1", "-nostats"]
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
        return False, str(e), front_has_audio and back_has_audio
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
    return code == 0, "\n".join(out_lines), front_has_audio and back_has_audio


def concat_reencode_three(ffmpeg, ffprobe, first, middle, last, output, target_resolution, progress_total=None, on_progress=None, on_proc=None):
    """三段视频重编码合并，支持统一分辨率"""
    width, height = target_resolution
    video_filter = build_scale_filter(width, height)
    first_has_audio = probe_has_audio(ffprobe, first)
    middle_has_audio = probe_has_audio(ffprobe, middle)
    last_has_audio = probe_has_audio(ffprobe, last)
    all_have_audio = first_has_audio and middle_has_audio and last_has_audio
    use_videotoolbox = sys.platform == "darwin"
    if use_videotoolbox:
        # macOS 硬件加速，10Mbps 码率保证画质，速度极快
        v_params = ["-c:v", "h264_videotoolbox", "-b:v", "10000k", "-allow_sw", "1"]
    else:
        # 标准 x264 优化：veryfast 提升速度，crf 23 平衡画质与体积
        v_params = ["-c:v", "libx264", "-crf", "23", "-preset", "veryfast"]

    if all_have_audio:
        filter_complex = (
            f"[0:v]{video_filter}[v0];"
            f"[1:v]{video_filter}[v1];"
            f"[2:v]{video_filter}[v2];"
            "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a0];"
            "[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a1];"
            "[2:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a2];"
            "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]"
        )
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(first),
            "-i",
            str(middle),
            "-i",
            str(last),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
        ] + v_params + [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    else:
        filter_complex = (
            f"[0:v]{video_filter}[v0];"
            f"[1:v]{video_filter}[v1];"
            f"[2:v]{video_filter}[v2];"
            "[v0][v1][v2]concat=n=3:v=1:a=0[v]"
        )
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(first),
            "-i",
            str(middle),
            "-i",
            str(last),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
        ] + v_params + [
            "-movflags",
            "+faststart",
            str(output),
        ]
    cmd = cmd + ["-progress", "pipe:1", "-nostats"]
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


class App:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.worker = None
        self.stop_event = threading.Event()
        self.total_tasks = 0
        self.completed_tasks = 0

        self.intro_dir = StringVar()
        self.front_dir = StringVar()
        self.middle_dir = StringVar()
        self.back_dir = StringVar()
        self.output_dir = StringVar()
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
        self.log_panels = []  # List of dicts: {frame, label, text}
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
        subtitle = tk.Label(header, text="开头 × 前半段 × 中段 × 后半段 组合合并，支持自动统一分辨率", font=font_small, bg=bg_color, fg="#666666")
        title.grid(row=0, column=0, sticky="w")
        subtitle.grid(row=1, column=0, sticky="w", pady=(2, 0))

        path_section = tk.Frame(container, bg=bg_color)
        path_section.grid(row=1, column=0, sticky="ew", pady=(0, 15))
        path_section.grid_columnconfigure(0, weight=1)
        tk.Label(path_section, text="素材与输出", font=font_normal, bg=bg_color, fg=fg_color).grid(row=0, column=0, sticky="w", pady=(0, 8))
        path_content = tk.Frame(path_section, bg=bg_color)
        path_content.grid(row=1, column=0, sticky="ew")
        path_content.grid_columnconfigure(0, weight=1)
        intro_row = self.build_path_row(path_content, "开头目录 (可选)", self.intro_dir, self.pick_intro)
        front_row = self.build_path_row(path_content, "前半段目录", self.front_dir, self.pick_front)
        middle_row = self.build_path_row(path_content, "中段目录 (可选)", self.middle_dir, self.pick_middle)
        back_row = self.build_path_row(path_content, "后半段目录", self.back_dir, self.pick_back)
        out_row = self.build_path_row(path_content, "输出目录", self.output_dir, self.pick_output)
        self.intro_count_var = StringVar(value="共 0 个视频")
        self.front_count_var = StringVar(value="共 0 个视频")
        self.middle_count_var = StringVar(value="共 0 个视频")
        self.back_count_var = StringVar(value="共 0 个视频")
        self.estimated_var = StringVar(value="预计输出数量：0")
        tk.Label(intro_row, textvariable=self.intro_count_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))
        tk.Label(front_row, textvariable=self.front_count_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))
        tk.Label(middle_row, textvariable=self.middle_count_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))
        tk.Label(back_row, textvariable=self.back_count_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))
        tk.Label(out_row, textvariable=self.estimated_var, font=font_small, bg=bg_color, fg="#666666").grid(row=1, column=1, sticky="w", padx=(10, 0))
        self.intro_dir.trace_add("write", lambda *args: self.update_counts())
        self.front_dir.trace_add("write", lambda *args: self.update_counts())
        self.middle_dir.trace_add("write", lambda *args: self.update_counts())
        self.back_dir.trace_add("write", lambda *args: self.update_counts())
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
            text="自动（以前半段为准）",
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
        
        # Initial system log
        self.log_text = ScrolledText(self.log_container, height=10, font=("Menlo", 11), state="disabled", bg="white", fg=fg_color, highlightthickness=1, highlightbackground="#d1d5db")
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self.refresh_resolution_state()

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

    def build_path_row(self, parent, label, var, command):
        row = parent.grid_size()[1]
        row_frame = tk.Frame(parent, bg=self.ui_bg)
        row_frame.grid(row=row, column=0, sticky="ew", pady=4)
        row_frame.grid_columnconfigure(1, weight=1, minsize=360)

        tk.Label(row_frame, text=label, font=self.font_normal, bg=self.ui_bg, fg=self.ui_fg, width=10, anchor="w").grid(row=0, column=0, sticky="w")
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

        def info(w):
            try:
                text = w.cget("text")
            except Exception:
                text = ""
            return f"{w.winfo_class():7} mapped={int(w.winfo_ismapped())} geom={w.winfo_geometry():>12} manager={w.winfo_manager():>5} text={text!r}"

        print("=== UI DUMP BEGIN ===", flush=True)
        print(f"Python {sys.version}")
        print(f"Tkinter {tk.TkVersion}")
        try:
            print(f"Tcl/Tk {self.root.tk.call('info', 'patchlevel')}")
        except:
            pass
        print(f"total={len(widgets)} mapped={len(mapped)} labels={len(labels)} entries={len(entries)} buttons={len(buttons)}", flush=True)
        for w in labels[:40]:
            print(info(w), flush=True)
        for w in entries[:20]:
            print(info(w), flush=True)
        print("=== UI DUMP END ===", flush=True)

    def refresh_resolution_state(self):
        if self.resolution_mode.get() == "custom":
            self.width_entry.configure(state="normal")
            self.height_entry.configure(state="normal")
        else:
            self.width_entry.configure(state="disabled")
            self.height_entry.configure(state="disabled")

    def pick_front(self):
        path = filedialog.askdirectory()
        if path:
            self.front_dir.set(path)
            self.update_counts()

    def pick_intro(self):
        path = filedialog.askdirectory()
        if path:
            self.intro_dir.set(path)
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

    def start_merge(self):
        if self.worker and self.worker.is_alive():
            return
        intro_dir = self.intro_dir.get()
        front_dir = self.front_dir.get()
        middle_dir = self.middle_dir.get()
        back_dir = self.back_dir.get()
        output_dir = self.output_dir.get()
        if not front_dir or not back_dir or not output_dir:
            messagebox.showerror("错误", "请先选择前半段、后半段和输出目录")
            return
        intros = list_videos(intro_dir) if intro_dir else []
        fronts = list_videos(front_dir)
        middles = list_videos(middle_dir) if middle_dir else []
        backs = list_videos(back_dir)
        if not fronts or not backs:
            messagebox.showerror("错误", "未找到可用的视频文件")
            return
        ffmpeg = find_binary("ffmpeg")
        ffprobe = find_binary("ffprobe")
        if not ffmpeg or not ffprobe:
            messagebox.showerror("错误", "未找到 FFmpeg/FFprobe，请将可执行文件放在程序目录或 ffmpeg 文件夹中")
            return

        self.stop_event.clear()
        # 中段为空时退化为两段拼接
        self.total_tasks = len(fronts) * len(backs) if not middles else len(fronts) * len(middles) * len(backs)
        self.completed_tasks = 0
        self.progress_canvas.update_idletasks()
        self.progress_canvas.coords(self.progress_bar_rect, 0, 0, 0, 16)
        self.progress_label.configure(text=f"0 / {self.total_tasks}")
        self.eta_label.configure(text="剩余时间估算：--:--")
        
        # Setup UI for concurrency
        try:
            max_workers = int(self.max_workers.get() or 1)
            max_workers = max(1, max_workers)
        except:
            max_workers = 1
            
        # Clear log container and rebuild grid
        for widget in self.log_container.winfo_children():
            widget.destroy()
        self.log_panels = []
        
        cols = math.ceil(math.sqrt(max_workers))
        rows = math.ceil(max_workers / cols)
        
        for r in range(rows):
            self.log_container.grid_rowconfigure(r, weight=1)
        for c in range(cols):
            self.log_container.grid_columnconfigure(c, weight=1)
            
        panel_height = 180
        for i in range(max_workers):
            r = i // cols
            c = i % cols
            frame = tk.Frame(self.log_container, bg="white", highlightthickness=1, highlightbackground="#d1d5db", height=panel_height)
            frame.grid(row=r, column=c, sticky="nsew", padx=2, pady=2)
            frame.grid_propagate(False)
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(1, weight=1)
            
            label = tk.Label(frame, text=f"线程 #{i+1}: 等待任务...", font=("Helvetica", 9, "bold"), bg="#f0f0f0", anchor="w", padx=5)
            label.grid(row=0, column=0, sticky="ew")
            
            text = ScrolledText(frame, height=7, font=("Menlo", 10), state="disabled", bg="white", fg="#333333", bd=0)
            text.grid(row=1, column=0, sticky="nsew")
            
            self.log_panels.append({"frame": frame, "label": label, "text": text})

        # Redirect general log to first panel
        if self.log_panels:
            self.log_text = self.log_panels[0]["text"]

        self.log("开始合并任务")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        mode_value = self.mode.get()
        mode_key = "auto" if mode_value == "优先无损（失败自动转兼容）" else "compat"
        config = {
            "intros": intros,
            "fronts": fronts,
            "middles": middles,
            "backs": backs,
            "output_dir": Path(output_dir),
            "mode": mode_key,
            "resolution_mode": self.resolution_mode.get(),
            "custom_resolution": (self.custom_width.get(), self.custom_height.get()),
            "skip_existing": self.skip_existing.get(),
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
            "max_workers": max_workers,
        }
        self.worker = threading.Thread(target=self.run_merge, args=(config,), daemon=True)
        self.worker.start()

    def stop_merge(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.log("正在停止任务...")
            self.cancel_current()
        self.stop_button.configure(state="disabled")

    def run_merge(self, config):
        intros = config.get("intros", [])
        fronts = config["fronts"]
        middles = config.get("middles", [])
        backs = config["backs"]
        output_dir = config["output_dir"]
        mode = config["mode"]
        resolution_mode = config["resolution_mode"]
        custom_resolution = config["custom_resolution"]
        skip_existing = config["skip_existing"]
        ffmpeg = config["ffmpeg"]
        ffprobe = config["ffprobe"]
        max_workers = config.get("max_workers", 1)

        # 判断是否使用三段合并
        use_middle = len(middles) > 0
        use_intro = len(intros) > 0

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log("创建输出目录失败")
            self.log(str(e))
            self.finish()
            return

        # Init slots
        self.thread_slots = queue.Queue()
        for i in range(max_workers):
            self.thread_slots.put(i)

        combinations = []
        if use_middle:
            for front in fronts:
                for middle in middles:
                    for back in backs:
                        output_name = f"{front.stem}_{middle.stem}_{back.stem}_merged.mp4"
                        output_path = output_dir / output_name
                        if skip_existing and output_path.exists():
                            self.log(f"跳过已存在：{output_name}")
                            self.completed_tasks += 1
                            self.update_progress()
                        else:
                            combinations.append((front, middle, back, output_name, output_path))
        else:
            for front in fronts:
                for back in backs:
                    output_name = f"{front.stem}_{back.stem}_merged.mp4"
                    output_path = output_dir / output_name
                    if skip_existing and output_path.exists():
                        self.log(f"跳过已存在：{output_name}")
                        self.completed_tasks += 1
                        self.update_progress()
                    else:
                        combinations.append((front, None, back, output_name, output_path))

        if not combinations:
            self.finish()
            return

        def merge_combination(first, middle, last, output_name, output_path, intro=None, intro_index=None):
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

                display_name = output_name
                if intro:
                    display_name = f"{intro.stem}_{output_name}"
                self.queue.put(("title_slot", slot_idx, f"正在处理: {display_name}"))
                self.queue.put(("log_slot", slot_idx, f"开始: {display_name}"))
                
                on_proc = lambda p: self.register_proc(p, output_path)
                is_three = middle is not None
                use_intro_local = intro is not None
                combined_output = output_path
                combined_name = output_name
                temp_output = None
                if use_intro_local:
                    combined_name = f"{intro.stem}_{output_name}"
                    temp_output = output_path.with_name(output_path.stem + "_body.mp4")
                    combined_output = temp_output
                
                if mode == "auto":
                    if is_three and is_copy_compatible_for_three(ffprobe, first, middle, last):
                        ok, logtxt = concat_copy_three(ffmpeg, first, middle, last, combined_output, stop_event=self.stop_event, on_proc=on_proc)
                        if ok:
                            self.queue.put(("log_slot", slot_idx, "无损合并成功"))
                            return True, "copy"
                        self.queue.put(("log_slot", slot_idx, "无损失败，转兼容..."))
                    elif not is_three and is_copy_compatible(ffprobe, first, last):
                        ok, logtxt = concat_copy(ffmpeg, first, last, combined_output, stop_event=self.stop_event, on_proc=on_proc)
                        if ok:
                            self.queue.put(("log_slot", slot_idx, "无损合并成功"))
                            return True, "copy"
                        self.queue.put(("log_slot", slot_idx, "无损失败，转兼容..."))
                
                if resolution_mode == "custom":
                    target_resolution = custom_resolution
                else:
                    target_resolution = probe_resolution(ffprobe, first)
                    if not target_resolution:
                        return False, f"无法获取分辨率: {first.name}"
                
                if is_three:
                    total_duration = (probe_duration(ffprobe, first) or 0.0) + (probe_duration(ffprobe, middle) or 0.0) + (probe_duration(ffprobe, last) or 0.0)
                else:
                    total_duration = (probe_duration(ffprobe, first) or 0.0) + (probe_duration(ffprobe, last) or 0.0)
                
                def on_progress_task(remaining):
                    m = int(remaining // 60)
                    s = int(remaining % 60)
                    self.queue.put(("title_slot", slot_idx, f"{combined_name} (ETA: {m:02d}:{s:02d})"))
                    self.queue.put(("eta", remaining))

                if is_three:
                    ok, logtxt, audio_kept = concat_reencode_three(
                        ffmpeg,
                        ffprobe,
                        first,
                        middle,
                        last,
                        combined_output,
                        target_resolution,
                        progress_total=total_duration,
                        on_progress=on_progress_task,
                        on_proc=on_proc,
                    )
                else:
                    ok, logtxt, audio_kept = concat_reencode(
                        ffmpeg,
                        ffprobe,
                        first,
                        last,
                        combined_output,
                        target_resolution,
                        progress_total=total_duration,
                        on_progress=on_progress_task,
                        on_proc=on_proc,
                    )
                
                if use_intro_local and ok:
                    intro_output = output_path.with_name(combined_name)
                    ok, logtxt = concat_copy(ffmpeg, intro, combined_output, intro_output, stop_event=self.stop_event, on_proc=on_proc)
                    try:
                        if temp_output and temp_output.exists():
                            os.remove(temp_output)
                    except Exception:
                        pass
                    if ok:
                        self.queue.put(("log_slot", slot_idx, "开头顺序拼接成功"))
                        return True, "copy"
                    safe_remove(intro_output)
                    self.queue.put(("log_slot", slot_idx, f"开头拼接失败: {logtxt}"))
                    return False, logtxt

                if ok:
                    self.queue.put(("log_slot", slot_idx, "兼容合并成功"))
                else:
                    safe_remove(combined_output)
                    self.queue.put(("log_slot", slot_idx, f"失败: {logtxt}"))
                    
                return (ok, "reencode" if ok else logtxt)
            finally:
                self.queue.put(("title_slot", slot_idx, f"线程 #{slot_idx+1}: 空闲"))
                self.thread_slots.put(slot_idx)

        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {}
                for idx, (first, middle, last, output_name, output_path) in enumerate(combinations):
                    if self.stop_event.is_set():
                        break
                    intro = None
                    if use_intro:
                        intro = intros[idx % len(intros)]
                    future = executor.submit(merge_combination, first, middle, last, output_name, output_path, intro=intro, intro_index=idx)
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
                        txt = self.log_panels[slot_idx]["text"]
                        txt.configure(state="normal")
                        txt.insert("end", text + "\n")
                        txt.configure(state="disabled")
                        txt.see("end")
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
                    m = int(remaining // 60)
                    s = int(remaining % 60)
                    self.eta_label.configure(text=f"剩余时间估算：{m:02d}:{s:02d}")
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
            for p in procs:
                try:
                    p.terminate()
                except Exception:
                    pass
                try:
                    p.kill()
                except Exception:
                    pass
            for out in outs:
                try:
                    if Path(out).exists():
                        os.remove(out)
                        self.log(f"已删除未完成文件: {Path(out).name}")
                except Exception:
                    pass
        finally:
            self.current_output_path = None

    def update_counts(self):
        try:
            i = self.intro_dir.get()
            f = self.front_dir.get()
            m = self.middle_dir.get()
            b = self.back_dir.get()
            ic = len(list_videos(i)) if i else 0
            fc = len(list_videos(f)) if f else 0
            mc = len(list_videos(m)) if m else 0
            bc = len(list_videos(b)) if b else 0
            self.intro_count_var.set(f"共 {ic} 个视频")
            self.front_count_var.set(f"共 {fc} 个视频")
            self.middle_count_var.set(f"共 {mc} 个视频")
            self.back_count_var.set(f"共 {bc} 个视频")
            total = fc * bc if mc == 0 else fc * mc * bc
            self.estimated_var.set(f"预计输出数量：{total}")
        except Exception:
            pass
 
def build_scale_filter(width, height):
    return f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"



def main():
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
