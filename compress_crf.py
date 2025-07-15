import os
import subprocess
from tqdm import tqdm

# Path to the folder with the videos
input_folder = "input"
output_folder = "output"

# Create the output folder if it doesn't exist
os.makedirs(output_folder, exist_ok=True)


def compress_video(input_file, output_file, crf=23, resize=None):
    """
    Compress a video using H.265 codec with GPU acceleration (NVIDIA NVENC).

    Parameters:
        input_file (str): Path to the input video file
        output_file (str): Path to save the compressed video
        crf (int): Constant Rate Factor for quality (lower = better quality)
        resize (tuple): Optional (width, height) to resize video
    """

    command = [
        "ffmpeg",
        "-hwaccel",
        "cuda",
        "-i",
        input_file,
        "-c:v",
        "hevc_nvenc",
        "-preset",
        "p6",
        "-rc",
        "vbr",  # Rate control mode: variable bitrate
        "-cq",
        str(crf),  # Constant quality factor for NVENC
        "-b:v",
        "0",  # Let encoder choose bitrate
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",  # Optimize MP4 for streaming
        "-y",
        output_file,
    ]

    if resize:
        width, height = resize
        command.insert(-2, "-vf")
        command.insert(-2, f"scale={width}:{height}")

    # Run the command
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )

    if result.returncode != 0:
        print(f"Error compressing {input_file}:\n{result.stderr}")
    else:
        print(f"Compressed successfully: {output_file}")


def compress_videos_in_folder(input_folder, output_folder, crf=23, resize=None):
    """
    Compress all videos in the input folder.
    """
    video_extensions = (".mp4", ".mkv", ".avi", ".mov")

    videos = [
        f for f in os.listdir(input_folder) if f.lower().endswith(video_extensions)
    ]

    for filename in tqdm(videos, desc="Compressing videos"):
        input_path = os.path.join(input_folder, filename)
        output_path = os.path.join(output_folder, filename)

        compress_video(input_path, output_path, crf=crf, resize=resize)


if __name__ == "__main__":
    # Example usage:
    crf_value = 20  # Lower = better quality, typical range 18–28
    resize_to = None  # or None to keep original resolution

    compress_videos_in_folder(
        input_folder, output_folder, crf=crf_value, resize=resize_to
    )
