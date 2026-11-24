"""Check an exported RGBA16 layer pair against a separate FP16 Screen/Add observation."""
import argparse
import json
from pathlib import Path
import struct
import zlib

import numpy as np

from export_rt0_pair import read_pair, recover_contribution
from verify_tower_layers import digest


def read_exported_png(path, expected_sha, width, height):
    """Decode the exact RGBA16, sRGB-tagged, unfiltered PNG subset our exporter writes."""
    data = path.read_bytes()
    if digest(data) != expected_sha or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Invalid PNG or recorded SHA-256 mismatch: {path.name}.")
    chunks = []
    offset = 8
    while offset < len(data):
        size, kind = struct.unpack_from(">I4s", data, offset)
        end = offset + 8 + size
        payload = data[offset + 8:end]
        crc, = struct.unpack_from(">I", data, end)
        if zlib.crc32(kind + payload) != crc:
            raise ValueError(f"Invalid PNG chunk checksum: {path.name}.")
        chunks.append((kind, payload))
        offset = end + 4
    if [kind for kind, _ in chunks] != [b"IHDR", b"sRGB", b"IDAT", b"IEND"]:
        raise ValueError(f"Unexpected PNG chunk sequence: {path.name}.")
    header, color, pixels, end_chunk = [payload for _, payload in chunks]
    if header != struct.pack(">IIBBBBB", width, height, 16, 6, 0, 0, 0) or color != b"\x00" or end_chunk:
        raise ValueError(f"Expected the exporter RGBA16 sRGB format: {path.name}.")
    row_bytes = 1 + width * 8
    expected_size = height * row_bytes
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(pixels, expected_size + 1)
    if len(decoded) != expected_size or not decoder.eof or decoder.unused_data:
        raise ValueError(f"Unexpected RGBA16 payload size: {path.name}.")
    rows = np.frombuffer(decoded, dtype=np.uint8).reshape(height, row_bytes)
    if rows[:, 0].any():
        raise ValueError(f"Expected unfiltered exporter PNG rows: {path.name}.")
    return rows[:, 1:].copy().view(">u2").reshape(height, width, 4)


def fp16_rank(rgb):
    # Map the signed FP16 bit patterns to monotone integer positions, merging signed zero.
    bits = rgb.view(np.uint16).astype(np.int32)
    magnitude = bits & 0x7fff
    return np.where(bits & 0x8000, -magnitude, magnitude)


def compare(predicted, observed, support):
    with np.errstate(over="ignore"):
        rounded = predicted.astype(np.float16)
    if not np.isfinite(rounded).all():
        raise ValueError("Recomposed RGB overflows the recorded FP16 format.")
    absolute = np.abs(rounded.astype(np.float64) - observed)
    steps = np.abs(fp16_rank(rounded) - fp16_rank(observed.astype(np.float16)))
    return {
        "affectedPixels": int(np.count_nonzero(support)),
        "exactAffectedRgbPixels": int(np.count_nonzero(support & ~np.any(steps, axis=2))),
        "maxAffectedAbsLinearError": float(absolute[support].max(initial=0)),
        "maxAffectedFp16Steps": int(steps[support].max(initial=0)),
        "maxAbsLinearError": float(absolute.max()),
        "maxFp16Steps": int(steps.max()),
        "aboveOneRgbChannels": int(np.count_nonzero(rounded > 1)),
        "peakLinearRgb": float(rounded.max()),
    }, rounded.astype(np.float64)


def changed_background(training, heldout, contribution):
    return int(np.count_nonzero(np.any(training != heldout, axis=2) & np.any(contribution != 0, axis=2)))


def check(manifest, training_report, held_report, held_sequences, control, max_steps):
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    trained, crop, trained_hashes, trained_host, trained_present = read_pair(training_report, metadata["draws"], None)
    if trained_hashes != metadata["inputSha256"]:
        raise ValueError("The supplied training capture does not match all five input SHA-256s in the manifest.")
    held, held_crop, held_hashes, held_host, held_present = read_pair(held_report, held_sequences, None)
    if set(trained_hashes.values()) & set(held_hashes.values()):
        raise ValueError("The held-out capture shares report or native plane SHA-256s with training.")
    if crop != held_crop:
        raise ValueError("The training and held-out target geometries or crops differ.")
    x, y, w, h, tw, th = crop
    if (x, y, w, h, tw, th) != tuple(metadata["crop"][key] for key in
                                     ("x", "y", "width", "height", "targetWidth", "targetHeight")):
        raise ValueError("Training geometry fails to match the layer manifest.")
    output_hashes = metadata["outputSha256"]
    screen, add = [recover_contribution(read_exported_png(manifest.parent / name, output_hashes[name], w, h))
                   for name in ("outer-screen.png", "inner-add.png")]
    before, middle, after = (frame[:, :, :3] for frame in held)
    trained_before, trained_middle = (frame[:, :, :3] for frame in trained[:2])
    screen_mask = np.any(screen != 0, axis=2) | np.any(middle != before, axis=2)
    add_mask = np.any(add != 0, axis=2) | np.any(after != middle, axis=2)
    screen_bg = changed_background(trained_before, before, screen)
    add_bg = changed_background(trained_before, before, add)
    add_stage_bg = changed_background(trained_middle, middle, add)
    if not all((screen_bg, add_bg, add_stage_bg)):
        raise ValueError("Held-out backgrounds must differ on both nonzero exported layer supports.")
    screen_metrics, predicted_middle = compare(before + (1 - before) * screen, middle, screen_mask)
    add_metrics, _ = compare(middle + add, after, add_mask)
    both_metrics, _ = compare(predicted_middle + add, after, screen_mask | add_mask)
    passes = all(row["maxFp16Steps"] <= max_steps for row in (screen_metrics, add_metrics, both_metrics))
    return {
        "claim": ("Controlled-fixture result. Same pose/time and a separate observation are caller-declared."
                  if control == "controlled-fixture" else
                  "Numerical comparison of distinct recorded frames. Same pose/time and isolated background change are unconfirmed."),
        "training": {"host": trained_host, "present": trained_present,
                     "draws": metadata["draws"], "inputSha256": trained_hashes},
        "heldOut": {"host": held_host, "present": held_present,
                    "draws": list(held_sequences), "inputSha256": held_hashes},
        "provenance": "The two input digest sets are disjoint. This excludes identical report or native-plane copies; "
                      "the source workflow and its independence require external evidence.",
        "testedLayerPngSha256": {name: output_hashes[name] for name in ("outer-screen.png", "inner-add.png")},
        "differentBackgroundPixelsOnExportedSupport": {"screen": screen_bg, "add": add_bg},
        "differentIntermediatePixelsOnExportedAddSupport": add_stage_bg,
        "maxAllowedFp16Steps": max_steps,
        "withinTolerance": passes,
        "screen": screen_metrics,
        "addOnObservedIntermediate": add_metrics,
        "both": both_metrics,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="layers.json produced by the exporter")
    parser.add_argument("training_report", type=Path, help="original report used to train the pair")
    parser.add_argument("held_report", type=Path, help="separate native or controlled fixture report")
    parser.add_argument("--screen", type=int, required=True, help="held-out Screen sequence")
    parser.add_argument("--add", type=int, required=True, help="held-out Add sequence")
    parser.add_argument("--control", choices=("controlled-fixture", "unconfirmed-frame"), required=True)
    parser.add_argument("--max-fp16-steps", type=int, default=2)
    args = parser.parse_args()
    if args.max_fp16_steps < 0:
        parser.error("--max-fp16-steps must be nonnegative")
    try:
        result = check(args.manifest, args.training_report, args.held_report, (args.screen, args.add),
                       args.control, args.max_fp16_steps)
    except (KeyError, OSError, TypeError, ValueError, struct.error, zlib.error) as error:
        parser.exit(1, f"Cannot compare held-out RT0 layers: {error}\n")
    print(json.dumps(result, indent=2, allow_nan=False))
    if not result["withinTolerance"]:
        parser.exit(2)


if __name__ == "__main__":
    main()
