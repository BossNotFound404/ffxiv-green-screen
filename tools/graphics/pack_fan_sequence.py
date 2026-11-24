"""Export aligned AE image sequences from consecutive native fan observations."""

import argparse
from collections import Counter
from fractions import Fraction
import json
import os
from pathlib import Path
from uuid import uuid4

import numpy as np
from PIL import Image

from inspect_draw_pixels import read_plane
from inspect_scene_buffers import member
from make_fan_ae_import import prepare as prepare_ae, recorded_plate
from pack_rt0_exr import write_rgb
from pack_terrain_fan import (
    ADD, ASSET, ASSET_SHA256, COVERAGE, PORTABLE_REFERENCE_CODES, SCREEN, load_observations,
    observable_coverage_alpha, portable_display, solve, solve_native, split_backing, validate_transfer,
)
from replay_alpha_witness import SEQUENCE_PACKET_LIMIT, copy_hashed, read_packet, replay, replay_controls
from select_fan_cycle import observe, select_cycle
from verify_tower_layers import digest, write_png


NATIVE_SCHEMA = "fan-native-sequence-v1"
EXPECTED_HOST = "ffxiv_dx11.exe"
FRAME_LIMIT = 360
# The v1 native probe holds five queued frames, at most twelve RT0 pairs and
# one terminal surface per frame. Each crop is at most 512 by 512 RGBA16F.
QUEUE_LIMIT = 5
MAX_FRAME_PIXEL_BYTES = 512 * 512 * 8 * (2 * 12 + 1)
PREFIX_FAULT = "fan_draw_budget_exhausted"
STACK = {
    "black": ("00-black", "black", "png"),
    "multiply": ("01-residual-multiply", "residual-multiply", "exr"),
    "add": ("02-add", "add", "exr"),
    "game": ("03-game-final", "game-final", "png"),
}
CROP_KEYS = ("x", "y", "width", "height", "targetWidth", "targetHeight")
ROLE_COUNTS = {COVERAGE: "stagedCoverageDraws", ADD: "stagedDecalAddDraws", SCREEN: "stagedLightningDraws"}
PARTICLE_DEFINITIONS = {COVERAGE: [3], ADD: [9], SCREEN: [4, 5, 6, 7, 8]}
TERMINAL_LAYOUTS = {28: "rgba8", 29: "rgba8", 87: "bgra8", 91: "bgra8"}


def integer(record, key, minimum=0):
    value = record[key]
    if type(value) is not int or value < minimum:
        raise ValueError(f"{key} must be an integer of at least {minimum}.")
    return value


def read_index(path, allow_verified_prefix=False):
    source = path.read_bytes()
    capture = json.loads(source)
    status = capture.get("status")
    if capture.get("schema") != NATIVE_SCHEMA or not (
            status == "complete" or (allow_verified_prefix and status == PREFIX_FAULT)):
        raise ValueError(f"Expected a complete {NATIVE_SCHEMA} index (status: {status}).")
    if (capture["hostExecutable"].lower() != EXPECTED_HOST
            or (capture["asset"], capture["assetSha256"]) != (ASSET, ASSET_SHA256)):
        raise ValueError("Index does not identify the supported FFXIV executable and blue fan asset.")
    first_present = integer(capture, "armPresent") + 1
    count = integer(capture, "requestedFrames", 2)
    frames = capture["frames"]
    observed = integer(capture, "observedFrames")
    if count > FRAME_LIMIT or not isinstance(frames, list):
        raise ValueError("Invalid native capture size or published frame list.")
    if status == "complete":
        if len(frames) != count or observed != count:
            raise ValueError("The complete index does not contain all requested consecutive frames.")
        for key in ("faultPresent", "faultRejectedAnchorDraws", "faultDroppedDraws"):
            if key in capture and integer(capture, key):
                raise ValueError("A complete native index cannot include a capture fault.")
    else:
        queue_size = integer(capture, "maxQueuedFrames", 1)
        if not (3 <= len(frames) <= observed < count and
                observed - len(frames) <= queue_size <= QUEUE_LIMIT):
            raise ValueError("The native fault does not leave a valid contiguous published prefix.")
        if (integer(capture, "faultPresent", 1) != first_present + observed or
                integer(capture, "faultRejectedAnchorDraws") != 0 or
                integer(capture, "faultDroppedDraws") < 1):
            raise ValueError("Fault counters or present disagree with the native per-frame fan draw limit.")
    if integer(capture, "stagedPixelBytes", 1) > integer(capture, "pixelByteBudget", 1):
        raise ValueError("The index exceeds the declared raw pixel storage budget.")
    if "stagedWitnessPacketBytes" in capture or "witnessPacketByteBudget" in capture:
        if not (integer(capture, "stagedWitnessPacketBytes") <= integer(capture, "witnessPacketByteBudget", 1)
                <= SEQUENCE_PACKET_LIMIT):
            raise ValueError("The index exceeds the declared alpha witness packet budget.")
    integer(capture, "processId", 1)
    integer(capture, "sessionShot", 1)
    integer(capture, "qpcFrequency", 1)
    last_qpc = integer(capture, "armQpc", 1)
    for index, frame in enumerate(frames):
        if (integer(frame, "index") != index or integer(frame, "present", 1) != first_present + index
                or frame["report"] != f"{path.name}.frame-{index}.json"):
            raise ValueError(f"Unexpected frame index, native present or report filename at index {index}.")
        qpc = integer(frame, "qpc", 1)
        if qpc <= last_qpc:
            raise ValueError(f"Non-increasing native timestamp at frame index {index}.")
        last_qpc = qpc
    return capture, digest(source)


def read_frame(directory, capture, frame):
    path = member(directory, frame["report"])
    if path.name != frame["report"]:
        raise ValueError("The frame report aliases a different file.")
    raw = path.read_bytes()
    record = json.loads(raw)
    if (record["api"] != "D3D11" or record["captureMode"] != "manual"
            or record["pixelCaptureMode"] != "before_after_rt0"
            or record["hostExecutable"].lower() != capture["hostExecutable"].lower()
            or integer(record, "processId", 1) != capture["processId"]
            or integer(record, "sessionShot", 1) != capture["sessionShot"]
            or integer(record, "present", 1) != frame["present"]):
        raise ValueError("Report host, pixel mode, session or present does not match the capture.")
    timing = record["sequenceFrame"]
    if any(integer(timing, key) != value for key, value in {
        "index": frame["index"], "qpc": frame["qpc"], "qpcFrequency": capture["qpcFrequency"],
    }.items()):
        raise ValueError("Frame timing differs between the report and the completed index.")
    integer(record, "sampledPresentThreadId", 1)
    integer(record, "armPresentThreadId", 1)
    if integer(record, "deferredDrawsObserved"):
        raise ValueError("Deferred draws were observed and were outside the immediate RT0 capture.")
    fan = record["fanCast"]
    if any(integer(fan, key) for key in ("rejectedAnchorDraws", "budgetDroppedDraws", "observationEntriesDropped")):
        raise ValueError("The probe rejected target draws or reached its fan observation budget.")
    total_draws = integer(record, "drawCount") + integer(record, "drawIndexedCount")
    events = record["events"]
    if integer(fan, "textureMatchedDraws") < len(events):
        raise ValueError("Fewer native texture matches were reported than retained draw pairs.")
    counts = Counter()
    previous = 0
    for event in events:
        seq = integer(event, "sequence", 1)
        if not previous < seq <= total_draws:
            raise ValueError("Target draws must have unique, increasing native ordinals.")
        previous = seq
        candidate = event["fanCandidate"]
        role = candidate["role"]
        if role not in PARTICLE_DEFINITIONS or candidate["authoredParticleDefinitions"] != PARTICLE_DEFINITIONS[role]:
            raise ValueError(f"Draw {seq} has an unsupported fan role or authored particle signature.")
        counts[role] += 1
        pixels = event["pixelCapture"]
        prefix = f"{path.name}.draw-{seq}"
        if (pixels["beforeFile"] != prefix + ".before.rgba16f"
                or pixels["afterFile"] != prefix + ".after.rgba16f"):
            raise ValueError(f"Draw {seq} must use its own frame's pair of raw FP16 sidecars.")
    if any(integer(fan, field) != counts[role] for role, field in ROLE_COUNTS.items()):
        raise ValueError("Target draw roles differ from the probe's staged counts.")

    terminal = record["terminalColor"]
    fmt = integer(terminal, "viewFormat")
    layout = TERMINAL_LAYOUTS.get(fmt)
    if (terminal["status"] != "captured" or layout is None or terminal["layout"] != layout
            or integer(terminal, "resourceFormat") != fmt
            or terminal["file"] != f"{path.name}.swapchain.{layout}"
            or terminal["stage"] != "swapchain_before_reshade_and_dxgi_present"
            or terminal["formatSource"] != "swapchain_resource_desc"
            or integer(terminal, "mipSlice") or integer(terminal, "arraySlice")):
        raise ValueError("Expected a successful same-present 8-bit SDR swapchain readback.")
    integer(terminal, "readbackThreadId", 1)
    integer(terminal, "targetResourceId", 1)
    crop = {key: integer(terminal, key) for key in CROP_KEYS}
    if (integer(record, "width", 1), integer(record, "height", 1)) != (crop["targetWidth"], crop["targetHeight"]):
        raise ValueError("Report and terminal swapchain dimensions differ.")
    terminal_rgba, terminal_hash = read_plane(path.parent, terminal, "file")
    game = np.rint(terminal_rgba[..., :3] * 255).astype(np.uint8)
    snapshot, entries, hashes = load_observations(path, sequence_frame=True)
    report_hash = digest(raw)
    if snapshot != record or hashes.get(path.name, report_hash) != report_hash:
        raise ValueError("Frame report changed during export.")
    if entries and entries[0]["surface"][0] != crop:
        raise ValueError("Terminal crop and native RT0 crop must have the same size and alignment.")
    for entry in entries:
        if entry["role"] == COVERAGE:
            witness = read_packet(path, entry)
            if witness is not None:
                entry["witness"] = witness
        elif "alphaWitness" in entry["row"]["pixelCapture"]:
            raise ValueError("An alpha witness can only accompany a coverage draw.")
    return crop, fmt, entries, game, {**hashes, path.name: report_hash, terminal["file"]: terminal_hash}


def recover_backing(entries, replay_exe, stage, hashes, portable=False, replay_adapter_index=None):
    for entry in entries:
        witness = entry.get("witness")
        if witness is None:
            continue
        if witness["status"] == "unavailable":
            entry["witnessEvidence"] = witness
            continue
        before, after = entry["before"][..., 3], entry["after"][..., 3]
        packet_file = witness["path"].name
        if portable and replay_exe is not None:
            entry["witnessGain"], entry["witnessEmission"], entry["witnessEvidence"] = replay_controls(
                entry, replay_exe, stage, hashes, replay_adapter_index)
        elif np.all(observable_coverage_alpha(before, after)):
            entry["witnessEvidence"] = {"status": "stored_rt0_alpha_available", "packetFile": packet_file}
        elif replay_exe is None:
            entry["witnessEvidence"] = {"status": "packet_available_replay_not_requested", "packetFile": packet_file}
        else:
            entry["witnessGain"], entry["witnessEvidence"] = replay(entry, replay_exe, stage, hashes)


def write_frame(stage, number, crop, entries, game, slope, offset, require_measured_backing=False, portable=False):
    if portable:
        native = solve_native(entries, crop, portable=True)
        layers = native.result(*portable_display(native, slope, offset))
    else:
        layers = solve(entries, crop, slope, offset)
    multiply, add, transmission, empty, support, unknown, near_unit, visible_roles, stages, gaps, coverage = layers
    unmeasurable = coverage["drawsWithAllRgbChangesUnmeasurable"]
    if require_measured_backing and unmeasurable:
        draws = ", ".join(map(str, unmeasurable))
        pending = any(entry["sequence"] in unmeasurable and entry.get("witnessEvidence", {}).get("status")
                      == "packet_available_replay_not_requested" for entry in entries)
        hint = " Supply --alpha-witness-replay-exe to verify the recorded draw packet." if pending else ""
        raise ValueError(f"Coverage draw(s) {draws} changed RGB but have no measurable native alpha at those pixels.{hint}")
    # Fully opaque native backing removes all dependence on the plate. When the
    # legacy calibration leaves a code offset, carry it into the Add pass.
    opaque = np.rint((1 - transmission) * 65535) == 65535
    if not portable:
        add[opaque] = (empty * multiply + add)[opaque]
    multiply[opaque] = 0
    calibrated = empty * multiply + add
    black_png, transmission, residual = split_backing(transmission, multiply)
    files = {key: f"{folder}/{prefix}_{number:06d}.{ext}" for key, (folder, prefix, ext) in STACK.items()}
    (stage / files["black"]).write_bytes(black_png)
    gain = write_rgb(stage / files["multiply"], residual)
    light = write_rgb(stage / files["add"], add)
    if np.any(transmission[~support] != 1) or np.any(gain[~support] != 1) or np.any(light[~support] != 0):
        raise ValueError("The encoded passes alter pixels outside measured effect support.")
    rebuilt = empty * transmission[..., None] * gain + light
    if not np.allclose(rebuilt, calibrated, rtol=2e-7, atol=1e-12):
        raise ValueError("Encoded passes failed to recombine on the calibrated pre-effect RGB.")
    reference = stage / files["game"]
    write_png(reference, game)
    with Image.open(reference) as decoded:
        if decoded.info.get("srgb") != 0 or not np.array_equal(np.asarray(decoded), game):
            raise ValueError("Game final PNG did not preserve the captured 8-bit display codes.")
    opacity = 1 - transmission
    alpha = opacity[opacity > 0]
    exported = (opacity > 0) | (gain != 1).any(axis=2) | (light != 0).any(axis=2)
    error_label = ("maxLayerEncodingAbsError" if portable else "maxRecompositionAbsErrorOnEstimatedPlate")
    return {
        "files": files,
        "observedSupportPixels": int(support.sum()),
        "exportedSupportPixels": int(exported.sum()),
        "touchesCropEdge": bool(support[0].any() or support[-1].any() or support[:, 0].any() or support[:, -1].any()),
        "opacityMax": float(opacity.max()),
        "opacityP95Nonzero": float(np.percentile(alpha, 95)) if alpha.size else None,
        "coverageAlpha": coverage,
        "fullyOpaqueBackingPixels": int(opaque.sum()),
        "visibleRgbDrawRoles": sorted(visible_roles),
        "stages": stages,
        "interstageGaps": gaps,
        "unknownRgbChannels": int(unknown.sum()),
        "nearUnitScreenRgbChannels": int(near_unit.sum()),
        error_label: float(np.abs(rebuilt - calibrated).max()),
        "outputSha256": {key: digest((stage / name).read_bytes()) for key, name in files.items()},
    }


def read_range_timing(frames, first, last, allow_inferred_tail):
    if not 0 <= first < last < len(frames):
        raise ValueError("Select at least two consecutive frames with valid zero-based inclusive --first/--last.")
    if last + 1 < len(frames):
        return frames[first]["qpc"], frames[last + 1]["qpc"]
    if not allow_inferred_tail:
        raise ValueError("A faulted prefix requires explicit --last and a published following present for its duration.")
    intervals = sorted(frames[i + 1]["qpc"] - frames[i]["qpc"] for i in range(first, last))
    middle = len(intervals) // 2
    typical_interval = Fraction(intervals[middle] + intervals[(len(intervals) - 1) // 2], 2)
    return frames[first]["qpc"], frames[last]["qpc"] + typical_interval


def pack(index_path, output, slope, offset, first=None, last=None, require_idle_brackets=False,
         require_measured_backing=False, alpha_witness_replay_exe=None, allow_verified_prefix=False,
         color_mode="terrain", replay_adapter_index=None, prepare_ae_project=False, auto_cycle=False):
    if auto_cycle and (first is not None or last is not None):
        raise ValueError("--auto-cycle cannot be combined with --first or --last.")
    index_path = index_path.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(output)
    slope, offset = validate_transfer(slope, offset)
    if color_mode not in {"terrain", "portable"}:
        raise ValueError("Choose terrain or portable color mode.")
    portable = color_mode == "portable"
    if replay_adapter_index is not None and (
            not portable or alpha_witness_replay_exe is None or type(replay_adapter_index) is not int
            or replay_adapter_index < 0):
        raise ValueError("A substitute graphics adapter requires portable color mode, a replayer and a nonnegative index.")
    capture, capture_hash = read_index(index_path, allow_verified_prefix)
    if alpha_witness_replay_exe is not None:
        alpha_witness_replay_exe = alpha_witness_replay_exe.resolve(strict=True)
        if alpha_witness_replay_exe.suffix.lower() != ".exe" or not alpha_witness_replay_exe.is_file():
            raise ValueError("Choose a local native alpha witness replay executable.")
    frames = capture["frames"]
    partial = capture["status"] != "complete"
    frequency = capture["qpcFrequency"]
    if not auto_cycle:
        if partial and (last is None or not 0 <= last < len(frames) - 1):
            raise ValueError("A faulted prefix requires explicit --last and a published following present for its duration.")
        first = 0 if first is None else first
        last = len(frames) - 1 if last is None else last
        origin, tail_qpc = read_range_timing(frames, first, last, not partial)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.with_name(f".fan-sequence-{uuid4().hex}")
    stage.mkdir()
    try:
        staged_exe = replay_exe_hash = None
        if alpha_witness_replay_exe is not None:
            staged_exe = stage / ".replay-alpha-witness.exe"
            replay_exe_hash = copy_hashed(alpha_witness_replay_exe, staged_exe)
        for folder, _, _ in STACK.values():
            (stage / folder).mkdir()
        expected_geometry = None
        rows = []
        activity, scanned_hashes = [], []
        raw_pixel_bytes = witness_packet_bytes = 0

        def append_export(frame, data):
            crop, _, entries, game, hashes = data
            index = frame["index"]
            number = index - first + 1
            next_qpc = tail_qpc if index == last else frames[index + 1]["qpc"]
            recover_backing(entries, staged_exe, stage, hashes, portable, replay_adapter_index)
            row = write_frame(stage, number, crop, entries, game, slope, offset, require_measured_backing, portable)
            if auto_cycle and row["touchesCropEdge"]:
                raise ValueError("Auto-cycle measured support touches the crop edge after backing recovery.")
            rows.append({
                "number": number, "sourceIndex": index, "present": frame["present"], "qpc": frame["qpc"],
                "startSeconds": (frame["qpc"] - origin) / frequency,
                "durationSeconds": float((next_qpc - frame["qpc"]) / frequency),
                "inputSha256": hashes, **row,
            })

        for frame in frames:
            index = frame["index"]
            try:
                data = read_frame(index_path.parent, capture, frame)
                crop, fmt, entries, _, hashes = data
                if expected_geometry is None:
                    expected_geometry = (crop, fmt)
                if (crop, fmt) != expected_geometry:
                    raise ValueError("The capture changed crop, target size or swapchain format mid-sequence.")
                raw_pixel_bytes += crop["width"] * crop["height"] * (4 + 16 * len(entries))
                witness_packet_bytes += sum(entry.get("witness", {}).get("bytes", 0) for entry in entries)
                if auto_cycle:
                    activity.append(observe(entries, crop))
                    scanned_hashes.append(hashes)
                elif first <= index <= last:
                    append_export(frame, data)
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
                raise ValueError(f"Frame index {index}, native present {frame['present']}: {error}") from error
        # Raw pixels include complete intervals queued but unpublished at the
        # fault. In contrast, the native packet counter only includes published frames.
        unpublished = capture["observedFrames"] - len(frames)
        unaccounted = capture["stagedPixelBytes"] - raw_pixel_bytes
        if (unaccounted % 4 or not 4 * unpublished <= unaccounted <= MAX_FRAME_PIXEL_BYTES * unpublished):
            raise ValueError("Native pixel bytes cannot account for the published files and queued intervals.")
        if witness_packet_bytes != capture.get("stagedWitnessPacketBytes", 0):
            raise ValueError("Native index and readback files disagree on the total number of alpha witness packet bytes.")
        selection = None
        if auto_cycle:
            selection = select_cycle(frames, activity, frequency)
            first, last = selection["first"]["sourceIndex"], selection["last"]["sourceIndex"]
            origin, tail_qpc = read_range_timing(frames, first, last, False)
            for frame in frames[first:last + 1]:
                index = frame["index"]
                try:
                    data = read_frame(index_path.parent, capture, frame)
                    if data[-1] != scanned_hashes[index]:
                        raise ValueError("Native inputs changed after automatic activity selection.")
                    append_export(frame, data)
                except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
                    raise ValueError(f"Frame index {index}, native present {frame['present']}: {error}") from error
        duration = float((tail_qpc - origin) / frequency)
        active_frames = [row["number"] for row in rows if row["observedSupportPixels"]]
        bracketed = bool(active_frames and active_frames[0] > 1 and active_frames[-1] < len(rows))
        if (auto_cycle or require_idle_brackets) and not bracketed:
            raise ValueError("The selected interval needs measured fan activity with neutral frames before and after.")
        original_index = index_path.read_bytes()
        if digest(original_index) != capture_hash:
            raise ValueError("Native index changed during export.")
        crop, fmt = expected_geometry
        manifest = {
            "schema": "fan-ae-sequence-v1", "asset": ASSET, "assetSha256": ASSET_SHA256,
            "hostExecutable": capture["hostExecutable"], "processId": capture["processId"],
            "sessionShot": capture["sessionShot"], "nativeStagedPixelBytes": capture["stagedPixelBytes"],
            "nativeStagedWitnessPacketBytes": witness_packet_bytes,
            "nativeIndexFile": index_path.name, "nativeIndexSha256": capture_hash,
            "nativeFrameCount": len(frames), "crop": crop, "terminalDxgiFormat": fmt,
            "transfer": {"slopeRgbCodesPerNativeUnit": slope.tolist(), "offsetRgbCodes": offset.tolist()},
            "timing": {
                "qpcFrequency": frequency, "durationSeconds": duration, "suggestedFps": len(rows) / duration,
                "finalDurationFromNextPresent": last + 1 < len(frames),
            },
            "activity": {
                "bracketedByNeutralFrames": bracketed,
                "firstActiveNumber": active_frames[0] if active_frames else None,
                "lastActiveNumber": active_frames[-1] if active_frames else None,
                "activeFrameCount": len(active_frames),
            },
            "coverageAlphaPolicy": {
                "requireMeasuredBacking": require_measured_backing,
                "isolatedReplayExecutableSha256": replay_exe_hash,
                "pixelCounting": "Coverage totals count each RGB-changing pixel once. A pixel with unknown "
                                 "coverage alpha in any RGB-changing coverage draw is classified as unknown.",
                "source": "Stored native RT0 alpha when available. Otherwise, a supplied replayer can verify "
                          "a captured draw packet against the original and measure transmittance on an alpha-one control.",
            },
            "scope": "One image per sampled native Present. Activity tracks the selected texture/blend signatures in the crop. "
                     "Neutral brackets alone cannot verify the full authored effect lifecycle.",
            "composite": "32-bpc linear sRGB; plate * (1 - black PNG alpha) * residual Multiply + Add.",
            "limits": "Multiply and Add are scene-specific estimates from the selected native draws. "
                      "Unobserved alpha and singular Screen channels use local identity gain. "
                      "Changes between draws and game post-processing are outside the transport estimate. "
                      "Game Final preserves captured SDR RGB as an independent reference.",
            "frames": rows,
        }
        if selection is not None:
            manifest["autoSelection"] = selection
        if portable:
            manifest["colorMode"] = "portable"
            manifest["portableColor"] = {
                "source": "Native draw gains and emissions, evaluated on spatially uniform display references.",
                "blackReferenceSrgbCode": 0,
                "multiplyFitSrgbCodes": list(PORTABLE_REFERENCE_CODES),
                "linearTransfer": "F0(x) = (F(x) - F(0)) / (1 - F(0)); F is the declared clipped diagonal transfer "
                                  "decoded to linear sRGB.",
                "addFit": "Display RGB on black under the normalized transfer.",
                "multiplyFit": "Nonnegative least squares with the black Add fixed, divided by the encoded backing.",
                "counts": {
                    "isolatedCoverageBlackRgbDraws": sum(
                        len(row["coverageAlpha"]["drawsWithIsolatedBlackRgbReplay"]) for row in rows),
                    "pairedNativeAddDraws": sum(
                        stage["role"] == ADD for row in rows for stage in row["stages"]),
                    "pairedNativeScreenDraws": sum(
                        stage["role"] == SCREEN for row in rows for stage in row["stages"]),
                },
            }
            manifest["coverageAlphaPolicy"]["source"] = (
                "Verified isolated coverage replay measures native transmittance over alpha one and emission over "
                "black when the native packet and replayer are available. Stored native alpha and observed pair "
                "subtraction are used where the isolated replay is unavailable.")
            if replay_adapter_index is not None:
                manifest["coverageAlphaPolicy"]["replayAdapterIndex"] = replay_adapter_index
            manifest["limits"] = (
                "Multiply and Add approximate the native effect on replacement plates in linear sRGB. "
                "Add and Screen draws are estimates from original FP16 pairs and their recorded blend equations. "
                "Unobserved alpha and singular Screen channels use local identity gain. Intervening draws and "
                "game post-processing are outside this model. Game Final preserves the captured SDR reference.")
        if partial:
            original_copy = "source-native-index.json"
            (stage / original_copy).write_bytes(original_index)
            manifest["nativeSource"] = {
                "status": capture["status"], "indexCopy": original_copy, "indexSha256": capture_hash,
                "requestedFrames": capture["requestedFrames"], "observedFrames": capture["observedFrames"],
                "verifiedPublishedFrames": len(frames), "unpublishedObservedFrames": unpublished,
                "verifiedPublishedPixelBytes": raw_pixel_bytes, "nativeStagedPixelBytes": capture["stagedPixelBytes"],
                "unpublishedStagedPixelBytes": unaccounted, "verifiedPublishedWitnessBytes": witness_packet_bytes,
                "lastPublishedPresent": frames[-1]["present"],
                "selectedTailTimestampPresent": frames[last + 1]["present"],
                "faultPresent": capture["faultPresent"], "faultRejectedAnchorDraws": capture["faultRejectedAnchorDraws"],
                "faultDroppedDraws": capture["faultDroppedDraws"],
            }
        if staged_exe is not None:
            staged_exe.unlink()
        if prepare_ae_project:
            plate = rows[selection["cleanPlate"]["sourceIndex"] - first] if auto_cycle else recorded_plate(rows)
            manifest["aeCleanPlate"] = {
                "source": ("captured_draw_free_frame_before_first_draw" if auto_cycle
                           else "captured_draw_free_frame_before_first_activity"),
                "present": plate["present"], "number": plate["number"],
                "sha256": plate["outputSha256"]["game"],
            }
        (stage / "sequence.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        if prepare_ae_project:
            prepare_ae(stage, stage / plate["files"]["game"])
        if output.exists():
            raise FileExistsError(output)
        os.rename(stage, output)
        return manifest
    finally:
        if stage.is_dir():
            for subdirectory in stage.iterdir():
                if subdirectory.is_dir():
                    for artifact in subdirectory.iterdir():
                        artifact.unlink()
                    subdirectory.rmdir()
                else:
                    subdirectory.unlink()
            stage.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", type=Path, help=f"completed {NATIVE_SCHEMA} index")
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    parser.add_argument("--slope", type=float, nargs=3, required=True, metavar=("R", "G", "B"),
                        help="RGB display code values per linear native unit")
    parser.add_argument("--offset", type=float, nargs=3, required=True, metavar=("R", "G", "B"),
                        help="RGB display code offsets on the 0–255 scale")
    parser.add_argument("--first", type=int, help="first source index, inclusive and zero-based (default: 0)")
    parser.add_argument("--last", type=int, help="last source index, inclusive and zero-based")
    parser.add_argument("--auto-cycle", action="store_true",
                        help="select an observed fan cast with draw-free opening and closing frames")
    parser.add_argument("--require-idle-brackets", action="store_true",
                        help="require observed fan activity between neutral opening and closing frames")
    parser.add_argument("--require-measured-backing", action="store_true",
                        help="reject a coverage draw when all of its RGB-changing pixels lack measurable alpha")
    parser.add_argument("--alpha-witness-replay-exe", type=Path,
                        help="verify the native draw packet; portable mode also measures native RGB over black")
    parser.add_argument("--allow-verified-prefix", action="store_true",
                        help="allow a selected range in the published prefix preceding a later native fan draw-budget fault")
    parser.add_argument("--color-mode", choices=("terrain", "portable"), default="terrain",
                        help="fit the captured terrain (default), or use uniform color references for replacement plates")
    parser.add_argument("--replay-adapter-index", type=int,
                        help="explicit substitute graphics adapter, subject to exact original replay validation")
    parser.add_argument("--prepare-ae", action="store_true",
                        help="include the AE importer and use a captured draw-free frame before first activity for the background")
    args = parser.parse_args()
    try:
        pack(args.index, args.out, args.slope, args.offset, args.first, args.last, args.require_idle_brackets,
             args.require_measured_backing, args.alpha_witness_replay_exe, args.allow_verified_prefix,
             args.color_mode, args.replay_adapter_index, args.prepare_ae, args.auto_cycle)
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        parser.exit(1, f"Fan sequence export rejected: {error}\n")
    print(args.out / "sequence.json")


if __name__ == "__main__":
    main()
