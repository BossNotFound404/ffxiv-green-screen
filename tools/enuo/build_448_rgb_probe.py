"""Build the 448 five-weather RGB overlay experiment from the verified v0.6 PMP."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import zipfile

from audit_448 import load_ex1_indices, probe, weather_rows
from avfx_chunks import chunk, chunks, rewrite


COLORS = {45: (3, "red", (1, 0, 0)), 48: (4, "green", (0, 1, 0)),
          56: (5, "blue", (0, 0, 1))}
GROUP = "bgcommon/env/obset/ex1_obset/obset_abr_a2/obset_a2_fld/obset_a2fb_"
EFFECT = "bg/ex1/03_abr_a2/common/vfx/eff/b0673_{}lnd_y.avfx"


def integer(value):
    return struct.pack("<i", value)


def floor_mesh(mdl, vertex_template):
    vertex_at = struct.unpack_from("<I", mdl, 16)[0]
    index_at = struct.unpack_from("<I", mdl, 28)[0]
    indices = mdl[index_at:]
    assert len(vertex_template) == 36 and len(indices) == 2496 * 2
    assert max(struct.unpack("<2496H", indices)) < 449
    vertices = bytearray()
    for i in range(449):
        x, y, z, w = struct.unpack_from("<4e", mdl, vertex_at + 8 * i)
        # Place the overlay halfway between the old white surface and the black base.
        vertices.extend(struct.pack("<4e", x, y - 1 / 32, z, w) + vertex_template[8:])
    return chunk("VDrw", vertices) + chunk("VIdx", indices)


def solid_overlay(template, floor, rgb):
    (root, body), = chunks(template)
    assert root == "AVFX" and hashlib.sha256(template).hexdigest() == (
        "8949ca5204a9ce4643eebdd63b5e6951a8fa40dd46616a223970dc8a3a5cd85e")
    nodes = list(chunks(body))
    # Particle 2 is an untextured model with infinite life and an authored solid RGB key.
    particle = [v for k, v in nodes if k == "Ptcl"][2]
    model = [v for k, v in nodes if k == "Modl"][2]
    fields = dict(chunks(particle))
    assert all(dict(chunks(fields[k]))["bEna"] == integer(0)
               for k in ("TC1", "TC2", "TN", "TR", "TD", "TP"))
    transform = fields["Rot"]
    color_curve = dict(chunks(fields["Col"]))["RGB"]
    key = dict(chunks(color_curve))["Keys"]
    assert key == struct.pack("<hh3f", 0, 1, 0, 0, 0)
    color = chunk("RGB", rewrite(color_curve, {"Keys": key[:4] + struct.pack("<3f", *rgb)}))
    particle = rewrite(particle, {"Data": chunk("MNO", integer(0)), "Col": color})
    vertices = dict(chunks(model))["VDrw"]
    assert len(vertices) == 452 * 36 and vertices[16:20] == b"\xff" * 4
    mesh = floor_mesh(floor, vertices[:36])

    emitter = dict(nodes)["Emit"]
    children = [v for k, v in chunks(emitter) if k == "ItPr"]
    child = rewrite(children[0], {"TgtB": integer(0)})
    emitter = b"".join(chunk(k, v) for k, v in chunks(emitter) if k != "ItPr")
    # Remove the stage animation and alpha ramp. Keep the parent's native white RGB.
    white = dict(chunks(dict(chunks(emitter))["Col"]))["RGB"]
    emitter = rewrite(emitter, {"PrCn": integer(1), "Rot": transform, "Col": chunk("RGB", white)})
    emitter += chunk("ItPr", child)
    body = b"".join(chunk(k, v) for k, v in nodes if k not in ("Ptcl", "Modl"))
    extent = struct.pack("<f", 2200)
    body = rewrite(body, {"PrCn": integer(1), "MdCn": integer(1), "Emit": emitter,
                          "CBSx": extent, "CBSz": extent})
    # Keep native order (particles precede textures; embedded models follow textures).
    ordered = list(chunks(body))
    first_texture = next(i for i, (tag, _) in enumerate(ordered) if tag == "Tex")
    ordered.insert(first_texture, ("Ptcl", particle))
    return chunk(root, b"".join(chunk(k, v) for k, v in ordered) + chunk("Modl", mesh))


def copy_weather_rows(original, kind, sources):
    rows = weather_rows(original, kind)
    result = bytearray(original)
    table, count = struct.unpack_from("<II", original, 24)
    for i in range(count):
        entry = 20 + table + i * 16
        start, channels, weather, end = struct.unpack_from("<4I", original, entry)
        if weather in sources:
            expected_count, data = rows[sources[weather]]
            assert channels == expected_count and end - start == len(data)
            assert rows[weather][1][:12 * channels] == data[:12 * channels]
            result[entry + start:entry + end] = data
    assert weather_rows(result, kind) == rows | {w: rows[s] for w, s in sources.items()}
    return bytes(result)


def build(base_path):
    with zipfile.ZipFile(base_path) as base:
        assets = {n: base.read(n) for n in base.namelist()}
        assert len(assets) == len(base.namelist())
    hashes = {n: hashlib.sha256(v).hexdigest() for n, v in assets.items()}
    fingerprint = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(fingerprint).hexdigest() == (
        "abbea4dc08a399ebbfb1b4a88b87ab708b44bc4e981dde339440514475b9ecad")
    default = json.loads(assets["default_mod.json"])
    files = default["Files"]
    env = json.loads(assets.pop("group_001_environment.json"))
    selected = env["Options"][env["DefaultSettings"]]
    files.update(selected["Files"])
    default["FileSwaps"].update(selected["FileSwaps"])
    # For RGB weather, start with the exact known black-floor lighting and sky.
    black_rows = dict.fromkeys(COLORS, 47)
    for path, local in files.items():
        if path.endswith((".envb", ".obsb")):
            assets[local] = copy_weather_rows(assets[local], path[-4:].upper().encode(), black_rows)
    load_ex1_indices()
    template = probe.read(EFFECT.format(2))
    evidence = json.loads((Path(__file__).resolve().parents[2] /
                           "docs/evidence/a2fb-weather.json").read_text())["resource_sha256"]
    for group in ("vfx01", "vfx03", "vfx04", "vfx05"):
        path = GROUP + group + ".obsb"
        native = probe.read(path)
        assert hashlib.sha256(native).hexdigest() == evidence[path]
        rows = weather_rows(native, b"OBSB")
        on, = (w for w, (_, row) in rows.items() if row[22])
        local = files.setdefault(path, "lights/" + group + ".obsb")
        sources = {w: (on if group == f"vfx{stage:02d}" else 44)
                   for w, (stage, _, _) in COLORS.items()}
        # vfx03 is already disabled in 47 by v0.6. Take ON from the native table.
        sources.update({46: 44, 47: 44})
        assets[local] = copy_weather_rows(native, b"OBSB", sources)
    for stage, name, rgb in COLORS.values():
        path = EFFECT.format(stage)
        default["FileSwaps"].pop(path, None)
        local = f"rgb/{name}.avfx"
        files[path] = local
        assets[local] = solid_overlay(template, assets["46/floor.mdl"], rgb)

    # Check the whole scene switch matrix, including groups neutralized in v0.6.
    for w in (45, 46, 47, 48, 56):
        enabled = {p[len(GROUP):-5] for p, local in files.items() if p.startswith(GROUP)
                   and weather_rows(assets[local], b"OBSB")[w][1][22]}
        expected = {"default", "vfx02"}
        if w == 46:
            expected.add("ultimate2")
        if w in COLORS:
            expected.add(f"vfx{COLORS[w][0]:02d}")
        assert enabled == expected, (w, enabled, expected)
    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - RGB Floor Probe v0.7", Version="0.7.0",
                Description="448 原地换色实验。46 白、47 黑、45 红、48 绿、56 蓝。彩色地面与阶段灯需进游戏确认。")
    for name, data in (("meta.json", meta), ("default_mod.json", default)):
        assets[name] = json.dumps(data, ensure_ascii=False, indent=2).encode()
    assert set(files.values()) <= assets.keys() and not files.keys() & default["FileSwaps"].keys()
    return assets


def red_depth_probe(assets):
    default = json.loads(assets["default_mod.json"])
    path = EFFECT.format(3)
    original_name = default["Files"].pop(path)
    original = assets[original_name]
    (tag, body), = chunks(original)
    parts = dict(chunks(body))
    assert tag == "AVFX" and parts["PrCn"] == parts["MdCn"] == integer(1)

    mesh = parts["Modl"]
    vertices = bytearray(dict(chunks(mesh))["VDrw"])
    assert len(vertices) == 449 * 36
    for offset in range(2, len(vertices), 36):
        height, = struct.unpack_from("<e", vertices, offset)
        struct.pack_into("<e", vertices, offset, height + 0.25)
    raised_mesh = rewrite(mesh, {"VDrw": vertices})
    raised = chunk(tag, rewrite(body, {"Modl": raised_mesh}))

    particle = parts["Ptcl"]
    fields = dict(chunks(particle))
    assert fields["DsDt"] == integer(1) and fields["DsDw"] == integer(0)
    through = chunk(tag, rewrite(body, {"Ptcl": rewrite(particle, {"DsDt": integer(0)})}))
    assert len(original) == len(through) and sum(a != b for a, b in zip(original, through)) == 1

    assets["rgb/red_raised.avfx"] = raised
    assets["rgb/red_no_depth.avfx"] = through
    options = [dict(Name=label, Files={path: local}, FileSwaps={}, Manipulations=[])
               for label, local in [("A · 红面抬高 0.25", "rgb/red_raised.avfx"),
                                    ("B · 原高度，关闭红面深度测试", "rgb/red_no_depth.avfx"),
                                    ("C · v0.7 原样对照", original_name)]]
    group = dict(Version=0, Type="Single", Name="45 红地对照", Priority=1, DefaultSettings=0,
                 Options=options, Description="在旅馆选档后重载 448。B 档用于检查遮挡，可能盖住角色。")
    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - Red Depth Probe v0.8", Version="0.8.0",
                Description="只测试 45 的红色面。默认抬高红面，另有深度测试和原样参照。")
    for name, data in (("meta.json", meta), ("default_mod.json", default),
                       ("group_001_red_depth.json", group)):
        assets[name] = json.dumps(data, ensure_ascii=False, indent=2).encode()


def scene_layer_probe(assets):
    """Place only the three weather backdrops in BaseLower, below ordinary effects."""
    default = json.loads(assets["default_mod.json"])
    for stage, color, rgb in COLORS.values():
        local = default["Files"][EFFECT.format(stage)]
        original = assets[local]
        (root, body), = chunks(original)
        fields = dict(chunks(body))
        particle = dict(chunks(fields["Ptcl"]))
        assert root == "AVFX" and fields["DwLy"] == integer(6)  # BehindCloud
        assert particle["DsDt"] == integer(1) and particle["DsDw"] == integer(0)
        curve = dict(chunks(particle["Col"]))["RGB"]
        assert dict(chunks(curve))["Keys"] == struct.pack("<hh3f", 0, 1, *rgb)
        updated = chunk(root, rewrite(body, {"DwLy": integer(3)}))  # BaseLower
        assert len(updated) == len(original) and sum(a != b for a, b in zip(original, updated)) == 1, color
        assets[local] = updated
    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - Five Weather Scene Layer v0.10", Version="0.10.0",
                Description="448 同机位五色测试。47 黑、45 红、48 绿、56 蓝、46 白。彩色面使用场景下层并保留深度测试。")
    assets["meta.json"] = json.dumps(meta, ensure_ascii=False, indent=2).encode()


def solid_red_probe(assets, pair_path):
    """Use the native ultimate platform for 45 while keeping 46 and 47 as controls."""
    from build_448_floor_probe import replace_path
    from build_448_material_pair import encode, read_package

    pair = read_package(pair_path, "f92da938e907961f2afdbb530bc51331f6f74d5a60c8e18ac8555f8153f445e8")
    layout = probe.read("bg/ex1/03_abr_a2/fld/a2fb/level/bg.lgb")
    assert hashlib.sha256(layout).hexdigest() == "b9077b2bc96f8b389392681ce8fa7f6231ca8fa5585e695792ddfbd6d6d3658c"
    default = json.loads(assets["default_mod.json"])
    files = default["Files"]
    options = json.loads(pair["group_001_floor_46.json"])["Options"]
    red, = (opt["Files"] for opt in options if opt["Name"] == "红")
    mat, = (p for p in red if p.endswith(".mtrl"))
    tex, = (p for p in red if p.endswith(".tex"))
    red_mat, red_tex = (p.replace("_k6_", "_k5_") for p in (mat, tex))
    for path in (red_mat, red_tex):
        assert "_k5_" in path and path not in files and probe.read(path) is None
    host = "bg/ex1/03_abr_a2/fld/a2fb/bgparts/a2fb_a1_boss1.mdl"
    assert files[host] == "47/floor.mdl"
    assert "studio/lower_black.mdl" in files.values()
    files.update({host: "45/floor.mdl", red_mat: "45/floor.mtrl", red_tex: "45/floor.tex"})
    assets["45/floor.mdl"] = replace_path(assets["46/floor.mdl"], mat, red_mat)
    assets["45/floor.mtrl"] = replace_path(pair[red[mat]], tex, red_tex)
    assets["45/floor.tex"] = pair[red[tex]]

    # The third platform shares ultimate with one center light and eight shafts.
    shaft = host.rsplit("/", 1)[0] + "/a2fb_l1_shft1b.mdl"
    assert shaft not in files and shaft.encode() in layout
    files[shaft] = "studio/no_triangles.mdl"
    table = GROUP + "ultimate.obsb"
    native = probe.read(table)
    assert weather_rows(native, b"OBSB")[47][1][22] == 1
    local = files[table]
    original = assets[local]
    # 45 is the only row to turn on. Preserve every other row from v0.10.
    rows = weather_rows(original, b"OBSB")
    enabled = weather_rows(native, b"OBSB")[47]
    assert rows[45][1][:22] + b"\x01" + rows[45][1][23:] == enabled[1]
    updated = bytearray(original)
    offset, count = struct.unpack_from("<II", original, 24)
    for i in range(count):
        entry = 20 + offset + 16 * i
        start, channels, weather, end = struct.unpack_from("<4I", original, entry)
        if weather == 45:
            assert channels == 1 and end - start == 24
            updated[entry + start:entry + end] = enabled[1]
    assets[local] = bytes(updated)
    assert weather_rows(assets[local], b"OBSB") == rows | {45: enabled}
    assert sum(a != b for a, b in zip(original, assets[local])) == 1
    assets.pop(files.pop(EFFECT.format(3)))
    default["FileSwaps"][EFFECT.format(3)] = default["FileSwaps"][EFFECT.format(2)]
    assert set(files.values()) <= assets.keys() and not files.keys() & default["FileSwaps"].keys()
    assets["default_mod.json"] = encode(default)
    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - Solid Red Floor v0.11", Version="0.11.0",
                Description="45 实体红地扇形测试，47 黑、46 白作参照。48 绿、56 蓝沿用 v0.10。")
    assets["meta.json"] = encode(meta)


def floor_decals(package):
    """Project the two remaining colors onto the v0.11 black stage."""
    from build_448_material_pair import encode, read_package

    assets = read_package(package, "49272b589f443a5ada6eb33b889dc03f8f35585e90391397b33d31f79c23a7d8")
    default = json.loads(assets["default_mod.json"])
    white = "bg/ex1/03_abr_a2/common/vfx/texture/rtstudio_floor_white.atex"
    assert white not in default["Files"]
    default["Files"][white] = "46/floor.tex"

    for weather in (48, 56):
        stage, color, rgb = COLORS[weather]
        local = default["Files"][EFFECT.format(stage)]
        (root, body), = chunks(assets[local])
        fields = dict(chunks(body))
        particle = dict(chunks(fields["Ptcl"]))
        assert root == "AVFX" and fields["DwLy"] == integer(3)
        assert fields["TxCn"] == integer(2) and fields["MdCn"] == integer(1)
        assert particle["PrVT"] == integer(13) and particle["DsDw"] == integer(0)
        assert dict(chunks(dict(chunks(particle["Col"]))["RGB"]))["Keys"] == struct.pack("<hh3f", 0, 1, *rgb)

        # The native rectangular decal uses independent X/Z sizes and Y projection depth.
        # Use a constant, single-key curve for each axis, spanning the 1988-unit floor.
        curve = particle["ARs"]
        assert dict(chunks(curve))["Keys"] == struct.pack("<hh3f", 0, 1, 1, 1, 1)
        scale = particle["Scl"]
        assert dict(chunks(scale)) == {"ACT": integer(0), "ACTR": integer(0)}
        for axis, size in (("X", 2048), ("Y", 2), ("Z", 2048)):
            scale += chunk(axis, rewrite(curve, {"Keys": struct.pack("<hh3f", 0, 1, 1, 1, size)}))
        tex = rewrite(particle["TC1"], {"bEna": integer(1), "TLst": bytes([2])})
        decal = rewrite(fields["Ptcl"], {
            "PrVT": integer(10), "Data": chunk("SS", struct.pack("<f", 1)),
            "DwPr": integer(0), "Scl": scale, "TC1": tex,
        })
        body = rewrite(body, {"Ptcl": decal, "MdCn": integer(0), "TxCn": integer(3)})
        assets[local] = chunk(root, b"".join(chunk(k, v) for k, v in chunks(body) if k != "Modl")
                              + chunk("Tex", white.encode() + bytes(1)))

    assets["default_mod.json"] = encode(default)
    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - Projected Floors v0.12", Version="0.12.0",
                Description="48 绿、56 蓝用贴地投影着色。47 黑、45 红、46 白沿用 v0.11。绿蓝扇面待实测。")
    assets["meta.json"] = encode(meta)
    return assets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    variant = parser.add_mutually_exclusive_group()
    variant.add_argument("--red-depth-probe", action="store_true", help="Build the v0.8 single-weather comparison")
    variant.add_argument("--scene-layer-probe", action="store_true", help="Build the v0.10 five-weather scene-layer test")
    variant.add_argument("--solid-red-probe", type=Path, metavar="MATERIAL_PAIR", help="Build v0.11 with the v0.9 red material")
    variant.add_argument("--floor-decals", action="store_true", help="Build v0.12 using the v0.11 baseline")
    args = parser.parse_args()
    assets = floor_decals(args.baseline) if args.floor_decals else build(args.baseline)
    if args.red_depth_probe:
        red_depth_probe(assets)
    elif args.scene_layer_probe or args.solid_red_probe:
        scene_layer_probe(assets)
        if args.solid_red_probe:
            solid_red_probe(assets, args.solid_red_probe)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for name, data in assets.items():
            out.writestr(name, data)
    with zipfile.ZipFile(buf) as out:
        assert out.testzip() is None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as out:
        out.write(buf.getvalue())
    print(args.output)


if __name__ == "__main__":
    main()
