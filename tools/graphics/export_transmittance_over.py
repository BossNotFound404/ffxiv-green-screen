"""Recover one linear FP16 source-over draw whose stored alpha encodes transmittance."""

import argparse
import json
from pathlib import Path

import numpy as np

from export_rt0_draw import read_draw, rgb_blend_mode
from verify_tower_layers import digest


def rounding_interval(values):
    half = values.astype(np.float16)
    below = np.nextafter(half, np.float16(-np.inf)).astype(np.float64)
    above = np.nextafter(half, np.float16(np.inf)).astype(np.float64)
    return (values + below) / 2, (values + above) / 2


def recover(before, after):
    """Return measured T (H×W), P (H×W×3), and closed FP16 rounding bounds statistics."""
    before = np.asarray(before, dtype=np.float64)
    after = np.asarray(after, dtype=np.float64)
    if before.ndim != 3 or before.shape != after.shape or before.shape[-1] != 4 or not before.size:
        raise ValueError("Expected two equally sized nonempty RGBA arrays.")
    for plane in (before, after):
        if not np.all(np.isfinite(plane) & (np.abs(plane) < np.finfo(np.float16).max)):
            raise ValueError("Nonfinite or saturated FP16 inputs do not support this inversion.")
        if not np.array_equal(plane, plane.astype(np.float16)):
            raise ValueError("Expected captured FP16 values.")

    base_rgb, base_alpha = before[..., :3], before[..., 3]
    dst_rgb, dst_alpha = after[..., :3], after[..., 3]
    if np.any((base_alpha <= 0) | (base_alpha > 1) | (dst_alpha < 0) | (dst_alpha > 1)):
        raise ValueError("Coverage requires nonzero normalized destination alpha before the draw and normalized alpha after it.")
    alpha_lo, alpha_hi = rounding_interval(dst_alpha)
    t_lo, t_hi = np.maximum(0, alpha_lo / base_alpha), np.minimum(1, alpha_hi / base_alpha)
    if np.any(t_hi < t_lo) or np.any(t_hi - t_lo >= 1):
        raise ValueError("Stored alpha does not bound physical transmittance to an informative interval.")
    t = np.minimum(dst_alpha / base_alpha, 1).astype(np.float32)
    if np.any((t < t_lo) | (t > t_hi)):
        raise ValueError("Float32 cannot represent the inferred transmittance in its FP16 rounding interval.")

    rgb_lo, rgb_hi = rounding_interval(dst_rgb)
    atten_lo, atten_hi = base_rgb * t_lo[..., None], base_rgb * t_hi[..., None]
    p_lo = rgb_lo - np.maximum(atten_lo, atten_hi)
    p_hi = rgb_hi - np.minimum(atten_lo, atten_hi)
    representative = dst_rgb - base_rgb * t[..., None]
    # Use zero for tiny negative residuals only if zero reconstructs the observed half value.
    can_zero = (base_rgb * t[..., None]).astype(np.float16) == dst_rgb
    rounded_negative = (representative < 0) & can_zero
    p = np.where(rounded_negative, 0, representative).astype(np.float32)
    if np.any((t == 1) & np.any(p != 0, axis=2)):
        raise ValueError("The chosen transmittance gives zero coverage with a color contribution.")

    # Test whether one common T could permit all three channels to have P=0.
    nonzero = base_rgb != 0
    f_lo = np.divide(rgb_lo, base_rgb, out=np.zeros_like(base_rgb), where=nonzero)
    f_hi = np.divide(rgb_hi, base_rgb, out=np.zeros_like(base_rgb), where=nonzero)
    black_lo = np.max(np.where(nonzero, np.minimum(f_lo, f_hi), -np.inf), axis=2)
    black_hi = np.min(np.where(nonzero, np.maximum(f_lo, f_hi), np.inf), axis=2)
    black_ok = np.maximum(t_lo, black_lo) <= np.minimum(t_hi, black_hi)
    black_ok &= np.all(nonzero | ((rgb_lo <= 0) & (rgb_hi >= 0)), axis=2)

    alpha_base_lo, alpha_base_hi = rounding_interval(base_alpha)
    reference_lo = np.maximum(0, alpha_lo / alpha_base_hi)
    reference_hi = np.minimum(1, alpha_hi / alpha_base_lo)
    opacity = 1 - t.astype(np.float64)
    return t, p, {
        "roundingModel": "Closed FP16 midpoint intervals, assuming round-to-nearest output and the exact stored FP16 input to blending. Endpoint ties are conservatively included.",
        "pixelCount": int(base_alpha.size),
        "beforeAlphaRange": [float(base_alpha.min()), float(base_alpha.max())],
        "beforeAlphaSubnormalPixels": int(np.count_nonzero(base_alpha < np.finfo(np.float16).tiny)),
        "maxAlphaToTransmittanceGain": float((1 / base_alpha).max()),
        "maxTransmittanceIntervalWidth": float((t_hi - t_lo).max()),
        "maxTransmittanceWidthIfBothAlphasWereRoundedMeasurements": float((reference_hi - reference_lo).max()),
        "transmittanceRange": [float(t.min()), float(t.max())],
        "coverageRange": [float(opacity.min()), float(opacity.max())],
        "coverageBounds": [float((1 - t_hi).min()), float((1 - t_lo).max())],
        "maxPremultipliedIntervalWidthRgb": (p_hi - p_lo).max(axis=(0, 1)).tolist(),
        "premultipliedMinRgb": p.min(axis=(0, 1)).tolist(),
        "premultipliedMaxRgb": p.max(axis=(0, 1)).tolist(),
        "negativePremultipliedChannelsBeforeRoundTripClamp": int(np.count_nonzero(representative < 0)),
        "negativePremultipliedChannelsRoundedToZeroWithSameFp16Result": int(rounded_negative.sum()),
        "negativePremultipliedChannelsInExport": int(np.count_nonzero(p < 0)),
        "channelsRequiringNegativeContributionWithinRoundingBounds": int(np.count_nonzero(p_hi < 0)),
        "channelsRequiringPositiveContributionWithinRoundingBounds": int(np.count_nonzero(p_lo > 0)),
        "pixelsWhereBlackSourceFitsRoundingBounds": int(black_ok.sum()),
        "pixelsWhereBlackSourceExcludedByRoundingBounds": int((~black_ok).sum()),
        "pixelsWithEstimatedStraightRgbAboveOne": int(np.count_nonzero(np.any(p > opacity[..., None], axis=2))),
    }


def round_trip(before, after, t, p):
    rgb = before[..., :3] * t + p
    alpha = before[..., 3] * t[..., 0]
    decoded = np.concatenate((rgb, alpha[..., None]), axis=2)
    half = decoded.astype(np.float16).astype(np.float64)
    return {"exactRgbaPixels": int(np.count_nonzero(np.all(half == after, axis=2))),
            "changedFp16ChannelsRgba": np.count_nonzero(half != after, axis=(0, 1)).tolist(),
            "maxAbsErrorBeforeFp16RoundingRgba": np.abs(decoded - after).max(axis=(0, 1)).tolist()}


def export(report, out, sequence):
    from pack_rt0_exr import write_rgb

    (before, after), hashes, row, crop, frame = read_draw(report, sequence, None)
    if (rgb_blend_mode(row) != "source-over" or not row["writeMask"] & 8
            or (row["srcBlend"], row["dstBlend"]) != (5, 6)
            or (row["srcAlphaBlend"], row["dstAlphaBlend"], row["alphaBlendOp"]) != (1, 6, 1)):
        raise ValueError("Expected RGB SRC_ALPHA/INV_SRC_ALPHA/ADD and alpha ZERO/INV_SRC_ALPHA/ADD.")
    t, p, metrics = recover(before, after)
    t_rgb = np.repeat(t[..., None], 3, axis=2)
    if round_trip(before, after, t_rgb, p)["exactRgbaPixels"] != metrics["pixelCount"]:
        raise ValueError("Estimated float32 passes fail exact reconstruction of captured FP16 RGBA.")

    out.mkdir(parents=True, exist_ok=False)
    multiply_path, add_path = out / "01-multiply.exr", out / "02-add.exr"
    decoded_t = write_rgb(multiply_path, t_rgb)
    decoded_p = write_rgb(add_path, p)
    verified = round_trip(before, after, decoded_t, decoded_p)
    if verified["exactRgbaPixels"] != metrics["pixelCount"]:
        raise ValueError("EXR passes fail exact reconstruction of captured FP16 RGBA.")

    result = {
        "scope": "One measured draw and its captured crop before later rendering or post-processing.",
        "gameProcess": frame["processId"], "present": frame["present"], "sequence": sequence, "crop": crop,
        "inputSha256": hashes,
        "composition": {
            "color": "linear output RGB = linear background RGB * 01-multiply RGB + 02-add RGB",
            "coverage": "1 - 01-multiply red",
            "capturedRt0Alpha": "background alpha * 01-multiply red",
            "format": "Two float32 linear RGB EXRs. Preserve signed and HDR values.",
        },
        "recovery": metrics, "reconstructionFromExr": verified,
        "outputSha256": {f.name: digest(f.read_bytes()) for f in (multiply_path, add_path)},
    }
    (out / "coverage.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--draw", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True, help="a new output directory")
    args = parser.parse_args()
    try:
        export(args.report, args.out, args.draw)
    except (ImportError, KeyError, OSError, TypeError, ValueError) as exc:
        parser.exit(1, f"Cannot export transmittance: {exc}\n")
    print(args.out / "coverage.json")


if __name__ == "__main__":
    main()
