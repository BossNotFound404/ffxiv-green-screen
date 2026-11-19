"""Inspect selected pixels in an existing Direct3D 11 capture with RenderDoc 1.47.

Standalone with the matching renderdoc Python module:
  python inspect_frame.py scene.rdc --list-draws
  python inspect_frame.py scene.rdc --event 42 --point 80 50 --point 30 12 --output pixels.json

In qrenderdoc --python, call main([...], initialize=False) from a short driver
using runpy.run_path, then raise SystemExit. qrenderdoc already owns the replay
runtime. For a capture already open in the UI, inspect_controller must be called
on its replay thread; restore the UI's previous event afterwards.
"""
import argparse
import json
import math
from pathlib import Path
import sys


FAILED_TESTS = (
    "sampleMasked", "backfaceCulled", "depthClipped", "depthBoundsFailed",
    "viewClipped", "scissorClipped", "shaderDiscarded", "depthTestFailed",
    "stencilTestFailed", "predicationSkipped",
)


def all_actions(controller):
    stack = list(reversed(controller.GetRootActions()))
    result = {}
    while stack:
        action = stack.pop()
        result[action.eventId] = action
        stack.extend(reversed(action.children))
    return result


def blend_equation(equation):
    return {"source": str(equation.source), "destination": str(equation.destination),
            "operation": str(equation.operation)}


def draw_state(controller, event, texture_id, subresource):
    controller.SetFrameEvent(event, True)
    pipe = controller.GetD3D11PipelineState()
    om = pipe.outputMerger
    targets = [i for i, view in enumerate(om.renderTargets)
               if view.resource == texture_id and view.firstMip == subresource.mip
               and view.firstSlice <= subresource.slice < view.firstSlice + view.numSlices]
    if len(targets) != 1:
        return {"available": False, "matchingSlots": targets}
    slot = targets[0]
    state = om.blendState
    blend = state.blends[slot if state.independentBlend else 0]
    return {
        "available": True, "slot": slot, "viewFormat": om.renderTargets[slot].format.Name(),
        "pixelShader": str(pipe.pixelShader.resourceId), "enabled": blend.enabled,
        "rgb": blend_equation(blend.colorBlend), "alpha": blend_equation(blend.alphaBlend),
        "writeMask": blend.writeMask, "blendFactor": list(state.blendFactor),
        "alphaToCoverage": state.alphaToCoverage, "sampleMask": state.sampleMask,
        "logicOperation": str(blend.logicOperation) if blend.logicOperationEnabled else None,
    }


def color(value):
    if not value.IsValid():
        return None
    rgba = list(value.col.floatValue)
    return rgba if all(math.isfinite(v) for v in rgba) else None


def fragment(mod, is_draw):
    failures = [name for name in FAILED_TESTS if getattr(mod, name)]
    can_read_shader = is_draw and not (mod.unboundPS or mod.directShaderWrite or failures)
    return {
        "event": mod.eventId, "fragment": mod.fragIndex, "primitive": mod.primitiveID,
        "draw": is_draw, "failedTests": failures, "unboundPS": mod.unboundPS,
        "directShaderWrite": mod.directShaderWrite,
        # The replay API only guarantees the pre-event value on the first fragment.
        "preMod": color(mod.preMod) if mod.fragIndex == 0 else None,
        "shaderOut": color(mod.shaderOut) if can_read_shader else None,
        "postMod": color(mod.postMod),
    }


def inspect_controller(controller, args):
    import renderdoc as rd

    if controller.GetAPIProperties().pipelineType != rd.GraphicsAPI.D3D11:
        raise ValueError("this inspector requires a D3D11 capture")
    actions = all_actions(controller)
    draws = {eid: action for eid, action in actions.items() if action.flags & rd.ActionFlags.Drawcall}
    if args.list_draws:
        return {"status": "inventory", "draws": [
            {"event": eid, "outputs": [str(value) for value in action.outputs]}
            for eid, action in sorted(draws.items())]}

    end = args.event if args.event is not None else max(actions, default=0)
    if end not in actions:
        raise ValueError("select an action event from the capture")
    controller.SetFrameEvent(end, True)
    textures = list(controller.GetTextures())
    mip = array_slice = 0
    view_format = None
    if args.texture is not None:
        key = args.texture.split("::")[-1]
        selected = [t for t in textures if str(t.resourceId).split("::")[-1] == key]
        if len(selected) != 1:
            raise ValueError("texture ID does not identify one captured texture")
        texture = selected[0]
    else:
        targets = controller.GetD3D11PipelineState().outputMerger.renderTargets
        if not 0 <= args.slot < len(targets) or targets[args.slot].resource == rd.ResourceId.Null():
            raise ValueError("no colour target bound at that event and slot")
        view = targets[args.slot]
        texture = next(t for t in textures if t.resourceId == view.resource)
        mip, array_slice, view_format = view.firstMip, view.firstSlice, view.format

    mip = mip if args.mip is None else args.mip
    array_slice = array_slice if args.slice is None else args.slice
    if not (0 <= mip < texture.mips and 0 <= array_slice < texture.arraysize
            and 0 <= args.sample < max(1, texture.msSamp)):
        raise ValueError("mip, array slice or sample is outside this texture")
    width, height = max(1, texture.width >> mip), max(1, texture.height >> mip)
    if any(not (0 <= x < width and 0 <= y < height) for x, y in args.point):
        raise ValueError("point outside the selected mip; coordinates start at top left")
    fmt = view_format if view_format is not None else texture.format
    if fmt.compType not in (rd.CompType.Float, rd.CompType.UNorm, rd.CompType.UNormSRGB, rd.CompType.SNorm):
        raise ValueError("select a typed float/normalised colour target for RGBA inspection")
    sub = rd.Subresource(mip, array_slice, args.sample)
    points, event_states = [], {}
    for x, y in args.point:
        controller.SetFrameEvent(end, True)
        history = controller.PixelHistory(texture.resourceId, x, y, sub, fmt.compType)
        records = [fragment(mod, mod.eventId in draws) for mod in history if mod.eventId <= end]
        for event in {row["event"] for row in records if row["draw"]}:
            if event not in event_states:
                event_states[event] = draw_state(controller, event, texture.resourceId, sub)
        selected = [r for r in records if r["draw"] and (end not in draws or r["event"] == end)]
        if not selected:
            status = "no-record"
        elif all(r["failedTests"] for r in selected):
            status = "rejected"
        elif all(r["failedTests"] or (r["shaderOut"] is not None and r["postMod"] is not None
                 and event_states[r["event"]]["available"]) for r in selected):
            status = "observed"
        else:
            status = "incomplete"
        points.append({"x": x, "y": y, "status": status, "history": records})
    good = any(p["status"] == "observed" for p in points) and all(
        p["status"] != "incomplete" for p in points)
    return {"status": "observed" if good else "incomplete",
            "scope": "Selected-pixel history only; effect identity and layer isolation are unverified.",
            "endEvent": end, "target": str(texture.resourceId), "format": fmt.Name(),
            "width": width, "height": height, "mip": mip, "slice": array_slice, "sample": args.sample,
            "drawStates": event_states, "points": points}


def main(argv=None, initialize=True):
    parser = argparse.ArgumentParser(prog="inspect_frame.py", description=__doc__.splitlines()[0])
    parser.add_argument("capture", type=Path)
    parser.add_argument("--list-draws", action="store_true")
    parser.add_argument("--event", type=int, help="last action to inspect, also selects its render target")
    parser.add_argument("--texture", help="optional explicit colour texture ID (as in RenderDoc)")
    parser.add_argument("--slot", type=int, default=0, help="colour output slot when selecting by event")
    parser.add_argument("--mip", type=int)
    parser.add_argument("--slice", type=int)
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--point", type=int, nargs=2, action="append", metavar=("X", "Y"), default=[])
    parser.add_argument("--output", type=Path, help="new JSON file; defaults to stdout")
    args = parser.parse_args(argv)
    if not args.list_draws and (not args.point or (args.event is None and args.texture is None)):
        parser.error("specify --list-draws, or select --event/--texture and at least one --point")
    if args.output and args.output.exists():
        parser.error("output file already exists")
    try:
        import renderdoc as rd
        if initialize:
            rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        try:
            capture = rd.OpenCaptureFile()
            try:
                result = capture.OpenFile(str(args.capture), "", None)
                if not result: raise RuntimeError(str(result))
                result, controller = capture.OpenCapture(rd.ReplayOptions(), None)
                if not result: raise RuntimeError(str(result))
                try:
                    report = inspect_controller(controller, args)
                finally:
                    controller.Shutdown()
            finally:
                capture.Shutdown()
        finally:
            if initialize: rd.ShutdownReplay()
    except Exception as exc:
        report = {"status": "failed", "error": str(exc)}
    output = json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False)
    if args.output:
        with args.output.open("x", encoding="utf-8") as dest:
            print(output, file=dest)
    else:
        print(output)
    return 0 if report["status"] in ("inventory", "observed") else 1


if __name__ == "__main__":
    sys.exit(main())
