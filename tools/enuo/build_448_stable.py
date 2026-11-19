"""Build the white floor and backdrop together and disable the white phase lamps."""
import argparse
import json
import struct
import zipfile
from pathlib import Path

from build_448_material_pair import encode, read_package
from stage_lamp_track import add_neutral_lamp_track
from white_cyclorama import add_white_cyclorama


MATCHED_CONTENTS = "712db708f54b37d048921fecee8c67b2df009bcbad7b1adf2975b111b01d5471"
QUIET_CONTENTS = "bf12e9995e430195170c00e0c15c7a11d910e5d0e7eec1f7e9a0704827512068"
OBJECT_GROUP = "bgcommon/env/obset/ex1_obset/obset_abr_a2/obset_a2_fld/obset_a2fb_"
WHITE_SKY = "bg/ex1/03_abr_a2/common/vfx/eff/b0673_1lnd_y.avfx"
WHITE_FLOOR = "bg/ex1/03_abr_a2/fld/a2fb/bgparts/a2fb_a1_boss2.mdl"


def disable_white_phase(table):
    assert table[:4] == b"OBSB" and table[12:16] == b"ENVS"
    start, count = struct.unpack_from("<2I", table, 24)
    for i in range(count):
        row = 20 + start + 16 * i
        offset, channels, weather, end = struct.unpack_from("<4I", table, row)
        if weather == 46:
            key = row + offset
            assert channels == 1 and end - offset == 24
            assert struct.unpack_from("<4I", table, key) == (12, 1, 29, 4)
            assert table[key + 22] == 1
            return table[:key + 22] + b"\0" + table[key + 23:]
    raise ValueError("Missing white weather in the stage light group")


def build(baseline, donor):
    assets = read_package(baseline, MATCHED_CONTENTS)
    quiet = read_package(donor, QUIET_CONTENTS)
    default = json.loads(assets["default_mod.json"])
    files = default["Files"]
    stage = files[OBJECT_GROUP + "ultimate2.obsb"]
    lamps = files[OBJECT_GROUP + "vfx01.obsb"]
    floor = files[WHITE_FLOOR]
    assets[stage] = quiet[stage]  # Restore the v0.14 weather-46 physical floor.
    assets[floor] = add_white_cyclorama(assets[floor])
    assets[lamps] = disable_white_phase(assets[lamps])
    # vfx01 also controls both blue lamps. The model now supplies the entire
    # white set; silence the old emitter if a controller activates it directly.
    del assets[files[WHITE_SKY]]
    files[WHITE_SKY] = "quiet/inactive.avfx"
    assets["default_mod.json"] = encode(default)

    light_group = json.loads(assets["group_001_light.json"])
    neutral = light_group["Options"][light_group["DefaultSettings"]]
    trial = {**neutral, "Name": "四色中性顶灯（试验）", "Files": dict(neutral["Files"])}
    for phase, weather in ((2, 47), (3, 45), (4, 48), (5, 56)):
        group = OBJECT_GROUP + f"vfx{phase:02}.obsb"
        local = f"lights/neutral_vfx{phase:02}.obsb"
        assets[local] = add_neutral_lamp_track(assets[files[group]], weather)
        trial["Files"][group] = local
    light_group["Options"].append(trial)
    light_group["Description"] = "进棚前选择灯光。试验档调整黑、红、绿、蓝棚的顶灯颜色，白棚在三个选项中共用同一套灯光开关。"
    assets["group_001_light.json"] = encode(light_group)

    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - Stable Stage v0.16", Version="0.16.0",
                Description="白棚用一个实体模型显示地板与天幕，关闭该棚原有的蓝色地灯和顶灯。其余四色沿用 v0.15 布景，保留副本转场清理。")
    assets["meta.json"] = encode(meta)
    return assets


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("baseline", "donor", "output"):
        parser.add_argument(arg, type=Path)
    args = parser.parse_args()
    assets = build(args.baseline, args.donor)
    with zipfile.ZipFile(args.output, "x") as archive:
        for name, data in sorted(assets.items()):
            archive.writestr(zipfile.ZipInfo(name), data, zipfile.ZIP_DEFLATED, compresslevel=9)
