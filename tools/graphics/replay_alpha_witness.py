"""Verify isolated alpha-one D3D11 coverage replays against their observed draw."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import numpy as np

from export_transmittance_over import rounding_interval
from inspect_scene_buffers import member
from verify_tower_layers import digest


METHOD = "isolated_packet_alpha_one_v1"
PACKET_LIMIT = 160 << 20
SEQUENCE_PACKET_LIMIT = 4 << 30


def copy_hashed(path, target):
    hasher = sha256()
    with path.open("rb") as source, target.open("xb") as destination:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            destination.write(chunk)
            hasher.update(chunk)
    return hasher.hexdigest()


def read_packet(report, entry):
    cap = entry["row"]["pixelCapture"]
    witness = cap.get("alphaWitness")
    if witness is None:
        return None
    status = witness["status"]
    if witness["method"] != METHOD or status not in {"captured", "unavailable"}:
        raise ValueError("Unsupported coverage alpha witness declaration.")
    if status == "unavailable":
        if "file" in witness or not isinstance(witness.get("detail"), str) or not witness["detail"]:
            raise ValueError("Unavailable alpha witness must report its capture failure.")
        return {"status": status, "detail": witness["detail"]}
    expected = f"{report.name}.draw-{entry['sequence']}.packet"
    if witness["file"] != expected:
        raise ValueError("Alpha witness does not belong to the same frame and draw.")
    path = member(report.parent, expected)
    size = path.stat().st_size
    if (path.name != expected or not 0 < size <= PACKET_LIMIT
            or type(witness.get("bytes")) is not int or witness["bytes"] != size):
        raise ValueError("Alpha witness packet alias, missing data or invalid size.")
    return {"status": status, "path": path, "bytes": size}


def verify_native_alpha(entry, values):
    before, after = entry["before"][..., 3], entry["after"][..., 3]
    lo, hi = rounding_interval(values)
    a, b = np.maximum(0, lo) * before, np.minimum(1, hi) * before
    amin, amax = np.minimum(a, b).astype(np.float16), np.maximum(a, b).astype(np.float16)
    if np.any((after < amin) | (after > amax)):
        raise ValueError("The witness transmittance cannot reproduce the stored original alpha within FP16 rounding.")


def _replay(entry, exe, parent, hashes, controls, adapter_index=None):
    packet = entry["witness"]
    row = entry["row"]
    cap = row["pixelCapture"]
    width, height = cap["width"], cap["height"]
    pixels = width * height
    with TemporaryDirectory(prefix=".alpha-witness-", dir=parent) as temp:
        work = Path(temp)
        native = []
        for key in ("beforeFile", "afterFile"):
            filename = cap[key]
            path = member(packet["path"].parent, filename)
            target = work / key
            if copy_hashed(path, target) != hashes[filename]:
                raise ValueError("A native before/after sidecar changed before alpha witness replay.")
            native.append(target)
        staged_packet = work / "native.packet"
        packet_hash = copy_hashed(packet["path"], staged_packet)
        if staged_packet.stat().st_size != packet["bytes"]:
            raise ValueError("The alpha witness packet size changed before replay.")
        output = work / "verified"
        try:
            mode = "--coverage-controls" if controls else "--alpha-witness"
            adapter_args = ["--adapter-index", str(adapter_index)] if adapter_index is not None else []
            result = subprocess.run(
                [str(exe), mode, *adapter_args, str(staged_packet), *map(str, native), str(output)],
                cwd=work, capture_output=True, encoding="utf-8", errors="replace", timeout=90, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError("Isolated alpha witness replay timed out.") from error
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            raise ValueError(f"Isolated alpha witness replay failed for draw {entry['sequence']}: {detail}")
        evidence = member(output, "result.json").read_bytes()
        meta = json.loads(evidence)
        expected = {
            "status": "baseline_exact", "method": METHOD, "sequence": entry["sequence"],
            "width": width, "height": height, "nativeAfterComparedBytes": pixels * 8,
            "witnessRgbComparedBytes": pixels * 6, "seedAlpha": 1,
            **{key: cap[key] for key in ("x", "y", "targetWidth", "targetHeight")},
        }
        if controls:
            expected.update(stage="single_coverage_draw_rt0_linear", backgroundAlpha=1,
                            controls=["black", "white", "gray", "green", "pattern"])
            if adapter_index is not None:
                expected.update(adapterSelection="explicit_index", adapterIndex=adapter_index)
        for key, value in expected.items():
            if type(meta.get(key)) is not type(value) or meta[key] != value:
                raise ValueError(f"Alpha witness replay did not prove the expected {key}.")
        plane = member(output, "transmission.r16f").read_bytes()
        if len(plane) != pixels * 2:
            raise ValueError("Isolated alpha witness has an incomplete transmittance plane.")
        values = np.frombuffer(plane, "<f2").reshape(height, width).astype(np.float64)
        if not np.all(np.isfinite(values) & (values >= 0) & (values <= 1)):
            raise ValueError("Invalid normalized transmittance in the alpha witness.")
        verify_native_alpha(entry, values)
        native_emission = None
        if controls:
            black = member(output, "black.rgba16f").read_bytes()
            if len(black) != pixels * 8:
                raise ValueError("Isolated coverage replay has an incomplete RGB-over-black plane.")
            rgba = np.frombuffer(black, "<f2").reshape(height, width, 4).astype(np.float64)
            if not np.isfinite(rgba).all() or not np.array_equal(rgba[..., 3], values):
                raise ValueError("Isolated RGB-over-black and alpha-one replay disagree about transmittance.")
            native_emission = rgba[..., :3].copy()
            expected["blackRgba16fSha256"] = digest(black)
            for key in ("adapterSelection", "adapterIndex", "capturedAdapterLuid", "replayAdapterLuid",
                        "replayVendorId", "replayDeviceId"):
                if key in meta:
                    expected[key] = meta[key]
        hashes[packet["path"].name] = packet_hash
        return values[..., None], native_emission, {
            **expected, "packetFile": packet["path"].name, "packetSha256": packet_hash,
            "transmissionSha256": digest(plane), "resultSha256": digest(evidence),
        }


def replay(entry, exe, parent, hashes):
    gain, _, evidence = _replay(entry, exe, parent, hashes, controls=False)
    return gain, evidence


def replay_controls(entry, exe, parent, hashes, adapter_index=None):
    return _replay(entry, exe, parent, hashes, controls=True, adapter_index=adapter_index)
