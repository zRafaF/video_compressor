import os
import subprocess
import shutil
import json
import sys
import time
import tempfile

# --- Main Configuration ---

# 1. SET FFMPEG PATH (IMPORTANT!)
# If ffmpeg.exe and ffprobe.exe are in your system PATH, you can leave this as "ffmpeg".
# Otherwise, provide the full path, e.g., "C:\\ffmpeg\\bin\\ffmpeg.exe"
FFMPEG_PATH = "ffmpeg"

# 2. CHOOSE YOUR PRESET:
#    "balanced": Good mix of quality and size (Single Pass).
#    "best_for_size": Aggressive compression (Single Pass).
#    "extreme_720p": Maximum compression, downscales video (Single Pass).
#    "best_quality_at_size": Uses 2-Pass VBR for best quality at a target size.
SELECTED_PRESET = "best_quality_at_size"

# 3. TARGET BITRATE (for "best_quality_at_size" preset ONLY)
# This determines the final file size. Higher value = larger file, better quality.
# Good values: 1080p -> 4000, 720p -> 2500, 4K -> 8000
TARGET_BITRATE_KBPS = 2000

# 4. SET MINIMUM COMPRESSION (in percent)
# If compression saves less than this %, the original file is copied.
MINIMUM_COMPRESSION_PERCENT = 10


# --- Script Settings (DO NOT CHANGE) ---
INPUT_FOLDER = "input"
OUTPUT_FOLDER = "output"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v")
AUDIO_CODEC = "copy"

# --- Quality Preset Definitions ---
QUALITY_PRESETS = {
    "best_quality_at_size": {"MODE": "VBR", "GPU_PRESET": "p7", "SCALE": None},
    "best_for_quality": {
        "MODE": "CQ",
        "H265_CQ": 23,
        "GPU_PRESET": "p5",
        "SCALE": None,
    },
    "balanced": {"MODE": "CQ", "H265_CQ": 28, "GPU_PRESET": "p6", "SCALE": None},
    "best_for_size": {"MODE": "CQ", "H265_CQ": 32, "GPU_PRESET": "p6", "SCALE": None},
    "extreme_720p": {
        "MODE": "CQ",
        "H265_CQ": 35,
        "GPU_PRESET": "p4",
        "SCALE": "-1:720",
    },
}

config = QUALITY_PRESETS[SELECTED_PRESET]


def get_video_details(file_path):
    """Uses ffprobe to get video details."""
    ffprobe_path = FFMPEG_PATH.replace("ffmpeg", "ffprobe")
    command = [
        ffprobe_path,
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
        codec, bit_rate = stream.get("codec_name"), int(stream.get("bit_rate", 0))
        total_frames = int(stream.get("nb_frames", 0))
        if total_frames == 0:
            duration_str, fr_str = stream.get("duration", "0"), stream.get(
                "avg_frame_rate", "0/1"
            )
            if "/" in fr_str and duration_str != "0":
                try:
                    duration, (num, den) = float(duration_str), map(
                        int, fr_str.split("/")
                    )
                    if den != 0:
                        total_frames = int(duration * (num / den))
                except (ValueError, ZeroDivisionError):
                    total_frames = 0
        return codec, bit_rate, total_frames
    except FileNotFoundError:
        print(
            f"\n[FATAL ERROR] Cannot find ffprobe. Please check the FFMPEG_PATH variable."
        )
        print(f"Current path is set to: '{ffprobe_path}'")
        sys.exit(1)
    except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError):
        return None, 0, 0


def monitor_ffmpeg_progress(process, total_frames, progress_file_path, description):
    """Displays a visual progress bar by monitoring an FFmpeg process."""
    bar_length = 40
    while process.poll() is None:
        time.sleep(0.5)
        if not os.path.exists(progress_file_path):
            continue
        try:
            with open(progress_file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            progress_data = {
                k.strip(): v.strip()
                for line in lines[-12:]
                if "=" in line
                for k, v in [line.split("=", 1)]
            }

            if total_frames > 0 and "frame" in progress_data:
                current_frame = int(progress_data["frame"])
                percent = (current_frame / total_frames) * 100
                filled_length = int(bar_length * current_frame // total_frames)
                bar = "█" * filled_length + "-" * (bar_length - filled_length)
                fps = float(progress_data.get("fps", 0.0))

                progress_text = (
                    f"{description}: |{bar}| {percent:5.1f}% ({fps:.1f} fps)"
                )
                sys.stdout.write(f"\r{progress_text.ljust(80)}")
                sys.stdout.flush()

        except (IOError, ValueError, KeyError):
            continue

    bar = "█" * bar_length
    progress_text = f"{description}: |{bar}| 100.0% (Complete)"
    sys.stdout.write(f"\r{progress_text.ljust(80)}\n")
    sys.stdout.flush()


def compress_video_gpu(
    input_file, output_file, total_frames, relative_path, preset_config
):
    """Compresses a video using either Single-Pass CQ or Two-Pass VBR."""
    progress_file_path = ""
    try:
        # Create a temporary file for progress reporting
        with tempfile.NamedTemporaryFile(delete=False, mode="w+", suffix=".txt") as tmp:
            progress_file_path = tmp.name

        # Ensure the temp file is closed before FFmpeg uses it
        # This is handled by the 'with' statement exiting.

        if preset_config["MODE"] == "VBR":
            target_bitrate = f"{TARGET_BITRATE_KBPS}k"
            max_bitrate = f"{int(TARGET_BITRATE_KBPS * 1.5)}k"

            common_args = [
                FFMPEG_PATH,
                "-hwaccel",
                "cuda",
                "-i",
                input_file,
                "-c:v",
                "hevc_nvenc",
                "-preset",
                preset_config["GPU_PRESET"],
                "-rc",
                "vbr",
                "-b:v",
                target_bitrate,
                "-maxrate",
                max_bitrate,
                "-multipass",
                "2",
                "-g",
                "250",
                "-bf",
                "3",
                "-b_ref_mode",
                "middle",
                "-temporal-aq",
                "1",
                "-spatial-aq",
                "1",
                "-y",
                "-progress",
                progress_file_path,
            ]

            # --- PASS 1 ---
            pass1_args = common_args + ["-pass", "1", "-an", "-f", "null", os.devnull]
            # THE FIX: Run Popen without piping stdout/stderr to avoid deadlocks
            process1 = subprocess.Popen(
                pass1_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            monitor_ffmpeg_progress(
                process1, total_frames, progress_file_path, "Pass 1/2 Analyzing"
            )
            if process1.returncode != 0:
                print(f"[ERROR] FFmpeg Pass 1 failed. Check for errors above.")
                return False

            # --- PASS 2 ---
            pass2_args = common_args + ["-pass", "2", "-c:a", AUDIO_CODEC, output_file]
            # THE FIX: Run Popen without piping stdout/stderr to avoid deadlocks
            process2 = subprocess.Popen(
                pass2_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            monitor_ffmpeg_progress(
                process2, total_frames, progress_file_path, "Pass 2/2 Encoding "
            )
            if process2.returncode != 0:
                print(f"[ERROR] FFmpeg Pass 2 failed. Check for errors above.")
                return False
            return True

        else:  # MODE is "CQ"
            command = [
                FFMPEG_PATH,
                "-hwaccel",
                "cuda",
                "-i",
                input_file,
                "-c:v",
                "hevc_nvenc",
                "-preset",
                preset_config["GPU_PRESET"],
                "-rc",
                "vbr_hq",
                "-cq",
                str(preset_config["H265_CQ"]),
                "-qmin",
                "0",
                "-b:v",
                "0",
                "-c:a",
                AUDIO_CODEC,
                "-sn",
                "-y",
                "-progress",
                progress_file_path,
                "-loglevel",
                "error",
            ]
            if preset_config.get("SCALE"):
                command.extend(["-vf", f"scale_cuda={preset_config['SCALE']}"])
            command.append(output_file)

            # THE FIX: Run Popen without piping stdout/stderr to avoid deadlocks
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            monitor_ffmpeg_progress(
                process, total_frames, progress_file_path, "Single Pass Encoding"
            )
            if process.returncode != 0:
                print(
                    f"[ERROR] FFmpeg failed on {relative_path}. Check for errors above."
                )
                return False
            return True

    except FileNotFoundError:
        print(
            f"\n[FATAL ERROR] Cannot find ffmpeg. Please check the FFMPEG_PATH variable."
        )
        print(f"Current path is set to: '{FFMPEG_PATH}'")
        sys.exit(1)
    finally:
        if os.path.exists(progress_file_path):
            os.remove(progress_file_path)


def process_files_recursively(root_input, root_output, preset_config):
    """Recursively scans and processes video files."""
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

        bitrate_threshold_kbps = (
            TARGET_BITRATE_KBPS if preset_config["MODE"] == "VBR" else 2500
        )
        if codec == "hevc" and (0 < bit_rate < (bitrate_threshold_kbps * 1000)):
            print(f"Already efficient. Copying...")
            shutil.copy2(input_path, output_path)
            continue

        print(f"Compressing (codec: {codec}, bitrate: {bit_rate/1000:.0f}kbps)...")
        success = compress_video_gpu(
            input_path, output_path, total_frames, relative_path, preset_config
        )

        if not success:
            print("Copying original due to compression error.")
            shutil.copy2(input_path, output_path)
            continue

        input_size, output_size = os.path.getsize(input_path), os.path.getsize(
            output_path
        )

        if output_size >= input_size:
            print(f"Output larger. Copying original.")
            shutil.copy2(input_path, output_path)
        else:
            reduction = ((input_size - output_size) / input_size) * 100
            if reduction < MINIMUM_COMPRESSION_PERCENT:
                print(f"Reduction ({reduction:.1f}%) below minimum. Copying original.")
                shutil.copy2(input_path, output_path)
            else:
                print(f"Compression successful. Size reduced by {reduction:.1f}%.")
        print("Finished processing file.")


if __name__ == "__main__":
    if not os.path.exists(INPUT_FOLDER):
        os.makedirs(INPUT_FOLDER)
    if not os.path.exists(OUTPUT_FOLDER):
        os.makedirs(OUTPUT_FOLDER)

    print("--- GPU H.265 Video Compressor (v8) ---")
    print(f"Selected Preset: '{SELECTED_PRESET}'")
    if config["MODE"] == "VBR":
        print(f"Mode: 2-Pass VBR | Target Bitrate: {TARGET_BITRATE_KBPS} kbps")
    else:
        print(f"Mode: Single-Pass CQ | CQ Level: {config['H265_CQ']}")
    print("-" * 42)

    try:
        process_files_recursively(INPUT_FOLDER, OUTPUT_FOLDER, preset_config=config)
        print("\nProcessing complete.")
    except KeyboardInterrupt:
        print("\n\n[!] Script stopped by user. Exiting.")
        sys.exit(1)
