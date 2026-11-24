"""Export two consecutive FP16 RT0 draws as linear-light Screen and Add layers."""
import argparse
import json
from pathlib import Path
import struct
import zlib

import numpy as np

from inspect_scene_buffers import member
from verify_tower_layers import digest, linear, srgb


def read_pair(report, sequences, expected_hashes):
    report = report.resolve(strict=True)
    source = report.read_bytes()
    record = json.loads(source.decode("utf-8-sig"))
    if record.get("api") != "D3D11" or record.get("pixelCaptureMode") != "before_after_rt0":
        raise ValueError("Expected a D3D11 before/after RT0 report.")
    sequence_screen, sequence_add = sequences
    if sequence_add != sequence_screen + 1:
        raise ValueError("The selected draws must be consecutive, in Screen-then-Add order.")

    hashes = {report.name: digest(source)}
    shape = crop = None
    data = []
    for sequence, mode in zip(sequences, ("screen", "add")):
        rows = [r for r in record["events"] if r["sequence"] == sequence]
        if len(rows) != 1:
            raise ValueError(f"Expected one record for draw {sequence}.")
        row = rows[0]
        rgb_factors = row["srcBlend"], row["dstBlend"]
        allowed = ((2, 4), (10, 2)) if mode == "screen" else ((2, 2), (5, 2))
        if (row["blendEnabled"] is not True or row["writeMask"] & 7 != 7
                or row["blendOp"] != 1 or rgb_factors not in allowed):
            raise ValueError(f"Draw {sequence} does not have the required full-RGB {mode} blend.")
        capture = row["pixelCapture"]
        if (capture["status"] != "captured" or capture["forwarded"] is not True or capture["rtIndex"] != 0
                or capture["viewFormat"] != 10 or capture["resourceFormat"] not in (9, 10)
                or capture["sampleCount"] != 1 or capture["layout"] != "rgba16f" or capture["srgb"] is not False):
            raise ValueError(f"Draw {sequence} is missing a single-sample linear RGBA16F RT0 pair.")
        x, y, w, h, tw, th = (capture[k] for k in ("x", "y", "width", "height", "targetWidth", "targetHeight"))
        if not (0 < w <= 512 and 0 < h <= 512 and 0 <= x <= tw - w and 0 <= y <= th - h
                and capture["rowBytes"] == w * 8):
            raise ValueError(f"Draw {sequence} has invalid dimensions or stride.")
        if crop is not None and crop != (x, y, w, h, tw, th):
            raise ValueError("The draws have different target sizes or crops.")
        crop = x, y, w, h, tw, th
        shape = h, w, 4
        for key in ("beforeFile", "afterFile"):
            path = member(report.parent, capture[key])
            if path.name in hashes:
                raise ValueError("The report must reference four distinct pixel planes.")
            raw = path.read_bytes()
            if len(raw) != w * h * 8:
                raise ValueError(f"Incomplete FP16 plane {path.name}.")
            hashes[path.name] = digest(raw)
            data.append(raw)
    if expected_hashes and list(hashes.values()) != [value.lower() for value in expected_hashes]:
        raise ValueError("Input SHA-256 mismatch.")
    if data[1] != data[2]:
        raise ValueError("Screen after and Add before differ at the byte level.")
    frames = [np.frombuffer(data[i], dtype="<f2").reshape(shape).astype(np.float64) for i in (0, 1, 3)]
    if not all(np.isfinite(frame).all() for frame in frames):
        raise ValueError("A captured RGBA16F value is nonfinite.")
    return frames, crop, hashes, record["hostExecutable"], record["present"]


def layer_codes(rgb):
    """Encode a contribution as straight sRGB with a quantized linear weight."""
    if np.any((rgb < 0) | (rgb > 1) | ~np.isfinite(rgb)):
        raise ValueError("A contribution cannot be encoded as unsigned unit-range RGBA.")
    alpha = np.ceil(np.max(rgb, axis=2, keepdims=True) * 65535).astype(np.uint16)
    weight = alpha.astype(np.float64) / 65535
    straight = np.divide(rgb, weight, out=np.zeros_like(rgb), where=weight > 0)
    rgb_codes = np.rint(srgb(np.minimum(straight, 1.0)) * 65535).astype(np.uint16)
    return np.concatenate((rgb_codes, alpha), axis=2)


def chunk(kind, data):
    check = zlib.crc32(kind + data)
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", check)


def png16(rgba):
    """Write PNG RGBA16 with IEC sRGB RGB and linear alpha."""
    h, w, _ = rgba.shape
    scanlines = b"".join(b"\x00" + row.tobytes() for row in rgba.astype(">u2"))
    header = struct.pack(">IIBBBBB", w, h, 16, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"sRGB", b"\x00")
            + chunk(b"IDAT", zlib.compress(scanlines, level=9)) + chunk(b"IEND", b""))


def recover_contribution(codes):
    rgba = codes.astype(np.float64) / 65535
    return linear(rgba[:, :, :3]) * rgba[:, :, 3:]


def reconstruction_metrics(expected, fp16_result):
    delta = np.abs(fp16_result - expected)
    return {
        "maxAbsLinearError": float(delta.max()),
        "exactRgbPixels": int(np.count_nonzero(np.all(delta == 0, axis=2))),
        "changedRgbChannels": int(np.count_nonzero(delta)),
        "aboveOneRgbChannels": int(np.count_nonzero(fp16_result > 1)),
        "peakLinearRgb": float(fp16_result.max()),
    }


def export(report, out, sequences, expected_hashes):
    frames, crop, hashes, host, present = read_pair(report, sequences, expected_hashes)
    before, middle, after = [frame[:, :, :3] for frame in frames]
    denominator = 1 - before
    if np.any(denominator == 0):
        raise ValueError("Screen inversion is singular where the stored background equals one.")
    screen = (middle - before) / denominator
    additive = after - middle
    screen_codes = layer_codes(screen)
    add_codes = layer_codes(additive)
    screen_rgb, add_rgb = recover_contribution(screen_codes), recover_contribution(add_codes)
    reconstructed_middle = (before + (1 - before) * screen_rgb).astype(np.float16).astype(np.float64)
    reconstructed_after = (reconstructed_middle + add_rgb).astype(np.float16).astype(np.float64)
    x, y, w, h, tw, th = crop
    payload = {
        "scope": "Effective RGB of two recorded draws within the captured RT0 crop.",
        "hostExecutable": host,
        "present": present,
        "draws": list(sequences),
        "crop": {"x": x, "y": y, "width": w, "height": h, "targetWidth": tw, "targetHeight": th},
        "inputSha256": hashes,
        "inputHashesComparedWithExpected": bool(expected_hashes),
        "screenMinimumAbsoluteDenominator": float(np.min(np.abs(denominator))),
        "sourceAlpha": "Stored RT0 alpha is not used. PNG alpha is a linear encoding weight, not opacity.",
        "composition": "Decode PNG RGB from sRGB, multiply by linear PNG alpha. "
                       "Apply Screen as B + (1-B)*screen on linear B. Then apply Add as B+add. "
                       "Keep values over one in a float working buffer.",
        "reconstructionFromEncodedCodes": {
            "pixelCount": w * h,
            "screen": reconstruction_metrics(middle, reconstructed_middle),
            "addOnRecordedBackground": reconstruction_metrics(
                after, (middle + add_rgb).astype(np.float16).astype(np.float64)),
            "both": reconstruction_metrics(after, reconstructed_after),
        },
        "outputSha256": {},
    }
    out.mkdir(parents=True, exist_ok=False)
    for stem, codes, contribution in (("outer-screen", screen_codes, screen), ("inner-add", add_codes, additive)):
        name = stem + ".png"
        encoded = png16(codes)
        (out / name).write_bytes(encoded)
        payload["outputSha256"][name] = digest(encoded)
        native = out / (stem + "-linear-rgb.npy")
        np.save(native, contribution.astype(np.float32), allow_pickle=False)
        payload["outputSha256"][native.name] = digest(native.read_bytes())
    (out / "layers.json").write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--screen", type=int, required=True)
    parser.add_argument("--add", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True, help="a new directory")
    parser.add_argument("--expected-sha256", nargs=5, metavar="HASH",
                        help="expected SHA-256 in report, Screen before/after, Add before/after order")
    args = parser.parse_args()
    try:
        export(args.report, args.out, (args.screen, args.add), args.expected_sha256)
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(1, f"Cannot export RT0 layers: {error}\n")
    print(args.out / "layers.json")


if __name__ == "__main__":
    main()
