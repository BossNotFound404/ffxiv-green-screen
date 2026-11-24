"""Turn a completed native blue fan capture into an editable After Effects sequence."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from fan_display_profile import InsufficientColorEvidence, resolve_profile
from pack_fan_sequence import pack, read_index


def export(index, output, replayer, adapter_index=None):
    index = index.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"输出目录已存在，请选择新目录。{output}")
    replayer = replayer.resolve(strict=True)
    capture, source_hash = read_index(index, allow_verified_prefix=True)
    profile = resolve_profile(index, capture)
    if profile["evidenceSha256"].get(index.name) != source_hash:
        raise ValueError("采集记录在测量颜色期间发生变化。")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".fan-export-") as work:
        staged = Path(work) / "sequence"
        def render(adapter):
            return pack(index, staged, profile["slope"], profile["offset"],
                        require_measured_backing=True, alpha_witness_replay_exe=replayer,
                        allow_verified_prefix=True, color_mode="portable", replay_adapter_index=adapter,
                        prepare_ae_project=True, auto_cycle=True)

        try:
            result = render(adapter_index)
        except ValueError as error:
            if adapter_index is not None or "captured_adapter_unavailable" not in str(error):
                raise
            # Windows can issue a new LUID after a restart. The native replayer still
            # requires a byte-exact original RGBA result for every substituted draw.
            result = render(0)
            result["replayAdapterFallback"] = "Captured adapter LUID unavailable; used DXGI adapter 0 with per-draw original-byte verification."
        if result["nativeIndexSha256"] != source_hash:
            raise ValueError("采集记录在导出期间发生变化。")
        # The calibration and the selected output must describe the same raw pixel reads.
        sampled = profile["evidenceSha256"]
        for frame in result["frames"]:
            if any(sampled[key] != sha for key, sha in frame["inputSha256"].items() if key in sampled):
                raise ValueError("颜色取样与导出读取的原始帧已不一致。")
        result["displayProfile"] = profile
        (staged / "sequence.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        if output.exists():
            raise FileExistsError(output)
        os.rename(staged, output)
    return result


def main():
    # The plugin reads bounded UTF-8 diagnostics from the child process.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--index", required=True, type=Path)
    cli.add_argument("--out", required=True, type=Path)
    cli.add_argument("--replay-exe", type=Path, help="use this native replayer in a source checkout")
    cli.add_argument("--adapter-index", type=int, help="graphics adapter for an archived capture after a driver restart")
    args = cli.parse_args()
    program = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    try:
        result = export(args.index, args.out, args.replay_exe or program.with_name("replay_draw_packet.exe"),
                        args.adapter_index)
    except InsufficientColorEvidence as exc:
        cli.exit(2, f"地面颜色不足以校准或机位发生了变化。请在有清晰地纹的普通场地固定镜头，重录一次。{exc}\n")
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        cli.exit(1, f"未能导出蓝扇序列。{exc}\n")
    print(f"已保存 {len(result['frames'])} 帧，时长 {result['timing']['durationSeconds']:.3f} 秒。")


if __name__ == "__main__":
    main()
