"""Real-ESRGAN and video export shared by the Gradio app and Nuke worker."""

import json
import subprocess
import time
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
import torch
from spandrel import ModelLoader

ROOT = Path(__file__).resolve().parents[1]
MODEL = None
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def probe(path, ffprobe="ffprobe"):
    data = subprocess.run([str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, text=True, check=True, creationflags=NO_WINDOW)
    info = json.loads(data.stdout)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    fps = Fraction(video.get("avg_frame_rate", "0/1"))
    if fps <= 0:
        fps = Fraction(video.get("r_frame_rate", "25/1"))
    return video, fps, float(info["format"].get("duration", 0))


def load_model():
    global MODEL
    if MODEL is None:
        if not torch.cuda.is_available():
            raise RuntimeError("Chế độ AI cần GPU CUDA. Hãy chọn Lanczos nếu không có GPU.")
        path = ROOT / "models/upscale/RealESRGAN_x4plus.pth"
        if not path.exists():
            raise RuntimeError("Thiếu model models/upscale/RealESRGAN_x4plus.pth")
        MODEL = ModelLoader().load_from_file(path).eval().cuda().half()
    return MODEL


@torch.inference_mode()
def enhance(frame, tile, cancel_event=None):
    model = load_model()
    height, width = frame.shape[:2]
    result = np.empty((height * 4, width * 4, 3), dtype=np.uint8)
    # Overlapping context prevents a hard boundary between independent tiles.
    pad = 32
    for y in range(0, height, tile):
        for x in range(0, width, tile):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Đã dừng xử lý.")
            x1, y1 = max(0, x-pad), max(0, y-pad)
            x2, y2 = min(width, x+tile+pad), min(height, y+tile+pad)
            rgb = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
            tensor = torch.from_numpy(rgb.copy()).permute(2, 0, 1).unsqueeze(0).cuda().half() / 255
            pred = model(tensor).clamp_(0, 1)[0].permute(1, 2, 0).float().cpu().numpy()
            pred = cv2.cvtColor((pred * 255).round().astype(np.uint8), cv2.COLOR_RGB2BGR)
            th, tw = min(tile, height-y), min(tile, width-x)
            result[y*4:(y+th)*4, x*4:(x+tw)*4] = pred[(y-y1)*4:(y-y1+th)*4, (x-x1)*4:(x-x1+tw)*4]
    return result


def fit_frame(frame, width, height, pixel_aspect=1, color_reference=None):
    h, w = frame.shape[:2]
    w *= pixel_aspect
    ratio = min(width/w, height/h)
    nw, nh = max(1, round(w*ratio)), max(1, round(h*ratio))
    resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LANCZOS4)
    if color_reference is not None:
        # Correct broad RGB shifts from inference while retaining the AI's fine detail.
        ref_h, ref_w = color_reference.shape[:2]
        grid = (max(1, min(nw, ref_w // 8)), max(1, min(nh, ref_h // 8)))
        reference_color = cv2.resize(color_reference, grid, interpolation=cv2.INTER_AREA)
        enhanced_color = cv2.resize(resized, grid, interpolation=cv2.INTER_AREA)
        correction = cv2.subtract(reference_color, enhanced_color, dtype=cv2.CV_16S)
        correction = cv2.resize(correction, (nw, nh), interpolation=cv2.INTER_LINEAR)
        resized = cv2.add(resized, correction, dtype=cv2.CV_8U)
    canvas = np.zeros((height, width, 3), np.uint8)
    x, y = (width-nw)//2, (height-nh)//2
    canvas[y:y+nh, x:x+nw] = resized
    return canvas


def process_video(source, target, mode, orientation, quality, seconds, tile, progress, cancel_event,
                  ffmpeg="ffmpeg", ffprobe="ffprobe", sequence_fps=None, pixel_aspect=None, srgb=False):
    cap = None
    if sequence_fps:
        fps = Fraction(sequence_fps)
        paths = sorted(Path(source).glob("frame.*.png"), key=lambda path: int(path.stem.split(".")[-1]))
        duration = len(paths) / float(fps)
        frames = iter(paths)

        def read_frame():
            path = next(frames, None)
            if path is None:
                return False, None
            frame = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise RuntimeError(f"Could not decode rendered frame: {path.name}")
            return True, frame
    else:
        video, fps, duration = probe(source, ffprobe)
        if video.get("duration") not in (None, "N/A"):
            duration = float(video["duration"])
        sar = video.get("sample_aspect_ratio", "1:1")
        if pixel_aspect is None and sar not in ("N/A", "0:1"):
            pixel_aspect = float(Fraction(sar.replace(":", "/")))
        cap = cv2.VideoCapture(source)
        read_frame = cap.read
    pixel_aspect = pixel_aspect or 1
    encoder = None
    partial = None
    started = time.monotonic()
    try:
        ok, frame = read_frame()
        if not ok:
            raise RuntimeError("Không giải mã được khung hình đầu tiên.")
        h, w = frame.shape[:2]
        display_width = w * pixel_aspect
        if orientation == "auto":
            scale = 3840 / max(display_width, h)
            width = max(2, 2 * round(display_width * scale / 2))
            height = max(2, 2 * round(h * scale / 2))
        else:
            width, height = (2160, 3840) if orientation == "portrait" else (3840, 2160)
        seconds = int(seconds)
        estimate = min(duration, seconds) if seconds else duration
        limit = max(1, round(seconds*float(fps))) if seconds else None
        total = max(1, round(estimate*float(fps)))
        if mode == "ai":
            progress(0, desc="Nạp model Real-ESRGAN…")
            load_model()
        target.parent.mkdir(parents=True, exist_ok=True)
        job_id = uuid4().hex[:12]
        partial = target.with_suffix(f".{job_id}.partial.mp4")
        log_path = target.with_suffix(f".{job_id}.log")
        # Read frames at the source's average FPS; retain audio from the source.
        # VFR inputs are normalized to this constant FPS by OpenCV decoding.
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0",
        ]
        if not sequence_fps:
            command.extend(["-i", source, "-map", "0:v:0", "-map", "1:a:0?",
                            "-c:a", "aac", "-b:a", "192k", "-af", "apad", "-shortest"])
        command.extend(["-vf", "scale=out_color_matrix=bt709:out_range=tv",
                        "-colorspace", "bt709", "-color_range", "tv"])
        if srgb:
            command.extend(["-color_primaries", "bt709", "-color_trc", "iec61966-2-1"])
        command.extend(["-c:v", "libx264", "-preset", "fast", "-crf", str(int(quality)),
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(partial)])
        count = 0
        with log_path.open("w", encoding="utf-8") as log:
            encoder = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log, creationflags=NO_WINDOW)
            while ok:
                if cancel_event.is_set():
                    raise InterruptedError("Đã dừng xử lý.")
                reference = None
                if mode == "ai":
                    reference = frame
                    frame = enhance(frame, int(tile), cancel_event)
                output = fit_frame(frame, width, height, pixel_aspect, reference)
                encoder.stdin.write(output.tobytes())
                count += 1
                elapsed = time.monotonic()-started
                progress(min(count/total, .99), desc=f"{count}/{total} khung hình · {count/max(elapsed,.01):.2f} FPS xử lý")
                if limit and count >= limit:
                    break
                ok, frame = read_frame()
            encoder.stdin.close()
            if encoder.wait() != 0:
                raise RuntimeError(f"FFmpeg thất bại. Xem {log_path}")
        if not limit and duration and count < total * .95:
            raise RuntimeError("Video bị ngắt khi giải mã; chưa xuất bản file kết quả.")
        result, _, _ = probe(partial, ffprobe)
        if (result["width"], result["height"]) != (width, height):
            raise RuntimeError("Kích thước đầu ra không hợp lệ.")
        if result.get("nb_frames") not in (None, "N/A") and int(result["nb_frames"]) != count:
            raise RuntimeError("Số khung hình đầu ra không khớp.")
        if cancel_event.is_set():
            raise InterruptedError("Đã dừng xử lý.")
        partial.rename(target)
        return f"Hoàn tất · {width} × {height} · {count} khung hình · {time.monotonic()-started:.1f} giây"
    finally:
        if cap is not None:
            cap.release()
        if encoder is not None and encoder.poll() is None:
            encoder.kill()
            encoder.wait()
        if partial and partial.exists():
            partial.unlink()
