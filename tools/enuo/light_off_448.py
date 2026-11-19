"""Build the stacked black/white floor experiment for the audited 448 layout."""
import hashlib
import struct

from audit_448 import probe, weather_rows
from environment_448 import match_47_to_46, no_floor_triangles


def default_stage():
    layout = probe.read("bg/ex1/03_abr_a2/fld/a2fb/level/bg.lgb")
    assert hashlib.sha256(layout).hexdigest() == "b9077b2bc96f8b389392681ce8fa7f6231ca8fa5585e695792ddfbd6d6d3658c"

    def number(offset):
        return struct.unpack_from("<I", layout, offset)[0]

    def string(offset):
        return layout[offset:layout.index(0, offset)].decode()

    layers = 20 + number(28)
    for i in range(number(32)):
        layer = layers + number(layers + 4 * i)
        if string(layer + number(layer + 4)) != "stg_01":
            continue
        assert number(layer + 12) == number(layer + 40) == 264
        refs = layer + number(layer + 36)
        objects = layer + number(layer + 8)
        paths = set()
        groups = {}
        hosts = []
        for j in range(264):
            ref = refs + 12 * j
            assert number(ref) == 1
            groups[number(ref + 4)] = string(ref + number(ref + 8))
        for j in range(264):
            obj = objects + number(objects + 4 * j)
            assert number(obj) == 1 and number(obj + 4) in groups
            path = string(obj + number(obj + 48))
            if path.endswith("/a2fb_t1_stg1a.mdl"):
                assert struct.unpack_from("<9f", layout, obj + 12) == (0, 0, 0, 0, 0, 0, 1, 1, 1)
                hosts.append(path)
            paths.add(path)
        host, = hosts
        group, = set(groups.values())
        assert group.endswith("_default.obsb") and len(paths) == 23
        return host, group, paths - {host}
    raise ValueError("Missing stg_01 layer")


def enable_default_floor(original):
    rows = weather_rows(original, b"OBSB")
    active = rows[44][1]
    result = bytearray(original)
    table, count = struct.unpack_from("<II", original, 24)
    for i in range(count):
        entry = 20 + table + 16 * i
        start, _, weather, _ = struct.unpack_from("<4I", original, entry)
        if weather in (46, 47):
            row = rows[weather][1]
            assert len(row) == 24 and row[:22] + b"\x01" + row[23:] == active and row[22] == 0
            result[entry + start + 22] = 1
    assert weather_rows(result, b"OBSB") == rows | {46: rows[44], 47: rows[44]}
    return bytes(result)


def lower_black_floor(floor):
    # The verified single mesh has 449 half4 positions in stream 0, stride 8.
    # Only translate Y and the two populated bounding boxes.
    assert hashlib.sha256(floor).hexdigest() == "abc10f3e6ff52f6d68f93de080fda1144b7d345fd9e76aaf6fc616fb152b0be7"
    vertex_at = struct.unpack_from("<I", floor, 16)[0]
    result = bytearray(floor)
    drop = 1 / 16
    for i in range(449):
        y_at = vertex_at + 8 * i + 2
        y, = struct.unpack_from("<e", floor, y_at)
        struct.pack_into("<e", result, y_at, y - drop)
    for relative in (-124, -108, -92, -76):
        y_at = vertex_at + relative
        y, = struct.unpack_from("<f", floor, y_at)
        struct.pack_into("<f", result, y_at, y - drop)
    return bytes(result)


def light_off_floors(stages, files, assets):
    base_host, base_group, decor = default_stage()
    black_host, main_group = stages["stg_3456"]
    black_local = files[black_host]
    hidden = "studio/no_triangles.mdl"
    # The two sets of shafts in ultimate2 also disappear when white switches off.
    prefix = base_host.rsplit("/", 1)[0]
    decor.update(f"{prefix}/a2fb_l1_shft1{suffix}.mdl" for suffix in ("c", "d"))
    assert not (decor | {base_host, base_group}) & files.keys()
    files.update(dict.fromkeys(sorted(decor), hidden))
    files[base_host] = "studio/lower_black.mdl"
    files[base_group] = "studio/base.obsb"
    # Keep the original black host asset for other weather that enables it.
    main_local = files[main_group]
    assets[main_local] = match_47_to_46(assets[main_local], b"OBSB")
    return {hidden: no_floor_triangles(assets[black_local]),
            files[base_host]: lower_black_floor(assets[black_local]),
            files[base_group]: enable_default_floor(probe.read(base_group))}
