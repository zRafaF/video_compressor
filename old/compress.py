import os
import subprocess
from tqdm import tqdm

# Path to the folder with the videos
input_folder = "input"
output_folder = "output"

# Create the output folder if it doesn't exist
os.makedirs(output_folder, exist_ok=True)


def compress_video(input_file, output_file):
    """
    Compress a video using H.265 codec with GPU acceleration (NVIDIA NVENC).
    """
    command = [
        "ffmpeg",
        "-hwaccel",
        "cuda",  # Use GPU acceleration
        "-i",
        input_file,  # Input file
        "-c:v",
        "hevc_nvenc",  # H.265 (HEVC) codec using NVIDIA NVENC
        "-preset",
        "p6",  # Set encoding speed/quality tradeoff (p1-p7, lower is faster)
        "-b:v",
        "2M",  # Bitrate for compression (adjust as needed)
        "-c:a",
        "aac",  # Audio codec
        "-strict",
        "experimental",  # Allow experimental codecs
        "-y",  # Overwrite output file if it exists
        output_file,
    ]

    subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


# Iterate through all video files in the folder
def compress_videos_in_folder(input_folder, output_folder):
    for filename in tqdm(os.listdir(input_folder)):
        if filename.endswith(
            (".mp4", ".mkv", ".avi", ".mov")
        ):  # Add more extensions if needed
            input_path = os.path.join(input_folder, filename)
            output_path = os.path.join(output_folder, filename)

            # Compress the video
            compress_video(input_path, output_path)


if __name__ == "__main__":
    print("Compressing videos...")
    compress_videos_in_folder(input_folder, output_folder)
