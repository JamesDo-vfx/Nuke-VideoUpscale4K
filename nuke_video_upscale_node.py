"""A Nuke video upscale Group backed by the bundled video worker."""

import json
import os
import queue
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path

import nuke

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / "runtime" / "python.exe"
ACTIVE = False
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv", ".ts"}
SRGB_SPACES = {"srgb", "srgb - texture", "utility - srgb - texture", "srgb encoded rec.709 (srgb)"}


def file_colorspace(knob):
    value = knob.value()
    if value != "default":
        return value
    for choice in knob.values():
        if choice.startswith("default (") and choice.endswith(")"):
            return choice[len("default ("):-1]
    return None


def render_input(node, input_node, folder, seconds, current_frame, source):
    root = nuke.root()
    first, last = int(root["first_frame"].value()), int(root["last_frame"].value())
    fps = root["fps"].value()
    if current_frame is not None:
        first = last = current_frame
    elif seconds:
        last = min(last, first + max(1, round(seconds * fps)) - 1)
    with node:
        reader = writer = None
        try:
            if input_node is None:
                reader = nuke.nodes.Read()
                reader["file"].fromUserText(source.as_posix())
            writer = nuke.nodes.Write(inputs=[reader if reader is not None else node.node("SourceVideo")])
            writer["file_type"].setValue("png")
            writer["file"].setValue((folder / "frame.%08d.png").as_posix())
            writer["channels"].setValue("rgb")
            colorspace = writer["colorspace"]
            for choice in colorspace.values():
                name = choice.split("\t", 1)[0]
                if name.lower() in SRGB_SPACES:
                    colorspace.setValue(name)
                    break
            encoded_space = file_colorspace(colorspace)
            nuke.execute(writer, first, last)
            pixel_aspect = (reader if reader is not None else input_node).pixelAspect()
        finally:
            if writer is not None:
                nuke.delete(writer)
            if reader is not None:
                nuke.delete(reader)
    options = ["--sequence-fps", str(fps), "--pixel-aspect", str(pixel_aspect)]
    if encoded_space and encoded_space.lower() in SRGB_SPACES:
        options.append("--srgb")
    return first, options, encoded_space


def add_cache_buttons(node):
    if node.knob("upscale") is None:
        node.addKnob(nuke.PyScript_Knob(
            "upscale", "Cache Video Sequence",
            "import nuke_video_upscale_node as upscale; upscale.start(nuke.thisNode())",
        ))
    else:
        node["upscale"].setLabel("Cache Video Sequence")
    if node.knob("cache_current_frame") is None:
        node.addKnob(nuke.PyScript_Knob(
            "cache_current_frame", "Cache Current Frame",
            "import nuke_video_upscale_node as upscale; upscale.start(nuke.thisNode(), single_frame=True)",
        ))


def input_changed(node):
    connected = node.input(0) is not None
    node["source_video"].setVisible(not connected)
    node["target_video"].setVisible(not connected)


def create_node():
    selected = nuke.selectedNodes()
    node = nuke.createNode("Group", inpanel=False)
    node.setName("VideoUpscale4K")
    node.addKnob(nuke.Tab_Knob("video_upscale", "Video Upscale 4K"))
    node.addKnob(nuke.File_Knob("source_video", "Source video"))
    node["source_video"].setTooltip("Used when the SourceVideo input is disconnected.")
    node.addKnob(nuke.File_Knob("target_video", "Output MP4"))
    node["target_video"].setTooltip("Leave empty to cache in 'output upscale 4k' beside the Nuke script.")
    for name, label, values in (
        ("method", "Method", ["Real-ESRGAN", "Lanczos"]),
        ("orientation", "Orientation", ["Auto", "Landscape", "Portrait"]),
        ("video_range", "Range", ["Full", "First 5s", "First 10s"]),
        ("tile", "AI tile", ["128", "256", "384"]),
        ("quality", "H.264 CRF", ["18", "15", "21", "24", "28"]),
    ):
        node.addKnob(nuke.Enumeration_Knob(name, label, values))
    node.addKnob(nuke.String_Knob("result_video", "Last result"))
    node["result_video"].setEnabled(False)
    add_cache_buttons(node)

    with node:
        source = nuke.nodes.Input(name="SourceVideo")
        result = nuke.nodes.Read(name="UpscaledVideo")
        preview = nuke.nodes.Switch(name="VideoOutput", inputs=[source, result], which=0)
        nuke.nodes.Output(name="OutputVideo", inputs=[preview])
    node["knobChanged"].setValue(
        "if nuke.thisKnob().name() == 'inputChange':\n"
        "    import nuke_video_upscale_node as upscale\n"
        "    upscale.input_changed(nuke.thisNode())"
    )
    if len(selected) == 1 and node.canSetInput(0, selected[0]):
        node.setInput(0, selected[0])
    return node


def start(node, single_frame=False):
    global ACTIVE
    if ACTIVE:
        nuke.message("A video upscale is already running.")
        return
    if not PYTHON.is_file():
        nuke.message("The bundled runtime is missing. Install the complete VideoUpscale4k package.")
        return

    input_node = node.input(0) if node.maxInputs() else None
    if input_node is None:
        source_path = node["source_video"].evaluate()
    else:
        source_path = nuke.filename(input_node) if input_node.Class() == "Read" else ""
    source = Path((source_path or "").strip().strip('"')).resolve()
    graph_input = input_node is not None and (source.suffix.lower() not in VIDEO_EXTENSIONS or not source.is_file())
    frame = int(nuke.root()["frame"].value()) if single_frame else None
    if not graph_input and not source.is_file():
        nuke.message("Select an existing video file.")
        return
    output = "" if input_node else node["target_video"].evaluate().strip().strip('"')
    if output:
        target = Path(output).resolve()
    else:
        script = Path(nuke.root().name())
        if not script.is_absolute():
            nuke.message("Save the Nuke script before creating the 4K video cache.")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        name = input_node.name() if graph_input else source.stem
        if single_frame:
            name = f"{name}_frame{frame}"
        target = script.parent / "output upscale 4k" / f"{name}_4K_{stamp}.mp4"
    if target.suffix.lower() != ".mp4" or target.exists():
        nuke.message("Choose a new output path ending in .mp4.")
        return

    seconds = {"Full": 0, "First 5s": 5, "First 10s": 10}[node["video_range"].value()]
    if single_frame:
        seconds = 0
    options = [
        "--mode", "ai" if node["method"].value() == "Real-ESRGAN" else "lanczos",
        "--orientation", node["orientation"].value().lower(),
        "--seconds", str(seconds),
        "--tile", node["tile"].value(),
        "--quality", node["quality"].value(),
    ]
    ACTIVE = True
    staging = None
    first = None
    try:
        colorspace = file_colorspace(input_node["colorspace"]) if input_node is not None and not graph_input else None
        if graph_input or single_frame:
            staging = tempfile.TemporaryDirectory(prefix="nuke-upscale-frames-")
            first, sequence_options, colorspace = render_input(node, input_node, Path(staging.name), seconds, frame, source)
            source = Path(staging.name)
            options.extend(sequence_options)
        threading.Thread(
            target=run_job, args=(node, source, target, options, input_node, first, staging, colorspace), daemon=True,
        ).start()
    except Exception as exc:
        if staging is not None:
            staging.cleanup()
        nuke.message(f"Could not render the input for upscale:\n{exc}")
        clear_active()


def read_messages(stream, messages):
    for line in stream:
        messages.put(line.rstrip())
    messages.put(None)


def run_job(node, source, target, options, input_node, first, staging, colorspace):
    task = nuke.ProgressTask("Upscale Video to 4K")
    task.setMessage("Starting bundled runtime...")
    try:
        with tempfile.TemporaryDirectory(prefix="nuke-upscale-") as folder:
            cancel = Path(folder) / "cancel"
            command = [
                str(PYTHON), "-B", "-X", "utf8", "-u", str(ROOT / "worker.py"),
                str(source), str(target), "--cancel-file", str(cancel), *options,
            ]
            env = os.environ.copy()
            windows = Path(env.get("SystemRoot", r"C:\Windows"))
            env["PATH"] = os.pathsep.join(map(str, (PYTHON.parent, ROOT / "bin", windows / "System32", windows)))
            with subprocess.Popen(
                command, cwd=str(PYTHON.parent), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", creationflags=subprocess.CREATE_NO_WINDOW,
            ) as worker:
                messages = queue.Queue()
                threading.Thread(target=read_messages, args=(worker.stdout, messages), daemon=True).start()
                last_error = ""
                while True:
                    if task.isCancelled() and not cancel.exists():
                        cancel.touch()
                        task.setMessage("Cancelling...")
                    try:
                        line = messages.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if line is None:
                        break
                    try:
                        event = json.loads(line)
                    except ValueError:
                        last_error = line
                        continue
                    if "error" in event:
                        last_error = event["error"]
                    elif not cancel.exists():
                        task.setProgress(event["progress"])
                        task.setMessage(event["message"])
                code = worker.wait()
        if code == 2:
            return
        if code != 0 or not target.is_file():
            raise RuntimeError(last_error or f"Video worker exited with code {code}.")
        nuke.executeInMainThread(load_result, args=(node, target.as_posix(), input_node, source, first, colorspace))
    except Exception as exc:
        nuke.executeInMainThread(nuke.message, args=(f"Video upscale failed:\n{exc}",))
    finally:
        try:
            if staging is not None:
                staging.cleanup()
        finally:
            del task
            nuke.executeInMainThread(clear_active)


def clear_active():
    global ACTIVE
    ACTIVE = False


def load_result(node, path, source_node, source, first, colorspace):
    try:
        if node not in nuke.allNodes("Group", group=nuke.root(), recurseGroups=True):
            return
        input_node = node.input(0) if node.maxInputs() else None
        if input_node != source_node:
            return
        if input_node is not None and first is None:
            current = nuke.filename(input_node)
            if not current or Path(current).resolve() != source:
                return
        reader = node.node("UpscaledVideo")
        reader["colorspace"].setValue(colorspace or "default")
        reader["file"].fromUserText(path)
        reader["frame_mode"].setValue("start at" if first is not None else "expression")
        reader["frame"].setValue(str(first) if first is not None else "frame")
        output = node.node("VideoOutput")
        if output is not None:
            output["which"].setValue(1)
        node["result_video"].setValue(path)
    except Exception:
        nuke.message(f"The video was saved but could not be loaded into VideoUpscale4K:\n{path}")
