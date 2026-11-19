"""Build a reversible two-texture Penumbra diagnostic package, never install it."""
from pathlib import Path
import struct,json,zipfile,hashlib

WORKSPACE=Path(__file__).resolve().parents[2]
OUT=WORKSPACE/'output'/'EnuoStudio_WhiteProbe_v0.1'
PACKAGE=OUT/'package'
PACKAGE.mkdir(parents=True,exist_ok=True)
(PACKAGE/'textures').mkdir(exist_ok=True)

def write_json(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def white_texture(size):
    mips=size.bit_length(); offsets=[]; cursor=80
    for i in range(mips):
        offsets.append(cursor)
        side=max(1,size>>i)
        cursor+=max(1,(side+3)//4)**2*8
    header=struct.pack('<IIHHHBB',0x800000,0x3420,size,size,1,mips,0)
    header+=struct.pack('<3I',0,1,2)
    header+=struct.pack('<13I',*(offsets+[0]*(13-len(offsets))))
    assert len(header)==80
    # BC1 index zero always chooses the first opaque RGB565 endpoint (white).
    block=struct.pack('<HHI',0xffff,0,0)
    return header+block*((cursor-80)//8)

base='bg/ex4/05_zon_z5/fld/z5fd/texture/'
floor=base+'z5fd_a3_emv01_d.tex'
back=base+'z5fd_a3_emv02_d.tex'
maps={floor:'textures/floor_white.tex',back:'textures/background_white.tex'}
for path,size in [('textures/floor_white.tex',2048),('textures/background_white.tex',4096)]:
    (PACKAGE/path).write_bytes(white_texture(size))

def option(name,description,paths):
    return {'Name':name,'Description':description,'Files':{p:maps[p] for p in paths},'FileSwaps':{},'Manipulations':[]}

write_json(PACKAGE/'meta.json',{
    'FileVersion':3,'Name':'Enuo Studio - White Probe v0.1',
    'Author':'Local recording workflow','Version':'0.1.0',
    'Description':'恩欧录制棚资源定位测试。地图 1361，天气 172。只替换候选地板/背景专用贴图；不修改碰撞、模型、材质或 VFX。白色贴图仍会受到场景渲染影响。选项切换后需要重新加载场景。',
    'Website':'','ModTags':['Enuo','Recording','Diagnostic']})
write_json(PACKAGE/'default_mod.json',{'Version':0,'Files':{},'FileSwaps':{},'Manipulations':[]})
group={'Version':0,'Name':'测试范围','Description':'建议从只改地板开始；切换后重新加载地图并重设天气 172。',
       'Priority':0,'Type':'Single','DefaultSettings':1,
       'Options':[
           option('0 - 原样（不替换）','用于恢复和对照。',[]),
           option('1 - 只改地板','只将候选地板贴图改为均匀白色。',[floor]),
           option('2 - 只改背景','只将候选背景贴图改为均匀白色。',[back]),
           option('3 - 地板和背景','同时替换两个候选贴图。',[floor,back])
       ]}
write_json(PACKAGE/'group_001_probe.json',group)

guide='''# 恩欧白色定位测试包 v0.1

这是资源定位样件，尚未验证游戏内导入/渲染。它不是完成的五色录制棚。

## 导入及第一次测试

1. 在 Penumbra 的 Mods/模组页面使用 Import/导入，选择同目录 EnuoStudio_WhiteProbe_v0.1.pmp，无需解压。
2. 在负责场景资源的集合中启用 Enuo Studio - White Probe v0.1。场景贴图通常需要 Default/默认集合；只给人物分配的集合可能不会作用于场景。不要修改集合分配；如不生效，先检查此模组在默认集合的启用状态和冲突提示。
3. “测试范围”先选“1 - 只改地板”（默认）。
4. 为避免继续使用已载入贴图，在 Hyperborea 中使用 Revert 返回原场景，再用 1361 的 Load Zone 加载恩欧，随后在 Anamnesis 重新设置天气 172「虚无」。如果 Anamnesis 正在锁定天气，返回原场景前先解除锁定，进入恩欧后再设定。
5. 角色在 (100,2,100) 落地，关闭飞行和穿墙。先移除测试特效，观察地板的颜色/渐变是否改变，并保留相同机位的对照截图。
6. 用原来的方式播放 vfx/monster/gimmick3/eff/n4gb_b_g02c0a1.avfx，检查以前会缺失的部分是否仍完整。

先只完成这一次测试。地板变灰、变蓝但纹理明显改变，也可以证明替换生效；不要求画面立即变成纯白。

## 四个互斥选项

- 0 - 原样（不替换）：不进行任何文件替换。
- 1 - 只改地板：只替换候选地板专用贴图。
- 2 - 只改背景：只替换候选背景专用贴图。
- 3 - 地板和背景：同时替换两张贴图。

每次换选项都应重新加载场景，再设置天气 172。角色重绘不能作为场景贴图已刷新成功的保证。

## 恢复

禁用本模组（或选 0），重新加载场景。包不修改客户端安装文件，也没有修改模型、碰撞、天气、材质、灯光或任何 AVFX。

## 结果怎么反馈

只需一张只改地板后的空场截图，并说明：地板是否变化、测试特效是否完整。完全无变化时，先检查集合、冲突和场景重载，不能直接判定候选路径错误。

## 范围与限制

仅用于当前本地恩欧场景测试。替换按资源路径生效，不按天气或地图编号限定；其他引用这两张贴图的场景也可能受影响。

只替换两张专用贴图，不处理黑色花纹、光束、雾和点光源。纯白贴图仍可能受材质和灯光调色。首次替换本身用于验证候选资源对应关系。

包中两张图像内容完全生成为不透明白色，尺寸、格式和各级缩小贴图布局与本机原资源一致。未包含原游戏纹理图像或模型。
'''
(OUT/'使用说明.md').write_text(guide,encoding='utf-8')

archive=OUT/'EnuoStudio_WhiteProbe_v0.1.pmp'
with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for path in sorted(PACKAGE.rglob('*')):
        if path.is_file(): z.write(path,path.relative_to(PACKAGE).as_posix())

# Package integrity, option isolation, and full BC1/mipmap coverage checks.
checks={}
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
    assert len(z.namelist())==5
    for name in ('meta.json','default_mod.json','group_001_probe.json'):
        json.loads(z.read(name))
    g=json.loads(z.read('group_001_probe.json'))
    assert g['Type']=='Single' and g['DefaultSettings']==1
    assert [set(o['Files']) for o in g['Options']]==[set(),{floor},{back},{floor,back}]
    for gamepath,relative in maps.items():
        data=z.read(relative)
        flags,fmt,w,h,depth,mips,array=struct.unpack_from('<IIHHHBB',data)
        assert (flags,fmt,depth,array)==(0x800000,0x3420,1,0) and w==h
        offsets=struct.unpack_from('<13I',data,28); cursor=80
        for mip in range(mips):
            assert offsets[mip]==cursor
            count=max(1,(max(1,w>>mip)+3)//4)**2
            end=cursor+count*8
            assert all(c0==65535 and c1==0 and indices==0 for c0,c1,indices in struct.iter_unpack('<HHI',data[cursor:end]))
            cursor=end
        assert cursor==len(data)
        checks[gamepath]={'file':relative,'size':len(data),'dimensions':[w,h],'mip_count':mips,'sha256':hashlib.sha256(data).hexdigest(),'all_texels_opaque_white':True}
report={'archive':archive.name,'archive_bytes':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'zip_crc_pass':True,'json_read_pass':True,'option_isolation_pass':True,'textures':checks,
        'in_game_import_tested':False,'in_game_rendering_tested':False}
write_json(OUT/'verification.json',report)
print(json.dumps(report,ensure_ascii=False,indent=2))
