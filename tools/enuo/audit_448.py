"""Print native weather IDs and object-group switches for the a2fb layout."""
import hashlib
import json
import re
import struct

from materials_audit import probe


def weather_rows(data, kind):
    assert data[:4] == kind and data[12:16] == b"ENVS"
    assert struct.unpack_from("<I", data, 4)[0] == len(data)
    offset, count = struct.unpack_from("<2I", data, 24)
    rows = {}
    for i in range(count):
        at = 20 + offset + 16 * i
        start, channels, weather, end = struct.unpack_from("<4I", data, at)
        assert weather not in rows and 0 < start <= end <= len(data) - at
        rows[weather] = (channels, data[at + start:at + end])
    return rows


def load_ex1_indices():
    for index in (probe.ROOT / "ex1").glob("02*.index2"):
        data = index.read_bytes()
        header = struct.unpack_from("<I", data, 12)[0]
        offset, size = struct.unpack_from("<II", data, header + 8)
        for key, value in struct.iter_unpack("<II", data[offset:offset + size]):
            probe.INDEX.setdefault(key, []).append((index, value))


def main():
    load_ex1_indices()
    base = "bg/ex1/03_abr_a2/fld/a2fb/level/"
    hashes = {}

    def read(path):
        data = probe.read(path)
        if data is None:
            raise ValueError(f"Cannot find {path}")
        hashes[path] = hashlib.sha256(data).hexdigest()
        return data

    level = read(base + "a2fb.lvb")
    scene = level.index(b"SCN1")
    settings = scene + 8 + struct.unpack_from("<i", level, scene + 16)[0]
    table = settings + struct.unpack_from("<i", level, settings + 64)[0]
    weather_ids = [w for w in struct.unpack_from("<32H", level, table) if 0 < w < 0x8000]
    envpath, = set(re.findall(rb"bgcommon/[a-z0-9_/]+\.envb", level))
    env_ids = sorted(weather_rows(read(envpath.decode()), b"ENVB"))
    layers = set()
    for name in ("bg", "vfx", "planmap", "sound"):
        layers.update(re.findall(rb"bgcommon/[a-z0-9_/]+\.obsb", read(base + name + ".lgb")))
    active = {w: [] for w in weather_ids}
    for path in sorted(p.decode() for p in layers):
        rows = weather_rows(read(path), b"OBSB")
        for w in weather_ids:
            channels, payload = rows[w]
            assert channels == 1 and len(payload) == 24
            assert struct.unpack_from("<4I", payload) == (12, 1, 29, 4)
            assert payload[22] in (0, 1) and payload[23] == 0
            if payload[22]:
                active[w].append(path.rsplit("obset_a2fb_", 1)[1].split(".")[0])
    print(json.dumps(dict(layout=base, weather_ids=weather_ids, environment_ids=env_ids,
                          active_groups=active, resource_sha256=hashes), indent=2))


if __name__ == "__main__":
    main()
