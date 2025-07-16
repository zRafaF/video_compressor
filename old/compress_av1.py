import os
import subprocess
import shutil
import json
from tqdm import tqdm

# --- Main Configuration ---
# Adjust these paths and settings to fit your needs.
input_folder = "input"
output_folder = "output"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv")

# --- AV1 Encoding Settings ---
# Constant Rate Factor (CRF). Higher value = smaller file, lower quality.
# For AV1, a good range is 25-45. A good starting point is 32.
AV1_CRF = 32

# CPU Encoder Preset. Controls the speed vs. compression efficiency.
# Range is 0-12. Slower presets (lower numbers) give better compression.
# A good balance is 7 or 8. Use 10-12 for faster, lower-quality encodes.
CPU_PRESET = "8"

# GPU Encoder Preset (for NVIDIA NVENC). p1-p7 (slowest to fastest).
# 'p6' is a good balance of speed and quality.
GPU_PRESET = "p6"

# Set a bitrate threshold (in bits per second). Videos below this bitrate
# will be copied, not re-encoded, to prevent them from getting larger.
# 1,500,000 bps = 1.5 Mbps.
BITRATE_THRESHOLD = 1_500_000

# Audio setting. 'copy' is fastest. 'libopus' is a modern, efficient codec
# that pairs well with AV1 but requires re-encoding.
AUDIO_CODEC = "copy"  # or "libopus"


def check_ffmpeg_encoders():
    """Checks which AV1 encoders are available in FFmpeg."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"], capture_output=True, text=True, check=True
        )
        output = result.stdout
        has_av1_nvenc = "av1_nvenc" in output
        has_libsvtav1 = "libsvtav1" in output
        return has_av1_nvenc, has_libsvtav1
    except FileNotFoundError:
        print(
            "[ERROR] FFmpeg not found. Please ensure it's installed and in your system's PATH."
        )
        return False, False
    except Exception as e:
        print(f"[ERROR] Could not check FFmpeg encoders: {e}")
        return False, False


def get_video_info(file_path):
    """Uses ffprobe to get the video codec and bitrate."""
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
        return codec, bit_rate
    except Exception:
        return None, 0  # Return defaults if ffprobe fails


def compress_video_av1(input_file, output_file, use_gpu=False):
    """
    Compresses a video to AV1 using either GPU (NVENC) or CPU (SVT-AV1).
    """
    if use_gpu:
        # Command for NVIDIA NVENC hardware encoding
        command = [
            "ffmpeg",
            "-hwaccel",
            "cuda",
            "-i",
            input_file,
            "-c:v",
            "av1_nvenc",
            "-preset",
            GPU_PRESET,
            "-rc",
            "vbr",
            "-cq",
            str(AV1_CRF),
            "-b:v",
            "0",
            "-c:a",
            AUDIO_CODEC,
            "-y",
            output_file,
        ]
    else:
        # Command for CPU-based encoding with SVT-AV1
        command = [
            "ffmpeg",
            "-i",
            input_file,
            "-c:v",
            "libsvtav1",
            "-crf",
            str(AV1_CRF),
            "-preset",
            CPU_PRESET,
            "-svtav1-params",
            "tune=0",  # Tune for visual quality over PSNR
            "-c:a",
            AUDIO_CODEC,
            "-y",
            output_file,
        ]

    # Run the command, capturing output to hide it unless there's an error
    result = subprocess.run(command, capture_output=True, text=True)

    # If conversion fails, print the error and copy the original file
    if result.returncode != 0:
        print(
            f"\n[ERROR] Failed to compress {os.path.basename(input_file)}:\n{result.stderr.strip()}"
        )
        try:
            shutil.copy2(input_file, output_file)
            print("        Copied original file as a fallback.")
        except Exception as e:
            print(f"        [ERROR] Failed to copy fallback file: {e}")


def process_files_recursively(root_input, root_output, use_gpu):
    """
    Recursively scans the input directory, compresses videos to AV1, and
    copies all other files, maintaining the original directory structure.
    """
    # First, find all files to get an accurate total for the progress bar
    all_files_to_process = [
        os.path.join(dp, f) for dp, dn, fn in os.walk(root_input) for f in fn
    ]

    # Process each file with a tqdm progress bar
    for input_path in tqdm(
        all_files_to_process, desc="Processing files", unit="file", ncols=100
    ):
        relative_path = os.path.relpath(input_path, root_input)
        output_path = os.path.join(root_output, relative_path)

        # Ensure the output directory for the current file exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Check if the file is a video
        if input_path.lower().endswith(VIDEO_EXTENSIONS):
            codec, bit_rate = get_video_info(input_path)

            # --- Intelligent Decision Logic ---
            # Condition 1: Is the video already AV1?
            # Condition 2: Is the bitrate too low to be worth re-encoding?
            if codec == "av1" or (0 < bit_rate < BITRATE_THRESHOLD):
                tqdm.write(
                    f"Copying (already efficient): {os.path.basename(input_path)}"
                )
                try:
                    shutil.copy2(input_path, output_path)
                except Exception as e:
                    print(f"\n[ERROR] Could not copy {input_path}: {e}")
            else:
                # If conditions are not met, it's worth compressing
                compress_video_av1(input_path, output_path, use_gpu=use_gpu)
        else:
            # It's not a video file, so just copy it directly
            try:
                shutil.copy2(input_path, output_path)
            except Exception as e:
                print(f"\n[ERROR] Could not copy {input_path}: {e}")


if __name__ == "__main__":
    # Check for available encoders at the start
    can_use_gpu, can_use_cpu = check_ffmpeg_encoders()
    encoder_to_use = "None"

    if can_use_gpu:
        encoder_to_use = "GPU (av1_nvenc)"
        use_gpu_flag = True
    elif can_use_cpu:
        encoder_to_use = "CPU (libsvtav1)"
        use_gpu_flag = False
    else:
        print(
            "[FATAL] No suitable AV1 encoder found in FFmpeg (av1_nvenc or libsvtav1)."
        )
        print("Please ensure your FFmpeg build includes one of these encoders.")
        exit()

    print("--- AV1 Video Encoder ---")
    print(f"Input folder:  '{os.path.abspath(input_folder)}'")
    print(f"Output folder: '{os.path.abspath(output_folder)}'")
    print(f"Using Encoder: {encoder_to_use}")
    print(f"CRF Quality:   {AV1_CRF} (Higher is smaller file)")
    print("-" * 25)

    process_files_recursively(input_folder, output_folder, use_gpu=use_gpu_flag)

    print("\n✅ Processing complete.")
