"""Inspect the observed render target before and after selected draws in a probe report."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image, ImageDraw

from inspect_scene_buffers import channel_stats, decode, FORMATS, member, PANEL, preview
from verify_tower_layers import linear


def additive_rgb(row):
    # D3D11: ADD=1, ONE=2, SRC_ALPHA=5 and RGB write bits are 1|2|4.
    return (row.get("blendEnabled") is True and row.get("blendOp") == 1
            and row.get("dstBlend") == 2 and row.get("srcBlend") in (2, 5)
            and row.get("writeMask", 0) & 7 == 7)


def measure_additive(before, after, fmt):
    before_rgb, after_rgb = before[:, :, :3], after[:, :, :3]
    finite = np.isfinite(before_rgb) & np.isfinite(after_rgb)
    if fmt in (29, 91):
        before_linear, after_linear = linear(before_rgb), linear(after_rgb)
    else:
        before_linear, after_linear = before_rgb, after_rgb
    change = np.full(before_rgb.shape, np.nan, dtype=np.float32)
    np.subtract(after_linear, before_linear, out=change, where=finite)

    if fmt == 10:
        limit = np.finfo(np.float16).max
        lower_before, lower_after = before_rgb == -limit, after_rgb == -limit
        upper_before, upper_after = before_rgb == limit, after_rgb == limit
        storage = "FP16 linear RGB; precision varies with stored magnitude."
    else:
        lower_before, lower_after = before_rgb == 0, after_rgb == 0
        upper_before, upper_after = before_rgb == 1, after_rgb == 1
        storage = ("8-bit sRGB RGB decoded to linear light; the linear code step varies with brightness."
                   if fmt in (29, 91) else "8-bit linear UNORM RGB; adjacent codes differ by 1/255.")

    def pixel_count(channels):
        return int(np.count_nonzero(np.any(channels, axis=2)))

    report = {
        "scope": "Observed signed linear RGB change from this draw on its recorded background.",
        "storageQuantization": storage,
        "uncertainty": "Both measurements are quantized. Boundary samples may hide clipping, "
                       "including channels already at a boundary before the draw. "
                       "Channels with a nonfinite input have NaN change.",
        "nonfinitePixels": pixel_count(~finite),
        "negativeChangePixels": pixel_count(change < 0),
        "beforeLowerBoundaryPixels": pixel_count(lower_before),
        "beforeUpperBoundaryPixels": pixel_count(upper_before),
        "afterLowerBoundaryPixels": pixel_count(lower_after),
        "afterUpperBoundaryPixels": pixel_count(upper_after),
        "newlyAtUpperBoundaryPixels": pixel_count(finite & ~upper_before & upper_after),
        "unchangedAtLowerBoundaryPixels": pixel_count(lower_before & lower_after),
        "unchangedAtUpperBoundaryPixels": pixel_count(upper_before & upper_after),
    }
    return change, report


def read_plane(folder, capture, file_key):
    fmt = capture["viewFormat"]
    if fmt not in (28, 29, 87, 91, 10) or capture["sampleCount"] != 1:
        raise ValueError("Unsupported captured color format or sample count.")
    x, y, w, h, tw, th = (capture[k] for k in ("x", "y", "width", "height", "targetWidth", "targetHeight"))
    bpp = FORMATS[fmt][1]
    if not (0 < w <= 512 and 0 < h <= 512 and 0 <= x <= tw - w and 0 <= y <= th - h
            and capture["rowBytes"] == w * bpp):
        raise ValueError("Invalid cropped texture size, offset or stride.")
    source = member(folder, capture[file_key])
    if source.stat().st_size != h * w * bpp:
        raise ValueError("Incomplete pixel file: " + source.name)
    data = source.read_bytes()
    return decode(np.frombuffer(data, dtype=np.uint8).reshape(h, w, bpp), fmt), hashlib.sha256(data).hexdigest()


def changed_rgb(before, after, capture):
    a, b = before[:, :, :3], after[:, :, :3]
    finite = np.all(np.isfinite(a) & np.isfinite(b), axis=2)
    changed = finite & np.any(a != b, axis=2)
    ys, xs = np.nonzero(changed)
    bounds = None
    if xs.size:
        bounds = [capture["x"] + int(xs.min()), capture["y"] + int(ys.min()),
                  capture["x"] + int(xs.max()) + 1, capture["y"] + int(ys.max()) + 1]
    return changed, {"finiteRgbPixelsChanged": int(xs.size), "uncomparableRgbPixels": int(np.count_nonzero(~finite)),
                     "targetBoundsXyxyExclusive": bounds}


def surface_key(capture):
    if not capture.get("targetResourceId") or not capture.get("targetViewId"):
        return None
    return tuple(capture[k] for k in ("targetResourceId", "targetViewId", "mipSlice", "arraySlice", "viewFormat",
                                      "targetWidth", "targetHeight", "x", "y", "width", "height"))


def inspect_terminal(folder, capture, artifacts):
    details = dict(capture)
    if capture["status"] != "captured":
        return details
    if capture["stage"] != "swapchain_before_reshade_and_dxgi_present" or capture["formatSource"] != "swapchain_resource_desc":
        raise ValueError("Unrecognized terminal readback stage or format source.")
    pixels, details["sha256"] = read_plane(folder, capture, "file")
    details["channels"] = {ch: channel_stats(plane) for ch, plane in zip("RGBA", pixels.transpose(2, 0, 1))}
    if artifacts is not None:
        rgb = pixels[:, :, :3]
        finite = np.isfinite(rgb).all(axis=2)
        image = np.rint(np.clip(np.where(finite[:, :, None], rgb, 0), 0, 1) * 255).astype(np.uint8)
        image[~finite] = (255, 0, 255)
        artifacts.append(("swapchain-stored-rgb.png", image))
        details["preview"] = "swapchain-stored-rgb.png"
        details["previewScale"] = "Stored RGB values displayed in [0,1]; display transfer and monitor output are unverified."
    return details


def inspect(path, artifacts=None):
    path = path.resolve(strict=True)
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    if record.get("api") != "D3D11" or record.get("pixelCaptureMode") != "before_after_rt0":
        raise ValueError("Expected a D3D11 draw report with RT0 pixel capture enabled.")
    result = {k: record[k] for k in ("hostExecutable", "processId", "present", "captureMode")}
    for k in ("wholeTowerMode", "wholeTowerBudget", "fanCast", "detailRowsDropped", "deferredDrawsObserved"):
        if k in record:
            result[k] = record[k]
    result["scope"] = "Stored output immediately before and after one draw. Each image includes the scene already in RT0."
    result["events"] = []
    result["sameSurfaceIntervals"] = []
    last_on_surface = {}
    panels = []
    for row in record["events"]:
        capture = row.get("pixelCapture")
        if not capture and not row.get("towerInventory") and not row.get("fanCandidate"):
            continue
        details = {"sequence": row["sequence"], "type": row["type"], "capture": capture}
        if "fanCandidate" in row:
            details["fanCandidate"] = row["fanCandidate"]
            details["textures"] = row["fanTextures"]
        else:
            details["candidates"] = row["towerCandidates"]
            details["textures"] = row.get("towerTextures", [])
        if row.get("towerInventory"):
            details["inventory"] = row["towerInventory"]
        result["events"].append(details)
        if not capture or capture["status"] != "captured":
            continue
        if capture.get("rtIndex") != 0 or not capture.get("forwarded"):
            raise ValueError("Unsupported captured format or draw metadata.")
        before, details["beforeFileSha256"] = read_plane(path.parent, capture, "beforeFile")
        after, details["afterFileSha256"] = read_plane(path.parent, capture, "afterFile")
        changed, stats = changed_rgb(before, after, capture)
        details.update(stats)
        if "fanCandidate" in row:
            prior_rgb, next_rgb = before[:, :, :3], after[:, :, :3]
            lower = np.any(next_rgb < prior_rgb, axis=2)
            higher = np.any(next_rgb > prior_rgb, axis=2)
            details["storedRgbChange"] = {
                "onlyDarkerPixels": int((changed & lower & ~higher).sum()),
                "onlyBrighterPixels": int((changed & higher & ~lower).sum()),
                "mixedDirectionPixels": int((changed & higher & lower).sum()),
            }
        key = surface_key(capture)
        if key is not None:
            if key in last_on_surface:
                previous_row, previous_after, previous_hash = last_on_surface[key]
                _, gap = changed_rgb(previous_after, before, capture)
                gap.update(afterDraw=previous_row, beforeDraw=row["sequence"],
                           identicalStoredRgba=(previous_hash == details["beforeFileSha256"]))
                result["sameSurfaceIntervals"].append(gap)
            last_on_surface[key] = row["sequence"], after, details["afterFileSha256"]
        if artifacts is not None:
            stem = f"draw-{row['sequence']:04d}"
            mask_name = stem + "-changed-rgb.png"
            artifacts.append((mask_name, changed.astype(np.uint8) * 255))
            details["changedRgbMask"] = mask_name
            if additive_rgb(row):
                change, measurement = measure_additive(before, after, capture["viewFormat"])
                change_name = stem + "-linear-rgb-change.npy"
                artifacts.append((change_name, change))
                details["observedAdditiveRgb"] = dict(measurement, file=change_name)
        for key, pixels in (("before", before), ("after", after)):
            details[key] = {ch: channel_stats(plane) for ch, plane in zip("RGBA", pixels.transpose(2, 0, 1))}
        peak = max(1., *(details[k][ch]["max"] or 0. for k in ("before", "after") for ch in "RGB"))
        details["previewScale"] = {"sharedPeak": peak, "log1p": peak > 1}
        panels.append((row["sequence"], *(preview(image[:, :, :3], peak, peak > 1) for image in (before, after))))
    if "terminalColor" in record:
        result["terminalColor"] = inspect_terminal(path.parent, record["terminalColor"], artifacts)
    return result, panels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    args = parser.parse_args()
    try:
        artifacts = []
        result, rows = inspect(args.report, artifacts)
        out = args.out.resolve()
        out.mkdir(parents=True, exist_ok=False)
        for name, values in artifacts:
            if name.endswith(".npy"):
                np.save(out / name, values, allow_pickle=False)
            else:
                Image.fromarray(values).save(out / name)
        if rows:
            pw, ph = PANEL
            sheet = Image.new("RGB", (2 * (pw + 16) + 16, 44 + len(rows) * (ph + 42)), "#16181c")
            draw = ImageDraw.Draw(sheet)
            draw.text((16, 12), result["hostExecutable"] + "  |  RT0  |  shared scale within each draw", fill="white")
            for i, (sequence, left, right) in enumerate(rows):
                y = 44 + i * (ph + 42)
                for col, (label, panel) in enumerate((("Before", left), ("After", right))):
                    x = 16 + col * (pw + 16)
                    draw.text((x, y), f"Draw {sequence}    {label}", fill="white")
                    sheet.paste(panel, (x + (pw - panel.width) // 2, y + 20))
            sheet.save(out / "contact.png")
        (out / "report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Cannot inspect draw pixels: {error}\n")
    print(out / "report.json")


if __name__ == "__main__":
    main()
