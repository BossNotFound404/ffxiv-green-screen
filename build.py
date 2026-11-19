"""Build recording studio packages from a local FFXIV client."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


# These are the two complete packages accepted by the studio plugin's importer.
STABLE_STAGE = "RoundTable_StableStage_v0.16.pmp"
CAPTURE_STAGE = "RoundTable_CaptureStage_v0.17.pmp"
STUDIO_HASHES = {
    STABLE_STAGE: "6556cbdc8cff5793aef80e900ce53a273b77ec5252fafd746d71066120388bfe",
    CAPTURE_STAGE: "5824fe4a956ae8de59ddc3df65950686c4573e6ab3716c1d7c6f9351d452903b",
}


def studio_steps():
    five_color = "output/EnuoStudio_FiveColor_v0.4/EnuoStudio_FiveColor_v0.4.pmp"
    return [
        ("build_448_floor_probe", five_color, "floors.pmp", "--light-off-floors"),
        ("build_448_material_pair", "floors.pmp", five_color, "materials.pmp"),
        ("build_448_rgb_probe", "floors.pmp", "red.pmp", "--solid-red-probe", "materials.pmp"),
        ("build_448_rgb_probe", "red.pmp", "projected.pmp", "--floor-decals"),
        ("build_448_sky_light", "projected.pmp", "sky.pmp"),
        ("build_448_quiet", "sky.pmp", "quiet.pmp"),
        ("build_448_matched", "quiet.pmp", "matched.pmp"),
        ("build_448_stable", "matched.pmp", "quiet.pmp", STABLE_STAGE),
        ("build_448_capture", STABLE_STAGE, CAPTURE_STAGE),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqpack", default=os.environ.get("FFXIV_SQPACK"), help="Local FFXIV game/sqpack folder")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--studio", action="store_true", help="Build the plugin's stable v0.16 and optional capture v0.17 stages")
    mode.add_argument("--all", action="store_true", help="Also build the archived 171, 169 and NativeBW experiments")
    parser.add_argument("--out", type=Path, help="New output folder (defaults to output/studio for --studio, otherwise output/packages)")
    args = parser.parse_args()
    if not args.sqpack:
        parser.error("Use --sqpack or set FFXIV_SQPACK.")
    sqpack = Path(args.sqpack).resolve()
    if not all(any((sqpack / folder).glob(pattern)) for folder, pattern in
               [("ex4", "02*.index2"), ("ffxiv", "01*.index2")]):
        parser.error("Expected a game/sqpack directory containing the ex4 and ffxiv indices.")
    if args.studio and not any((sqpack / "ex1").glob("02*.index2")):
        parser.error("The studio also requires the ex1 area data in game/sqpack.")
    repo = Path(__file__).resolve().parent
    default_output = repo / "output" / ("studio" if args.studio else "packages")
    destination = (args.out or default_output).resolve()
    if destination.exists():
        parser.error("Output folder already exists. Choose a new folder with --out.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    steps = [("build_white_probe",), ("build_stage_probe",), ("build_cloud_probe",), ("build_five_color",)]
    if args.all:
        steps.extend([("build_weather171_probe",), ("build_weather171_environment",),
                      ("build_weather169_probe",), ("build_weather169_probe", "--control"),
                      ("build_weather169_probe", "--layer-test"), ("layouts",), ("build_native_dual",)])
    env = dict(os.environ, FFXIV_SQPACK=str(sqpack), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env.pop("PYTHONOPTIMIZE", None)
    with tempfile.TemporaryDirectory(dir=destination.parent) as staging:
        root = Path(staging)
        tools = root / "tools" / "enuo"
        shutil.copytree(repo / "tools" / "enuo", tools)
        if args.studio:
            evidence = root / "docs" / "evidence"
            evidence.mkdir(parents=True)
            for filename in ("packages.json", "a2fb-weather.json"):
                shutil.copy2(repo / "docs" / "evidence" / filename, evidence)
            steps.extend(studio_steps())
        for name, *flags in steps:
            print("Building", name, *flags, flush=True)
            subprocess.run([sys.executable, str(tools / (name + ".py")), *flags], env=env, cwd=root,
                           check=True, stdout=subprocess.DEVNULL)
        if args.studio:
            packages = [root / name for name in STUDIO_HASHES]
            for package in packages:
                digest = hashlib.sha256(package.read_bytes()).hexdigest()
                if digest != STUDIO_HASHES[package.name]:
                    raise RuntimeError(f"{package.name} does not match the plugin's required SHA-256. Got {digest}.")
        else:
            packages = sorted((root / "output").glob("*/*.pmp"))
            expected = 10 if args.all else 4
            if len(packages) != expected:
                raise RuntimeError(f"Expected {expected} packages, got {len(packages)}.")
        ready = root / "packages"
        ready.mkdir()
        for package in packages:
            shutil.copy2(package, ready / package.name)
        ready.rename(destination)
    print(f"{len(packages)} packages in {destination}")


if __name__ == "__main__":
    main()
