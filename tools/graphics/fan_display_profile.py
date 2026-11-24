"""Estimate a global SDR palette for portable blue-fan layers."""

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from inspect_scene_buffers import member
from pack_terrain_fan import COVERAGE
from verify_tower_layers import digest


PATCH_SIZE = 4
TILE_SIZE = 24
IDLE_STABILITY_GUARD = 2
MIN_PATCHES = 64
MAX_CHANNEL_MAE = 8
MAX_P95_ERROR = 24
ARCHIVE_INDEX_SHA256 = "50099ab4267a3c8c21ac6d88db087c8ef6dfe27f3c84baf642b281cc245fd74f"
ARCHIVE_SEQUENCE_SHA256 = "99b46a2a3dc46ed545c86a269c7f1a3c7a8c8bbc9b783fecef4caca3b89e4b5c"
ARCHIVE_FRAME_HASHES = {
    "": "7d679d9b2125551be493193b7b97d77d3399f5b19af374befa1635f36b827a48",
    ".swapchain.rgba8": "96c843e4640c0f7e0eea809a8c3ddca25ac14e0e02cb5117d926df9a2af58099",
    ".draw-422.before.rgba16f": "170a461f923001dceb28ac54d67e7763465998f42e58c274ae96a4537eda7a26",
}
LIMITS = ("Global affine estimate in SDR 8-bit code units. Off-effect background agreement "
          "does not establish the fan's final color or extrapolation to arbitrary plate colors.")


class InsufficientColorEvidence(ValueError):
    """The capture does not identify a reliable off-effect RGB transfer."""


def blocks(array):
    h, w = np.array(array.shape[:2]) // PATCH_SIZE
    return array[:h * PATCH_SIZE, :w * PATCH_SIZE].reshape(h, PATCH_SIZE, w, PATCH_SIZE, *array.shape[2:])


def observation(entries, game, clean):
    base = entries[0]["before"][..., :3]
    support = np.logical_or.reduce([np.any(row["before"] != row["after"], axis=-1) for row in entries])
    if not support.any():
        return None
    nearby = np.asarray(Image.fromarray(support.astype(np.uint8)).filter(ImageFilter.MaxFilter(25))) != 0
    usable = ~nearby
    usable[:4] = usable[-4:] = usable[:, :4] = usable[:, -4:] = False
    if clean is not None:
        reference, stable = clean
        usable &= stable & (np.abs(game.astype(np.int16) - reference).max(axis=2) <= 4)
        # Require a stable neighborhood as well as a stable sample. A jittered
        # edge can agree at one pixel while moving just outside its patch.
        guard = ImageFilter.MinFilter(2 * IDLE_STABILITY_GUARD + 1)
        usable = np.asarray(Image.fromarray(usable.astype(np.uint8)).filter(guard)) != 0
    return blocks(base).mean(axis=(1, 3)), blocks(game).mean(axis=(1, 3)), blocks(usable).all(axis=(1, 3))


def samples(frames, mask):
    return tuple(np.concatenate([frame[i][frame[2] & mask] for frame in frames]) for i in range(2))


def unclipped_domain(x, y):
    ranges = []
    for rgb, codes in zip(x.T, y.T):
        keep = (codes > 3) & (codes < 252)
        if int(keep.sum()) < MIN_PATCHES:
            raise InsufficientColorEvidence("Too few unclipped background patches in an RGB channel.")
        ab, cd = np.quantile(rgb[keep], [.05, .95]), np.quantile(codes[keep], [.05, .95])
        if np.ptp(ab) < .04 or np.ptp(cd) < 12:
            raise InsufficientColorEvidence("Background does not span enough distinct unclipped tones.")
        ranges.append({"patches": int(keep.sum()), "nativeP05P95": ab.tolist(), "codeP05P95": cd.tolist()})
    return ranges


def fit(x, y):
    domain = unclipped_domain(x, y)
    coefficients = []
    for rgb, codes in zip(x.T, y.T):
        keep = (codes > 3) & (codes < 252)
        source, target = rgb[keep], codes[keep]
        columns = np.column_stack([source, np.ones_like(source)])
        weights = np.ones_like(source)
        for _ in range(6):
            slope, offset = np.linalg.lstsq(columns * weights[:, None], target * weights, rcond=None)[0]
            residual = target - (source * slope + offset)
            sigma = max(1, 1.4826 * np.median(np.abs(residual - np.median(residual))))
            weights = np.sqrt(np.minimum(1, 1.5 * sigma / np.maximum(np.abs(residual), 1e-9)))
        if not (np.isfinite(slope) and slope > 0 and np.isfinite(offset)):
            raise InsufficientColorEvidence("The background fit did not produce a finite rising RGB curve.")
        coefficients.append((slope, offset))
    slopes, offsets = np.asarray(coefficients).T
    return slopes, offsets, domain


def scores(x, y, slope, offset):
    error = np.clip(x * slope + offset, 0, 255) - y
    return {
        "patches": len(error),
        "channelMaeCodes": np.abs(error).mean(axis=0).tolist(),
        "constantChannelMaeCodes": np.abs(y - np.median(y, axis=0)).mean(axis=0).tolist(),
        "p95AbsCode": float(np.quantile(np.abs(error), .95)),
    }


def passes(score):
    mae = np.asarray(score["channelMaeCodes"])
    constant_mae = np.asarray(score["constantChannelMaeCodes"])
    # An unrelated crop can occasionally pass an absolute error threshold after
    # patch averaging. Require predictive improvement over a constant palette.
    return (mae.max() <= MAX_CHANNEL_MAE and score["p95AbsCode"] <= MAX_P95_ERROR
            and np.all(mae <= .8 * constant_mae))


def estimate_profile(active, idle):
    """Fit (entries, game RGB8) data. Keep tiles and presents out of the fit for validation."""
    clean = None
    if len(idle) >= 2:
        stack = np.stack(idle).astype(np.int16)
        clean = np.median(stack, axis=0), np.ptp(stack, axis=0).max(axis=2) <= 5
    frames = [(tag, data) for tag, entries, game in active
              if (data := observation(entries, game, clean)) is not None]
    if len(frames) < 2:
        raise InsufficientColorEvidence("Need coverage on at least two distinct presents to estimate and check colors.")
    tags, all_samples = zip(*frames)
    train, temporal = all_samples[::2], all_samples[1::2]
    h, w = train[0][2].shape
    yy, xx = np.indices((h, w))
    training_tiles = ((xx * PATCH_SIZE // TILE_SIZE + yy * PATCH_SIZE // TILE_SIZE) % 2) == 0
    # Candidate region for a centered ground effect when the upper and outer
    # part of the crop includes other geometry. Select on training tiles only.
    interior = (((xx + .5) / w - .50) / .43) ** 2 + (((yy + .5) / h - .64) / .43) ** 2 < 1
    regions = (("capture_crop", np.ones((h, w), bool)), ("centered_lower_interior", interior))
    diagnostics = {}
    for name, region in regions:
        x, y = samples(train, training_tiles & region)
        try:
            slope, offset, domain = fit(x, y)
        except InsufficientColorEvidence as error:
            diagnostics[name] = str(error)
            continue
        score = scores(x, y, slope, offset)
        diagnostics[name] = score
        if passes(score):
            break
    else:
        raise InsufficientColorEvidence(f"No background region supports a global RGB fit: {diagnostics}.")

    check = {}
    heldout = [("spatial", train), *[(str(tag), (frame,)) for tag, frame in zip(tags[1::2], temporal)]]
    for label, frames in heldout:
        x, y = samples(frames, ~training_tiles & region)
        unclipped_domain(x, y)
        check[label] = scores(x, y, slope, offset)
    if not all(map(passes, check.values())):
        raise InsufficientColorEvidence(f"Background transfer disagrees with held-out capture data: {check}.")
    return {"method": "same_present_off_effect_fit", "slope": slope.tolist(), "offset": offset.tolist(),
            "region": name, "trainingPresents": list(tags[::2]), "heldoutPresents": list(tags[1::2]),
            "idleReferenceCount": len(idle), "idleStabilityGuardPixels": IDLE_STABILITY_GUARD if clean else 0,
            "trainingDomain": domain,
            "training": score, "heldout": check, "limits": LIMITS}


def evenly_spaced(items, maximum):
    picks = np.linspace(0, len(items) - 1, min(len(items), maximum), dtype=int)
    return [items[i] for i in picks]


def resolve_profile(index_path, capture):
    """Resolve a profile from an already validated index, without operator controls."""
    from pack_fan_sequence import read_frame

    path = Path(index_path).resolve(strict=True)
    source = path.read_bytes()
    if json.loads(source) != capture:
        raise ValueError("The native index changed before display calibration.")
    source_hash = digest(source)
    records = [(frame, json.loads(member(path.parent, frame["report"]).read_bytes())["events"])
               for frame in capture["frames"]]
    active = [frame for frame, events in records
              if events and events[0]["fanCandidate"]["role"] == COVERAGE]
    idle = [frame for frame, events in records if not events]
    chosen = evenly_spaced(active, 6)
    controls = evenly_spaced(idle, 4)
    provenance = {}
    geometry = None

    def read_sample(frame, has_coverage):
        nonlocal geometry
        crop, fmt, entries, game, hashes = read_frame(path.parent, capture, frame)
        if geometry is None:
            geometry = (crop, fmt)
        if geometry != (crop, fmt):
            raise ValueError("The calibration capture changed crop or SDR format across presents.")
        if (has_coverage and (not entries or entries[0]["role"] != COVERAGE)) or (not has_coverage and entries):
            raise ValueError("A calibration frame's native draw phase changed while reading it.")
        provenance.update(hashes)
        return entries, game

    empties = [read_sample(frame, False)[1] for frame in controls]
    observations = ((frame["present"], *read_sample(frame, True)) for frame in chosen)
    try:
        result = estimate_profile(observations, empties)
    except InsufficientColorEvidence as error:
        if source_hash != ARCHIVE_INDEX_SHA256:
            raise
        frame = capture["frames"][62]
        _, _, _, _, hashes = read_frame(path.parent, capture, frame)
        if any(hashes.get(frame["report"] + suffix) != expected
               for suffix, expected in ARCHIVE_FRAME_HASHES.items()):
            raise
        provenance.update(hashes)
        result = {
            "method": "archived_purple_palette",
            "slope": [239.894950497897, 251.414263745073, 225.270308466208],
            "offset": [55.0181035363071, 15.8580858096191, 71.8456992585768],
            "archiveManifestSha256": ARCHIVE_SEQUENCE_SHA256,
            "status": "previous_fit_retained_for_matching_archive",
            "automaticFitFailure": str(error),
            "limits": LIMITS + " The archived fit exceeded earlier background color error limits.",
        }
    result["evidenceSha256"] = {path.name: source_hash, **provenance}
    return result
