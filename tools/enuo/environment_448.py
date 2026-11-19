"""Build the optional environment controls for the 448 floor probe."""
import json
import struct
import hashlib

from audit_448 import probe, weather_rows


def match_47_to_46(original, kind):
    rows = weather_rows(original, kind)
    channels, reference = rows[46]
    count_47, current = rows[47]
    assert channels == count_47 and len(reference) == len(current)
    assert reference[:12 * channels] == current[:12 * channels] and current != reference
    offset, count = struct.unpack_from("<II", original, 24)
    changed = bytearray(original)
    for i in range(count):
        entry = 20 + offset + 16 * i
        start, _, weather, end = struct.unpack_from("<4I", original, entry)
        if weather == 47:
            changed[entry + start:entry + end] = reference
    assert len(changed) == len(original)
    assert weather_rows(changed, kind) == (rows | {47: rows[46]})
    return bytes(changed)


def stage_light_controls():
    # light01 in bg.lgb binds one light to each of these object sets.
    prefix = "bgcommon/env/obset/ex1_obset/obset_abr_a2/obset_a2_fld/obset_a2fb_"
    assets = {}
    mapping = {}
    for group, expected in [("vfx02", (1, 0)), ("vfx03", (0, 1))]:
        path = prefix + group + ".obsb"
        original = probe.read(path)
        rows = weather_rows(original, b"OBSB")
        assert all(rows[w][0] == 1 and len(rows[w][1]) == 24 for w in (46, 47))
        assert tuple(rows[w][1][22] for w in (46, 47)) == expected
        local = "lights/" + group + ".obsb"
        assets[local] = match_47_to_46(original, b"OBSB")
        mapping[path] = local
    return mapping, assets


def environment_controls():
    env_path = "bgcommon/env/global/ex1_genv/genv_abr_a2/genv_a2_fld/genv_a2fb.envb"
    changed = match_47_to_46(probe.read(env_path), b"ENVB")

    layout = probe.read("bg/ex1/03_abr_a2/fld/a2fb/level/vfx.lgb")
    swaps = {}
    for stage in (2, 3):
        effect = f"bg/ex1/03_abr_a2/common/vfx/eff/b0673_{stage}lnd_y.avfx"
        assert effect.encode() + b"\0" in layout and probe.read(effect).startswith(b"XFVA")
        swaps[effect] = "vfx/path/nothing.avfx"
    env = {env_path: "environment/a2fb.envb"}
    options = [
        dict(Name=name, Files=files, FileSwaps=effects, Manipulations=[])
        for name, files, effects in [
            ("原始环境（地板对照）", {}, {}),
            ("仅统一天气环境", env, {}),
            ("仅隐藏两项场景特效", {}, swaps),
            ("统一环境并隐藏两项场景特效", env, swaps),
        ]
    ]
    group = dict(Version=0, Type="Single", Name="环境对照", Priority=1,
                 DefaultSettings=3, Options=options, Description="进棚前选择，改选项后重新加载场景。")
    return {"environment/a2fb.envb": bytes(changed),
            "group_001_environment.json": json.dumps(group, ensure_ascii=False, indent=2).encode()}


def main_group_controls(stages, files, assets):
    _, white_group = stages["stg_02"]
    black_model, main_group = stages["stg_3456"]
    white_table = files[white_group]
    # In v0.3 the white host is enabled only in 46. Keep it visible in both.
    fixed_white = match_47_to_46(assets[white_table], b"OBSB")
    on_path = files.pop(main_group)
    off_path = "lights/main_off.obsb"
    off = match_47_to_46(assets[on_path], b"OBSB")
    for table, expected in [(fixed_white, (1, 1)), (assets[on_path], (0, 1)), (off, (0, 0))]:
        rows = weather_rows(table, b"OBSB")
        assert tuple(rows[w][1][22] for w in (46, 47)) == expected

    # Both options use the same white geometry. The black host must draw no
    # triangles when the main group activates at the same coordinates.
    mdl_path = files[black_model]
    mdl = assets[mdl_path]
    options = [dict(Name=name, Files={main_group: path}, FileSwaps={}, Manipulations=[])
               for name, path in [("A · 47 开启主场地组", on_path), ("B · 47 关闭主场地组", off_path)]]
    group = dict(Version=0, Type="Single", Name="主场地组", Priority=2, DefaultSettings=0,
                 Options=options, Description="两个选项均使用相同白地。改选项后重载场景。")
    return {white_table: fixed_white, mdl_path: no_floor_triangles(mdl), off_path: off,
            "group_002_main.json": json.dumps(group, ensure_ascii=False, indent=2).encode()}


def no_floor_triangles(mdl):
    start = struct.unpack_from("<I", mdl, 28)[0]
    size = struct.unpack_from("<I", mdl, 52)[0]
    assert mdl[64] == 1 and start + size == len(mdl) and size == 2496 * 2
    return mdl[:start] + bytes(size)


def center_light_controls(stages, files, assets):
    # The audited layout binds ultimate to the floor host, eight instances of
    # one shaft model, and light 5909895. Recheck before using that partition.
    layout = probe.read("bg/ex1/03_abr_a2/fld/a2fb/level/bg.lgb")
    assert hashlib.sha256(layout).hexdigest() == "b9077b2bc96f8b389392681ce8fa7f6231ca8fa5585e695792ddfbd6d6d3658c"
    shaft = "bg/ex1/03_abr_a2/fld/a2fb/bgparts/a2fb_l1_shft1b.mdl"
    assert shaft.encode() + b"\0" in layout and shaft not in files
    white, white_group = stages["stg_02"]
    black, main_group = stages["stg_3456"]
    by_weather = {key: files.pop(key) for key in (white, black, white_group, main_group)}
    hidden = "lights/no_triangles.mdl"
    always_white_host = "lights/fixed_host.obsb"
    main_off = "lights/main_off.obsb"
    files[shaft] = hidden
    white_table = assets[by_weather[white_group]]
    main_table = assets[by_weather[main_group]]
    assert [weather_rows(white_table, b"OBSB")[w][1][22] for w in (46, 47)] == [1, 0]
    assert [weather_rows(main_table, b"OBSB")[w][1][22] for w in (46, 47)] == [0, 1]
    # Both fixed-black options draw the original black mesh via the same host.
    # The switched host and the eight shafts have no drawable triangles.
    fixed_black = {white: by_weather[black], black: hidden, white_group: always_white_host}
    options = [
        dict(Name=name, Files=mapping, FileSwaps={}, Manipulations=[])
        for name, mapping in [
            ("A · 固定黑地，47 开灯", fixed_black | {main_group: by_weather[main_group]}),
            ("B · 固定黑地，47 关灯", fixed_black | {main_group: main_off}),
            ("C · 46 白地，47 黑地", by_weather),
        ]
    ]
    off_table = match_47_to_46(main_table, b"OBSB")
    assert sum(a != b for a, b in zip(off_table, main_table)) == 1
    group = dict(Version=0, Type="Single", Name="中心灯对照", Priority=2, DefaultSettings=0,
                 Options=options, Description="A、B 用同一黑地对照中心灯，C 复查原地换色和特效填充。改档后重载场景。")
    return {hidden: no_floor_triangles(assets[by_weather[black]]),
            always_white_host: match_47_to_46(white_table, b"OBSB"), main_off: off_table,
            "group_002_light.json": json.dumps(group, ensure_ascii=False, indent=2).encode()}
