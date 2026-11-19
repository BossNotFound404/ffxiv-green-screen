from pathlib import Path
import json
import struct
import zipfile

output = Path(__file__).resolve().parents[2] / 'output'
source = output / 'EnuoStudio_StageProbe_v0.3/EnuoStudio_StageProbe_v0.3.pmp'
folder = output / 'EnuoStudio_FiveColor_v0.4'
folder.mkdir(exist_ok=True)
package = folder / 'EnuoStudio_FiveColor_v0.4.pmp'
colors = [('白', 'white', (1, 1, 1)), ('黑', 'black', (0, 0, 0)),
          ('红', 'red', (1, 0, 0)), ('绿', 'green', (0, 1, 0)), ('蓝', 'blue', (0, 0, 1))]


def painted_material(original, rgb):
    data = bytearray(original)
    _, size, dataset, strings, _, tex, uv, channels, extra = struct.unpack_from('<I4H4B', data)
    header = 16 + 4 * (tex + uv + channels) + strings + extra + dataset
    values, keys, constants, samplers = struct.unpack_from('<4H', data, header)
    start = header + 12 + 8 * keys
    values_at = start + 8 * constants + 12 * samplers
    assert values_at + values == size == len(data)
    found = set()
    for key, offset, length in struct.iter_unpack('<IHH', data[start:start + constants * 8]):
        if key in {0x38A64362, 0xAA676D0F}:
            assert length >= 12 and struct.unpack_from('<3f', data, values_at + offset) == (1, 1, 1)
            struct.pack_into('<3f', data, values_at + offset, *rgb)
            found.add(key)
    assert 0x38A64362 in found
    return bytes(data)


def painted_texture(original, rgb):
    # Keep every original mip offset. BC1 selector zero uses endpoint zero even for black.
    white = struct.pack('<HHI', 65535, 0, 0)
    count, rem = divmod(len(original) - 80, 8)
    assert not rem and original[80:] == white * count
    red, green, blue = rgb
    endpoint = (31 * red << 11) | (63 * green << 5) | (31 * blue)
    return original[:80] + struct.pack('<HHI', endpoint, 0, 0) * count


def encode(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8')


with zipfile.ZipFile(source) as old:
    baseline = {path: old.read(path) for path in old.namelist()}
resources = dict(baseline)
default = json.loads(resources['default_mod.json'])
surfaces = dict(default['Files'])
for name in list(resources):
    if name.startswith('group_'):
        group = json.loads(resources.pop(name))
        active = group['Options'][group['DefaultSettings']]
        if name.startswith('group_002'):
            surfaces.update(active['Files'])
        else:
            default['Files'].update(active['Files'])
            default['FileSwaps'].update(active['FileSwaps'])
for path in surfaces:
    default['Files'].pop(path, None)
assert len(surfaces) == 5 and len(default['FileSwaps']) == 9
options = []
for label, suffix, rgb in colors:
    files = {}
    for path, asset in surfaces.items():
        paint = painted_texture if asset.endswith('.tex') else painted_material
        data = paint(baseline[asset], rgb)
        if suffix == 'white':
            assert data == baseline[asset]
            dest = asset
        else:
            dest = f'colors/{suffix}/{asset}'
        resources[dest] = data
        files[path] = dest
    options.append(dict(Name=label, Files=files, FileSwaps={}, Manipulations=[]))
resources['group_001_color.json'] = encode(dict(Version=0, Name='棚色', Type='Single',
    DefaultSettings=0, Priority=1, Options=options, Description='切换后重载 1361，再设天气 172。'))
resources['default_mod.json'] = encode(default)
meta = json.loads(resources['meta.json'])
meta.update(Name='Enuo Studio - Five Colors v0.4', Version='0.4.0',
            Description='恩欧录制棚。使用天气 172，在棚色选项切换白、黑、红、绿、蓝。')
resources['meta.json'] = encode(meta)
with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for name, data in resources.items():
        archive.writestr(name, data)
with zipfile.ZipFile(package) as archive:
    assert archive.testzip() is None
    for option in json.loads(archive.read('group_001_color.json'))['Options']:
        assert set(option['Files']) == set(surfaces)
        for file in [*default['Files'].values(), *option['Files'].values()]:
            assert file in archive.namelist()
print(package.name, package.stat().st_size)
