"""Export same-frame native fan Add and Screen observations to linear RGB float32 EXRs."""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from check_rt0_heldout import compare
from export_rt0_draw import read_draw, rgb_blend_mode
from pack_rt0_exr import write_rgb
from verify_tower_layers import digest


LIGHT_ROLES = {"fan_decal_add", "fan_lightning"}


def identity(row, crop):
    cap = row["pixelCapture"]
    return (crop, *(cap[key] for key in ("targetResourceId", "targetViewId", "mipSlice", "arraySlice")))


def describe_gap(previous, current):
    result = {
        "afterDraw": previous["sequence"], "beforeDraw": current["sequence"],
        "sameSurface": previous["surface"] == current["surface"],
    }
    if previous["after"].shape != current["before"].shape:
        return {**result, "rgbDifferentPixels": None, "rgbDifferentChannels": None,
                "reason": "The two observations have different crop dimensions."}
    delta = current["before"] - previous["after"]
    rgb = delta[:, :, :3]
    alpha = delta[:, :, 3]
    return {
        **result,
        "rgbDifferentPixels": int(np.any(rgb != 0, axis=2).sum()),
        "rgbDifferentChannels": int(np.count_nonzero(rgb)),
        "maxAbsLinearRgb": float(np.abs(rgb).max()),
        "alphaDifferentPixels": int(np.count_nonzero(alpha)),
        "maxAbsAlpha": float(np.abs(alpha).max()),
    }


def continuous(gap):
    return gap["sameSurface"] and gap["rgbDifferentChannels"] == 0


def coefficients(entry):
    background = entry["before"][:, :, :3]
    difference = entry["after"][:, :, :3] - background
    if entry["mode"] == "add":
        return np.ones_like(background), difference, np.zeros_like(background, dtype=bool), None
    denominator = 1 - background
    singular = denominator == 0
    if np.any(difference[singular] != 0):
        raise ValueError("Screen changed a unit-background channel.")
    emission = np.divide(difference, denominator, out=np.zeros_like(background), where=~singular)
    minimum = float(np.abs(denominator[~singular]).min()) if np.any(~singular) else None
    return 1 - emission, emission, singular, minimum


def light_pass(row, before, after):
    """Return float64 RGB gain/emission, channel uncertainty, and the Screen denominator minimum."""
    mode = rgb_blend_mode(row)
    if mode not in ("screen", "add"):
        raise ValueError("Expected a native Add or Screen observation.")
    return coefficients({"mode": mode, "before": before, "after": after})


def deliver(folder, group):
    folder.mkdir()
    first, last = group[0], group[-1]
    original = first["before"][:, :, :3]
    observed = last["after"][:, :, :3]
    multiplication, emission = np.ones_like(original), np.zeros_like(original)
    uncertain = np.zeros_like(original, dtype=bool)
    stages = []
    for entry in group:
        multiplier, additive, singular, minimum = coefficients(entry)
        uncertain = (uncertain & (multiplier != 0)) | (singular & (multiplication != 0))
        multiplication *= multiplier
        emission = emission * multiplier + additive
        changed = np.any(entry["before"][:, :, :3] != entry["after"][:, :, :3], axis=2)
        stages.append({
            "draw": entry["sequence"], "role": entry["role"], "blend": entry["mode"],
            "rgbChangedPixels": int(changed.sum()), "minimumNonzeroAbsScreenDenominator": minimum,
            "singularChannels": int(singular.sum()),
        })
    gain_path = folder / "01-multiply.exr"
    add_path = folder / "02-add.exr"
    gain = write_rgb(gain_path, multiplication).astype(np.float64)
    light = write_rgb(add_path, emission).astype(np.float64)
    support = np.any((gain != 1) | (light != 0) | (original != observed), axis=2)
    metrics, _ = compare(original * gain + light, observed, support)
    paths = [gain_path, add_path]
    if uncertain.any():
        uncertainty = folder / "uncertain-rgb.png"
        Image.fromarray(uncertain.astype(np.uint8) * 255).save(uncertainty)
        paths.append(uncertainty)
    return {
        "directory": folder.name, "stages": stages,
        "sameBackgroundRgbAfterExrDecode": metrics,
        "inputRgbMax": float(original.max()), "outputRgbMax": float(observed.max()),
        "multiplierRange": [float(gain.min()), float(gain.max())],
        "emissionRange": [float(light.min()), float(light.max())],
        "uncertainChannels": int(uncertain.sum()),
        "outputSha256": {path.name: digest(path.read_bytes()) for path in paths},
    }


def export_lights(report_path, out_path):
    report_path, out_path = Path(report_path), Path(out_path)
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if report.get("fanCast", {}).get("status") not in ("captured", "observed_variant"):
        raise ValueError("Expected a completed native fan capture.")
    if out_path.exists():
        raise FileExistsError(out_path)
    entries, unsupported, hashes = [], [], {}
    for row in sorted(report["events"], key=lambda row: row["sequence"]):
        role = row.get("fanCandidate", {}).get("role")
        if role not in LIGHT_ROLES | {"fan_coverage"}:
            continue
        sequence = row["sequence"]
        try:
            (before, after), provenance, event, crop, _ = read_draw(report_path, sequence, None)
            mode = rgb_blend_mode(event)
        except ValueError as error:
            unsupported.append({"draw": sequence, "role": role, "reason": str(error)})
            continue
        hashes.update(provenance)
        entries.append({"sequence": sequence, "role": role, "mode": mode, "crop": crop,
                        "surface": identity(event, crop), "before": before, "after": after})
    lights = [
        entry for entry in entries
        if entry["role"] in LIGHT_ROLES and entry["mode"] in ("screen", "add")
    ]
    for entry in entries:
        if entry["role"] in LIGHT_ROLES and entry["mode"] not in ("add", "screen"):
            unsupported.append({"draw": entry["sequence"], "role": entry["role"], "blend": entry["mode"],
                                "reason": "RGB transport cannot be recovered here without source-over coverage evidence."})
    if not lights:
        raise ValueError("No supported light draw in the report.")
    out_path.mkdir(parents=True)
    outputs = [deliver(out_path / f"draw-{entry['sequence']}", [entry]) for entry in lights]
    gaps = [describe_gap(previous, current) for previous, current in zip(entries, entries[1:])]
    gap_lookup = {(gap["afterDraw"], gap["beforeDraw"]): gap for gap in gaps}
    groups = []
    for entry in lights:
        previous = groups[-1][-1] if groups else None
        gap = gap_lookup.get((previous["sequence"], entry["sequence"])) if previous else None
        if gap is not None and continuous(gap):
            groups[-1].append(entry)
        else:
            groups.append([entry])
    for group in groups:
        if len(group) < 2:
            continue
        first, last = group[0]["sequence"], group[-1]["sequence"]
        outputs.append(deliver(out_path / f"light-{first}-{last}", group))
        screen_group = [entry for entry in group if entry["mode"] == "screen"]
        if (len(screen_group) >= 2 and
                all(continuous(gap_lookup.get((a["sequence"], b["sequence"]),
                                              {"sameSurface": False, "rgbDifferentChannels": -1}))
                    for a, b in zip(screen_group, screen_group[1:]))):
            first, last = screen_group[0]["sequence"], screen_group[-1]["sequence"]
            outputs.append(deliver(out_path / f"screen-{first}-{last}", screen_group))
    result = {
        "present": report["present"], "host": report["hostExecutable"], "crop": lights[0]["crop"],
        "formula": "Linear RGB: background * 01-multiply.exr + 02-add.exr",
        "scope": "Same-frame linear FP16 RT0 measurements before game post-processing.",
        "limits": "EXRs represent measured RGB transport. Alpha resets are outside this model. Screen inversion is "
                  "estimated from the observed background and can be uncertain at unit inputs. Independent background "
                  "replay requires native packet evidence; no alternate-background validation is claimed here.",
        "observedGaps": gaps, "unsupportedDraws": unsupported, "inputSha256": hashes, "outputs": outputs,
    }
    (out_path / "verification.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    export_lights(args.report, args.out)
