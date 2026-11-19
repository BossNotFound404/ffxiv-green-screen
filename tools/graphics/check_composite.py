"""Check selected draws against RenderDoc pixel history from inspect_frame.py.

python check_composite.py pixels.json --events 22 24 --output check.json

The predicted pixel starts at its observed scene background. The unrounded
contribution and transmission describe the selected shader outputs at that pixel;
they do not establish effect identity or predict shaders that sample a new scene.
"""
import argparse
import json
import math
from pathlib import Path
import struct


# RenderDoc 1.47 names, with enum integers used by some Python binding builds.
FACTORS = ("Zero", "One", "SrcCol", "InvSrcCol", "DstCol", "InvDstCol",
           "SrcAlpha", "InvSrcAlpha", "DstAlpha", "InvDstAlpha")


def enum_name(value, names):
    text = str(value).rsplit(".", 1)[-1]
    if text.isdigit() and int(text) < len(names):
        return names[int(text)]
    return text


def check_color(values):
    if not isinstance(values, list) or len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError("missing or invalid observed RGBA")
    return values


def coefficients(equation, source, channel):
    """Return (contribution, transmission) for out = contribution + transmission * dst."""
    if enum_name(equation["operation"], ("Add",)) != "Add":
        raise ValueError("unsupported blend operation")
    src = source[channel]
    alpha = source[3]
    factors = (enum_name(equation["source"], FACTORS), enum_name(equation["destination"], FACTORS))
    rules = {
        ("One", "Zero"): (src, 0), ("Zero", "One"): (0, 1),
        ("One", "One"): (src, 1), ("SrcAlpha", "One"): (src * alpha, 1),
        ("One", "InvSrcAlpha"): (src, 1 - alpha),
        ("SrcAlpha", "InvSrcAlpha"): (src * alpha, 1 - alpha),
        ("One", "InvSrcCol"): (src, 1 - src),
        ("InvDstCol", "One"): (src, 1 - src),
    }
    if factors not in rules:
        raise ValueError(f"unsupported blend factors {factors}")
    return rules[factors]


def format_rules(name):
    name = name.upper().removeprefix("DXGI_FORMAT_")
    if "SRGB" in name:
        raise ValueError(f"sRGB replay is unverified ({name})")
    if name in ("R8G8B8A8_UNORM", "B8G8R8A8_UNORM"):
        return lambda v: round(min(1., max(0., v)) * 255) / 255, 1.0001 / 255
    if name in ("R16G16B16A16_FLOAT", "R32G32B32A32_FLOAT"):
        code, tolerance = ("e", 0.001) if name.startswith("R16") else ("f", 1e-6)
        return lambda v: struct.unpack(code, struct.pack(code, v))[0], tolerance
    raise ValueError(f"unsupported target format {name}")


def check_point(point, data, events, store_value, tolerance):
    history = point["history"]
    local_events = [r["event"] for r in history]
    if local_events != sorted(local_events):
        raise ValueError("unordered pixel history")
    first, last = min(events), max(events)
    span = [r for r in history if first <= r["event"] <= last]
    earlier = [r for r in history if r["event"] < first]
    if any(r["event"] not in events for r in span):
        raise ValueError("another target write is interleaved with the selected draws")
    background = span[0]["preMod"] if span else (earlier[-1]["postMod"] if earlier else None)
    expected = check_color(background).copy()
    contribution, transmission = [0.] * 4, [1.] * 4
    observed = expected.copy()
    seen, passed = set(), set()
    max_error = 0.
    worst = None
    for record in span:
        event = record["event"]
        seen.add(event)
        if not record["draw"] or record["directShaderWrite"] or record["unboundPS"]:
            raise ValueError(f"event {event} has no comparable raster blend")
        pre = record["preMod"]
        if pre is not None and max(abs(a - b) for a, b in zip(check_color(pre), observed)) > tolerance:
            raise ValueError(f"discontinuity before event {event}")
        observed = check_color(record["postMod"])
        if not record["failedTests"]:
            state = data["drawStates"].get(str(event))
            if not state or not state.get("available"):
                raise ValueError(f"blend state unavailable for event {event}")
            if state["viewFormat"] != data["format"]:
                raise ValueError(f"target view format changes at event {event}")
            if state["logicOperation"] is not None or state["alphaToCoverage"]:
                raise ValueError(f"unsupported logic operation or alpha-to-coverage at event {event}")
            if not state["sampleMask"] & (1 << data["sample"]):
                raise ValueError(f"reported passing fragment on masked sample at event {event}")
            source = check_color(record["shaderOut"])
            if "UNORM" in state["viewFormat"].upper() and any(v < 0 or v > 1 for v in source):
                raise ValueError(f"out-of-range source on normalized target at event {event}")
            passed.add(event)
            for ch in range(4):
                if not state["writeMask"] & (1 << ch):
                    continue
                additive, factor = (source[ch], 0)
                if state["enabled"]:
                    additive, factor = coefficients(state["alpha" if ch == 3 else "rgb"], source, ch)
                contribution[ch] = additive + factor * contribution[ch]
                transmission[ch] *= factor
                expected[ch] = store_value(additive + factor * expected[ch])
        error = max(abs(a - b) for a, b in zip(expected, observed))
        if error > max_error:
            max_error, worst = error, [event, record["fragment"]]
    return {"status": "matched" if max_error <= tolerance else "mismatch",
            "background": background, "idealContributionRGB": contribution[:3],
            "idealBackgroundTransmissionRGB": transmission[:3],
            "predicted": expected, "observed": observed, "maxChannelError": max_error,
            "worstEventFragment": worst, "touchedEvents": sorted(seen),
            "passingEvents": sorted(passed)}


def verify(data, events):
    if data.get("status") != "observed" or not data.get("points"):
        raise ValueError("input has no observed pixel history")
    if len(events) != len(set(events)) or any(i <= 0 or i > data["endEvent"] for i in events):
        raise ValueError("event selection must be unique, positive and within the capture")
    store_value, tolerance = format_rules(data["format"])
    points = []
    passed = set()
    for point in data["points"]:
        try:
            result = check_point(point, data, events, store_value, tolerance)
            passed.update(result["passingEvents"])
        except (ValueError, OverflowError) as exc:
            result = {"status": "unverified", "reason": str(exc)}
        points.append({"x": point["x"], "y": point["y"], **result})
    unobserved = sorted(set(events) - passed)
    statuses = {p["status"] for p in points}
    status = "matched"
    if unobserved or "unverified" in statuses:
        status = "unverified"
    if "mismatch" in statuses:
        status = "mismatch"
    return {"status": status, "target": data["target"], "events": events,
            "scope": "Selected samples on the observed background; source attribution and whole-image alpha unverified.",
            "format": data["format"], "tolerance": tolerance, "noPassingSample": unobserved, "points": points}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history", type=Path)
    parser.add_argument("--events", type=int, nargs="+", required=True)
    parser.add_argument("--output", type=Path, help="new JSON file; defaults to stdout")
    args = parser.parse_args()
    try:
        result = verify(json.loads(args.history.read_text(encoding="utf-8-sig")), args.events)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {"status": "unverified", "reason": str(exc)}
    text = json.dumps(result, indent=2, allow_nan=False)
    if args.output:
        try:
            with args.output.open("x", encoding="utf-8") as f:
                print(text, file=f)
        except OSError as exc:
            parser.error(str(exc))
    else:
        print(text)
    return 0 if result["status"] == "matched" else 1


if __name__ == "__main__":
    raise SystemExit(main())
