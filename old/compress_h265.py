import os
import subprocess
import shutil
import json
import multiprocessing
import time
import sys
import tempfile

# --- Main Configuration ---
# Set the number of CPU cores you want to use for encoding.
# The script will use these IN ADDITION to the GPU, if available.
CPU_WORKERS = 1

# --- H.265 (HEVC) Encoding Settings ---
H265_CRF = 25
CPU_PRESET = "medium"
GPU_PRESET = "p6"
BITRATE_THRESHOLD = 2_000_000
AUDIO_CODEC = "copy"

# --- Script Behavior ---
input_folder = "input"
output_folder = "output"
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv")


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
        # Total frames can be in 'nb_frames' or calculated from duration/avg_frame_rate
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
        return None, 0, 0


def update_progress_line(line_num, text):
    """Updates a specific line in the console using ANSI escape codes."""
    sys.stdout.write(f"\x1b[{line_num};0H")  # Move cursor to line
    sys.stdout.write(f"\x1b[2K")  # Clear entire line
    sys.stdout.write(text)
    sys.stdout.flush()


def compress_video_h265(input_file, output_file, total_frames, use_gpu, slot):
    """Compresses a video while monitoring and displaying real-time progress."""
    progress_file_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, mode="w+", suffix=".txt") as tmp:
            progress_file_path = tmp.name

        if use_gpu:
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
                str(H265_CRF),
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
        else:
            command = [
                "ffmpeg",
                "-i",
                input_file,
                "-c:v",
                "libx265",
                "-crf",
                str(H265_CRF),
                "-preset",
                CPU_PRESET,
                "-c:a",
                AUDIO_CODEC,
                "-y",
                "-progress",
                progress_file_path,
                "-loglevel",
                "error",
                output_file,
            ]

        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Monitor progress
        while process.poll() is None:
            time.sleep(0.5)
            try:
                with open(progress_file_path, "r") as f:
                    lines = f.readlines()
                progress_data = {}
                for line in lines:
                    if "=" in line:
                        key, value = line.strip().split("=", 1)
                        progress_data[key] = value

                if total_frames > 0 and "frame" in progress_data:
                    current_frame = int(progress_data["frame"])
                    percent = (current_frame / total_frames) * 100
                    fps = progress_data.get("fps", "0.0")
                    bitrate = progress_data.get("bitrate", "N/A")
                    worker_type = "GPU" if use_gpu else "CPU"

                    progress_text = (
                        f"[{worker_type}][{percent:3.1f}%] "
                        f"Encoding {os.path.basename(input_file)} "
                        f"({fps}fps, {bitrate})"
                    )
                    update_progress_line(slot, progress_text)

            except FileNotFoundError:
                continue  # Progress file might not be created yet
            except Exception:
                continue  # Ignore parsing errors

        if process.returncode != 0:
            # Fallback copy if ffmpeg fails
            shutil.copy2(input_file, output_file)

    finally:
        if os.path.exists(progress_file_path):
            os.remove(progress_file_path)
        # Clear the progress line when done
        update_progress_line(slot, "")


def init_worker(lock, num_slots):
    """Initializer for each worker process."""
    global print_lock, total_slots
    print_lock = lock
    total_slots = num_slots
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)


def process_single_file(args):
    """Worker function to process one file."""
    input_path, use_gpu, slot = args
    pid = os.getpid()
    worker_type = "GPU" if use_gpu else "CPU"
    filename = os.path.basename(input_path)

    relative_path = os.path.relpath(input_path, input_folder)
    output_path = os.path.join(output_folder, relative_path)

    if os.path.exists(output_path):
        return "skipped"

    log_message = f"[{worker_type} PID: {pid}] Starting: {filename}"
    with print_lock:
        sys.stdout.write(f"\x1b[{total_slots + 2};0H{log_message}\n")

    start_time = time.time()
    result_status = "error"

    if input_path.lower().endswith(VIDEO_EXTENSIONS):
        codec, bit_rate, total_frames = get_video_details(input_path)
        if codec == "hevc" or (0 < bit_rate < BITRATE_THRESHOLD):
            try:
                shutil.copy2(input_path, output_path)
                result_status = "copied_video"
            except Exception:
                pass
        else:
            compress_video_h265(input_path, output_path, total_frames, use_gpu, slot)
            result_status = "compressed"
    else:
        try:
            shutil.copy2(input_path, output_path)
            result_status = "copied_other"
        except Exception:
            pass

    end_time = time.time()
    duration = end_time - start_time

    log_message = f"[{worker_type} PID: {pid}] Finished: {filename} ({result_status}) in {duration:.2f}s"
    with print_lock:
        sys.stdout.write(f"\x1b[{total_slots + 2};0H{log_message}\n")

    return result_status


def check_ffmpeg_encoders():
    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"], capture_output=True, text=True, check=True
        )
        return "hevc_nvenc" in result.stdout, "libx265" in result.stdout
    except FileNotFoundError:
        return False, False


if __name__ == "__main__":
    has_gpu_encoder, has_cpu_encoder = check_ffmpeg_encoders()

    cpu_worker_count = CPU_WORKERS if has_cpu_encoder else 0
    gpu_worker_count = 1 if has_gpu_encoder else 0

    if gpu_worker_count == 0 and cpu_worker_count == 0:
        print(
            "[FATAL] No suitable encoders found (hevc_nvenc for GPU, libx265 for CPU). Exiting."
        )
        sys.exit(1)

    all_files = [
        os.path.join(dp, f) for dp, dn, fn in os.walk(input_folder) for f in fn
    ]

    # Create separate GPU and CPU task lists with different slots

    gpu_tasks = []
    cpu_tasks = []

    # GPU slots will be 1..gpu_worker_count
    # CPU slots will be gpu_worker_count+1 .. total_slots

    gpu_slot = 1
    cpu_slot = gpu_worker_count + 1

    worker_configs = []
    if has_gpu_encoder:
        worker_configs.append(True)
    for _ in range(cpu_worker_count):
        worker_configs.append(False)

    # Distribute files round-robin between GPU and CPU workers
    for i, file_path in enumerate(all_files):
        use_gpu = worker_configs[i % len(worker_configs)]
        if use_gpu:
            gpu_tasks.append((file_path, True, gpu_slot))
        else:
            cpu_tasks.append((file_path, False, cpu_slot))
            cpu_slot += 1
            if cpu_slot > gpu_worker_count + cpu_worker_count:
                cpu_slot = gpu_worker_count + 1

    num_processes = gpu_worker_count + cpu_worker_count

    sys.stdout.write("\x1b[2J\x1b[H")
    print("--- H.265 (HEVC) Parallel Video Encoder ---")
    print(f"Found {len(all_files)} files to process.")
    print(
        f"Starting {num_processes} parallel workers ({gpu_worker_count} GPU, {cpu_worker_count} CPU)."
    )
    print("Press Ctrl+C to terminate all processes.")
    print("-" * 42)
    for i in range(num_processes + 2):
        print("")

    lock = multiprocessing.Manager().Lock()
    multiprocessing.set_start_method("spawn", force=True)

    # Make globals available for main-process GPU calls
    print_lock = lock
    total_slots = num_processes

    results = []

    pool = None
    try:
        # Start CPU pool
        if cpu_worker_count > 0:
            pool = multiprocessing.Pool(
                processes=cpu_worker_count,
                initializer=init_worker,
                initargs=(lock, num_processes),
            )
            cpu_results_iter = pool.imap_unordered(
                process_single_file,
                cpu_tasks,
            )
        else:
            cpu_results_iter = []

        # Run GPU tasks serially
        if gpu_worker_count > 0:
            for task in gpu_tasks:
                result = process_single_file(task)
                results.append(result)

        for res in cpu_results_iter:
            results.append(res)

        if pool:
            pool.close()
            pool.join()

    except KeyboardInterrupt:
        sys.stdout.write(f"\x1b[{num_processes + 3};0H")
        print(
            "\n\n[!] Keyboard interrupt received. Terminating all worker processes...\n"
        )
        if pool:
            pool.terminate()
            pool.join()
        sys.exit(1)

    sys.stdout.write(f"\x1b[{num_processes + 3};0H")
    print("\nProcessing complete.")
    print("\n--- Summary ---")
    print(f"Compressed: {results.count('compressed')}")
    print(f"Copied (Efficient Video): {results.count('copied_video')}")
    print(f"Copied (Non-Video): {results.count('copied_other')}")
    print(f"Skipped (Already Exists): {results.count('skipped')}")
    print(f"Errors: {results.count('error')}")
