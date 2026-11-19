"""Package the environment A/B test on top of the validated white stage."""
from pathlib import Path
import json
import zipfile

from probe import read


output = Path(__file__).resolve().parents[2] / 'output'
previous = output / 'EnuoStudio_StageProbe_v0.2/EnuoStudio_StageProbe_v0.2.pmp'
directory = output / 'EnuoStudio_StageProbe_v0.3'
directory.mkdir(exist_ok=True)
package = directory / 'EnuoStudio_StageProbe_v0.3.pmp'
effect_dir = 'bg/ex4/05_zon_z5/common/vfx/eff/'
names = (
    'b4251bck01', 'b4255bak01', 'z5fd01bak1', 'z5fd01bak2', 'z5fd01bak4',
    'z5fd02bak2', 'z5fd03bak3', 'z5fd01und1', 'z5fd03und1',
)
swaps = {effect_dir + name + '_y1.avfx': 'vfx/path/nothing.avfx' for name in names}
for path in swaps:
    data = read(path)
    assert data and data.startswith(b'XFVA'), path

group_name = 'group_004_environment.json'
group = {
    'Version': 0, 'Type': 'Single', 'Priority': 4, 'DefaultSettings': 1,
    'Name': 'D - 周边环境特效',
    'Description': '在天气 172 下比较天空云雾。切换后重新加载场景。',
    'Options': [
        {'Name': name, 'Files': {}, 'FileSwaps': paths, 'Manipulations': []}
        for name, paths in [('保留原环境特效', {}), ('屏蔽九个候选环境特效', swaps)]
    ],
}

def encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')

with zipfile.ZipFile(previous) as source:
    contents = {name: source.read(name) for name in source.namelist()}
meta = json.loads(contents['meta.json'])
meta.update(Name='Enuo Studio - Stage Probe v0.3', Version='0.3.0',
            Description='恩欧白棚云雾对照测试。天气 172。选项 D 控制九个候选环境特效。')
contents['meta.json'] = encode(meta)
contents[group_name] = encode(group)
with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for name, data in contents.items():
        archive.writestr(name, data)

with zipfile.ZipFile(previous) as before, zipfile.ZipFile(package) as after:
    assert after.testzip() is None
    assert set(after.namelist()) == set(before.namelist()) | {group_name}
    for name in before.namelist():
        if name != 'meta.json':
            assert before.read(name) == after.read(name)
    active = json.loads(after.read(group_name))['Options'][1]['FileSwaps']
    assert active == swaps and len(active) == 9
print(package, package.stat().st_size)
