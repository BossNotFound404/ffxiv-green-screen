"""Fold a verified contiguous series of linear RT0 draws into two RGB EXR passes."""
import argparse
import json
from pathlib import Path

import numpy as np
import OpenEXR
from PIL import Image

from check_rt0_heldout import compare
from export_rt0_draw import blend_mode, read_draw
from verify_tower_layers import digest


def write_rgb(path, values):
    pixels = values.astype(np.float32)
    header = {"compression": OpenEXR.ZIP_COMPRESSION, "type": OpenEXR.scanlineimage}
    with OpenEXR.File(header, {"RGB": pixels}) as image:
        image.write(str(path))
    with OpenEXR.File(str(path)) as image:
        decoded = image.channels()["RGB"].pixels.copy()
    if not np.array_equal(pixels, decoded):
        raise ValueError(f"{path.name} failed the float32 EXR round trip.")
    return decoded


def pack(report, layers, out, first, last):
    if last < first:
        raise ValueError("The last draw must be at or after the first.")
    prior, present, surface = None, None, None
    input_hashes = {}
    for seq in range(first, last + 1):
        (before, after), hashes, event, crop, frame = read_draw(report, seq, None)
        input_hashes.update(hashes)
        cap = event["pixelCapture"]
        identity = (crop, *(cap[k] for k in ("targetResourceId", "targetViewId", "mipSlice", "arraySlice")))
        if prior is None:
            present, surface = frame["present"], identity
            base = before[:, :, :3].copy()
            gain, emission = np.ones_like(base), np.zeros_like(base)
            uncertain = np.zeros_like(base, dtype=bool)
            stages = []
        elif identity != surface or not np.array_equal(before, prior):
            raise ValueError(f"Discontinuous RT0 input at draw {seq}.")
        prior = after
        mode = blend_mode(event)
        if np.array_equal(before, after):
            # An invisible over draw can be ambiguous where the input alpha is already opaque.
            if mode == "source-over":
                uncertain |= (before[:, :, 3:] >= 31 / 32) & (gain != 0)
            if mode == "screen":
                uncertain |= (before[:, :, :3] == 1) & (gain != 0)
            stages.append({"draw": seq, "mode": mode, "observedNoChange": True})
            continue
        folder = layers / f"draw-{seq}"
        meta = json.loads((folder / "layer.json").read_text(encoding="utf-8-sig"))
        hdr_over = mode == "source-over" and meta.get("format") == "linear premultiplied float32 RGBA"
        array = folder / ("layer.exr" if hdr_over else "layer-linear.npy")
        if (meta["inputSha256"] != hashes or meta["draw"] != seq or meta["blend"] != mode
                or digest(array.read_bytes()) != meta["outputSha256"][array.name]):
            raise ValueError(f"Layer provenance mismatch at draw {seq}.")
        if hdr_over:
            with OpenEXR.File(str(array)) as image:
                field = image.channels()["RGBA"].pixels.astype(np.float64)
        else:
            field = np.load(array, allow_pickle=False).astype(np.float64)
        expected_channels = 4 if mode == "source-over" else 3
        if field.shape != (*base.shape[:2], expected_channels) or np.isinf(field).any():
            raise ValueError(f"Invalid contribution array at draw {seq}.")
        additive = field[:, :, :3].copy()
        missing = np.isnan(additive)
        if mode == "screen":
            if not np.array_equal(missing, before[:, :, :3] == 1):
                raise ValueError(f"Unexpected Screen uncertainty at draw {seq}.")
            additive[missing] = 0
            multiplier = 1 - additive
        elif mode == "source-over":
            multiplier = 1 - field[:, :, 3:]
        else:
            multiplier = 1.0
        if not np.isfinite(multiplier).all() or not np.isfinite(additive).all():
            raise ValueError(f"Unresolved non-Screen values at draw {seq}.")
        uncertain = (uncertain & (multiplier != 0)) | (missing & (gain != 0))
        gain *= multiplier
        emission = emission * multiplier + additive
        stages.append({"draw": seq, "mode": mode, "singularChannels": int(missing.sum())})

    out.mkdir(parents=True, exist_ok=False)
    transmission = write_rgb(out / "01-multiply.exr", gain)
    light = write_rgb(out / "02-add.exr", emission)
    image = light.astype(np.float64) + base * transmission.astype(np.float64)
    metrics, _ = compare(image, prior[:, :, :3], np.any(base != prior[:, :, :3], axis=2))
    Image.fromarray(uncertain.astype(np.uint8) * 255).save(out / "uncertain-rgb.png")
    result = {
        "host": frame["hostExecutable"], "present": present, "crop": crop,
        "captureSha256": hashes[report.name], "inputSha256": input_hashes, "stages": stages,
        "scope": "Measured RT0 interval before scene post-processing. Reconstructed from a single background.",
        "formula": "Linear RGB: new background * 01-multiply.exr + 02-add.exr",
        "uncertainty": "The PNG marks channels with indeterminate background response. Near-unit Screen inputs also amplify FP16 measurement uncertainty. Shader response to a different scene has not been validated.",
        "uncertainChannels": int(uncertain.sum()), "uncertainPixels": int(uncertain.any(axis=2).sum()),
        "sameBackgroundAfterExrDecode": metrics,
        "outputSha256": {p.name: digest(p.read_bytes()) for p in sorted(out.iterdir())},
    }
    (out / "verification.json").write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("report", type=Path)
    cli.add_argument("layers", type=Path)
    cli.add_argument("--first", type=int, required=True)
    cli.add_argument("--last", type=int, required=True)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    pack(args.report, args.layers, args.out, args.first, args.last)
