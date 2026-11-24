"""Export one explicitly selected linear FP16 RT0 draw as a straight RGBA16 PNG."""
import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from PIL import Image

from check_rt0_heldout import compare
from export_rt0_pair import layer_codes, png16, recover_contribution
from inspect_scene_buffers import member
from verify_tower_layers import digest, srgb


def read_draw(report, sequence, expected_hashes):
    report = report.resolve(strict=True)
    source = report.read_bytes()
    record = json.loads(source.decode("utf-8-sig"))
    if record.get("api") != "D3D11" or record.get("pixelCaptureMode") != "before_after_rt0":
        raise ValueError("Expected a D3D11 before/after RT0 report.")
    rows = [row for row in record["events"] if row["sequence"] == sequence]
    if len(rows) != 1 or "pixelCapture" not in rows[0]:
        raise ValueError(f"Expected one pixel-capture record for draw {sequence}.")
    row = rows[0]
    cap = row["pixelCapture"]
    if (cap["status"] != "captured" or cap["forwarded"] is not True or cap["rtIndex"] != 0
            or cap["viewFormat"] != 10 or cap["resourceFormat"] not in (9, 10)
            or cap["sampleCount"] != 1 or cap["layout"] != "rgba16f" or cap["srgb"] is not False):
        raise ValueError(f"Draw {sequence} lacks a successful single-sample linear RGBA16F RT0 pair "
                         f"(status: {cap['status']}).")
    x, y, w, h, tw, th = (cap[k] for k in ("x", "y", "width", "height", "targetWidth", "targetHeight"))
    if not (0 < w <= 512 and 0 < h <= 512 and 0 <= x <= tw - w and 0 <= y <= th - h and cap["rowBytes"] == w * 8):
        raise ValueError(f"Draw {sequence} has invalid dimensions or stride.")
    hashes = {report.name: digest(source)}
    frames = []
    for key in ("beforeFile", "afterFile"):
        path = member(report.parent, cap[key])
        if path.name in hashes:
            raise ValueError("Expected two distinct before/after sidecars.")
        raw = path.read_bytes()
        if len(raw) != w * h * 8:
            raise ValueError(f"Incomplete FP16 plane: {path.name}.")
        hashes[path.name] = digest(raw)
        frame = np.frombuffer(raw, dtype="<f2").reshape(h, w, 4).astype(np.float64)
        if np.any(~np.isfinite(frame)) or np.any(np.abs(frame) == np.finfo(np.float16).max):
            raise ValueError("Captured values contain a nonfinite component or reach an FP16 saturation boundary.")
        frames.append(frame)
    if expected_hashes and list(hashes.values()) != [value.lower() for value in expected_hashes]:
        raise ValueError("Input SHA-256 mismatch.")
    crop = {"x": x, "y": y, "width": w, "height": h, "targetWidth": tw, "targetHeight": th}
    return frames, hashes, row, crop, record


def rgb_blend_mode(row):
    if row["blendEnabled"] is not True or row["blendOp"] != 1 or row["writeMask"] & 7 != 7:
        raise ValueError("Expected enabled ADD blending with all three RGB channels writable.")
    if (row.get("logicOperation") is not None or row.get("alphaToCoverage")
            or not row.get("sampleMask", 1) & 1):
        raise ValueError("Logic ops, alpha-to-coverage, and a masked RT0 sample are unsupported.")
    factors = row["srcBlend"], row["dstBlend"]
    if factors in ((2, 4), (10, 2)):
        return "screen"
    if factors in ((2, 2), (5, 2)):
        return "add"
    if factors in ((2, 6), (5, 6)):
        return "source-over"
    raise ValueError(f"Unsupported RGB blend factors: {factors}.")


def blend_mode(row):
    mode = rgb_blend_mode(row)
    if mode == "source-over" and (not row["writeMask"] & 8 or
            (row["srcAlphaBlend"], row["dstAlphaBlend"], row["alphaBlendOp"]) != (2, 6, 1)):
        raise ValueError("Source-over coverage is underdetermined: RT0 alpha must be written with ONE/INV_SRC_ALPHA/ADD.")
    return mode


def measured_over(before, after):
    base_alpha, stored_alpha = before[:, :, 3:], after[:, :, 3:]
    if np.any((base_alpha < 0) | (base_alpha > 1) | (stored_alpha < 0) | (stored_alpha > 1)):
        raise ValueError("Source-over coverage requires normalized stored alpha.")
    remaining = 1 - base_alpha
    # Above 31/32, FP16 half-step uncertainty is magnified by more than 32.
    if np.any(remaining < 1 / 32):
        raise ValueError("Stored background alpha is opaque or too close to one for reliable coverage inversion.")
    opacity = (stored_alpha - base_alpha) / remaining
    premultiplied = after[:, :, :3] - before[:, :, :3] * (1 - opacity)
    if np.any((opacity < 0) | (opacity > 1)):
        raise ValueError("Measured opacity exceeds the coverage range.")
    return premultiplied, opacity


def source_over(before, after):
    premultiplied, opacity = measured_over(before, after)
    if np.any((premultiplied < 0) | (premultiplied > opacity)):
        raise ValueError("Measured RGB and stored alpha do not encode a valid straight source-over layer.")
    straight = np.divide(premultiplied, opacity, out=np.zeros_like(premultiplied), where=opacity > 0)
    color = np.rint(srgb(straight) * 65535).astype(np.uint16)
    alpha = np.rint(opacity * 65535)
    alpha = np.where(opacity > 0, np.maximum(alpha, 1), 0).astype(np.uint16)
    return np.concatenate((color, alpha), axis=2), premultiplied, opacity


def export_hdr_over(report, out, sequence, expected_hashes):
    import OpenEXR

    (before, after), hashes, row, crop, frame = read_draw(report, sequence, expected_hashes)
    if blend_mode(row) != "source-over":
        raise ValueError("Expected a source-over draw with observable alpha.")
    premultiplied, opacity = measured_over(before, after)
    if not np.any(opacity) or np.any((opacity == 0) & (premultiplied != 0)):
        raise ValueError("Zero coverage with a color change cannot be represented by this layer.")
    # Subtracting two rounded RT0 values can yield a tiny negative. Accept a
    # nonnegative layer only when it exactly reproduces all recorded RGB and A.
    rgba = np.concatenate((np.maximum(premultiplied, 0), opacity), axis=2).astype(np.float32)
    out = out.resolve()
    if out.exists():
        raise ValueError("Choose a new output directory.")
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out.parent) as temp:
        stage = Path(temp) / "verified"
        stage.mkdir()
        layer = stage / "layer.exr"
        with OpenEXR.File({"compression": OpenEXR.ZIP_COMPRESSION}, {"RGBA": rgba}) as image:
            image.write(str(layer))
        with OpenEXR.File(str(layer)) as image:
            decoded = image.channels()["RGBA"].pixels.astype(np.float64)
        if not np.array_equal(rgba, decoded):
            raise ValueError("EXR changed the stored RGBA values.")
        support = np.any(before != after, axis=2) | (decoded[:, :, 3] > 0)
        rgb, _ = compare(decoded[:, :, :3] + before[:, :, :3] * (1 - decoded[:, :, 3:]), after[:, :, :3], support)
        alpha, _ = compare(decoded[:, :, 3:] + before[:, :, 3:] * (1 - decoded[:, :, 3:]), after[:, :, 3:], support)
        if rgb["maxFp16Steps"] or alpha["maxFp16Steps"]:
            raise ValueError("This HDR layer fails exact native reconstruction on the observed background.")
        meta = {"present": frame["present"], "draw": sequence, "host": frame["hostExecutable"],
                "crop": crop, "blend": "source-over", "format": "linear premultiplied float32 RGBA",
                "scope": "One draw on its observed RT0 background, before game post-processing.",
                "peakPremultipliedRgb": float(decoded[:, :, :3].max()),
                "pixelsWithStraightRgbAboveOne": int(np.any(decoded[:, :, :3] > decoded[:, :, 3:], axis=2).sum()),
                "pixelsWithNegativeRoundingClamped": int((premultiplied < 0).any(axis=2).sum()),
                "reconstructionRgb": rgb, "reconstructionAlpha": alpha, "inputSha256": hashes,
                "outputSha256": {layer.name: digest(layer.read_bytes())}}
        encoded = layer.read_bytes()
    metadata = json.dumps(meta, indent=2, allow_nan=False) + "\n"
    out.mkdir()
    try:
        (out / "layer.exr").write_bytes(encoded)
        (out / "layer.json").write_text(metadata, encoding="utf-8")
    except OSError:
        (out / "layer.json").unlink(missing_ok=True)
        (out / "layer.exr").unlink(missing_ok=True)
        out.rmdir()
        raise


def export(report, out, sequence, expected_hashes, allow_partial_screen=False):
    (before, after), hashes, row, crop, record = read_draw(report, sequence, expected_hashes)
    mode = blend_mode(row)
    background, observed = before[:, :, :3], after[:, :, :3]
    opacity = None
    unknown = None
    minimum_denominator = None
    if mode == "source-over":
        codes, contribution, opacity = source_over(before, after)
        minimum_denominator = float(np.min(1 - before[:, :, 3]))
    else:
        contribution = observed - background
        if mode == "screen":
            denominator = 1 - background
            singular = denominator == 0
            if singular.any():
                if not allow_partial_screen:
                    raise ValueError("Screen inversion is singular on a unit background component.")
                if np.any(contribution[singular] != 0):
                    raise ValueError("A unit input changed under Screen; a zero fallback would misrepresent this draw.")
                unknown = singular
            contribution = np.divide(contribution, denominator, out=np.zeros_like(background), where=~singular)
            if np.any(~singular):
                minimum_denominator = float(np.min(np.abs(denominator[~singular])))
        codes = layer_codes(contribution)
    support = codes[:, :, 3] != 0
    if not np.any(support):
        raise ValueError("The pair measures no encodable RGB contribution or coverage.")
    decoded = recover_contribution(codes)
    alpha_metrics = None
    if opacity is not None:
        alpha = codes[:, :, 3:] / 65535
        predicted = background * (1 - alpha) + decoded
        predicted_alpha = before[:, :, 3:] * (1 - alpha) + alpha
        alpha_metrics, _ = compare(predicted_alpha, after[:, :, 3:], support)
    else:
        predicted = background + (1 - background) * decoded if mode == "screen" else background + decoded
    metrics, _ = compare(predicted, observed, support | np.any(background != observed, axis=2))
    tolerance = 2
    residual = max(metrics["maxFp16Steps"], alpha_metrics["maxFp16Steps"] if alpha_metrics else 0)
    if residual > tolerance:
        raise ValueError(f"The encoded layer misses recorded FP16 by up to {residual} steps (limit {tolerance}).")
    alpha_label = ("Recovered coverage from the stored alpha blend equation, subject to FP16 and PNG quantization."
                   if opacity is not None else "Linear transport weight for the RGB contribution.")
    result = {
        "scope": "One selected draw's effective contribution inside the observed RT0 crop.",
        "attribution": "Candidate metadata is copied from the observation. Effect and instance ownership are unconfirmed.",
        "hostExecutable": record["hostExecutable"],
        "present": record["present"],
        "draw": sequence,
        "candidates": row.get("towerCandidates", []),
        "crop": crop,
        "inputSha256": hashes,
        "inputHashesComparedWithExpected": bool(expected_hashes),
        "blend": mode,
        "blendRecord": {key: row[key] for key in ("srcBlend", "dstBlend", "blendOp", "srcAlphaBlend",
                                                   "dstAlphaBlend", "alphaBlendOp", "writeMask") if key in row},
        "minimumInversionDenominator": minimum_denominator,
        "pngAlpha": alpha_label,
        "composition": "Decode sRGB RGB, weight by the linear PNG alpha, and use the reported blend in linear light.",
        "linearArray": ("Premultiplied linear source-over RGB and recovered alpha."
                        if opacity is not None else "Effective linear RGB used by the reported blend."),
        "maxAllowedFp16Steps": tolerance,
        "reconstructionRgb": metrics,
        "reconstructionAlpha": alpha_metrics,
        "outputSha256": {},
    }
    data = png16(codes)
    out.mkdir(parents=True, exist_ok=False)
    (out / "layer.png").write_bytes(data)
    native = out / "layer-linear.npy"
    if opacity is not None:
        contribution = np.concatenate((contribution, opacity), axis=2)
    if unknown is not None:
        contribution[unknown] = np.nan
    np.save(native, contribution.astype(np.float32), allow_pickle=False)
    result["outputSha256"] = {"layer.png": digest(data), native.name: digest(native.read_bytes())}
    if unknown is not None:
        mask = out / "unknown-rgb-channels.png"
        Image.fromarray(unknown.astype(np.uint8) * 255).save(mask)
        result["outputSha256"][mask.name] = digest(mask.read_bytes())
        result["unknownChannels"] = int(unknown.sum())
        result["pixelsWithUnknownChannels"] = int(unknown.any(axis=2).sum())
        result["unknownPolicy"] = "The PNG substitutes zero for unit-background Screen channels. The mask marks those channels, and the linear array stores NaN. Their response to a new background is unknown."
    (out / "layer.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True, help="a new directory")
    parser.add_argument("--expected-sha256", nargs=3, metavar="HASH",
                        help="expected report, before, and after SHA-256s in that order")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--hdr-over", action="store_true", help="export a verified premultiplied HDR source-over layer to OpenEXR")
    modes.add_argument("--allow-partial-screen", action="store_true",
                        help="export observable Screen channels and mark any unit-background unknowns in a separate RGB mask")
    args = parser.parse_args()
    try:
        if args.hdr_over:
            export_hdr_over(args.report, args.out, args.sequence, args.expected_sha256)
        else:
            export(args.report, args.out, args.sequence, args.expected_sha256, args.allow_partial_screen)
    except (ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        parser.exit(1, f"Cannot export RT0 draw: {error}\n")
    print(args.out / "layer.json")


if __name__ == "__main__":
    main()
