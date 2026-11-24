"""Package and check the isolated D3D11 tower fixture. Requires Pillow and NumPy.

Inputs are unflipped, packed linear R8G8B8A8_UNORM images, one texel per pixel.
--screen and --add must each be an independent draw on black. Declare whether
the source was a direct GPU redraw in the fixture or replayed from a capture.
--reference takes a name, a GPU-rendered background and its GPU final frame.
Supply the background from which the capture originated first.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

import numpy as np
from PIL import Image, ImageDraw, PngImagePlugin


TOLERANCE = 2  # linear UNORM8 code values


def read_raw(path, width, height):
    data = Path(path).read_bytes()
    if len(data) != width * height * 4:
        raise ValueError(f"{path}: expected {width * height * 4} bytes, got {len(data)}")
    return np.frombuffer(data, np.uint8).reshape(height, width, 4)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unorm8(value):
    return np.rint(np.clip(value, 0.0, 1.0) * 255.0).astype(np.uint8)


def srgb(value):
    return np.where(value <= 0.0031308, value * 12.92, 1.055 * value ** (1 / 2.4) - 0.055)


def linear(value):
    return np.where(value <= 0.04045, value / 12.92, ((value + 0.055) / 1.055) ** 2.4)


def write_png(path, pixels):
    metadata = PngImagePlugin.PngInfo()
    metadata.add(b"sRGB", bytes([0]))
    Image.fromarray(pixels).save(path, pnginfo=metadata)


def encode_layer(raw, path):
    # Stored alpha is a transport weight. Even a shader writing zero alpha can emit light.
    rgb = raw[:, :, :3].astype(np.float64)
    weight = raw[:, :, :3].max(axis=2)
    straight = np.divide(rgb, weight[:, :, None], out=np.zeros_like(rgb), where=weight[:, :, None] != 0)
    write_png(path, np.dstack((unorm8(srgb(straight)), weight)))
    with Image.open(path) as png:
        if png.mode != "RGBA" or png.info.get("srgb") != 0:
            raise ValueError("Expected an sRGB-tagged straight RGBA PNG")
        rgba = np.asarray(png).astype(np.float64) / 255
    contribution = linear(rgba[:, :, :3]) * rgba[:, :, 3:4]
    return contribution, weight, {
        "sourceReadbackSHA256": digest(raw),
        "pngSHA256": digest(path.read_bytes()),
        "nonzeroAlphaPixels": int(np.count_nonzero(weight)),
        "partialAlphaPixels": int(np.count_nonzero((weight > 0) & (weight < 255))),
        "opaquePixels": int(np.count_nonzero(weight == 255)),
        "maxContributionErrorInLinear8BitLevels": float(np.max(np.abs(contribution * 255 - rgb))),
    }


def witness_points(body, light):
    mask = body > 0
    padded = np.pad(mask, 1)
    interior = (mask & padded[1:-1, :-2] & padded[1:-1, 2:]
                & padded[:-2, 1:-1] & padded[2:, 1:-1])
    samples = {}
    for name, area, strength in (
        ("outside", ~mask & (light == 0), None),
        ("screenEdge", mask & ~interior & (light == 0), body),
        ("screenInterior", interior & (light == 0), body),
        ("emissiveDetail", light > 0, light),
    ):
        ys, xs = np.nonzero(area)
        if xs.size:
            i = int(np.argmax(strength[ys, xs])) if strength is not None else 0
            samples[name] = (int(xs[i]), int(ys[i]))
    return samples


def compare(folder, name, background, expected, screen, add, samples, gpu_body=None):
    bg_rgb = background[:, :, :3].astype(np.float64) / 255
    # Quantize the intermediate render target between the two GPU draws.
    after_screen = unorm8(screen + bg_rgb * (1 - screen))
    before_clamp = after_screen.astype(np.float64) / 255 + add
    final = unorm8(before_clamp)
    error = np.abs(final.astype(np.int16) - expected[:, :, :3].astype(np.int16))
    pixel_error = error.max(axis=2)
    alpha_errors = int(np.count_nonzero(expected[:, :, 3] != background[:, :, 3]))
    max_error = int(pixel_error.max())
    new_clip = (before_clamp > 1.0) & (after_screen < 255) & (expected[:, :, :3] == 255)
    if gpu_body is not None:
        new_clip &= gpu_body[:, :, :3] < 255
    clipped = np.any(new_clip, axis=2)
    points = dict(samples)
    if clipped.any():
        y, x = np.unravel_index(np.argmax(np.where(new_clip, before_clamp, 0).max(axis=2)), clipped.shape)
        points["clippedEmission"] = (int(x), int(y))
    stats = {
        "backgroundSHA256": digest(background), "gpuFinalSHA256": digest(expected),
        "exactRGBPixels": int(np.count_nonzero(pixel_error == 0)),
        "outsideTolerancePixels": int(np.count_nonzero(pixel_error > TOLERANCE)),
        "maxChannelError": max_error, "gpuDestinationAlphaChangedPixels": alpha_errors,
        "addClippedPixels": int(np.count_nonzero(np.any(before_clamp > 1.0, axis=2))),
        "newlyClippedPixelsConfirmedOnGPU": int(np.count_nonzero(clipped)),
        "gpuSaturatedPixels": int(np.count_nonzero(np.any(expected[:, :, :3] == 255, axis=2))),
        "points": {label: {"xy": [x, y], "background": background[y, x, :3].tolist(),
                           "gpuFinal": expected[y, x, :3].tolist(), "fromPNG": final[y, x].tolist()}
                   for label, (x, y) in points.items()},
    }
    if gpu_body is not None:
        err = np.abs(after_screen.astype(np.int16) - gpu_body[:, :, :3].astype(np.int16)).max(axis=2)
        stats["screenIntermediate"] = {
            "gpuSHA256": digest(gpu_body),
            "exactRGBPixels": int(np.count_nonzero(err == 0)),
            "maxChannelError": int(err.max()),
            "outsideTolerancePixels": int(np.count_nonzero(err > TOLERANCE)),
            "gpuDestinationAlphaChangedPixels": int(np.count_nonzero(gpu_body[:, :, 3] != background[:, :, 3])),
        }
    # Store display PNGs in sRGB. Difference is |linear UNORM8 error| * 32 in display code values.
    views = [unorm8(srgb(bg_rgb)), unorm8(srgb(expected[:, :, :3].astype(np.float64) / 255)),
             unorm8(srgb(final.astype(np.float64) / 255)), np.clip(error * 32, 0, 255).astype(np.uint8)]
    for label, view in zip(("background", "gpu", "recomposed", "diff-x32"), views):
        write_png(folder / f"{name}-{label}.png", view)
    return stats


def make_comparison(folder, names, width, height, samples):
    thumb_w = min(384, width)
    thumb_h = max(1, round(thumb_w * height / width))
    row_h = thumb_h + 32
    canvas = Image.new("RGB", (4 * thumb_w, len(names) * row_h), "#202428")
    draw = ImageDraw.Draw(canvas)
    for row, name in enumerate(names):
        for col, (label, caption) in enumerate((
            ("background", "GPU background"), ("gpu", "GPU original"),
            ("recomposed", "Screen + Add PNGs"), ("diff-x32", "|difference| x32"),
        )):
            x, y = col * thumb_w, row * row_h
            draw.text((x + 5, y + 8), f"{name}  /  {caption}", fill="white")
            with Image.open(folder / f"{name}-{label}.png") as view:
                canvas.paste(view.resize((thumb_w, thumb_h), Image.Resampling.NEAREST), (x, y + 32))
    write_png(folder / "comparison.png", np.asarray(canvas))
    # Match crop coordinates across both backgrounds and both images; keep the edge pixels unmarked.
    labels = ("screenEdge", "emissiveDetail")
    if not all(label in samples for label in labels):
        return
    crop_w, crop_h = min(30, width), min(22, height)
    cell_w, cell_h = 150, 140
    details = Image.new("RGB", (len(labels) * 3 * cell_w, cell_h * len(names)), "#202428")
    draw = ImageDraw.Draw(details)
    for row, name in enumerate(names):
        for index, label in enumerate(labels):
            px, py = samples[label]
            left = max(0, min(px - crop_w // 2, width - crop_w))
            top = max(0, min(py - crop_h // 2, height - crop_h))
            box = (left, top, left + crop_w, top + crop_h)
            for col, view in enumerate(("gpu", "recomposed", "diff-x32")):
                x, y = (index * 3 + col) * cell_w, row * cell_h
                draw.text((x + 3, y + 1), f"{name} / {label}", fill="white")
                draw.text((x + 3, y + 16), view, fill="#e0e6ec")
                with Image.open(folder / f"{name}-{view}.png") as image:
                    crop = image.crop(box).resize((crop_w * 5, crop_h * 5), Image.Resampling.NEAREST)
                details.paste(crop, (x, y + 30))
    write_png(folder / "detail-comparison.png", np.asarray(details))


def run(args, folder):
    def read(path):
        return read_raw(path, args.width, args.height)

    screen, body, screen_info = encode_layer(read(args.screen), folder / "tower-screen.png")
    add, light, add_info = encode_layer(read(args.add), folder / "tower-add.png")
    replay = read(args.replay_original)
    samples = witness_points(body, light)
    region = (body > 0) | (light > 0)
    reasons, comparisons, differences = [], {}, {}
    if len(samples) != 4:
        reasons.append("Missing background, screen edge, screen interior or emission witness in the fixture")
    first_background = None
    body_files = dict(args.body_reference)
    for name, background_file, final_file in args.reference:
        background, expected = read(background_file), read(final_file)
        if first_background is None:
            first_background = background
            if not np.array_equal(expected, replay):
                reasons.append("The unchanged replay differs from the first independent GPU reference")
        else:
            count = int(np.count_nonzero(region & np.any(background[:, :, :3] != first_background[:, :, :3], axis=2)))
            differences[name] = count
            if not count:
                reasons.append(f"{name}: background has no change inside the effect")
        body_ref = read(body_files[name]) if name in body_files else None
        stats = compare(folder, name, background, expected, screen, add, samples, body_ref)
        comparisons[name] = stats
        if stats["outsideTolerancePixels"] or stats["gpuDestinationAlphaChangedPixels"]:
            reasons.append(f"{name}: GPU comparison failed")
        mid = stats.get("screenIntermediate")
        if mid and (mid["outsideTolerancePixels"] or mid["gpuDestinationAlphaChangedPixels"]):
            reasons.append(f"{name}: GPU intermediate Screen comparison failed")
        if name in args.require_clipping and not stats["newlyClippedPixelsConfirmedOnGPU"]:
            reasons.append(f"{name}: no additive clipping confirmed against the GPU final")
    report = {
        "status": "failed" if reasons else "passed", "reasons": reasons,
        "scope": "Isolated D3D11 tower-texture fixture; no FF14 game capture.",
        "layerSource": args.layer_source,
        "originalFrameSource": "Unmodified first background via the same path as the layers.",
        "gpuReferenceSource": "Independently rendered backgrounds and final frames, used only for comparison.",
        "width": args.width, "height": args.height, "workingTarget": "linear R8G8B8A8_UNORM",
        "toleranceInLinear8BitLevels": TOLERANCE,
        "pngEncoding": "sRGB straight RGB, linear alpha = max RGB of each draw on black (transport weight)",
        "usage": "Composite in linear light with the Screen PNG first, the Add PNG second, using straight alpha.",
        "layers": {"screen": screen_info, "add": add_info}, "unchangedReplaySHA256": digest(replay),
        "changedBackgroundPixelsInsideEffectComparedToFirst": differences,
        "references": comparisons,
    }
    (folder / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    make_comparison(folder, comparisons, args.width, args.height, samples)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("--screen", "--add", "--replay-original", "--out"):
        parser.add_argument(flag, type=Path, required=True)
    for flag in ("--width", "--height"):
        parser.add_argument(flag, type=int, required=True)
    parser.add_argument("--layer-source", choices=("fixture-redraw", "renderdoc-replay"), required=True)
    parser.add_argument("--reference", action="append", nargs=3, metavar=("NAME", "BACKGROUND", "FINAL"), required=True)
    parser.add_argument("--body-reference", action="append", nargs=2, default=[], metavar=("NAME", "BODY"),
                        help="Also compare the Screen PNG over this background against the independent intermediate.")
    parser.add_argument("--require-clipping", action="append", default=[], metavar="NAME",
                        help="Require this fixture reference to contain an additive highlight clipped by the GPU.")
    args = parser.parse_args()
    names = [ref[0] for ref in args.reference]
    if (len(names) < 2 or len(set(name.lower() for name in names)) != len(names)
            or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name) for name in names)):
        parser.error("Pass at least two references with unique short alphanumeric labels")
    if any(name not in names for name in args.require_clipping):
        parser.error("--require-clipping needs a reference with that name")
    body_names = [name for name, _ in args.body_reference]
    if len(set(body_names)) != len(body_names) or any(name not in names for name in body_names):
        parser.error("--body-reference needs one entry per named reference, with unique names")
    if args.width <= 0 or args.height <= 0:
        parser.error("Image dimensions must be positive")
    if args.out.exists():
        parser.error("Choose a new output directory")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    folder = args.out.with_name(f".{args.out.name}.{uuid4().hex}.partial")
    folder.mkdir()
    try:
        report = run(args, folder)
        folder.rename(args.out)
    except BaseException:
        for file in folder.iterdir():
            file.unlink()
        folder.rmdir()
        raise
    print(f"{report['status']}: {args.out / 'report.json'}")
    return int(report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
