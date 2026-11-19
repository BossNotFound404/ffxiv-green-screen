"""Read the installed ENUO object-set tables; build a reversible v0.4 add-on."""
import json
import struct
import zipfile
from pathlib import Path
from materials_audit import probe

root = Path(__file__).resolve().parents[2]
folder = root / 'output' / 'EnuoStudio_171Probe_v0.1'
folder.mkdir(parents=True, exist_ok=True)
archive = folder / 'EnuoStudio_171Probe_v0.1.pmp'
directory = 'bgcommon/env/obset/ex4_obset/obset_zon_z5/obset_z5_fld/obset_z5fd/'
bg = probe.read('bg/ex4/05_zon_z5/fld/z5fd/level/bg.lgb')
mappings = {}
assets = {}
originals = {}
for layer, expected in [('a01', (1, 0, 0)), ('a02', (0, 0, 1)), ('a03', (0, 1, 0))]:
    filename = 'obset_z5fd_bg_' + layer + '.obsb'
    path = directory + filename
    assert path.encode() + b'\0' in bg
    original = probe.read(path)
    assert original[:4] == b'OBSB' and original[12:16] == b'ENVS'
    assert struct.unpack_from('<I', original, 4)[0] == len(original)
    table_offset, count = struct.unpack_from('<II', original, 24)
    fields = {}
    for index in range(count):
        record = 20 + table_offset + index * 16
        offset, entries, weather, end = struct.unpack_from('<4I', original, record)
        start = record + offset
        assert entries == 1 and end - offset == 24
        assert struct.unpack_from('<4I', original, start) == (12, 1, 29, 4)
        assert original[start + 20:start + 22] == b'\x32\0'
        fields[weather] = start + 22
    assert tuple(original[fields[w]] for w in (171, 172, 173)) == expected
    if expected[0] != expected[1]:
        changed = bytearray(original)
        changed[fields[171]] = original[fields[172]]
        assert [i for i in range(len(original)) if changed[i] != original[i]] == [fields[171]]
        mappings[path] = filename
        assets[filename] = bytes(changed)
        originals[filename] = original

def document(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf8')

assert len(mappings) == 2
assets['meta.json'] = document(dict(FileVersion=3, Name='Enuo Studio - 171 Transfer Probe',
    Author='Local recording workflow', Version='0.1.0',
    Description='配合五色棚 v0.4，测试天气 171 复用 172 的场地图层。'))
assets['default_mod.json'] = document(dict(Version=0, Files=mappings, FileSwaps={}, Manipulations=[]))
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as package:
    for name, data in assets.items():
        package.writestr(name, data)
with zipfile.ZipFile(archive) as package:
    assert package.testzip() is None
    target_files = json.loads(package.read('default_mod.json'))['Files']
    assert target_files == mappings
    for name in target_files.values():
        actual = package.read(name)
        assert len(actual) == len(originals[name])
        assert sum(a != b for a, b in zip(actual, originals[name])) == 1
with zipfile.ZipFile(root / 'output/EnuoStudio_FiveColor_v0.4/EnuoStudio_FiveColor_v0.4.pmp') as base:
    for name in base.namelist():
        if name.endswith('.json'):
            data = json.loads(base.read(name))
            for container in [data, *data.get('Options', [])]:
                assert mappings.keys().isdisjoint(container.get('Files', {}))
                assert mappings.keys().isdisjoint(container.get('FileSwaps', {}))
print(archive.name, archive.stat().st_size, 'bytes; two verified one-byte edits')
