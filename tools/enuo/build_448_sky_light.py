"""Build the five-weather studio backdrop and neutral lighting test from v0.12."""
import argparse
import io
import json
import struct
import zipfile
from pathlib import Path

from build_448_material_pair import encode, read_package
from build_448_rgb_probe import (COLORS, EFFECT, GROUP, chunk, chunks, copy_weather_rows,
                                 integer, load_ex1_indices, probe, rewrite, solid_overlay,
                                 weather_rows)

PALETTE = {46: (1, "white", (1, 1, 1)), 47: (2, "black", (0, 0, 0)), **COLORS}
BASELINE = "7823ae995ba66207e47d0c106a7bcccf887ec31ea46a61f68d01e6532cd8f1be"


def paint(particle, rgb):
    color = dict(chunks(particle))["Col"]
    curve = dict(chunks(color))["RGB"]
    key = dict(chunks(curve))["Keys"]
    assert len(key) == 16 and key[:4] == struct.pack("<hh", 0, 1)
    curve = rewrite(curve, {"Keys": key[:4] + struct.pack("<3f", *rgb)})
    return rewrite(particle, {"Col": rewrite(color, {"RGB": curve})})


def backdrop_mesh(model):
    sample = dict(chunks(model))["VDrw"][:36]
    # The capture area around (100, 0, 100) lies inside this box. Its open
    # bottom leaves the receiving floor and all projected combat effects alone.
    corners = [(x, y, z) for y in (-12, 256) for x, z in
               [(-256, -256), (256, -256), (256, 256), (-256, 256)]]
    verts = b"".join(struct.pack("<3e", *corner) + sample[6:] for corner in corners)
    indices = []
    for a, b, c, d in [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6),
                       (3, 0, 4, 7), (4, 5, 6, 7)]:
        indices.extend([a, b, c, a, c, d, c, b, a, d, c, a])
    return chunk("VDrw", verts) + chunk("VIdx", struct.pack("<60H", *indices))


def with_backdrop(base, sky_particle, mesh, has_decal):
    (root, body), = chunks(base)
    fields = dict(chunks(body))
    assert root == "AVFX" and fields["PrCn"] == integer(1)
    # Keep the model particle's authored sort priority and depth test. Only
    # the five distant faces are drawn, so no sky triangles overlap the floor.
    count = 2 if has_decal else 1
    emitter = fields["Emit"]
    if has_decal:
        child = dict(chunks(emitter))["ItPr"]
        emitter = rewrite(emitter, {"PrCn": integer(2)}) + chunk("ItPr", rewrite(child, {"TgtB": integer(1)}))
    body = rewrite(body, {"DwLy": integer(3), "PrCn": integer(count), "MdCn": integer(1),
                          "CBSy": struct.pack("<f", 640), "Emit": emitter})
    nodes = []
    for tag, value in chunks(body):
        if tag == "Ptcl":
            if has_decal:
                nodes.append((tag, value))
            nodes.append((tag, sky_particle))
        elif tag != "Modl":
            nodes.append((tag, value))
    return chunk(root, b"".join(chunk(k, v) for k, v in nodes) + chunk("Modl", mesh))


def neutral_light(original):
    """Only edit the three color bytes and gain in identified ColorIntensity records."""
    result = bytearray(original)
    table, count = struct.unpack_from("<2I", original, 24)
    for i in range(count):
        entry = 20 + table + i * 16
        start, channels, weather, end = struct.unpack_from("<4I", original, entry)
        if weather not in PALETTE:
            continue
        for j in range(channels):
            head = entry + start + 12 * j
            offset, keys, kind = struct.unpack_from("<3I", original, head)
            if kind not in (0, 1):
                continue
            assert keys == 1
            key = head + offset + struct.unpack_from("<I", original, head + offset)[0]
            assert struct.unpack_from("<f", original, key)[0] == 43200
            if kind == 0:
                assert original[key+44:key+48] == b"007V"
                # Extra ambient light provides neutral fill on faces in shadow.
                edits = [(20, bytes.fromhex("cce0ffff"), 0.2, 0.65)]
            else:
                edits = [(4, bytes.fromhex("b2d1ffff"), 0.3, 0.3),
                         (8, bytes.fromhex("b2e8ffff"), 0.3, 0.3),
                         (12, bytes.fromhex("7fffb2ff"), 0.5, 0.5)]
            for field, expected, old_gain, new_gain in edits:
                target = key + struct.unpack_from("<I", original, key + field)[0]
                assert target + 8 <= entry + end
                assert original[target:target+8] == expected + struct.pack("<f", old_gain)
                result[target:target+8] = bytes([255]*4) + struct.pack("<f", new_gain)
    before = weather_rows(original, b"ENVB")
    after = weather_rows(bytes(result), b"ENVB")
    assert {w for w in before if before[w] != after[w]} == PALETTE.keys()
    assert len({after[w] for w in PALETTE}) == 1
    return bytes(result)


def build(path):
    assets = read_package(path, BASELINE)
    default = json.loads(assets["default_mod.json"])
    files, swaps = default["Files"], default["FileSwaps"]
    load_ex1_indices()
    native = probe.read(EFFECT.format(2))
    template = solid_overlay(native, assets["46/floor.mdl"], (1, 1, 1))
    fields = dict(chunks(next(chunks(template))[1]))
    mesh = backdrop_mesh(fields["Modl"])
    green = assets[files[EFFECT.format(4)]]
    black_stage = files[GROUP + "ultimate.obsb"]
    assets[black_stage] = copy_weather_rows(assets[black_stage], b"OBSB", {45: 47})
    for weather, (stage, name, rgb) in PALETTE.items():
        group_path = GROUP + f"vfx{stage:02d}.obsb"
        local = files[group_path]
        # Use the native group's own ON record and its own OFF record.
        native_table = probe.read(group_path)
        rows = weather_rows(native_table, b"OBSB")
        on, = [w for w, (_, row) in rows.items() if row[22]]
        assert rows[44][1][22] == 0
        matrix = copy_weather_rows(native_table, b"OBSB", {w: on if w == weather else 44 for w in PALETTE})
        new_rows = weather_rows(matrix, b"OBSB")
        old_rows = weather_rows(assets[local], b"OBSB")
        assert all(new_rows[w] == old_rows[w] for w in old_rows.keys() - PALETTE.keys())
        assets[local] = matrix
        effect = EFFECT.format(stage)
        swaps.pop(effect, None)
        if weather in COLORS:
            base = assets[files[effect]] if weather != 45 else green
            root, body = next(chunks(base))
            particle = dict(chunks(body))["Ptcl"]
            base = chunk(root, rewrite(body, {"Ptcl": paint(particle, rgb)}))
        else:
            base = template
        local = f"sky/{name}.avfx"
        files[effect] = local
        assets[local] = with_backdrop(base, paint(fields["Ptcl"], rgb), mesh, weather in COLORS)
    env = "environment/a2fb.envb"
    neutral = "environment/neutral.envb"
    assets[neutral] = neutral_light(assets[env])
    env_path, = [k for k, v in files.items() if v == env]
    del files[env_path]
    options = [dict(Name=label, Files={env_path: local}, FileSwaps={}, Manipulations=[])
               for label, local in [("中性补光", neutral), ("原环境光对照", env)]]
    assets["group_001_light.json"] = encode(dict(Name="人物灯光", Type="Single", Version=0,
        Priority=1, DefaultSettings=0, Options=options, Description="两档使用同一天幕和地板。换灯后请重新加载场地。"))
    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - Sky and Light v0.13", Version="0.13.0",
                Description="448 五色天幕与人物补光测试。47 黑、45 红、48 绿、56 蓝、46 白。")
    assets["meta.json"] = encode(meta)
    assets["default_mod.json"] = encode(default)
    used = set(files.values()) | {env, neutral}
    assert used <= assets.keys() and not files.keys() & swaps.keys()
    return {k: v for k, v in assets.items() if k.endswith(".json") or k in used}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    assets = build(args.baseline)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in sorted(assets.items()):
            archive.writestr(zipfile.ZipInfo(name), data, zipfile.ZIP_DEFLATED, compresslevel=9)
    with zipfile.ZipFile(buf) as archive:
        assert archive.testzip() is None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(buf.getvalue())
    print(args.output)


if __name__ == "__main__":
    main()
