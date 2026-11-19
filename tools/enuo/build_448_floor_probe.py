"""Build the local 448 floor-only A/B experiment. Reads SqPack and FiveColor v0.4."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct
import zipfile

from audit_448 import load_ex1_indices, weather_rows
from materials_audit import model, probe


def read_stage_bindings():
    """Resolve the object-set reference of each of the two platform instances."""
    load_ex1_indices()
    data = probe.read("bg/ex1/03_abr_a2/fld/a2fb/level/bg.lgb")
    assert data[:4] == b"LGB1" and data[12:16] == b"LGP1"

    def number(at):
        return struct.unpack_from("<I", data, at)[0]

    def string(at):
        return data[at:data.index(0, at)].decode()

    table = 20 + number(28)
    stages = {}
    for i in range(number(32)):
        layer = table + number(table + i * 4)
        name = string(layer + number(layer + 4))
        if name not in ("stg_02", "stg_3456"):
            continue
        assert number(layer + 12) == number(layer + 40) == 1
        objects = layer + number(layer + 8)
        obj = objects + number(objects)
        ref = layer + number(layer + 36)
        assert number(obj) == number(ref) == 1 and number(obj + 4) == number(ref + 4)
        assert struct.unpack_from("<9f", data, obj + 12) == (0, 0, 0, 0, 0, 0, 1, 1, 1)
        stages[name] = (string(obj + number(obj + 48)), string(ref + number(ref + 8)))
    assert set(stages) == {"stg_02", "stg_3456"}
    return stages


def switch_46(path, initial):
    original = probe.read(path)
    records = weather_rows(original, b"OBSB")
    assert all(records[w][0] == 1 and len(records[w][1]) == 24 for w in (45, 46, 47))
    assert [records[w][1][22] for w in (45, 46, 47)] == [1 - initial, initial, initial]
    result = bytearray(original)
    offset, count = struct.unpack_from("<II", original, 24)
    for i in range(count):
        record = 20 + offset + i * 16
        start, channels, weather, end = struct.unpack_from("<4I", original, record)
        if weather == 46:
            assert channels == 1 and end - start == 24
            result[record + start + 22] = 1 - initial
    assert sum(a != b for a, b in zip(result, original)) == 1
    assert weather_rows(result, b"OBSB")[46][1][22] == 1 - initial
    return bytes(result)


def replace_path(data, old, new):
    assert len(old) == len(new) and data.count(old.encode() + b"\0") == 1
    return data.replace(old.encode() + b"\0", new.encode() + b"\0")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("five_color", type=Path, help="EnuoStudio_FiveColor_v0.4.pmp")
    parser.add_argument("output", type=Path, help="New output .pmp path")
    parser.add_argument("--environment", action="store_true", help="Build v0.2 with environment A/B controls")
    parser.add_argument("--stage-lights", action="store_true", help="Build v0.3 with v0.2 controls and matched stage lights")
    lighting = parser.add_mutually_exclusive_group()
    lighting.add_argument("--main-group", action="store_true", help="Build v0.4 with a fixed white floor and an isolated object-set switch")
    lighting.add_argument("--center-light", action="store_true", help="Build v0.5 with black-floor light controls and a black/white switching mode")
    lighting.add_argument("--light-off-floors", action="store_true", help="Build v0.6 with weather-switched floors and the center light off")
    args = parser.parse_args()
    stages = read_stage_bindings()
    source = "bg/ex4/05_zon_z5/fld/z5fd/"
    material = source + "material/z5fd_a3_emv01a.mtrl"
    texture = source + "texture/z5fd_a3_emv01_d.tex"
    floor = model(source + "bgparts/z5fd_a3_stg00.mdl", native=True)
    files, assets = {}, {}

    def bind(game_path, local, data):
        assert game_path not in files and local not in assets
        files[game_path] = local
        assets[local] = data

    with zipfile.ZipFile(args.five_color) as base:
        reference = Path(__file__).resolve().parents[2] / "docs/evidence/packages.json"
        # Match the verified five-color package regardless of ZIP timestamps.
        names = base.namelist()
        hashes = {n: hashlib.sha256(base.read(n)).hexdigest() for n in names}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode())
        expected = json.loads(reference.read_text(encoding="utf-8"))["EnuoStudio_FiveColor_v0.4.pmp"]
        assert len(hashes) == len(names) and digest.hexdigest() == expected["contents_sha256"]
        options = json.loads(base.read("group_001_color.json"))["Options"]
        palette = {option["Name"]: option["Files"] for option in options}
        for stage, weather, color, initial in [("stg_02", "46", "白", 0), ("stg_3456", "47", "黑", 1)]:
            host, group = stages[stage]
            private_mat = material.replace("_a3_", "_k" + weather[-1] + "_")
            private_tex = texture.replace("_a3_", "_k" + weather[-1] + "_")
            assert probe.read(private_mat) is None and probe.read(private_tex) is None
            bind(host, weather + "/floor.mdl", replace_path(floor, material, private_mat))
            bind(private_mat, weather + "/floor.mtrl",
                 replace_path(base.read(palette[color][material]), texture, private_tex))
            bind(private_tex, weather + "/floor.tex", base.read(palette[color][texture]))
            bind(group, weather + "/stage.obsb", switch_46(group, initial))
    meta = dict(FileVersion=3, Name="Round Table Studio - Floor Probe v0.1",
                Author="Local recording workflow", Version="0.1.0",
                Description="448 贴地特效测试。46 白地，47 黑地。原场景装饰仍可能出现。")
    assert len(files) == 8 and len(set(files.values())) == 8
    use_stage_lights = args.stage_lights or args.main_group or args.center_light or args.light_off_floors
    if args.environment or use_stage_lights:
        from environment_448 import environment_controls
        assets.update(environment_controls())
        meta.update(Name="Round Table Studio - Environment Probe v0.2", Version="0.2.0",
                    Description="448 黑白地板。比较统一天气环境与隐藏两项阶段场景特效后的画面。")
    if use_stage_lights:
        from environment_448 import stage_light_controls
        mapping, lights = stage_light_controls()
        for path, local in mapping.items():
            bind(path, local, lights[local])
        meta.update(Name="Round Table Studio - Stage Light Probe v0.3", Version="0.3.0",
                    Description="以 v0.2 为基线，令天气 47 的两组阶段灯开关及参数与 46 相同。")
    if args.main_group:
        from environment_448 import main_group_controls
        assets.update(main_group_controls(stages, files, assets))
        meta.update(Name="Round Table Studio - Main Group Probe v0.4", Version="0.4.0",
                    Description="固定白地，比较天气 47 主场地组开关对人物受光的影响。")
    if args.center_light:
        from environment_448 import center_light_controls
        assets.update(center_light_controls(stages, files, assets))
        meta.update(Name="Round Table Studio - Center Light Probe v0.5", Version="0.5.0",
                    Description="固定黑地，隔离主场地组中心灯。另有 46 白地、47 黑地的填充复查档。")
    if args.light_off_floors:
        from light_off_448 import light_off_floors
        assets.update(light_off_floors(stages, files, assets))
        meta.update(Name="Round Table Studio - Light Off Floors v0.6", Version="0.6.0",
                    Description="中心灯持续关闭：46 白地，47 黑地。仅供本地摄影棚测试，结束后停用。")
    for name, obj in [("meta.json", meta), ("default_mod.json", dict(Version=0, Files=files,
                                                                     FileSwaps={}, Manipulations=[]))]:
        assets[name] = json.dumps(obj, ensure_ascii=False, indent=2).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, content in assets.items():
            archive.writestr(name, content)
    with zipfile.ZipFile(buffer) as archive:
        assert archive.testzip() is None and set(files.values()) <= set(archive.namelist())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as handle:
        handle.write(buffer.getvalue())
    print(f"{args.output.name}: {len(files)} resource mappings")


if __name__ == "__main__":
    main()
