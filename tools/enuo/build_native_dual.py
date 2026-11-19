"""Make an independent native-weather A/B package from the verified studio assets."""
import json
from pathlib import Path
import struct
import zipfile

from materials_audit import model, probe

out = Path(__file__).resolve().parents[2] / 'output'
base = 'bg/ex4/05_zon_z5/fld/z5fd/'
parts = base + 'bgparts/'
envpath = 'bgcommon/env/global/ex4_genv/genv_zon_z5/genv_z5_fld/genv_z5fd.envb'
files = {}
assets = {}

def bind(path, local, data):
    assert path not in files
    assert local not in assets or assets[local] == data
    assets[local] = data
    files[path] = local

def private(path):
    assert '_a3_emv' in path
    return path.replace('_a3_emv', '_bw_emv')

with zipfile.ZipFile(out / 'EnuoStudio_FiveColor_v0.4/EnuoStudio_FiveColor_v0.4.pmp') as old:
    default = json.loads(old.read('default_mod.json'))
    for path, local in default['Files'].items():
        bind(path, local, old.read(local))
    options = json.loads(old.read('group_001_color.json'))['Options']
    white, black = (option['Files'] for option in options[:2])
    assert set(white) == set(black) and len(white) == 5
    assert options[0]['Name'] == '白' and options[1]['Name'] == '黑'
    for path, local in black.items():
        bind(path, local, old.read(local))
    for path, local in white.items():
        data = old.read(local)
        if path.endswith('.mtrl'):
            textures = [tex for tex in white if tex.endswith('.tex') and tex.encode() in data]
            assert len(textures) == 1
            data = data.replace(textures[0].encode(), private(textures[0]).encode())
        bind(private(path), 'white/' + local, data)

with zipfile.ZipFile(out / 'EnuoStudio_171Probe_v0.2/EnuoStudio_171Probe_v0.2.pmp') as old:
    bind(envpath, 'environment/same.envb', old.read('environment/171.envb'))

# Native 171 activates a01; native 172 activates a03. Substitute two a01
# models at the exact a03 origin and neutralize the other a01 visible meshes.
objects = json.loads((probe.OUT / 'layout_objects.json').read_text(encoding='utf-8'))
group = [obj for obj in objects if obj['file'] == 'bg' and obj['layer'] == 'z5fd_bg_a01']
assert len(group) == 19 and all(obj['type'] == 1 for obj in group)
templates = {'z5fd_a1_stg00.mdl': 'z5fd_a3_stg00.mdl', 'z5fd_a1_stg01.mdl': 'z5fd_u3_emv01.mdl'}
floor = model(parts + templates['z5fd_a1_stg00.mdl'], native=True)
idx = struct.unpack_from('<I', floor, 28)[0]
length = struct.unpack_from('<I', floor, 52)[0]
assert idx + length == len(floor) and length == 2496 * 2
hidden = floor[:idx] + bytes(length)
for path in sorted({obj['model'] for obj in group}):
    name = path.rsplit('/', 1)[1]
    if name not in templates:
        bind(path, 'models/no_triangles.mdl', hidden)
        continue
    placed = [obj for obj in group if obj['model'] == path]
    assert len(placed) == 1 and placed[0]['position'] == [100,0,100]
    assert placed[0]['rotation'] == [0,0,0] and placed[0]['scale'] == [1,1,1]
    data = model(parts + templates[name], native=True)
    names = [p for p in white if p.endswith('.mtrl') and p.encode() in data]
    assert len(names) == (1 if name == 'z5fd_a1_stg00.mdl' else 2)
    for p in names:
        assert private(p) in files
        data = data.replace(p.encode(), private(p).encode())
    bind(path, 'models/white_' + name, data)

meta = dict(FileVersion=3, Name='Enuo Studio - Native Black and White v0.1', Version='0.1.0',
            Author='Local recording workflow', Description='独立测试包。天气 171 白棚、172 黑棚。')
default['Files'] = files
for name, obj in [('meta.json', meta), ('default_mod.json', default)]:
    assets[name] = json.dumps(obj, ensure_ascii=False, indent=2).encode('utf8')
folder = out / 'EnuoStudio_NativeBW_v0.1'
folder.mkdir(exist_ok=True)
package = folder / 'EnuoStudio_NativeBW_v0.1.pmp'
with zipfile.ZipFile(package, 'x', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for name, data in assets.items():
        z.writestr(name, data)
with zipfile.ZipFile(package) as z:
    assert z.testzip() is None
    assert set(files.values()) <= set(z.namelist())
    assert not any(p.endswith(('.lgb','.lvb','.pcb','.obsb')) for p in files)
print(package.name, package.stat().st_size, len(files), 'resource mappings')
