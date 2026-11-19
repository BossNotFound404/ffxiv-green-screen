"""Give every weather the same decal/sky emitter over the permanent black receiver."""
import argparse
import json
import struct
import zipfile
from pathlib import Path

from build_448_material_pair import encode, read_package
from build_448_rgb_probe import EFFECT, GROUP, chunk, chunks, copy_weather_rows, weather_rows
from build_448_sky_light import PALETTE, paint


def fill_shadows(env):
    # Only the identified ExtraAmbient ColorIntensity key in the neutral preset.
    # Use diffuse fill so model-rendered effects can benefit without a new lamp.
    result = bytearray(env)
    table, count = struct.unpack_from("<2I", env, 24)
    touched = []
    for i in range(count):
        entry = 20 + table + 16 * i
        start, channels, weather, end = struct.unpack_from("<4I", env, entry)
        if weather not in PALETTE:
            continue
        for j in range(channels):
            head = entry + start + 12 * j
            offset, keys, kind = struct.unpack_from("<3I", env, head)
            if kind != 0:
                continue
            assert keys == 1
            key = head + offset + struct.unpack_from("<I", env, head + offset)[0]
            target = key + struct.unpack_from("<I", env, key + 20)[0]
            assert key + 48 <= entry + end and key < target <= entry + end - 8
            assert env[key + 44:key + 48] == b"007V"
            assert env[target:target + 8] == bytes([255]*4) + struct.pack("<f", 0.65)
            struct.pack_into("<f", result, target + 4, 1.0)
            touched.append(weather)
    assert sorted(touched) == sorted(PALETTE)
    return bytes(result)


def build(baseline):
    assets = read_package(baseline, "bf12e9995e430195170c00e0c15c7a11d910e5d0e7eec1f7e9a0704827512068")
    files = json.loads(assets["default_mod.json"])["Files"]
    white_stage = files[GROUP + "ultimate2.obsb"]
    assets[white_stage] = copy_weather_rows(assets[white_stage], b"OBSB", {46: 47})
    for group, enabled in [("default", 1), ("ultimate", 0), ("ultimate2", 0)]:
        rows = weather_rows(assets[files[GROUP + group + ".obsb"]], b"OBSB")
        assert all(rows[w][1][22] == enabled for w in PALETTE)

    template = assets[files[EFFECT.format(4)]]
    (root, body), = chunks(template)
    assert len([p for tag, p in chunks(body) if tag == "Ptcl"]) == 2
    for stage, _, rgb in PALETTE.values():
        payload = b"".join(chunk(tag, paint(p, rgb) if tag == "Ptcl" else p)
                           for tag, p in chunks(body))
        assets[files[EFFECT.format(stage)]] = chunk(root, payload)
    assets["environment/neutral.envb"] = fill_shadows(assets["environment/neutral.envb"])
    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - Matched Stage v0.15", Version="0.15.0",
                Description="五色共用固定地面，每色由一个发射器同时绘制天幕与地板投影。")
    assets["meta.json"] = encode(meta)
    return assets


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    assets = build(args.baseline)
    with zipfile.ZipFile(args.output, "x") as archive:
        for name, value in sorted(assets.items()):
            archive.writestr(zipfile.ZipInfo(name), value, zipfile.ZIP_DEFLATED, compresslevel=9)
