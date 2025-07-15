import os
import subprocess
import shutil
import json
import sys
import time
import tempfile

# --- Configuration ---
input_folder = "input"
output_folder = "output"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv")

# --- H.265 (HEVC) Encoding Settings ---
# Constant Rate Factor (CRF). Lower value = better quality, larger file.
# For NVENC, a good range is 20-28.
H265_CRF = 26
# GPU Encoder Preset. p1-p7 (fastest (lowest quality) to slowest (best quality)). 'p6' is a good balance.
GPU_PRESET = "p6"
# Set a threshold. Videos below this bitrate (in bps) will be copied.
BITRATE_THRESHOLD = 2_000_000
# Audio setting. 'copy' is fastest and avoids re-encoding.
AUDIO_CODEC = "copy"


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
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        stream = json.loads(result.stdout)["streams"][0]
        codec = stream.get("codec_name")
        bit_rate = int(stream.get("bit_rate", 0))

        # Get total frames for progress calculation
        total_frames = int(stream.get("nb_frames", 0))
        if total_frames == 0 and "duration" in stream and "avg_frame_rate" in stream:
            duration = float(stream["duration"])
            frame_rate_str = stream["avg_frame_rate"]
            if "/" in frame_rate_str:
                num, den = map(int, frame_rate_str.split("/"))
                if den != 0:
                    frame_rate = num / den
                    total_frames = int(duration * frame_rate)

        return codec, bit_rate, total_frames
    except Exception:
        # If ffprobe fails, return defaults that will trigger a conversion attempt
        return None, 0, 0


def compress_video_gpu(
    input_file, output_file, total_frames, relative_path, crf=26, resize=None
):
    """
    Compresses a video using NVENC and displays real-time progress.
    """
    progress_file_path = ""
    try:
        # Create a temporary file for ffmpeg to write its progress to
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
            "-rc",
            "vbr",
            "-cq",
            str(crf),
            "-b:v",
            "0",
            "-c:a",
            AUDIO_CODEC,
            "-y",
            "-progress",
            progress_file_path,
            "-loglevel",
            "error",
            output_file,
        ]
        if resize:
            width, height = resize
            command.insert(-2, "-vf")
            command.insert(-2, f"scale_cuda={width}:{height}")

        # Start the ffmpeg process
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Monitor the progress file
        while process.poll() is None:
            time.sleep(0.5)
            try:
                with open(progress_file_path, "r") as f:
                    # Go to the end of the file to get the latest progress
                    f.seek(0, os.SEEK_END)
                    if f.tell() == 0:
                        continue  # Skip if file is empty
                    f.seek(
                        max(0, f.tell() - 512)
                    )  # Seek back a bit to read recent lines
                    lines = f.readlines()

                progress_data = {}
                for line in lines[-12:]:  # Only parse the last few lines for speed
                    if "=" in line:
                        key, value = line.strip().split("=", 1)
                        progress_data[key] = value

                if total_frames > 0 and "frame" in progress_data:
                    current_frame = int(progress_data["frame"])
                    percent = (current_frame / total_frames) * 100
                    fps = progress_data.get("fps", "0.0")
                    bitrate = progress_data.get("bitrate", "N/A")

                    # Create the progress string and print it on a single, updating line
                    progress_text = (
                        f"[GPU] [{percent:3.1f}%] "
                        f"Encoding {relative_path} ({fps}fps @ {bitrate})"
                    )
                    sys.stdout.write(f"\r{progress_text}")
                    sys.stdout.flush()

            except (FileNotFoundError, IndexError):
                # Ignore if the progress file isn't ready or is empty
                continue

        # Print a newline to move off the progress line
        sys.stdout.write("\r" + " " * 120 + "\r")  # Clear the line
        sys.stdout.flush()

        if process.returncode != 0:
            print(f"[ERROR] FFmpeg failed on {relative_path}. Copying original.")
            shutil.copy2(input_file, output_file)

    finally:
        # Ensure the temporary progress file is always deleted
        if os.path.exists(progress_file_path):
            os.remove(progress_file_path)


def process_files_recursively(root_input, root_output, crf=26, resize=None):
    """Recursively scans and intelligently processes files one by one."""
    all_files_to_process = [
        os.path.join(dp, f) for dp, dn, fn in os.walk(root_input) for f in fn
    ]
    total_files = len(all_files_to_process)

    for i, input_path in enumerate(all_files_to_process):
        relative_path = os.path.relpath(input_path, root_input)
        output_path = os.path.join(root_output, relative_path)

        print(f"\n--- Processing file {i + 1} of {total_files}: {relative_path} ---")

        if os.path.exists(output_path):
            print("Output file already exists. Skipping.")
            continue

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        if input_path.lower().endswith(VIDEO_EXTENSIONS):
            codec, bit_rate, total_frames = get_video_details(input_path)

            if codec == "hevc" or (0 < bit_rate < BITRATE_THRESHOLD):
                print("Video is already efficient. Copying file directly...")
                shutil.copy2(input_path, output_path)
            else:
                print("Video requires compression. Starting GPU encoder...")
                compress_video_gpu(
                    input_path,
                    output_path,
                    total_frames,
                    relative_path,
                    crf=crf,
                    resize=resize,
                )
        else:
            print("Not a video file. Copying directly...")
            shutil.copy2(input_path, output_path)

        print("Finished processing file.")


if __name__ == "__main__":
    crf_value = H265_CRF
    resize_to = None  # Example: (1920, 1080) or None

    print("--- GPU-Only H.265 Video Encoder ---")
    print(f"Input folder:  '{os.path.abspath(input_folder)}'")
    print(f"Output folder: '{os.path.abspath(output_folder)}'")
    print("-" * 36)

    try:
        process_files_recursively(
            input_folder, output_folder, crf=crf_value, resize=resize_to
        )
        print("\nProcessing complete.")
    except KeyboardInterrupt:
        print("\n\n[!] Keyboard interrupt received. Exiting script.")
        sys.exit(1)
