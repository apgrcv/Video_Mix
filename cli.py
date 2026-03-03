import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Constants
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}

def list_videos(directory):
    base = Path(directory)
    if not base.exists():
        print(f"Directory not found: {directory}")
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
    completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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

def build_scale_filter(width, height):
    # 强制统一像素长宽比(SAR)为1:1，并统一颜色格式为yuv420p，防止合并后黑屏或花屏
    return f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"

def concat_copy(ffmpeg, front, back, output):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
    try:
        tmp.write(f"file '{str(front)}'\n")
        tmp.write(f"file '{str(back)}'\n")
        tmp.close()
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            tmp.name,
            "-c",
            "copy",
            str(output),
        ]
        code, out, err = run_command(cmd)
        return code == 0, out + err
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

def concat_reencode(ffmpeg, ffprobe, front, back, output, target_resolution):
    width, height = target_resolution
    video_filter = build_scale_filter(width, height)
    front_has_audio = probe_has_audio(ffprobe, front)
    back_has_audio = probe_has_audio(ffprobe, back)
    
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
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "medium",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ]
    else:
        # Handle cases where one or both inputs lack audio
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
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "medium",
            "-movflags",
            "+faststart",
            str(output),
        ]
    
    code, out, err = run_command(cmd)
    return code == 0, out + err

def main():
    parser = argparse.ArgumentParser(description="视频混剪工具命令行版")
    parser.add_argument("--front", required=True, help="前半段视频目录")
    parser.add_argument("--back", required=True, help="后半段视频目录")
    parser.add_argument("--output", required=True, help="输出目录")
    parser.add_argument("--mode", choices=["copy", "reencode"], default="copy", help="合并模式: copy(优先无损), reencode(强制重编码)")
    parser.add_argument("--width", type=int, default=1920, help="目标宽度 (仅reencode模式有效)")
    parser.add_argument("--height", type=int, default=1080, help="目标高度 (仅reencode模式有效)")
    
    args = parser.parse_args()
    
    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")
    
    if not ffmpeg or not ffprobe:
        print("Error: ffmpeg or ffprobe not found.")
        sys.exit(1)
        
    print(f"FFmpeg: {ffmpeg}")
    print(f"FFprobe: {ffprobe}")
    
    front_videos = list_videos(args.front)
    back_videos = list_videos(args.back)
    
    if not front_videos:
        print("No videos found in front directory.")
        sys.exit(1)
    if not back_videos:
        print("No videos found in back directory.")
        sys.exit(1)
        
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    count = min(len(front_videos), len(back_videos))
    print(f"Found {count} pairs to process.")
    
    for i in range(count):
        front = front_videos[i]
        back = back_videos[i]
        out_name = f"{front.stem}_{back.stem}_merged.mp4"
        out_path = output_dir / out_name
        
        if out_path.exists():
            print(f"[{i+1}/{count}] Skipping existing: {out_name}")
            continue
            
        print(f"[{i+1}/{count}] Processing: {front.name} + {back.name}")
        
        success = False
        if args.mode == "copy":
            success, msg = concat_copy(ffmpeg, front, back, out_path)
            if not success:
                print("  Copy failed, retrying with re-encode...")
                # Auto fallback logic similar to GUI
                target_res = (args.width, args.height)
                # Try to use front video resolution if possible
                front_res = probe_resolution(ffprobe, front)
                if front_res:
                    target_res = front_res
                
                success, msg = concat_reencode(ffmpeg, ffprobe, front, back, out_path, target_res)
        else:
            target_res = (args.width, args.height)
            success, msg = concat_reencode(ffmpeg, ffprobe, front, back, out_path, target_res)
            
        if success:
            print("  Success!")
        else:
            print(f"  Failed: {msg}")

if __name__ == "__main__":
    main()
