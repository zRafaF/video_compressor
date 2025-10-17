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
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v")

# --- H.265 (HEVC) Encoding Settings ---
# Constant Rate Factor (CRF). Lower value = better quality, larger file.
# For NVENC, a good range is 20-28.
H265_CRF = 28
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
    Now with better error handling and subtitle stripping.
    """
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
            "-rc",
            "vbr",
            "-cq",
            str(crf),
            "-b:v",
            "0",
            "-c:a",
            AUDIO_CODEC,
            "-sn",  # <-- KEY ADDITION: Strips subtitle streams
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

        # Start the ffmpeg process, now capturing stderr
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        # Monitor progress file (same as before)
        while process.poll() is None:
            time.sleep(0.5)
            try:
                with open(progress_file_path, "r") as f:
                    f.seek(0, os.SEEK_END)
                    if f.tell() == 0:
                        continue
                    f.seek(max(0, f.tell() - 512))
                    lines = f.readlines()

                progress_data = {}
                for line in lines[-12:]:
                    if "=" in line:
                        key, value = line.strip().split("=", 1)
                        progress_data[key] = value

                if total_frames > 0 and "frame" in progress_data:
                    current_frame = int(progress_data["frame"])
                    percent = (current_frame / total_frames) * 100
                    fps = progress_data.get("fps", "0.0")
                    bitrate = progress_data.get("bitrate", "N/A")
                    progress_text = (
                        f"[GPU] [{percent:3.1f}%] "
                        f"Encoding {relative_path} ({fps}fps @ {bitrate})"
                    )
                    sys.stdout.write(f"\r{progress_text}")
                    sys.stdout.flush()
            except (FileNotFoundError, IndexError):
                continue

        sys.stdout.write("\r" + " " * 120 + "\r")
        sys.stdout.flush()

        # Check for errors and print them
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            print(f"[ERROR] FFmpeg failed on {relative_path}.")
            print("--- FFmpeg Error Output ---")
            print(stderr if stderr else "No error output captured.")
            print("---------------------------")
            print("Copying original file instead.")
            shutil.copy2(input_file, output_file)

    finally:
        if os.path.exists(progress_file_path):
            os.remove(progress_file_path)


def process_files_recursively(root_input, root_output, crf=26, resize=None):
    """
    Recursively scans files, changing .m4v to .mp4 on output,
    and preserving all other extensions.
    """
    all_files_to_process = [
        os.path.join(dp, f) for dp, dn, fn in os.walk(root_input) for f in fn
    ]
    total_files = len(all_files_to_process)

    for i, input_path in enumerate(all_files_to_process):
        relative_path = os.path.relpath(input_path, root_input)

        # --- START OF MODIFICATION ---
        # Get the original filename and extension
        relative_path_without_ext, original_ext = os.path.splitext(relative_path)

        # Decide the output extension based on the original
        output_ext = ".mp4" if original_ext.lower() == ".m4v" else original_ext

        # Construct the new output path with the correct extension
        output_relative_path = relative_path_without_ext + output_ext
        output_path = os.path.join(root_output, output_relative_path)
        # --- END OF MODIFICATION ---

        print(f"\n--- Processing file {i + 1} of {total_files}: {relative_path} ---")

        if os.path.exists(output_path):
            print("Output file already exists. Skipping.")
            continue

        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Process video files
        if input_path.lower().endswith(VIDEO_EXTENSIONS):
            codec, bit_rate, total_frames = get_video_details(input_path)

            # This condition handles videos that are already efficient and should just be copied.
            # Even when copying a .m4v, we save it as a more compatible .mp4.
            if codec == "hevc" or (0 < bit_rate < BITRATE_THRESHOLD):
                print(f"Video is already efficient. Copying to {output_path}...")
                shutil.copy2(input_path, output_path)
            else:
                print(f"Video requires compression. Encoding to {output_path}...")
                compress_video_gpu(
                    input_path,
                    output_path,
                    total_frames,
                    relative_path,
                    crf=crf,
                    resize=resize,
                )
        # Directly copy non-video files
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
