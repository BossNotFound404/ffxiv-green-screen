"""Add a physical neutral-gray set in native weather 44 to Stable or Capture Stage."""
import argparse
import io
import json
import struct
import zipfile
from pathlib import Path

from build_448_capture import STABLE_CONTENTS
from build_448_material_pair import encode, read_package
from build_448_stable import OBJECT_GROUP, WHITE_FLOOR
from stage_lamp_track import set_visible_lamp_gain


BASELINES = {
    "0.16.0": (STABLE_CONTENTS, "0.18", "Stable Stage"),
    "0.17.0": ("4a861384ec3cf54509d887d8262433365a9387b5df02982e2769114d0c82bb79",
               "0.19", "Capture Stage"),
}
GRAY_HOST = "bg/ex1/03_abr_a2/fld/a2fb/bgparts/a2fb_a1_boss1.mdl"
SHAFT = "bg/ex1/03_abr_a2/fld/a2fb/bgparts/a2fb_l1_shft1b.mdl"
WEATHER_IDS = {44, 45, 46, 47, 48, 56}
GRAY_LINEAR = ((128 / 255 + 0.055) / 1.055) ** 2.4


def gray_material(white):
    # Retain the white diffuse texture, shader and samplers. Apply the target
    # once in each color branch, not again in the sampled texture.
    _, size, dataset, strings, _, tex, uv, channels, extra = struct.unpack_from("<I4H4B", white)
    header = 16 + 4 * (tex + uv + channels) + strings + extra + dataset
    values, keys, constants, samplers = struct.unpack_from("<4H", white, header)
    start = header + 12 + 8 * keys
    values_at = start + 8 * constants + 12 * samplers
    assert values_at + values == size == len(white)
    targets = {0x38A64362, 0xAA676D0F}
    data = bytearray(white)
    for key, offset, length in struct.iter_unpack("<IHH", white[start:start + constants * 8]):
        if key in targets:
            assert length == 12 and struct.unpack_from("<3f", white, values_at + offset) == (1, 1, 1)
            struct.pack_into("<3f", data, values_at + offset, *([GRAY_LINEAR] * 3))
            targets.remove(key)
    assert not targets
    return bytes(data)


def build(baseline):
    with zipfile.ZipFile(baseline) as archive:
        version = json.loads(archive.read("meta.json"))["Version"]
    if version not in BASELINES:
        raise ValueError("Expected Stable Stage v0.16 or Capture Stage v0.17")
    digest, version, name = BASELINES[version]
    assets = read_package(baseline, digest)
    default = json.loads(assets["default_mod.json"])
    files = default["Files"]

    # Both platform instances have the identity transform in bg.lgb. The old
    # RGB emitter still draws red; this physical host is off in all five colors.
    assert files[WHITE_FLOOR] == "46/floor.mdl" and files[GRAY_HOST] == "45/floor.mdl"
    assert list(files.values()).count("45/floor.mdl") == 1
    white_mat, = (k for k, v in files.items() if v == "46/floor.mtrl")
    gray_mat = white_mat.replace("_k6_", "_k4_")
    white_path = white_mat.encode() + b"\0"
    white_model = assets[files[WHITE_FLOOR]]
    assert gray_mat != white_mat and gray_mat not in files and white_model.count(white_path) == 1
    del assets[files[GRAY_HOST]]
    files[GRAY_HOST] = "44/floor.mdl"
    files[gray_mat] = "44/floor.mtrl"
    assets["44/floor.mdl"] = white_model.replace(white_path, gray_mat.encode() + b"\0")
    assets["44/floor.mtrl"] = gray_material(assets[files[white_mat]])

    # The remaining members of ultimate are eight copies of a hidden shaft
    # and the green center lamp. Keep the mesh visible with zero lamp gain.
    assert files[SHAFT] == "studio/no_triangles.mdl"
    host = files[OBJECT_GROUP + "ultimate.obsb"]
    assets[host] = set_visible_lamp_gain(assets[host], 44, 0.0, was_visible=False)

    # Match weather 46 in each lighting option, including the original-env
    # comparison option. The 7-channel payload is owner-relative in this table.
    light_group = json.loads(assets["group_001_light.json"])
    environments = {v for option in light_group["Options"]
                    for v in option["Files"].values() if v.endswith(".envb")}
    assert environments == {"environment/a2fb.envb", "environment/neutral.envb"}
    for local in environments:
        data = assets[local]
        assert data[:4] == b"ENVB" and data[12:16] == b"ENVS"
        offset, count = struct.unpack_from("<2I", data, 24)
        spans = {}
        for i in range(count):
            entry = 20 + offset + 16 * i
            start, channels, weather, end = struct.unpack_from("<4I", data, entry)
            assert channels == 7 and end - start == 532
            spans[weather] = slice(entry + start, entry + end)
        assert set(spans) == WEATHER_IDS
        source, target = data[spans[46]], spans[44]
        assert source[:7 * 12] == data[target][:7 * 12]
        assets[local] = data[:target.start] + source + data[target.stop:]

    assets["default_mod.json"] = encode(default)
    meta = json.loads(assets["meta.json"])
    meta.update(Name=f"Round Table Studio - {name} v{version}", Version=f"{version}.0",
                Description="六色摄影棚。中灰使用与白棚相同的实体地板、围壁和环境参数，材质以 sRGB 128 为目标。")
    assets["meta.json"] = encode(meta)
    return assets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    assets = build(args.baseline)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, value in sorted(assets.items()):
            archive.writestr(zipfile.ZipInfo(name), value, zipfile.ZIP_DEFLATED, compresslevel=9)
    with zipfile.ZipFile(buf) as archive:
        assert archive.testzip() is None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(buf.getvalue())


if __name__ == "__main__":
    main()
