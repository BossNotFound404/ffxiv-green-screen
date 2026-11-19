"""Add a local weather binding for 169, preserving native 1361 records."""
from pathlib import Path
import argparse
import json
import struct
import zipfile
from materials_audit import probe

def read_table(data):
    assert data[:4] in (b'ENVB', b'OBSB') and data[12:16] == b'ENVS'
    assert struct.unpack_from('<I', data, 4)[0] == len(data)
    offset, count, tail = struct.unpack_from('<3I', data, 24)
    table = 20 + offset
    cursor = table + count * 16
    rows = {}
    for i in range(count):
        pos = table + 16 * i
        start, channels, weather, end = struct.unpack_from('<4I', data, pos)
        assert pos + start == cursor and start + 12 * channels <= end
        trailer = pos + end
        assert weather not in rows
        assert struct.unpack_from('<2I', data, trailer + 12) == (20 + tail - 1 - trailer,) * 2
        rows[weather] = (channels, data[cursor:trailer], data[trailer:trailer + 12])
        cursor = trailer + 20
    assert cursor == 20 + tail - 1 and data[cursor:] == bytes(13)
    return data[:table], rows, data[cursor:]

def write_table(header, rows, suffix):
    table = len(header)
    result = bytearray(header) + bytearray(16 * len(rows))
    tail = len(result) + sum(len(payload) + 20 for _, payload, _ in rows.values())
    for i, (weather, (channels, payload, trailer)) in enumerate(sorted(rows.items())):
        pos = table + 16 * i
        start = len(result) - pos
        struct.pack_into('<4I', result, pos, start, channels, weather, start + len(payload))
        result.extend(payload)
        distance = tail - len(result)
        result.extend(trailer + struct.pack('<2I', distance, distance))
    assert len(result) == tail
    result.extend(suffix)
    struct.pack_into('<I', result, 4, len(result))
    struct.pack_into('<2I', result, 28, len(rows), tail + 1 - 20)
    return bytes(result)

level = 'bg/ex4/05_zon_z5/fld/z5fd/level/z5fd.lvb'
parser = argparse.ArgumentParser()
parser.add_argument('--control', action='store_true', help='Omit LVB and bind native 171 as a positive control.')
parser.add_argument('--layer-test', action='store_true', help='Switch only the 169 object-set flags to the small stage.')
args = parser.parse_args()
control = args.control or args.layer_test
env = 'bgcommon/env/global/ex4_genv/genv_zon_z5/genv_z5_fld/genv_z5fd.envb'
obset = 'bgcommon/env/obset/ex4_obset/obset_zon_z5/obset_z5_fld/obset_z5fd/obset_z5fd_bg_'
targets = {}
for path in [env, *(obset + layer + '.obsb' for layer in ('a01', 'a02', 'a03'))]:
    original = probe.read(path)
    header, rows, suffix = read_table(original)
    assert set(rows) == ({171, 172, 173} if path == env else {2, 3, 4, 5, 171, 172, 173})
    assert write_table(header, rows, suffix) == original
    expected = rows | {169: rows[172]}
    if args.layer_test and path != env:
        expected[169] = rows[173]
    if control:
        expected[171] = rows[172]
    targets[path] = write_table(header, expected, suffix)
    _, parsed, _ = read_table(targets[path])
    assert parsed == expected

lvb = probe.read(level)
scene = lvb.index(b'SCN1')
settings = scene + 8 + struct.unpack_from('<i', lvb, scene + 16)[0]
weather_table = settings + struct.unpack_from('<i', lvb, settings + 64)[0]
assert struct.unpack_from('<32H', lvb, weather_table) == (32770, 171, 172, 173) + (0,) * 28
changed = bytearray(lvb)
struct.pack_into('<H', changed, weather_table + 8, 169)
assert sum(x != y for x, y in zip(lvb, changed)) == 1
if not control:
    targets[level] = bytes(changed)

version = '0.2' if control else '0.1'
if args.layer_test:
    version = '0.3'
output = Path(__file__).resolve().parents[2] / 'output' / f'EnuoStudio_169Probe_v{version}'
output.mkdir(parents=True, exist_ok=True)
package = output / f'EnuoStudio_169Probe_v{version}.pmp'
mapping = {path: 'resources/' + path.rsplit('/', 1)[1] for path in targets}
meta = dict(FileVersion=3, Name='Enuo Studio - 169 Binding Probe', Author='Local recording workflow',
            Version='0.1.0', Description='配合五色棚 v0.4。在恩欧为 169 加入与 172 相同的本地环境与场地组。测试期间停用 171 附加包。')
default = dict(Version=0, Files=mapping, FileSwaps={}, Manipulations=[])
if control:
    assert len(mapping) == 4 and level not in mapping
    meta.update(Name='Enuo Studio - 169 Native Control v0.2', Version='0.2.0',
                Description='与五色棚 v0.4 搭配。171 与 169 指向相同的 172 棚景，173 保持原样。')
if args.layer_test:
    meta.update(Name='Enuo Studio - 169 Layer Test v0.3', Version='0.3.0',
                Description='与五色棚 v0.4 搭配。对照 v0.2，仅反转 169 的大地面与小平台组标记。')
    prior = output.parent / 'EnuoStudio_169Probe_v0.2/EnuoStudio_169Probe_v0.2.pmp'
    with zipfile.ZipFile(prior) as baseline:
        for path, asset in mapping.items():
            old, new = baseline.read(asset), targets[path]
            assert len(old) == len(new)
            diff = [i for i, (a, b) in enumerate(zip(old, new)) if a != b]
            if path.endswith(('a02.obsb', 'a03.obsb')):
                assert len(diff) == 1
                before, after = old[diff[0]], new[diff[0]]
                assert (before, after) == ((0, 1) if path.endswith('a02.obsb') else (1, 0))
            else:
                assert not diff
with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as z:
    for path, data in targets.items():
        z.writestr(mapping[path], data)
    for name, document in [('meta.json', meta), ('default_mod.json', default)]:
        z.writestr(name, json.dumps(document, ensure_ascii=False, indent=2).encode('utf8'))
with zipfile.ZipFile(package) as z:
    assert z.testzip() is None
    for path, asset in json.loads(z.read('default_mod.json'))['Files'].items():
        assert z.read(asset) == targets[path]
print(package.name, package.stat().st_size)
