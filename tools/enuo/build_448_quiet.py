"""Preserve the v0.13 studio while removing identifiable encounter cues."""
import argparse
import hashlib
import json
import struct
import zipfile
from pathlib import Path

from audit_448 import load_ex1_indices, probe
from build_448_material_pair import encode, read_package
from build_448_rgb_probe import chunk, chunks, integer, rewrite

BASELINE = "7b5db238c50afdf08eec7f269bbbf1cb279bec572737fea5315afaa575751879"
ZONE = "bg/ex1/03_abr_a2/"
LEVEL = ZONE + "fld/a2fb/level/"
EFFECT = ZONE + "common/vfx/eff/"
SOUND_ENV = "bgcommon/env/sound/abr_a2/a2_fld/sound_a2fb.essb"
CHANGE = "bgcommon/sound/abr/abr_spot_a2fb_change_start.scd"
RING = "bgcommon/sound/fst/placednpc_a2fb_CircleDamageFloor_on.scd"


def quiet_weather(data):
    # The first of five references is the ambient loop. Slots 1-4 already
    # point to a native empty string, relative to the start of the array.
    assert data[:4] == b"ESSB" and data[12:16] == b"ENVS"
    result = bytearray(data)
    offset, count = struct.unpack_from("<2I", data, 24)
    changed = set()
    for i in range(count):
        entry = 20 + offset + i * 16
        start, channels, weather, end = struct.unpack_from("<4I", data, entry)
        if weather not in (45, 46, 47, 48, 56):
            continue
        key = entry + start
        assert channels == 1 and end - start == 48
        assert struct.unpack_from("<7I", data, key) == (12, 1, 20, 4, 0, 12, 5)
        slots = key + 28
        first, *empty = struct.unpack_from("<5I", data, slots)
        assert data[slots + first:].startswith(b"bgcommon/sound/") and len(set(empty)) == 1
        assert data[slots + empty[0]] == 0
        struct.pack_into("<I", result, slots, empty[0])
        changed.add(weather)
    assert changed == {45, 46, 47, 48, 56}
    return bytes(result)


def muted_sound(data, digest):
    assert hashlib.sha256(data).hexdigest() == digest and data[:8] == b"SEDBSSCF"
    header, = struct.unpack_from("<H", data, 14)
    assert header == 48 and struct.unpack_from("<H", data, header)[0] == 1
    sound, = struct.unpack_from("<I", data, header + 32)
    volume = sound + 8  # Lumina SoundBasicDesc.Volume
    assert 0 < struct.unpack_from("<f", data, volume)[0] <= 1
    return data[:volume] + bytes(4) + data[volume + 4:]


def inactive_effect(template):
    (root, body), = chunks(template)
    fields = dict(chunks(body))
    assert root == "AVFX" and fields["TlCn"] == fields["EmCn"] == integer(1)
    timeline = fields["TmLn"]
    item = dict(chunks(timeline))["Item"]
    assert dict(chunks(timeline))["TICn"] == integer(1)
    assert dict(chunks(item))["bEna"] == integer(1) and fields["EfCn"] == integer(0)
    timeline = rewrite(timeline, {"Item": rewrite(item, {"bEna": integer(0)})})
    return chunk(root, rewrite(body, {"TmLn": timeline}))


def build(baseline):
    assets = read_package(baseline, BASELINE)
    load_ex1_indices()
    scene = probe.read(LEVEL + "vfx.lgb")
    sound_layout = probe.read(LEVEL + "sound.lgb")
    assert hashlib.sha256(sound_layout).hexdigest() == "7297fc799f808778d01356c865939cd6dcbedbef296bec7485f1ead852fb57a2"
    layer = 36 + struct.unpack_from("<I", sound_layout, 36)[0]
    relative, count = struct.unpack_from("<2I", sound_layout, layer + 8)
    table = layer + relative
    instance = table + struct.unpack_from("<I", sound_layout, table)[0]
    assert count == 1 and struct.unpack_from("<2I", sound_layout, instance) == (7, 5941210)
    edge = probe.read(ZONE + "shared/for_vfx/sgvf_a2fb_b0798.sgb")
    sound_env = probe.read(SOUND_ENV)
    default = json.loads(assets["default_mod.json"])
    files = default["Files"]
    for source, path in [(scene, EFFECT + "b0673_1cng_y.avfx"), (edge, EFFECT + "b0798_edge_y.avfx")]:
        assert path.encode() + b"\0" in source and path not in files
        files[path] = "quiet/inactive.avfx"
    assets["quiet/inactive.avfx"] = inactive_effect(assets["sky/white.avfx"])
    assert SOUND_ENV.encode() in probe.read(LEVEL + "a2fb.lvb")
    files[SOUND_ENV] = "quiet/studio.essb"
    assets[files[SOUND_ENV]] = quiet_weather(sound_env)
    for source, path, local, digest in [
        (sound_env, CHANGE, "change", "b69720b1a25071ca67bd97a52d664a8237af9c4096bfe871d2e2e49c163b4643"),
        (edge, RING, "ring", "d524ebc3202984f026788157341fb74518764ce31c5e1bf71c0316fe4fcb7bd3"),
    ]:
        assert path.encode() + b"\0" in source
        files[path.lower()] = f"quiet/{local}.scd"
        assets[files[path.lower()]] = muted_sound(probe.read(path), digest)
    assets["default_mod.json"] = encode(default)
    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - Quiet Stage v0.14", Version="0.14.0",
                Description="448 五色录制棚。关闭场地转场、边缘效果及对应音效。")
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
