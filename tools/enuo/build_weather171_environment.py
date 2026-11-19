"""Extend the object-set probe with an A/B switch for the Enuo environment."""
from pathlib import Path
import json
import struct
import zipfile
from materials_audit import probe

out = Path(__file__).resolve().parents[2] / 'output'
prior = out / 'EnuoStudio_171Probe_v0.1/EnuoStudio_171Probe_v0.1.pmp'
folder = out / 'EnuoStudio_171Probe_v0.2'
folder.mkdir(exist_ok=True)
package = folder / 'EnuoStudio_171Probe_v0.2.pmp'
env = 'bgcommon/env/global/ex4_genv/genv_zon_z5/genv_z5_fld/genv_z5fd.envb'
raw = probe.read(env)
assert raw[:4] == b'ENVB' and raw[12:16] == b'ENVS'
assert struct.unpack_from('<I', raw, 4)[0] == len(raw)
offset, count = struct.unpack_from('<II', raw, 24)
records = {}
for i in range(count):
    pos = 20 + offset + 16 * i
    start, entries, weather, end = struct.unpack_from('<4I', raw, pos)
    records[weather] = (pos + start, pos + end, entries)
a, b, n = records[171]
c, d, m = records[172]
assert (a, b, c, d, n, m) == (84, 728, 748, 1392, 9, 9)
assert raw[a:a + n * 12] == raw[c:c + m * 12]
# Both blocks use the same layout. Keep the ID table and position-dependent
# offsets in the trailers intact; substitute only the environment values.
patched = raw[:a] + raw[c:d] + raw[b:]
assert len(patched) == len(raw) and patched != raw
assert patched[:a+n*12] == raw[:a+n*12] and patched[b:] == raw[b:]
assert patched[a:b] == patched[c:d]

def encode(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2).encode('utf-8')

with zipfile.ZipFile(prior) as z:
    files = {name: z.read(name) for name in z.namelist()}
old_assets = files.copy()
meta = json.loads(files['meta.json'])
meta.update(Name='Enuo Studio - 171 Environment Probe v0.2', Version='0.2.0',
            Description='与五色棚 v0.4 搭配。包含 171 场地组移植，可选择是否同时复用 172 环境。')
files['meta.json'] = encode(meta)
asset = 'environment/171.envb'
files[asset] = patched
options = [dict(Name=label, Files=mapping, FileSwaps={}, Manipulations=[])
           for label, mapping in [('原始 171 环境', {}), ('复用 172 环境', {env: asset})]]
files['group_001_environment.json'] = encode(dict(Version=0, Name='171 环境',
    Description='切换选项后重新进入场地。', Type='Single', Priority=1, DefaultSettings=1, Options=options))
with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as z:
    for name, data in files.items():
        z.writestr(name, data)
with zipfile.ZipFile(package) as z:
    assert z.testzip() is None and z.read(asset) == patched
    for name in old_assets:
        if name != 'meta.json':
            assert z.read(name) == old_assets[name]
print(package.name, package.stat().st_size)
