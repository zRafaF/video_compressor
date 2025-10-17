import os
import subprocess
import shutil
import json
import sys
import time
import tempfile

# --- Main Configuration ---

# 1. CHOOSE YOUR PRESET:
#    "balanced": Good mix of quality and size.
#    "best_for_size": Aggressive compression, noticeable quality loss.
#    "extreme_720p": Maximum compression. Downscales video to 720p height.
SELECTED_PRESET = "best_for_size"

# 2. SET MINIMUM COMPRESSION (in percent)
# If compression saves less than this %, the original file is copied.
MINIMUM_COMPRESSION_PERCENT = 10

# --- Script Settings ---
INPUT_FOLDER = "input"
OUTPUT_FOLDER = "output"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v")
BITRATE_THRESHOLD = 2_500_000  # 2.5 Mbps
AUDIO_CODEC = "copy"

# --- NEW: More Aggressive Quality Preset Definitions ---
# H265_CQ: Higher value = smaller file, lower quality. 32-35 is very aggressive.
# GPU_PRESET: Faster presets (lower p-number) for when quality is less critical.
# SCALE: (Optional) Downscales video. "-1:720" keeps aspect ratio for 720p height.
QUALITY_PRESETS = {
    "best_for_quality": {"H265_CQ": 23, "GPU_PRESET": "p7", "SCALE": None},
    "balanced": {"H265_CQ": 28, "GPU_PRESET": "p6", "SCALE": None},
    "best_for_size": {"H265_CQ": 32, "GPU_PRESET": "p5", "SCALE": None},
    "extreme_720p": {"H265_CQ": 35, "GPU_PRESET": "p4", "SCALE": "-1:720"},
}

# Apply the selected preset
config = QUALITY_PRESETS[SELECTED_PRESET]
H265_CQ = config["H265_CQ"]
GPU_PRESET = config["GPU_PRESET"]
SCALE_VIDEO = config["SCALE"]


def get_video_details(file_path):
    """Uses ffprobe to get codec, bitrate, and total frame count."""
    command = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-select_streams",
        "v:0",
        file_path,
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, encoding="utf-8"
        )
        data = json.loads(result.stdout)
        if not data.get("streams"):
            return None, 0, 0
        stream = data["streams"][0]
        codec = stream.get("codec_name")
        bit_rate = int(stream.get("bit_rate", 0))
        total_frames = int(stream.get("nb_frames", 0))
        if total_frames == 0:
            duration_str, frame_rate_str = stream.get("duration", "0"), stream.get(
                "avg_frame_rate", "0/1"
            )
            if "/" in frame_rate_str and duration_str != "0":
                try:
                    duration, (num, den) = float(duration_str), map(
                        int, frame_rate_str.split("/")
                    )
                    if den != 0:
                        total_frames = int(duration * (num / den))
                except (ValueError, ZeroDivisionError):
                    total_frames = 0
        return codec, bit_rate, total_frames
    except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError):
        return None, 0, 0


def compress_video_gpu(
    input_file, output_file, total_frames, relative_path, cq_value, scale
):
    """Compresses a video using NVENC and returns True on success, False on failure."""
    progress_file_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, mode="w+", suffix=".txt") as tmp:
            progress_file_path = tmp.name

        command = [
            "ffmpeg",
            "-hwaccel",
            "cuda",
            "-i",
            input_file,
            "-c:v",
            "hevc_nvenc",
            "-preset",
            GPU_PRESET,
            "-tune",
            "hq",
            "-rc",
            "vbr_hq",
            "-cq",
            str(cq_value),
            "-qmin",
            "0",
            "-b:v",
            "0",
            "-look_ahead",
            "32",
            "-spatial-aq",
            "1",
            "-temporal-aq",
            "1",
            "-c:a",
            AUDIO_CODEC,
            "-sn",
            "-y",
            "-progress",
            progress_file_path,
            "-loglevel",
            "error",
        ]

        # --- NEW: Add video filter for scaling if specified ---
        if scale:
            command.extend(["-vf", f"scale_cuda={scale}"])

        command.append(output_file)

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        while process.poll() is None:
            time.sleep(0.5)
            try:
                with open(progress_file_path, "r") as f:
                    lines = f.readlines()
                progress_data = {
                    k.strip(): v.strip()
                    for line in lines[-12:]
                    if "=" in line
                    for k, v in [line.split("=", 1)]
                }
                if total_frames > 0 and "frame" in progress_data:
                    percent = (int(progress_data["frame"]) / total_frames) * 100
                    fps, bitrate = progress_data.get("fps", "0.0"), progress_data.get(
                        "bitrate", "N/A"
                    )
                    progress_text = f"[GPU] [{percent:3.1f}%] Encoding {relative_path} ({float(fps):.1f}fps @ {bitrate})"
                    sys.stdout.write(f"\r{progress_text.ljust(100)}")
                    sys.stdout.flush()
            except (IOError, ValueError):
                continue

        sys.stdout.write("\r" + " " * 100 + "\r")
        sys.stdout.flush()

        _, stderr = process.communicate()
        if process.returncode != 0:
            print(
                f"[ERROR] FFmpeg failed on {relative_path}.\n--- FFmpeg Error ---\n{stderr.strip()}\n--------------------"
            )
            return False
        return True
    finally:
        if os.path.exists(progress_file_path):
            os.remove(progress_file_path)


def process_files_recursively(root_input, root_output, config):
    """Recursively scans and processes video files with new size checks."""
    cq_value, scale_value = config["H265_CQ"], config["SCALE"]
    all_files = [
        os.path.join(dp, f)
        for dp, _, fn in os.walk(root_input)
        for f in fn
        if not f.startswith(".")
    ]

    for i, input_path in enumerate(all_files):
        relative_path = os.path.relpath(input_path, root_input)
        output_path = os.path.join(
            root_output, os.path.splitext(relative_path)[0] + ".mp4"
        )

        print(f"\n--- Processing file {i + 1} of {len(all_files)}: {relative_path} ---")

        if os.path.exists(output_path):
            print("Output file already exists. Skipping.")
            continue
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if not input_path.lower().endswith(VIDEO_EXTENSIONS):
            print("Not a video file. Copying directly...")
            shutil.copy2(input_path, output_path)
            continue

        codec, bit_rate, total_frames = get_video_details(input_path)
        if codec is None and bit_rate == 0:
            print("Could not get details. Copying file.")
            shutil.copy2(input_path, output_path)
            continue

        if codec == "hevc" or (0 < bit_rate < BITRATE_THRESHOLD):
            print(
                f"Already efficient (codec: {codec}, bitrate: {bit_rate/1000:.0f}kbps). Copying..."
            )
            shutil.copy2(input_path, output_path)
            continue

        print(f"Compressing (codec: {codec}, bitrate: {bit_rate/1000:.0f}kbps)...")
        success = compress_video_gpu(
            input_path, output_path, total_frames, relative_path, cq_value, scale_value
        )

        if not success:
            print("Copying original due to compression error.")
            shutil.copy2(input_path, output_path)
            continue

        input_size, output_size = os.path.getsize(input_path), os.path.getsize(
            output_path
        )

        if output_size >= input_size:
            print(
                f"Output larger ({output_size/1e6:.2f}MB > {input_size/1e6:.2f}MB). Copying original."
            )
            shutil.copy2(input_path, output_path)
        else:
            reduction = ((input_size - output_size) / input_size) * 100
            if reduction < MINIMUM_COMPRESSION_PERCENT:
                print(
                    f"Reduction ({reduction:.1f}%) below minimum ({MINIMUM_COMPRESSION_PERCENT}%). Copying original."
                )
                shutil.copy2(input_path, output_path)
            else:
                print(f"Compression successful. Size reduced by {reduction:.1f}%.")
        print("Finished processing file.")


if __name__ == "__main__":
    if not os.path.exists(INPUT_FOLDER):
        os.makedirs(INPUT_FOLDER)
    if not os.path.exists(OUTPUT_FOLDER):
        os.makedirs(OUTPUT_FOLDER)

    print("--- GPU H.265 Video Compressor (v4) ---")
    print(
        f"Selected Preset: '{SELECTED_PRESET}' (CQ: {H265_CQ}, GPU Preset: {GPU_PRESET}, Scale: {SCALE_VIDEO or 'None'})"
    )
    print(f"Minimum Compression: {MINIMUM_COMPRESSION_PERCENT}%")
    print("-" * 42)

    try:
        process_files_recursively(INPUT_FOLDER, OUTPUT_FOLDER, config=config)
        print("\nProcessing complete.")
    except KeyboardInterrupt:
        print("\n\n[!] Script stopped by user. Exiting.")
        sys.exit(1)
