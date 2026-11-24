"""Inspect cropped FF14 GPU resource readbacks. Requires Python 3.10+, NumPy and Pillow."""
import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import shutil
import sys
from uuid import uuid4

import numpy as np
from PIL import Image, ImageDraw, ImageFont


# Numeric DXGI_FORMAT values and packed channel definitions:
# https://learn.microsoft.com/en-us/windows/win32/api/dxgiformat/ne-dxgiformat-dxgi_format
FORMATS = {
    2: ("R32G32B32A32_FLOAT", 16), 6: ("R32G32B32_FLOAT", 12),
    10: ("R16G16B16A16_FLOAT", 8), 11: ("R16G16B16A16_UNORM", 8), 16: ("R32G32_FLOAT", 8),
    24: ("R10G10B10A2_UNORM", 4), 26: ("R11G11B10_FLOAT", 4),
    28: ("R8G8B8A8_UNORM", 4), 29: ("R8G8B8A8_UNORM_SRGB", 4),
    34: ("R16G16_FLOAT", 4), 41: ("R32_FLOAT", 4), 54: ("R16_FLOAT", 2),
    87: ("B8G8R8A8_UNORM", 4), 88: ("B8G8R8X8_UNORM", 4),
    91: ("B8G8R8A8_UNORM_SRGB", 4), 93: ("B8G8R8X8_UNORM_SRGB", 4),
}
GEOMETRY = ("width", "height", "x", "y", "cropWidth", "cropHeight")
IDENTITY = (*GEOMETRY, "dxgiFormat", "bytesPerPixel")
PANEL = (320, 160)


def member(root, filename):
    if not isinstance(filename, str) or not filename or ":" in filename:
        raise ValueError("Expected a relative filename")
    rel = PureWindowsPath(filename)
    if rel.anchor or ".." in rel.parts or any(PureWindowsPath(p).is_reserved() for p in rel.parts):
        raise ValueError("Unsafe sample path")
    path = root.joinpath(*rel.parts).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Sample path leaves the capture directory or is not a file")
    return path


def manifest(root):
    source = member(root, "manifest.json").read_bytes()
    obj = json.loads(source.decode("utf-8-sig"))
    if not isinstance(obj, dict) or type(obj.get("schema")) is not int or obj["schema"] != 1 or obj.get("source") != "ffxiv_dx11":
        raise ValueError("Expected a schema 1 ffxiv_dx11 manifest")
    if not isinstance(obj.get("samples"), list):
        raise ValueError("Missing sample list")
    samples = {}
    for sample in obj["samples"]:
        if not isinstance(sample, dict) or not isinstance(sample.get("name"), str) or not sample["name"].strip():
            raise ValueError("Missing sample name")
        name = sample["name"]
        if name in samples:
            raise ValueError("Duplicate sample name: " + name)
        if sample.get("file") is not None:
            if not all(type(sample.get(k)) is int for k in IDENTITY):
                raise ValueError("Missing integer dimensions, format or pixel stride for " + name)
            w, h, x, y, cw, ch = (sample[k] for k in GEOMETRY)
            if min(w, h, cw, ch, sample["bytesPerPixel"]) < 1 or min(x, y) < 0 or x+cw > w or y+ch > h:
                raise ValueError("Invalid crop for " + name)
            fmt = FORMATS.get(sample["dxgiFormat"])
            if fmt and sample["bytesPerPixel"] != fmt[1]:
                raise ValueError("Unexpected bytesPerPixel for " + name)
            if not isinstance(sample.get("sha256"), str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", sample["sha256"]):
                raise ValueError("Invalid SHA-256 for " + name)
            path = member(root, sample["file"])
            if path.stat().st_size != cw * ch * sample["bytesPerPixel"]:
                raise ValueError("Incorrect packed readback length for " + name)
            check = hashlib.sha256()
            with path.open("rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    check.update(block)
            if check.hexdigest() != sample["sha256"].lower():
                raise ValueError("SHA-256 mismatch for " + name)
            sample["_path"] = path
        elif not isinstance(sample.get("note"), str) or not sample["note"].strip():
            raise ValueError("Sample has neither a readback nor a reason: " + name)
        samples[name] = sample
    evidence = {k: obj[k] for k in ("source", "processId", "processStartUtc", "utc", "callback", "label", "probeVersion") if k in obj}
    evidence["manifestSha256"] = hashlib.sha256(source).hexdigest()
    return evidence, samples


def unpack_ufloat(bits, mantissa_bits):
    # An unsigned miniature float uses the same exponent bias/specials as IEEE half.
    # Align its mantissa to half's 10 bits without changing its five exponent bits.
    return np.uint16(bits << (10 - mantissa_bits)).view(np.float16).astype(np.float32)


def decode(raw, fmt):
    h, w = raw.shape[:2]
    if fmt in (2, 6, 10, 11, 16, 34, 41, 54):
        dtype = {2: "<f4", 6: "<f4", 10: "<f2", 11: "<u2", 16: "<f4", 34: "<f2", 41: "<f4", 54: "<f2"}[fmt]
        values = raw.view(dtype).reshape(h, w, -1).astype(np.float32)
        return values / 65535 if fmt == 11 else values
    if fmt in (24, 26):
        word = raw.view("<u4").reshape(h, w)
        if fmt == 26:
            return np.stack((unpack_ufloat(word & 2047, 6), unpack_ufloat((word >> 11) & 2047, 6),
                             unpack_ufloat(word >> 22, 5)), axis=2)
        rgb = [((word >> shift) & 1023).astype(np.float32) / 1023 for shift in (0, 10, 20)]
        return np.stack([*rgb, (word >> 30).astype(np.float32) / 3], axis=2)
    channels = (0, 1, 2, 3) if fmt in (28, 29) else (2, 1, 0)
    if fmt in (87, 91):
        channels = (*channels, 3)
    return raw[:, :, channels].astype(np.float32) / 255


def raw_view(sample):
    shape = sample["cropHeight"], sample["cropWidth"], sample["bytesPerPixel"]
    return np.memmap(sample["_path"], mode="r", dtype="u1", shape=shape)


def save_native_backbuffer(sample, folder):
    """Preserve even RGB under zero alpha for byte-for-byte inspection."""
    if not sample or "_path" not in sample or sample["dxgiFormat"] not in (28, 29, 87, 91):
        return None
    pixels = raw_view(sample)
    if sample["dxgiFormat"] in (87, 91):
        pixels = pixels[:, :, [2, 1, 0, 3]]
    name = "backbuffer-native-rgba.png"
    Image.fromarray(np.ascontiguousarray(pixels)).save(folder / name)
    return name


def channel_stats(channel):
    finite = channel[np.isfinite(channel)]
    return {
        "finite": int(finite.size), "nan": int(np.isnan(channel).sum()),
        "positiveInfinity": int(np.isposinf(channel).sum()), "negativeInfinity": int(np.isneginf(channel).sum()),
        "belowZero": int((finite < 0).sum()), "aboveOne": int((finite > 1).sum()),
        "min": float(finite.min()) if finite.size else None,
        "max": float(finite.max()) if finite.size else None,
        "mean": float(finite.mean(dtype=np.float64)) if finite.size else None,
    }


def as_panel(colors):
    image = Image.fromarray(colors)
    if image.width < PANEL[0] and image.height < PANEL[1]:
        scale = min(PANEL[0] // image.width, PANEL[1] // image.height)
        image = image.resize((image.width * scale, image.height * scale), Image.Resampling.NEAREST)
    else:
        image.thumbnail(PANEL, Image.Resampling.LANCZOS)
    return image


def preview(values, peak, logarithmic):
    finite = np.isfinite(values)
    visible = np.maximum(np.where(finite, values, 0), 0)
    if logarithmic:
        visible = np.log1p(visible) / np.log1p(max(1, peak))
    rgb = np.uint8(np.rint(np.clip(visible, 0, 1) * 255))
    if values.shape[2] == 1:
        rgb = np.repeat(rgb, 3, axis=2)
    elif values.shape[2] == 2:
        rgb = np.dstack((rgb, np.zeros(rgb.shape[:2], dtype=np.uint8)))
    rgb[~finite.all(axis=2)] = (255, 0, 255)
    return as_panel(rgb)


def compare(sample, raw, values, other):
    if other is None or "_path" not in other:
        return {"status": "missing_reference"}, None
    differences = {k: [sample[k], other[k]] for k in IDENTITY if sample[k] != other[k]}
    if differences:
        # A resize may also recreate the render targets. Describe each readback
        # independently; a heat map would imply correspondence between pixels.
        def contents(pixels):
            return {"allBytesZero": not bool(pixels.any()),
                    "pixelsDifferentFromTopLeft": int(np.any(pixels != pixels[0, 0], axis=2).sum())}
        return {"status": "incompatible_crop_or_format", "differentFieldsCurrentThenReference": differences,
                "currentReadback": contents(raw), "referenceReadback": contents(raw_view(other))}, None
    ref_raw = raw_view(other)
    ref = decode(ref_raw, other["dxgiFormat"])
    byte_change = np.any(raw != ref_raw, axis=2)
    rgb_peak = np.zeros(values.shape[:2], dtype=np.float64)
    rgb_special = np.zeros(values.shape[:2], dtype=bool)
    summary = {}
    for index, name in enumerate("RGBA"[:values.shape[2]]):
        a, b = values[:, :, index], ref[:, :, index]
        finite = np.isfinite(a) & np.isfinite(b)
        delta = np.zeros(a.shape, np.float64)
        # A signalling NaN can warn on a SIMD input cast even for a masked lane.
        with np.errstate(invalid="ignore"):
            np.subtract(a, b, out=delta, where=finite, dtype=np.float64)
        np.abs(delta, out=delta)
        special = ~finite & ~((a == b) | (np.isnan(a) & np.isnan(b)))
        summary[name] = {"pairedFinite": int(finite.sum()), "changedFinite": int(np.count_nonzero(delta)),
                         "maxAbsDelta": float(delta.max()) if finite.any() else None,
                         "nonfiniteClassChanges": int(special.sum())}
        if index < 3:
            np.maximum(rgb_peak, delta, out=rgb_peak)
            rgb_special |= special
    peak = float(rgb_peak.max())
    heat = np.zeros((*rgb_peak.shape, 3), dtype=np.uint8)
    changed = rgb_peak > 0
    bounds = peak_xy = None
    if peak > 0:
        heat[:, :, 0] = np.rint(255 * np.sqrt(rgb_peak / peak)).astype(np.uint8)
        heat[:, :, 1] = np.rint(255 * rgb_peak / peak).astype(np.uint8)
        xs, ys = np.flatnonzero(changed.any(axis=0)), np.flatnonzero(changed.any(axis=1))
        bounds = [int(xs[0]) + sample["x"], int(ys[0]) + sample["y"],
                  int(xs[-1]-xs[0])+1, int(ys[-1]-ys[0])+1]
        peak_y, peak_x = np.unravel_index(rgb_peak.argmax(), rgb_peak.shape)
        peak_xy = [int(peak_x) + sample["x"], int(peak_y) + sample["y"]]
    heat[rgb_special] = (255, 0, 255)
    return {"status": "compared", "rawChangedPixels": int(byte_change.sum()), "rgbFiniteChangedPixels": int(changed.sum()),
            "rgbNonfiniteClassChangedPixels": int(rgb_special.sum()), "referenceSha256": other["sha256"],
            "rgbChangeBoundsXYWHInTarget": bounds, "heatPeakXYInTarget": peak_xy,
            "heatMaxRgbDelta": peak, "channels": summary}, as_panel(heat)


def inspect_sample(sample, reference):
    name = sample["name"]
    panels = []
    if "_path" not in sample:
        return {"name": name, "status": "no_readback", "note": sample["note"]}, panels
    fmt = sample["dxgiFormat"]
    report = {k: sample[k] for k in ("name", "file", "sha256", *IDENTITY)}
    if "resourceDxgiFormat" in sample:
        report["resourceDxgiFormat"] = sample["resourceDxgiFormat"]
    if fmt not in FORMATS:
        report.update(status="unsupported_format", note="Integrity verified. No typed colour decoder.")
        return report, panels
    raw = raw_view(sample)
    values = decode(raw, fmt)
    stats = {name: channel_stats(channel) for name, channel in zip("RGBA", values.transpose(2, 0, 1))}
    peak = max((s["max"] or 0) for k, s in stats.items() if k != "A")
    log_scale = peak > 1
    report.update(status="decoded", formatName=FORMATS[fmt][0], channels=stats, storedChannels="RGBA"[:values.shape[2]],
                  srgbStorage=fmt in (29, 91, 93), hasAlpha=values.shape[2] == 4,
                  rgbPreviewScale="log1p(value)/log1p(peak)" if log_scale else "unit_clamp", rgbPreviewPeak=peak)
    rgb_label = {1:"R shown as gray", 2:"RG (blue zero)"}.get(values.shape[2], "RGB")
    label = rgb_label + ("  log peak %.4g" % peak if log_scale else "  [0, 1]")
    panels.append((label, preview(values[:, :, :3], peak, log_scale)))
    alpha = preview(values[:, :, 3:4], 1, False) if report["hasAlpha"] else None
    panels.append(("Alpha  [0, 1]" if alpha else "No stored alpha", alpha))
    if reference is not None:
        comparison, heat = compare(sample, raw, values, reference.get(name))
        report["referenceComparison"] = comparison
        caption = ("RGB difference  peak %.4g" % comparison["heatMaxRgbDelta"]
                   if heat else comparison["status"].replace("_", " "))
        panels.append((caption, heat))
    return report, panels


def contact(rows, reference):
    pw, ph = PANEL
    gutter, row_height = 14, ph + 78
    columns = 3 if reference else 2
    canvas = Image.new("RGB", ((pw+gutter)*columns+gutter, 90+row_height*max(1,len(rows))), "#191b20")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.text((gutter, 12), "FF14 scene buffers   |   captured ROI", fill="white", font=font)
    draw.text((gutter, 35), "Stored values. Magenta marks nonfinite RGB or alpha. Scene colour space is unknown.", fill="#d0d2d8", font=font)
    if reference:
        draw.text((gutter, 55), "Reference is a separate capture. Changes can include motion, camera or other effects.", fill="#d0d2d8", font=font)
    if not rows:
        draw.text((gutter, 100), "No samples recorded", fill="white", font=font)
    for i, (r, panels) in enumerate(rows):
        y = 90 + i*row_height
        if r["status"] != "decoded":
            draw.text((gutter,y), r["name"]+"   "+r["status"]+"   "+r["note"], fill="#e1b989",font=font)
            continue
        draw.text((gutter, y), "%s   %s   %dx%d at (%d,%d)" % (r["name"], r["formatName"], r["cropWidth"], r["cropHeight"], r["x"], r["y"]), fill="white", font=font)
        for j, (caption, image) in enumerate(panels):
            x = gutter + j*(pw+gutter)
            draw.text((x, y+22), caption, fill="#c5c7cd",font=font)
            if image is not None:
                canvas.paste(image,(x+(pw-image.width)//2, y+44+(ph-image.height)//2))
    return canvas


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="capture folder containing manifest.json")
    parser.add_argument("--reference", type=Path, help="separate empty-studio capture folder")
    parser.add_argument("--out", type=Path, required=True, help="new output folder")
    args = parser.parse_args(argv)
    out = args.out.resolve()
    if out.exists():
        parser.error("Output directory already exists.")
    stage = out.with_name("." + out.name + "-" + uuid4().hex)
    try:
        root = args.input.resolve(strict=True)
        current, samples = manifest(root)
        reference = None
        baseline = None
        if args.reference is not None:
            ref_root = args.reference.resolve(strict=True)
            if ref_root == root:
                raise ValueError("Supply a separate reference capture.")
            baseline, reference = manifest(ref_root)
        rows = [inspect_sample(sample, reference) for sample in samples.values()]
        decoded = sum(r["status"] == "decoded" for r, _ in rows)
        report = {"schema": 1, "source": "ffxiv_dx11", "capture": current, "referenceCapture": baseline,
                  "decodedSamples": decoded, "hasReference": reference is not None,
                  "scope": "GPU resources visible at sampling time. Buffer contents alone do not identify a draw, a skill, or a reusable alpha channel.",
                  "colorInterpretation": "Stored components; UNORM normalized to [0,1], sRGB components not linearized, floats preserved. Scene colour management is unknown.",
                  "referenceScope": "A separate capture; differences can include camera, motion, UI, or other scene changes." if reference is not None else None,
                  "samples": [r for r, _ in rows]}
        stage.mkdir(parents=True)
        report["nativeBackBufferRgbaPng"] = save_native_backbuffer(samples.get("SwapChainBackBuffer"), stage)
        contact(rows, reference is not None).save(stage / "contact.png")
        (stage / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
        stage.rename(out)
    except (OSError, ValueError, OverflowError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(out / "report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
