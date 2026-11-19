"""Package two selectable floor materials for the tested weather 46/47 setup."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile


def read_package(path, expected):
    with zipfile.ZipFile(path) as package:
        assets = {n: package.read(n) for n in package.namelist()}
        assert len(assets) == len(package.namelist())
    hashes = {n: hashlib.sha256(v).hexdigest() for n, v in assets.items()}
    digest = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(digest).hexdigest() != expected:
        raise ValueError(f"Unexpected package contents: {path}")
    return assets


def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode()


def build(baseline, donor):
    evidence = Path(__file__).resolve().parents[2] / "docs/evidence/packages.json"
    donor_hash = json.loads(evidence.read_text(encoding="utf-8"))["EnuoStudio_FiveColor_v0.4.pmp"]["contents_sha256"]
    palette = read_package(donor, donor_hash)
    assets = read_package(baseline, "abbea4dc08a399ebbfb1b4a88b87ab708b44bc4e981dde339440514475b9ecad")
    default = json.loads(assets["default_mod.json"])
    env = json.loads(assets.pop("group_001_environment.json"))
    active = env["Options"][env["DefaultSettings"]]
    default["Files"].update(active["Files"])
    default["FileSwaps"].update(active["FileSwaps"])
    colors = json.loads(palette["group_001_color.json"])["Options"]
    assert [c["Name"] for c in colors] == ["白", "黑", "红", "绿", "蓝"]

    for index, (weather, original, initial) in enumerate([("46", "白", 2), ("47", "黑", 1)], 1):
        paths = {local.rsplit(".", 1)[-1]: game for game, local in default["Files"].items()
                 if local in (f"{weather}/floor.mtrl", f"{weather}/floor.tex")}
        mat, tex = paths["mtrl"], paths["tex"]
        private = f"_k{weather[-1]}_"
        old_mat, old_tex = mat.replace(private, "_a3_"), tex.replace(private, "_a3_")
        assert private in tex and private in mat
        base_mat = assets.pop(default["Files"].pop(mat))
        base_tex = assets.pop(default["Files"].pop(tex))
        options = []
        for color in colors:
            label = color["Name"]
            material = palette[color["Files"][old_mat]]
            source = old_tex.encode() + b"\0"
            assert material.count(source) == 1
            material = material.replace(source, tex.encode() + b"\0")
            texture = palette[color["Files"][old_tex]]
            if label == original:
                assert material == base_mat and texture == base_tex
            # Share texture bytes across both weather slots, keeping the two game paths independent.
            suffix = color["Files"][old_tex]
            local_tex = "palette/" + suffix
            local_mat = f"{weather}/" + color["Files"][old_mat]
            assets[local_mat] = material
            assets[local_tex] = texture
            options.append(dict(Name=label, Files={mat: local_mat, tex: local_tex}, FileSwaps={}, Manipulations=[]))
        group = dict(Version=0, Type="Single", Name=weather + " 地色", Priority=index,
                     DefaultSettings=initial, Options=options, Description="在旅馆选色，再加载 448。")
        assets[f"group_00{index}_floor_{weather}.json"] = encode(group)
    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - Material Pair v0.9", Version="0.9.0",
                Description="在旅馆为 46、47 各选一种地色；加载 448 后用天气原地切这两色。默认 46 红、47 黑。")
    assets["meta.json"] = encode(meta)
    assets["default_mod.json"] = encode(default)
    return assets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("baseline", "donor", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    assets = build(args.baseline, args.donor)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in assets.items():
            archive.writestr(name, data)
    with zipfile.ZipFile(buf) as archive:
        assert archive.testzip() is None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as out:
        out.write(buf.getvalue())


if __name__ == "__main__":
    main()
