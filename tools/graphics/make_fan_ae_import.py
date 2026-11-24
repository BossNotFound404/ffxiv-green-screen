"""Add a local, timestamp-aware After Effects importer to an exported fan sequence."""
import argparse
import hashlib
import io
import json
import math
from pathlib import Path

from PIL import Image


def recorded_plate(frames):
    """Find the last draw-free game frame before the fan first became visible."""
    idle = None
    for frame in frames:
        if frame["observedSupportPixels"] or frame["exportedSupportPixels"]:
            if idle is not None:
                return idle
            break
        if not frame["stages"]:
            idle = frame
    raise ValueError("No draw-free game frame before visible fan activity. Supply a same-camera clean PNG with --plate.")


def selected_plate(record):
    frames = record["frames"]
    if "autoSelection" not in record:
        return recorded_plate(frames)

    point = record["autoSelection"]["cleanPlate"]
    index = point["sourceIndex"]
    matches = [frame for frame in frames if frame["sourceIndex"] == index]
    if type(index) is not int or len(matches) != 1:
        raise ValueError("The auto-selected clean plate must identify one exported source index.")
    source = matches[0]
    if (any(type(point[key]) is not int or point[key] != source[key] for key in ("present", "qpc"))
            or source["stages"] or source["observedSupportPixels"] or source["exportedSupportPixels"]):
        raise ValueError("The auto-selected clean plate does not match a draw-free captured present and QPC.")
    if "aeCleanPlate" in record:
        evidence = record["aeCleanPlate"]
        if (evidence["present"], evidence["number"], evidence["sha256"]) != (
                source["present"], source["number"], source["outputSha256"]["game"]):
            raise ValueError("The saved clean plate provenance differs from the auto-selected game frame.")
    return source


def prepare(directory, plate=None):
    directory = directory.resolve(strict=True)
    record = json.loads((directory / "sequence.json").read_text(encoding="utf-8-sig"))
    frames, crop = record["frames"], record["crop"]
    width, height = crop["width"], crop["height"]
    if len(frames) < 2:
        raise ValueError("At least two captured frames are required.")
    paths = {}
    for key in ("black", "multiply", "add", "game"):
        for frame in frames:
            member = (directory / frame["files"][key]).resolve(strict=True)
            if not member.is_relative_to(directory) or not member.is_file():
                raise ValueError("Footage must be a regular file in the sequence folder.")
        paths[key] = frames[0]["files"][key]
    times = [f["startSeconds"] for f in frames]
    duration = record["timing"]["durationSeconds"]
    if (times[0] != 0 or not all(math.isfinite(v) for v in (*times, duration))
            or any(b <= a for a, b in zip(times, times[1:])) or duration <= times[-1]):
        raise ValueError("Time stamps must increase from zero through the last source frame.")
    if plate is None:
        source = selected_plate(record)
        data = (directory / source["files"]["game"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != source["outputSha256"]["game"]:
            raise ValueError("The recorded plate no longer matches its captured game frame.")
    else:
        data = plate.read_bytes()
    with Image.open(io.BytesIO(data)) as img:
        if img.size != (width, height) or img.format != "PNG":
            raise ValueError("The clean PNG must match the native crop.")
        img.verify()
    paths["plate"] = "clean-plate.png"
    config = dict(paths=paths, width=width, height=height, times=times, duration=duration)
    template = Path(__file__).with_name("fan_sequence_import.jsx").read_text(encoding="utf-8")
    marker = "/*__CAPTURE_CONFIG__*/ null"
    if template.count(marker) != 1:
        raise ValueError("Invalid importer template.")
    script = template.replace(marker, json.dumps(config, ensure_ascii=True, allow_nan=False))
    plate_out = directory / paths["plate"]
    script_out = directory / "导入蓝扇序列.jsx"
    outputs = [(plate_out, data), (script_out, script.encode("utf-8-sig"))]
    for target, data in outputs:
        if target.exists() and target.read_bytes() != data:
            raise FileExistsError(target)
    for target, data in outputs:
        if not target.exists():
            with target.open("xb") as stream:
                stream.write(data)
    return script_out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sequence_dir", type=Path)
    parser.add_argument("--plate", type=Path, help="same-camera clean PNG; defaults to a captured frame before the effect")
    args = parser.parse_args()
    print(prepare(args.sequence_dir, args.plate))
