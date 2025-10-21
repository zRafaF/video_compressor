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
TARGET_BITRATE_KBPS = 2000

# 4. SET MINIMUM COMPRESSION (in percent)
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
    """Uses ffprobe to get video details, with a fallback for container bitrate."""
    ffprobe_path = FFMPEG_PATH.replace("ffmpeg", "ffprobe")
    stream_command = [
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
    format_command = [
        ffprobe_path,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        file_path,
    ]

    try:
        result = subprocess.run(
            stream_command, capture_output=True, text=True, check=True, encoding="utf-8"
        )
        data = json.loads(result.stdout)

        if not data.get("streams"):
            return None, 0, 0
        stream = data["streams"][0]

        codec, bit_rate = stream.get("codec_name"), int(stream.get("bit_rate", 0))
        total_frames = int(stream.get("nb_frames", 0))

        if bit_rate == 0:
            format_result = subprocess.run(
                format_command,
                capture_output=True,
                text=True,
                check=True,
                encoding="utf-8",
            )
            format_data = json.loads(format_result.stdout)
            bit_rate = int(format_data.get("format", {}).get("bit_rate", 0))

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
        sys.exit(1)
    except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError):
        return None, 0, 0


def monitor_ffmpeg_progress(process, total_frames, progress_file_path, description):
    """
    REWRITTEN: Displays a progress bar by reliably reading FFmpeg's dedicated progress file.
    """
    bar_length = 40

    # Wait for the progress file to be created
    while (
        not os.path.exists(progress_file_path)
        or os.path.getsize(progress_file_path) == 0
    ):
        if process.poll() is not None:
            return  # Process ended before progress file was made
        time.sleep(0.1)

    last_frame = 0
    while process.poll() is None:
        try:
            with open(progress_file_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            progress_data = {}
            for line in lines:
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    progress_data[key] = value

            if "frame" in progress_data:
                current_frame = int(progress_data["frame"])
                if current_frame > last_frame:
                    last_frame = current_frame
                    percent = (
                        (current_frame / total_frames) * 100 if total_frames > 0 else 0
                    )
                    filled_length = (
                        int(bar_length * current_frame // total_frames)
                        if total_frames > 0
                        else 0
                    )
                    bar = "█" * filled_length + "-" * (bar_length - filled_length)

                    progress_text = f"{description}: |{bar}| {percent:5.1f}%"
                    sys.stdout.write(f"\r{progress_text.ljust(80)}")
                    sys.stdout.flush()
        except (IOError, ValueError):
            pass  # File might be locked, try again

        time.sleep(0.5)

    bar = "█" * bar_length
    progress_text = f"{description}: |{bar}| 100.0% (Complete)"
    sys.stdout.write(f"\r{progress_text.ljust(80)}\n")
    sys.stdout.flush()


def compress_video_gpu(input_file, output_file, total_frames, preset_config):
    """Compresses a video using the robust -progress file method and redirects logs."""
    progress_file_path = ""
    log_file_path = os.path.splitext(output_file)[0] + "_ffmpeg_log.txt"

    try:
        with tempfile.NamedTemporaryFile(
            delete=False, mode="w+", suffix=".txt", encoding="utf-8"
        ) as tmp:
            progress_file_path = tmp.name

        base_args = [FFMPEG_PATH, "-nostdin", "-fflags", "+genpts"]

        if input_file.lower().endswith(".mkv"):
            base_args.extend(["-f", "matroska"])

        base_args.extend(["-hwaccel", "cuda", "-i", input_file])

        if preset_config["MODE"] == "VBR":
            target_bitrate = f"{TARGET_BITRATE_KBPS}k"
            max_bitrate = f"{int(TARGET_BITRATE_KBPS * 1.5)}k"
            common_args = base_args + [
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

            with open(log_file_path, "w", encoding="utf-8") as log_file:
                # Pass 1
                pass1_args = common_args + [
                    "-pass",
                    "1",
                    "-an",
                    "-f",
                    "null",
                    os.devnull,
                ]
                process1 = subprocess.Popen(
                    pass1_args, stdout=log_file, stderr=subprocess.STDOUT
                )
                monitor_ffmpeg_progress(
                    process1, total_frames, progress_file_path, "Pass 1/2 Analyzing"
                )
                if process1.wait() != 0:
                    print(f"\n[ERROR] FFmpeg Pass 1 failed. See log: {log_file_path}")
                    return False

                # Pass 2
                pass2_args = common_args + [
                    "-pass",
                    "2",
                    "-c:a",
                    AUDIO_CODEC,
                    output_file,
                ]
                process2 = subprocess.Popen(
                    pass2_args, stdout=log_file, stderr=subprocess.STDOUT
                )
                monitor_ffmpeg_progress(
                    process2, total_frames, progress_file_path, "Pass 2/2 Encoding "
                )
                if process2.wait() != 0:
                    print(f"\n[ERROR] FFmpeg Pass 2 failed. See log: {log_file_path}")
                    return False

            if os.path.exists(log_file_path):
                os.remove(log_file_path)
            return True

        else:  # MODE is "CQ"
            command_to_run = base_args + [
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
            ]
            if preset_config.get("SCALE"):
                command_to_run.extend(["-vf", f"scale_cuda={preset_config['SCALE']}"])
            command_to_run.append(output_file)

            with open(log_file_path, "w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    command_to_run, stdout=log_file, stderr=subprocess.STDOUT
                )

            monitor_ffmpeg_progress(
                process, total_frames, progress_file_path, "Single Pass Encoding"
            )

            if process.wait() != 0:
                print(f"\n[ERROR] FFmpeg process failed. See log: {log_file_path}")
                return False

            if os.path.exists(log_file_path):
                os.remove(log_file_path)
            return True

    except FileNotFoundError:
        print(f"\n[FATAL ERROR] Cannot find ffmpeg. Please check the FFMPEG_PATH.")
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
        print(f"\n--- Processing file {i + 1} of {len(all_files)}: {relative_path} ---")

        is_video = input_path.lower().endswith(VIDEO_EXTENSIONS)
        output_path = (
            os.path.join(root_output, os.path.splitext(relative_path)[0] + ".mp4")
            if is_video
            else os.path.join(root_output, relative_path)
        )

        if os.path.exists(output_path):
            print("Output file already exists. Skipping.")
            continue
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if not is_video:
            print("Not a video file. Copying directly...")
            shutil.copy2(input_path, output_path)
            continue

        codec, bit_rate, total_frames = get_video_details(input_path)
        if total_frames == 0:
            print(
                "Warning: Could not determine total frames. Progress bar may not be accurate."
            )

        bitrate_threshold_kbps = (
            TARGET_BITRATE_KBPS if preset_config["MODE"] == "VBR" else 2500
        )
        if codec == "hevc" and (0 < bit_rate < (bitrate_threshold_kbps * 1000)):
            print(f"Already efficient. Copying...")
            shutil.copy2(input_path, output_path)
            continue

        print(
            f"Compressing (codec: {codec or 'unknown'}, bitrate: {bit_rate/1000:.0f}kbps)..."
        )
        success = compress_video_gpu(
            input_path, output_path, total_frames, preset_config
        )

        if not success:
            print("Copying original due to compression error.")
            if os.path.exists(output_path):
                os.remove(output_path)
            shutil.copy2(input_path, output_path)
            continue

        input_size = os.path.getsize(input_path)
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            print("Output file not created or is empty. Copying original.")
            shutil.copy2(input_path, output_path)
            continue

        output_size = os.path.getsize(output_path)
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

    print("--- GPU H.265 Video Compressor (v17) ---")
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
