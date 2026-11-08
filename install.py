"""Install the complete plugin into the current user's Nuke plug-in folder."""

import os
import re
import shutil
import tempfile
from pathlib import Path


FILES = ("Install.bat", "install.py", "menu.py", "nuke_video_upscale_node.py", "worker.py", "README.md")
PLUGIN_FOLDER = "VideoUpscale4k"
DIRECTORIES = ("app", "bin", "models", "runtime")
REQUIRED = FILES + (
    "app/video_upscale_core.py", "runtime/python.exe", "bin/ffmpeg.exe", "bin/ffprobe.exe",
    "models/upscale/RealESRGAN_x4plus.pth",
)
REGISTRATION_BLOCK = re.compile(
    rb"(?m)^# Nuke Video Upscale 4K\r?\nimport nuke\r?\n"
    rb"nuke\.pluginAddPath\([^\r\n]*\)\r?(?:\n|\Z)"
)


def register(startup):
    original = startup.read_bytes() if startup.exists() else b""
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    block = newline.join((
        b"# Nuke Video Upscale 4K",
        b"import nuke",
        f"nuke.pluginAddPath('./{PLUGIN_FOLDER}')".encode("ascii"),
        b"",
    ))
    matches = list(REGISTRATION_BLOCK.finditer(original))
    if len(matches) == 1 and matches[0].group() == block:
        return
    updated = REGISTRATION_BLOCK.sub(b"", original)
    if updated and not updated.endswith(newline):
        updated += newline
    updated += block
    temp = tempfile.NamedTemporaryFile(dir=startup.parent, delete=False)
    path = Path(temp.name)
    try:
        with temp:
            temp.write(updated)
        os.replace(path, startup)
    finally:
        path.unlink(missing_ok=True)


def install(source, nuke_folder):
    for filename in REQUIRED:
        if not (source / filename).is_file():
            raise RuntimeError(f"Package is incomplete: {filename}")

    nuke_folder.mkdir(parents=True, exist_ok=True)
    destination = nuke_folder / PLUGIN_FOLDER
    startup = nuke_folder / "init.py"
    if source == destination.resolve():
        register(startup)
        return destination

    if destination.exists() and not all((destination / name).is_file() for name in ("install.py", "menu.py", "worker.py")):
        raise RuntimeError(f"An existing folder is not a recognized installation: {destination}")

    with tempfile.TemporaryDirectory(prefix="video-upscale-install-", dir=nuke_folder) as temp:
        staged = Path(temp) / "package"
        staged.mkdir()
        for name in FILES:
            shutil.copy2(source / name, staged / name)
        for name in DIRECTORIES:
            shutil.copytree(source / name, staged / name, ignore=shutil.ignore_patterns("__pycache__"))

        previous = Path(temp) / "previous"
        if destination.exists():
            destination.rename(previous)
        try:
            staged.rename(destination)
            register(startup)
        except OSError:
            if destination.exists():
                destination.rename(staged)
            if previous.exists():
                previous.rename(destination)
            raise
    return destination


if __name__ == "__main__":
    try:
        destination = install(Path(__file__).resolve().parent, Path.home() / ".nuke")
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"Installation failed: {exc}") from None
    print(f"Installed to: {destination}")
    print("Restart Nuke and search for Video Upscale 4K in the Tab menu.")
