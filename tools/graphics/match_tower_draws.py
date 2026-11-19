"""Find candidate m0562_share_trap_o0g2 draws in an existing D3D11 RenderDoc capture.

With the RenderDoc 1.47 Python module:
  python match_tower_draws.py cast.rdc --output tower-draws.json
Inside qrenderdoc's Python runtime, invoke main([...], initialize=False).
Only original, single-slice BC1 textures with unchanged top-level blocks match.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path


# SHA-256 of the original .atex mip 0 BC1 blocks, without header or lower mips.
TEXTURES = {
    "bh_uv_moyo4t": ((256, 256), "d0deb77d977d344924e95a8c40cd63684d147e2b13ead6e648b61eb823353043"),
    "bh_grd02s": ((128, 128), "2ff2aa8466455a8f52e0339c6773cd5e01db878f20bf4f11c3cfaa161784cce9"),
    "bh_uv_cryst1s": ((128, 128), "2016a13bf0eef470a485130928e429d87932b846af6cea246d647af2f5c47c5d"),
    "m0057_uv_line1s": ((256, 128), "1d8b905d802bf8e4f907e380e1081eaa09b9b0f383af3474b8eb87681ad1de49"),
}
GROUPS = {
    "particle_2_screen": ("bh_uv_moyo4t", "bh_grd02s", "bh_uv_cryst1s"),
    "particle_3_add": ("m0057_uv_line1s", "bh_grd02s"),
}
EXPECTED_MODES = {"particle_2_screen": "screen", "particle_3_add": "add"}


def output_evidence(view, slot, blend, shader_mask, groups):
    import renderdoc as rd

    # A bound RTV may have no shader output or only one writable channel (e.g. a mask).
    fmt = view.format
    format_known = fmt.compCount > 0 or fmt.type == rd.ResourceFormatType.A8
    format_mask = 8 if fmt.type == rd.ResourceFormatType.A8 else (1 << fmt.compCount) - 1
    writable_rgb = blend.writeMask & 7
    if format_known:
        writable_rgb &= format_mask
    if shader_mask is not None:
        writable_rgb &= shader_mask
    rgb, alpha = blend.colorBlend, blend.alphaBlend
    factors = (rgb.source, rgb.destination)
    mult = rd.BlendMultiplier
    mode = "other"
    source_weight = None
    if blend.logicOperationEnabled:
        mode = "logic_operation"
    elif not blend.enabled:
        mode = "replace"
    elif rgb.operation == rd.BlendOperation.Add:
        if factors in ((mult.One, mult.InvSrcCol), (mult.InvDstCol, mult.One)):
            mode = "screen"
        elif factors in ((mult.One, mult.One), (mult.SrcAlpha, mult.One)):
            mode = "add"
            source_weight = "one" if rgb.source == mult.One else "source_alpha"
        elif factors in ((mult.One, mult.InvSrcAlpha), (mult.SrcAlpha, mult.InvSrcAlpha)):
            mode = "over"

    comparisons = {}
    for group in groups:
        if not writable_rgb:
            status = "no_rgb_write"
        elif mode != EXPECTED_MODES[group]:
            status = "mismatch"
        elif shader_mask is None or not format_known:
            status = "unverified"
        elif writable_rgb != 7:
            status = "partial_rgb"
        else:
            status = "consistent"
        comparisons[group] = status
    return {"slot": slot, "resource": str(view.resource), "viewFormat": fmt.Name(),
            "firstMip": view.firstMip, "firstSlice": view.firstSlice, "numSlices": view.numSlices,
            "writeMask": blend.writeMask, "shaderOutputMask": shader_mask,
            "writableRGBMask": writable_rgb if not writable_rgb or (shader_mask is not None and format_known) else None,
            "blendEnabled": blend.enabled, "colorSrc": str(rgb.source), "colorDst": str(rgb.destination),
            "colorOp": str(rgb.operation), "alphaSrc": str(alpha.source),
            "alphaDst": str(alpha.destination), "alphaOp": str(alpha.operation),
            "logicOperation": str(blend.logicOperation) if blend.logicOperationEnabled else None,
            "rgbMode": mode, "additiveSourceWeight": source_weight, "groupAgreement": comparisons}


def scan(controller):
    import renderdoc as rd

    if controller.GetAPIProperties().pipelineType != rd.GraphicsAPI.D3D11:
        raise ValueError("This matcher requires a Direct3D 11 capture.")
    shapes = {size for size, _ in TEXTURES.values()}
    identities = {(size, digest): name for name, (size, digest) in TEXTURES.items()}
    group_shapes = [Counter(TEXTURES[name][0] for name in names) for names in GROUPS.values()]
    sizes = {tex.resourceId: (tex.width, tex.height) for tex in controller.GetTextures()
             if tex.format.type == rd.ResourceFormatType.BC1 and tex.dimension == 2
             and tex.arraysize == 1 and tex.depth == 1 and tex.msSamp == 1
             and (tex.width, tex.height) in shapes}
    stack = list(controller.GetRootActions())
    draw_ids = set()
    last = 0
    while stack:
        action = stack.pop()
        stack.extend(action.children)
        last = max(last, action.eventId)
        if action.flags & rd.ActionFlags.Drawcall:
            draw_ids.add(action.eventId)
    if last == 0:
        raise ValueError("No actions in capture.")

    seen = defaultdict(set)

    def identify(rid):
        width, height = sizes[rid]
        blocks = controller.GetTextureData(rid, rd.Subresource(0, 0, 0))
        if len(blocks) != ((width + 3) // 4) * ((height + 3) // 4) * 8:
            raise ValueError("Unexpected BC1 readback size for " + str(rid))
        name = identities.get(((width, height), hashlib.sha256(blocks).hexdigest()))
        if name:
            seen[name].add(str(rid))
        return name

    # Inventory at frame end is informative even when no complete group was bound.
    controller.SetFrameEvent(last, True)
    resources_by_event = defaultdict(set)
    for rid in sizes:
        identify(rid)
        for use in controller.GetUsage(rid):
            if use.eventId in draw_ids and use.usage in (rd.ResourceUsage.PS_Resource, rd.ResourceUsage.All_Resource):
                resources_by_event[use.eventId].add(rid)

    candidates = []
    consistent_draws = 0
    examined = 0
    for eid, rids in sorted(resources_by_event.items()):
        counts = Counter(sizes[rid] for rid in rids)
        if all(required - counts for required in group_shapes):
            continue
        controller.SetFrameEvent(eid, True)
        pipe = controller.GetPipelineState()
        descriptors = pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, True)
        matches = defaultdict(list)
        known_at_draw = {}
        # Re-read at the actual event. A resource ID can be updated during a frame.
        for used in descriptors:
            view = used.descriptor
            rid = view.resource
            if rid not in sizes or used.access.staticallyUnused:
                continue
            if rid not in known_at_draw:
                known_at_draw[rid] = identify(rid)
            name = known_at_draw[rid]
            if name is None:
                continue
            locations = controller.GetDescriptorLocations(used.access.descriptorStore,
                                                          [rd.DescriptorRange(used.access)])
            if len(locations) != 1:
                raise ValueError("Could not resolve D3D11 pixel-shader register at event " + str(eid))
            matches[name].append({"resource": str(rid), "register": locations[0].fixedBindNumber,
                                  "viewFormat": view.format.Name(), "firstMip": view.firstMip,
                                  "mipCount": view.numMips})
        examined += 1
        groups = [group for group, names in GROUPS.items() if all(name in matches for name in names)]
        if not groups:
            continue
        native = controller.GetD3D11PipelineState()
        om = native.outputMerger
        reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        shader_masks = defaultdict(int)
        has_signature = reflection is not None and len(reflection.outputSignature) > 0
        if has_signature:
            for param in reflection.outputSignature:
                if param.systemValue == rd.ShaderBuiltin.ColorOutput:
                    # D3D11 DXBC output signatures flag components that are *never* written.
                    shader_masks[param.semanticIndex] |= param.regChannelMask & ~param.channelUsedMask
        outputs = []
        for slot, view in enumerate(om.renderTargets):
            if view.resource == rd.ResourceId.Null():
                continue
            blend = om.blendState.blends[slot if om.blendState.independentBlend else 0]
            mask = shader_masks[slot] if has_signature else None
            outputs.append(output_evidence(view, slot, blend, mask, groups))
        consistent = [group for group in groups
                      if any(out["groupAgreement"][group] == "consistent" for out in outputs)]
        if consistent:
            consistent_draws += 1
        candidates.append({"event": eid, "matches": groups, "rgbConsistentGroups": consistent,
                           "pixelShader": str(native.pixelShader.resourceId),
                           "bindings": dict(matches), "outputs": outputs})
    status = "no_matching_group"
    if candidates:
        status = "candidates" if consistent_draws else "texture_matches_only"
    return {
        "status": status, "textureMatchedDraws": len(candidates), "rgbConsistentDraws": consistent_draws,
        "asset": "vfx/omen/eff/m0562_share_trap_o0g2.avfx",
        "scope": "matches lists texture groups. rgbConsistentGroups requires a matching blend on a writable full RGB output. "
                 "Output slots can serve different passes. Target visibility, instance identity and whole-effect isolation require further replay evidence.",
        "eligibleTextures": len(sizes), "psDrawsChecked": examined,
        "matchedTextureIds": {name: sorted(seen[name]) for name in TEXTURES},
        "groups": GROUPS, "events": candidates,
    }


def main(argv=None, initialize=True):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", type=Path, help="new JSON file, defaults to stdout")
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("output file already exists")
    try:
        import renderdoc as rd

        if initialize:
            rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        try:
            cap = rd.OpenCaptureFile()
            try:
                result = cap.OpenFile(str(args.capture), "", None)
                if not result:
                    raise RuntimeError(str(result))
                result, control = cap.OpenCapture(rd.ReplayOptions(), None)
                if not result:
                    raise RuntimeError(str(result))
                try:
                    report = scan(control)
                finally:
                    control.Shutdown()
            finally:
                cap.Shutdown()
        finally:
            if initialize:
                rd.ShutdownReplay()
    except ModuleNotFoundError as error:
        report = {"status": "failed", "error": "Run with the RenderDoc 1.47 Python module. " + str(error)}
    except Exception as error:
        report = {"status": "failed", "error": str(error)}
    data = json.dumps(report, indent=2, ensure_ascii=True)
    if args.output:
        with args.output.open("x", encoding="utf-8") as dest:
            print(data, file=dest)
    else:
        print(data)
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
