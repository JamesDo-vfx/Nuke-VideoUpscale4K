"""Run one video job in the bundled Python, outside Nuke."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


class CancelFile:
    def __init__(self, path):
        self.path = path

    def is_set(self):
        return self.path.exists()


def progress(value, desc):
    print(json.dumps({"progress": int(value * 100), "message": desc}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--mode", choices=("ai", "lanczos"), default="ai")
    parser.add_argument("--orientation", choices=("auto", "landscape", "portrait"), default="auto")
    parser.add_argument("--quality", type=int, choices=range(15, 29), default=18)
    parser.add_argument("--seconds", type=int, choices=(0, 5, 10), default=0)
    parser.add_argument("--tile", type=int, choices=(128, 256, 384), default=128)
    parser.add_argument("--cancel-file", type=Path, required=True)
    parser.add_argument("--sequence-fps")
    parser.add_argument("--pixel-aspect", type=float)
    parser.add_argument("--srgb", action="store_true")
    args = parser.parse_args()
    try:
        progress(0, "Starting bundled runtime...")
        from app.video_upscale_core import process_video

        cancel = CancelFile(args.cancel_file)
        if cancel.is_set():
            return 2
        valid_source = args.source.is_dir() if args.sequence_fps else args.source.is_file()
        if not valid_source or args.target.suffix.lower() != ".mp4" or args.target.exists():
            raise ValueError("Choose an existing video and a new MP4 output path.")
        status = process_video(
            str(args.source), args.target, args.mode, args.orientation, args.quality, args.seconds,
            args.tile, progress, cancel, ROOT / "bin/ffmpeg.exe", ROOT / "bin/ffprobe.exe",
            sequence_fps=args.sequence_fps, pixel_aspect=args.pixel_aspect, srgb=args.srgb,
        )
        progress(1, status)
    except InterruptedError:
        return 2
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
