"""Export display-linear Multiply and Add from a native fan on its captured terrain."""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np
from PIL import Image

from export_fan_light import describe_gap, identity, light_pass
from export_rt0_draw import read_draw, rgb_blend_mode
from export_rt0_pair import png16
from pack_rt0_exr import write_rgb
from verify_tower_layers import digest, linear, srgb


ASSET = "vfx/monster/gimmick3/eff/n4gb_b_g02c0a1.avfx"
ASSET_SHA256 = "a3877e010b18f29fdbdfca7164a6450daf6856f84d4b4e5ecd65f0b4eff37a58"
COVERAGE, ADD, SCREEN = "fan_coverage", "fan_decal_add", "fan_lightning"
BLENDS = {
    COVERAGE: ("source-over", 5, 6, "tone013_c_a1"),
    ADD: ("add", 5, 2, "uv_fire_hikkaki_a1"),
    SCREEN: ("screen", 2, 4, "ele020bm_a1"),
}
NEAR_UNIT = 0.03
PORTABLE_REFERENCE_CODES = (64, 96, 128, 160, 192)


@dataclass
class NativeFan:
    base: np.ndarray
    gain: np.ndarray
    emission: np.ndarray
    transmission: np.ndarray
    support: np.ndarray
    unknown: np.ndarray
    near_unit: np.ndarray
    visible_roles: set
    stages: list
    gaps: list
    coverage: dict

    def result(self, multiply, add, empty):
        return (multiply, add, self.transmission, empty, self.support, self.unknown,
                self.near_unit, self.visible_roles, self.stages, self.gaps, self.coverage)


def observable_coverage_alpha(start, end):
    return (start > 0) & (start <= 1) & (end >= 0) & (end <= start)


def coverage_pass(row, before, after, witness_gain=None, witness_emission=None):
    if (row["srcAlphaBlend"], row["dstAlphaBlend"], row["alphaBlendOp"]) != (1, 6, 1) or not row["writeMask"] & 8:
        raise ValueError("Coverage requires stored ZERO/INV_SRC_ALPHA/ADD alpha.")
    start, end = before[..., 3:], after[..., 3:]
    if witness_gain is None:
        valid = observable_coverage_alpha(start, end)
        # With unobservable alpha, keep unit gain and retain the local RGB delta.
        gain = np.divide(end, start, out=np.ones_like(start), where=valid)
    else:
        valid, gain = np.ones_like(start, dtype=bool), witness_gain
    if witness_emission is not None:
        return gain, witness_emission, np.broadcast_to(~valid, witness_emission.shape)
    attenuated = before[..., :3] * gain
    observed = after[..., :3]
    emission = observed - attenuated
    # A tiny negative residual can already round to the same native FP16 sample.
    emission[(emission < 0) & (attenuated.astype(np.float16) == observed)] = 0
    return gain, emission, np.broadcast_to(~valid, observed.shape)


def load_observations(report, sequence_frame=False):
    record = json.loads(report.read_text(encoding="utf-8-sig"))
    fan = record["fanCast"]
    if (fan["asset"], fan["assetSha256"]) != (ASSET, ASSET_SHA256):
        raise ValueError("Unsupported fan asset or SHA-256.")
    if (fan["status"] != "captured" or fan["qualification"] != "captured"
            or fan["budgetDroppedDraws"] != 0 or fan["observationEntriesDropped"] != 0):
        raise ValueError("Expected a qualified fan capture within its observation and draw budgets.")
    if not sequence_frame and fan["anchors"]["shareTarget"] is not True:
        raise ValueError("Coverage and lightning anchors must share their native RT0.")
    observations, hashes = [], {}
    for event in sorted(record["events"], key=lambda event: event["sequence"]):
        seq = event["sequence"]
        (before, after), inputs, row, crop, snapshot = read_draw(report, seq, None)
        if snapshot != record or hashes.get(report.name, inputs[report.name]) != inputs[report.name]:
            raise ValueError("Capture report changed while reading its draws.")
        if set(inputs).intersection(hashes) - {report.name}:
            raise ValueError(f"Draw {seq} reuses another draw's pixel sidecar.")
        hashes.update(inputs)
        candidate, cap = row["fanCandidate"], row["pixelCapture"]
        role = candidate["role"]
        if role not in BLENDS:
            raise ValueError(f"Unexpected fan role for draw {seq}.")
        mode, src, dst, texture = BLENDS[role]
        if (candidate["expectedBlendAndTarget"] is not True or cap["captureRule"] != role
                or cap["stage"] != "draw_rt0_before_after" or rgb_blend_mode(row) != mode
                or (row["srcBlend"], row["dstBlend"]) != (src, dst)
                or not any(entry["name"] == texture for entry in row["fanTextures"])):
            raise ValueError(f"Unsupported fan candidate, texture or blend for draw {seq}.")
        surface = identity(row, crop)
        if (cap["targetResourceId"] <= 0 or cap["targetViewId"] <= 0
                or (observations and surface != observations[0]["surface"])):
            raise ValueError(f"Missing or inconsistent native RT0 surface for draw {seq}.")
        observations.append(dict(sequence=seq, role=role, blend=mode, row=row, surface=surface,
                                 before=before, after=after))
    if not sequence_frame:
        anchors = fan["anchors"]
        roles = {item["sequence"]: item["role"] for item in observations}
        if (not observations or observations[0]["role"] != COVERAGE
                or roles.get(anchors["coverageSequence"]) != COVERAGE
                or roles.get(anchors["lightningSequence"]) != SCREEN):
            raise ValueError("Expected a pre-effect coverage pair and its qualified lightning anchor.")
    return record, observations, hashes


def split_backing(transmission, multiply):
    """Expose the recorded neutral attenuation below the remaining display gain."""
    alpha = np.rint((1 - transmission) * 65535).astype(np.uint16)
    transmission = 1 - alpha.astype(np.float64) / 65535
    if np.any((transmission == 0) & np.any(multiply != 0, axis=2)):
        raise ValueError("Opaque black cannot reproduce nonzero display gain on every plate.")
    color_gain = np.divide(multiply, transmission[..., None], out=np.ones_like(multiply),
                           where=transmission[..., None] > 0)
    rgba = np.zeros((*alpha.shape, 4), dtype=np.uint16)
    rgba[..., 3] = alpha
    return png16(rgba), transmission, color_gain


def validate_transfer(slope, offset):
    slope, offset = np.asarray(slope, dtype=float), np.asarray(offset, dtype=float)
    if (slope.shape != (3,) or offset.shape != (3,) or not np.isfinite(slope).all()
            or not np.isfinite(offset).all() or np.any(slope <= 0)):
        raise ValueError("Provide three finite positive RGB slopes and three finite RGB offsets.")
    return slope, offset


def solve_native(entries, crop, portable=False):
    """Compose native linear gains and emissions in the captured draw order."""
    shape = (crop["height"], crop["width"], 3)
    base = entries[0]["before"][..., :3] if entries else np.zeros(shape)
    gain, emission = np.ones_like(base), np.zeros_like(base)
    transmission = np.ones(base.shape[:2])
    unknown, near_unit = np.zeros_like(base, dtype=bool), np.zeros_like(base, dtype=bool)
    support = np.zeros(base.shape[:2], dtype=bool)
    coverage_rgb_changed = np.zeros_like(support)
    coverage_alpha_unknown = np.zeros_like(support)
    unmeasurable_draws = []
    witness_draws = []
    replayed_emission_draws = []
    visible_roles, stages = set(), []
    for entry in entries:
        before, after, role = entry["before"], entry["after"], entry["role"]
        rgb_changed = before[..., :3] != after[..., :3]
        changed = rgb_changed.any(axis=2)
        local_support = changed
        near = np.zeros_like(near_unit)
        denominator_min = None
        coverage_stats = {}
        if role == COVERAGE:
            witness_gain = entry.get("witnessGain")
            witness_emission = entry.get("witnessEmission") if portable else None
            local_gain, local_emission, missing = coverage_pass(
                entry["row"], before, after, witness_gain, witness_emission)
            local_support = changed | (local_gain[..., 0] != 1) | (local_emission != 0).any(axis=2)
            transmission *= local_gain[..., 0]
            unmeasurable = changed & missing[..., 0]
            measurable_count = int((changed & ~missing[..., 0]).sum())
            unmeasurable_count = int(unmeasurable.sum())
            if unmeasurable_count and not measurable_count:
                unmeasurable_draws.append(entry["sequence"])
            coverage_alpha_unknown |= unmeasurable
            coverage_rgb_changed |= changed
            coverage_stats = {
                "rgbChangedPixelsMeasurableCoverageAlpha": measurable_count,
                "rgbChangedPixelsUnknownCoverageAlpha": unmeasurable_count,
                "coverageAlphaSource": "isolated_alpha_one_replay" if witness_gain is not None else "stored_rt0_alpha",
            }
            if portable:
                coverage_stats["nativeRgbSource"] = ("isolated_rgb_over_black_replay" if witness_emission is not None
                                                     else "captured_pair_subtraction")
            if "witnessEvidence" in entry:
                coverage_stats["alphaWitness"] = entry["witnessEvidence"]
            if witness_gain is not None:
                witness_draws.append(entry["sequence"])
            if witness_emission is not None:
                replayed_emission_draws.append(entry["sequence"])
        else:
            local_gain, local_emission, missing, denominator_min = light_pass(entry["row"], before, after)
            if role == SCREEN:
                near = np.abs(1 - before[..., :3]) < NEAR_UNIT
        if changed.any():
            visible_roles.add(role)
        support |= local_support
        unknown |= missing
        near_unit |= near
        gain *= local_gain
        emission = emission * local_gain + local_emission
        stages.append({
            "sequence": entry["sequence"], "role": role, "blend": entry["blend"],
            "rgbChangedPixels": int(changed.sum()),
            "indeterminateRgbChangedChannels": int((missing & rgb_changed).sum()),
            "nearUnitRgbChangedChannels": int((near & rgb_changed).sum()),
            "minimumNonzeroAbsScreenDenominator": denominator_min,
            **coverage_stats,
        })
    gaps = []
    for previous, current in zip(entries, entries[1:]):
        gap = describe_gap(previous, current)
        delta = current["before"] - previous["after"]
        rgb = np.abs(delta[..., :3]).max(axis=2)
        alpha = delta[..., 3] != 0
        gap["withinEffectSupport"] = {"rgbDifferentPixels": int((support & (rgb != 0)).sum()),
                                      "alphaDifferentPixels": int((support & alpha).sum()),
                                      "medianAbsMaxChannelRgb": float(np.median(rgb[support])) if support.any() else None,
                                      "maxAbsLinearRgb": float(rgb[support].max()) if support.any() else None}
        gaps.append(gap)

    unknown &= support[..., None]
    near_unit &= support[..., None]
    coverage = {
        "rgbChangedPixels": int(coverage_rgb_changed.sum()),
        "rgbChangedPixelsMeasurable": int((coverage_rgb_changed & ~coverage_alpha_unknown).sum()),
        "rgbChangedPixelsUnknown": int(coverage_alpha_unknown.sum()),
        "drawsWithAllRgbChangesUnmeasurable": unmeasurable_draws,
        "drawsWithIsolatedAlphaReplay": witness_draws,
    }
    if portable:
        coverage["drawsWithIsolatedBlackRgbReplay"] = replayed_emission_draws
    return NativeFan(base, gain, emission, transmission, support, unknown, near_unit,
                     visible_roles, stages, gaps, coverage)


def display_response(rgb, slope, offset):
    return linear(np.clip((rgb * slope + offset) / 255, 0, 1))


def solve(entries, crop, slope, offset):
    """Estimate selected-draw display transport on the captured plate."""
    native = solve_native(entries, crop)
    empty = display_response(native.base, slope, offset)
    attenuated = display_response(native.gain * native.base, slope, offset)
    resulting = display_response(native.gain * native.base + native.emission, slope, offset)
    multiply = np.clip(native.gain, 0, 1)
    np.divide(np.minimum(attenuated, resulting), empty, out=multiply, where=empty > 0)
    multiply = np.clip(multiply, 0, 1)
    add = np.maximum(0, resulting - empty * multiply)
    multiply[~native.support], add[~native.support] = 1, 0
    return native.result(multiply, add, empty)


def portable_display(native, slope, offset):
    """Project native transport into linear sRGB using spatially uniform references."""
    floor = display_response(np.zeros(3), slope, offset)
    span = 1 - floor
    if np.any(span <= 0):
        raise ValueError("Portable mode needs a display calibration with a distinguishable black and white.")

    def response(rgb):
        return np.maximum(0, display_response(rgb, slope, offset) - floor) / span

    # Match the response on black exactly, then fit the remaining plate gain
    # on the same mids at every pixel. The old terrain RGB never enters this fit.
    add = response(native.emission)
    references = linear(np.asarray(PORTABLE_REFERENCE_CODES, dtype=float) / 255)
    multiply = np.zeros_like(add)
    for plate in references:
        uniform_native = (255 * srgb(floor + plate * span) - offset) / slope
        transported = response(uniform_native * native.gain + native.emission)
        multiply += plate * (transported - add)
    multiply = np.maximum(0, multiply / (references @ references))
    multiply[~native.support], add[~native.support] = 1, 0
    # The source plate is returned for diagnostics only. It never changes the export.
    return multiply, add, response(native.base)


def pack(report, output, slope, offset, separate_backing=False):
    slope, offset = validate_transfer(slope, offset)
    if output.exists():
        raise FileExistsError(output)
    record, entries, hashes = load_observations(report.resolve(strict=True))
    crop = entries[0]["surface"][0]
    multiply, add, transmission, _, support, unknown, near_unit, visible_roles, stages, gaps, coverage = solve(
        entries, crop, slope, offset)
    if not {COVERAGE, SCREEN} <= visible_roles:
        raise ValueError("The crop has no visible RGB change from coverage or lightning.")

    present = record["present"]
    color_gain = multiply
    if separate_backing:
        black_png, transmission, color_gain = split_backing(transmission, multiply)
    manifest = {
        "asset": ASSET, "assetSha256": ASSET_SHA256, "present": present,
        "host": record["hostExecutable"], "crop": crop,
        "transfer": {"slopeRgbCodesPerNativeUnit": slope.tolist(), "offsetRgbCodes": offset.tolist()},
        "formula": "Display-linear RGB = plate * Multiply + Add.",
        "scope": "Scene-specific estimate from the selected native RT0 pairs and their pre-effect HDR plate. "
                 "Game post-processing and transfer to other backgrounds are unverified.",
        "observedSupportPixels": int(support.sum()), "stages": stages, "interstageGaps": gaps,
        "coverageAlpha": coverage,
        "gapPolicy": "Interstage changes are measured separately and excluded from the fan passes.",
        "uncertainty": {
            "unknownRgbChannels": int(unknown.sum()), "nearUnitScreenRgbChannels": int(near_unit.sum()),
            "nearUnitScreenThreshold": NEAR_UNIT,
            "maskPolicy": "Each RGB marker applies to its corresponding channel within observed support. "
                          "Unobservable coverage alpha uses local unit gain and the observed RGB delta. "
                          "Exact-unit Screen uses identity. Near-unit Screen estimates amplify FP16 rounding.",
        },
        "inputSha256": hashes,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    gain_label = "residual-multiply" if separate_backing else "multiply"
    paths = [output / name for name in (
        f"{present}-01-{gain_label}.exr", f"{present}-02-add.exr", "uncertain-rgb.png",
        "near-unit-screen-rgb.png", "verification.json",
    )]
    if separate_backing:
        paths.insert(-1, output / f"{present}-00-black.png")
    try:
        decoded_gain = write_rgb(paths[0], color_gain)
        decoded_add = write_rgb(paths[1], add)
        if np.any(decoded_gain[~support] != 1) or np.any(decoded_add[~support] != 0):
            raise ValueError("EXR values outside observed support must be neutral.")
        Image.fromarray(unknown.astype(np.uint8) * 255).save(paths[2])
        Image.fromarray(near_unit.astype(np.uint8) * 255).save(paths[3])
        if separate_backing:
            paths[-2].write_bytes(black_png)
            reassembled = transmission[..., None] * decoded_gain
            error = float(np.max(np.abs(reassembled - multiply.astype(np.float32))))
            if not np.allclose(reassembled, multiply.astype(np.float32), rtol=2e-7, atol=1e-12):
                raise ValueError("Layer split failed to preserve the original display gain.")
            manifest["formula"] = "plate * (1 - black PNG alpha) * residual Multiply + Add"
            manifest["backing"] = {
                "source": "Product of measured coverage-draw transmission. Colored source contribution stays in the upper passes.",
                "opacityMax": float(1 - transmission.min()),
                "gainRecombinationMaxAbsError": error,
                "stackBottomToTop": [paths[-2].name, paths[0].name, paths[1].name],
            }
        manifest["outputSha256"] = {path.name: digest(path.read_bytes()) for path in paths[:-1]}
        paths[-1].write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except Exception:
        for path in paths:
            path.unlink(missing_ok=True)
        output.rmdir()
        raise
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="qualified native fan JSON with its FP16 sidecars")
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    parser.add_argument("--slope", type=float, nargs=3, required=True, metavar=("R", "G", "B"),
                        help="RGB display code values per linear native unit")
    parser.add_argument("--offset", type=float, nargs=3, required=True, metavar=("R", "G", "B"),
                        help="RGB display code offsets on the 0-255 scale")
    parser.add_argument("--separate-backing", action="store_true",
                        help="export a normal black RGBA16 PNG and compensate Multiply for use in 32-bpc linear composites")
    args = parser.parse_args()
    try:
        pack(args.report, args.out, args.slope, args.offset, args.separate_backing)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        parser.exit(1, f"Terrain fan export rejected: {error}\n")
    print(args.out / "verification.json")
