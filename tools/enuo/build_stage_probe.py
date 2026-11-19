"""Build v0.2 for user-side testing. Read game archives; write only workspace outputs."""
from pathlib import Path
import json,struct,zipfile,hashlib,shutil
import probe

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'output'/'EnuoStudio_StageProbe_v0.2'
PKG=OUT/'package'
PKG.mkdir(parents=True,exist_ok=True)
BASE='bg/ex4/05_zon_z5/fld/z5fd/'
changes=[]

def save(rel,data):
    p=PKG/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)

def js(rel,data):save(rel,(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))

def parse_material(b):
    version,size,dataset,stringsize,shader,nt,nu,nc,additional=struct.unpack_from('<I4H4B',b)
    assert size==len(b)
    stringstart=16+4*(nt+nu+nc)
    header=stringstart+stringsize+additional+dataset
    valuebytes,nkeys,nconstants,nsamplers,flags1,flags2=struct.unpack_from('<6H',b,header)
    keypos=header+12;constantpos=keypos+nkeys*8;samplerpos=constantpos+nconstants*8;valuepos=samplerpos+nsamplers*12
    assert valuepos+valuebytes==len(b)
    keys=dict(struct.iter_unpack('<II',b[keypos:constantpos]))
    constants=list(struct.iter_unpack('<IHH',b[constantpos:samplerpos]))
    assert len(keys)==nkeys
    for _,o,n in constants:assert o+n<=valuebytes and n%4==0
    textures=[]
    for i in range(nt):
        off,flags=struct.unpack_from('<HH',b,16+4*i)
        p=stringstart+off;q=b.index(0,p)
        textures.append((p,q,b[p:q].decode('ascii'),flags))
    return dict(header=header,keypos=keypos,constantpos=constantpos,valuepos=valuepos,keys=keys,constants=constants,textures=textures)

# Parameter/key IDs: PassiveModding/Meddle, Meddle.Formats/Constants/Names.cs.
PARAMS={0x2C2A34DD:0.0,0x3F8AC211:0.0,0x141722D5:0.0,0x86D60CB8:0.0,
        0x38A64362:1.0,0xAA676D0F:1.0,0xBCEA8C11:0.0,0x5095E770:0.0,0xBFE9D12D:0.0}
KEYS={0xB616DC5A:0x669A451B,0x4F4F0636:0x7C6FA05B}

def neutral_material(path):
    original=probe.read(path);assert original is not None
    info=parse_material(original);b=bytearray(original);diff=[]
    for key,offset,size in info['constants']:
        if key in PARAMS:
            old=list(struct.unpack_from('<'+'f'*(size//4),b,info['valuepos']+offset))
            new=[PARAMS[key]]*(size//4)
            struct.pack_into('<'+'f'*(size//4),b,info['valuepos']+offset,*new)
            diff.append({'id':hex(key),'old':old,'new':new})
    assert {0x2C2A34DD,0x141722D5,0x38A64362,0xBCEA8C11,0x5095E770}.issubset(k for k,_,_ in info['constants'])
    keys=dict(info['keys']);keys.update(KEYS)
    b=b[:info['keypos']]+b''.join(struct.pack('<II',k,v) for k,v in sorted(keys.items()))+b[info['constantpos']:]
    struct.pack_into('<H',b,4,len(b));struct.pack_into('<H',b,info['header']+2,len(keys))
    after=parse_material(b)
    assert all(after['keys'][k]==v for k,v in KEYS.items())
    assert [t[2:] for t in after['textures']]==[t[2:] for t in info['textures']]
    assert b[:4]==original[:4]
    changes.append({'path':path,'operation':'neutral material parameters','parameters':diff,'original_keys':{hex(k):hex(v) for k,v in info['keys'].items()},'new_keys':{hex(k):hex(v) for k,v in keys.items()}})
    return bytes(b)

white={}
for suffix,file in [('emv01','floor_white.tex'),('emv02','background_white.tex')]:
    data=(ROOT/'output'/'EnuoStudio_WhiteProbe_v0.1'/'package'/'textures'/file).read_bytes()
    save('textures/'+file,data)
    white[BASE+'texture/z5fd_a3_'+suffix+'_d.tex']='textures/'+file

# Generated, four-by-four, one-mip BGRA8 transparent black. No original texture pixels included.
blank=struct.pack('<IIHHHBB',0x800000,0x1450,4,4,1,1,0)+struct.pack('<3I',0,0,0)+struct.pack('<13I',80,*([0]*12))+bytes(64)
assert len(blank)==144
save('textures/transparent.tex',blank)

neutral={}
for name in ['z5fd_a3_emv01a','z5fd_a3_emv02a','z5fd_a3_emv02b']:
    path=BASE+'material/'+name+'.mtrl';rel='materials/'+name+'.mtrl'
    save(rel,neutral_material(path));neutral[path]=rel

# Three dedicated textures on the a1 environmental meshes, all using alpha-tested bg materials.
# These are candidates for the black sigils, not yet runtime-identified.
sigils={}
for name in ['z5fd_a1_emv01a','z5fd_a1_emv02a','z5fd_a1_emv03a']:
    path=BASE+'material/'+name+'.mtrl';original=probe.read(path);info=parse_material(original)
    assert info['keys'].get(0xA9A3EE25)==0x72AAA9AE,('not alpha tested',path)
    target=info['textures'][0]
    assert target[2].startswith(BASE+'texture/z5fd_a1_emv') and target[3]==0
    sigils[target[2]]='textures/transparent.tex'
assert len(sigils)==3

# Redirect only the three local lightshaft materials to a private zero texture.
# Shared game textures and other zones' light shafts are not edited.
shaft={};private=BASE+'texture/p0.tex'
for name in ['z5fd_a3_lsf01a','z5fd_a3_lsf02a','z5fd_a3_lsf03a']:
    path=BASE+'material/'+name+'.mtrl';original=probe.read(path);info=parse_material(original);b=bytearray(original)
    for p,q,old,flags in info['textures']:
        assert len(private)<=q-p and flags==0
        b[p:q]=private.encode()+bytes(q-p-len(private))
    assert len(b)==len(original)
    after=parse_material(b);assert all(t[2]==private for t in after['textures'])
    rel='materials/'+name+'.mtrl';save(rel,bytes(b));shaft[path]=rel
    changes.append({'path':path,'operation':'private transparent texture binding','old_textures':[t[2] for t in info['textures']],'new_texture':private})
shaft[private]='textures/transparent.tex'

def opt(name,files):return {'Name':name,'Files':files,'FileSwaps':{},'Manipulations':[]}
def group(n,name,desc,off,on):
    js(f'group_{n:03d}_stage.json',{'Version':0,'Name':name,'Description':desc,'Priority':n,'Type':'Single','DefaultSettings':1,'Options':[opt(off,{}),opt(on,{'1':sigils,'2':neutral,'3':shaft}[str(n)])]})

js('meta.json',{'FileVersion':3,'Name':'Enuo Studio - Stage Probe v0.2','Author':'Local recording workflow','Version':'0.2.0',
    'Description':'恩欧本地录制棚第二轮测试。固定天气172。默认白色地板/背景，另提供候选法阵隐藏、统一材质、光束隐藏三个独立开关。未在游戏内验证；与v0.1互斥启用。','ModTags':['Enuo','Recording','Diagnostic']})
js('default_mod.json',{'Version':0,'Files':white,'FileSwaps':{},'Manipulations':[]})
group(1,'A - 候选法阵','透明化三张专用环境贴图；仍有法阵时请关闭本项作对照，不代表已定位全部来源。','保留','隐藏候选法阵')
group(2,'B - 地板与背景材质','关闭漫反射/高光/阴影参数，统一自发光，改为单组颜色。并非已验证的完全无光照着色器。','原材质','统一白色材质')
group(3,'C - 场景光束','仅将三个a3光束材质的贴图绑定到私有透明纹理，不修改共享游戏贴图。','保留','隐藏光束')

archive=OUT/'EnuoStudio_StageProbe_v0.2.pmp'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
    for p in sorted(PKG.rglob('*')):
        if p.is_file():z.write(p,p.relative_to(PKG).as_posix())

# Static checks of the deliverable, including every redirected file and binary material boundary.
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
    targets=set()
    for name in z.namelist():
        if name.endswith('.json'):
            doc=json.loads(z.read(name))
            for container in [doc]+doc.get('Options',[]):
                assert not container.get('FileSwaps') and not container.get('Manipulations')
                for game,rel in container.get('Files',{}).items():
                    assert game.startswith(BASE) and rel in z.namelist()
                    assert '..' not in rel and not rel.startswith(('/', '\\'))
                    targets.add(game)
        elif name.endswith('.mtrl'):parse_material(z.read(name))
    assert len(targets)==12,len(targets)
    assert not any(p.endswith(('.pcb','.mdl','.lgb','.sgb','.avfx')) for p in targets)
    assert z.read('textures/transparent.tex')[80:]==bytes(64)

report={'package':archive.name,'bytes':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'zip_crc_pass':True,'file_references_pass':True,'material_bounds_pass':True,'game_paths':sorted(targets),
        'changes':changes,'game_install_modified':False,'installed_automatically':False,'in_game_tested':False,
        'limits':['Sigil identity is a candidate based on model/material references.','Uniform emissive materials may still be affected by fog/post-processing or shader behavior.','No geometric or collision edits.']}
(OUT/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
(OUT/'使用说明.md').write_text('''# 恩欧 Stage Probe v0.2

这是第二轮定位/材质验证包，不是最终五色棚。

1. 在 Penumbra 默认集合中禁用 v0.1，导入本目录的 v0.2.pmp，然后启用 v0.2。不要同时启用两版。
2. 保持 A、B、C 三项为默认开启；基础白色地板/背景总是包含在本包中。
3. Hyperborea 返回原场景，再加载 1361；Anamnesis 重新设定天气 172「虚无」。如锁定天气，返回原场景前先解除，进入恩欧后再设定。
4. 用与上一版相近的机位截图空场，观察法阵、人物投影、地平线色差和局部色块。
5. 用原来的方法播放 n4gb_b_g02c0a1.avfx，检查特效是否完整。

三个选项：
- A 隐藏候选法阵：透明化 a1_emv01/02/03 的三张专用纹理；未在运行时确认它们就是截图全部法阵的来源。
- B 统一白色材质：在原 bg 着色器内关闭漫反射、高光与阴影参数，统一自发光为白色，禁用顶点颜色，使用单组颜色计算。不是替换全局着色器，也不是已验证的无光照保证。
- C 隐藏光束：仅修改三个 a3 光束材质的贴图引用，指向包内私有透明纹理。

需要分辨某项作用时，只关闭那一项，重新加载地图后比较。无需每项都测试一遍；首先反馈默认组合的结果。

不修改碰撞、可见几何、LGB/SGB、天气或任何 AVFX；你要录制的测试 VFX 没有被重定向。本轮不全局关闭灯光，以免同时改变目标特效。

注意：仍可能受环境雾、曝光/色调映射、屏幕空间效果或背景其他元素影响。原地板中央顶点高度一致，色差不是几何不平的充分证据；本轮先分离材质因素。

恢复：禁用 v0.2，重载场景。需要回到第一轮时再启用 v0.1。替换按资源路径生效，可能影响引用这些专用资源的普通/极恩欧其他阶段。

仅包结构、材质数据边界与文件映射已检查。导入后的画面、法阵是否消失、特效是否保持完整，都需要游戏内验收。
''',encoding='utf-8')
print(json.dumps({k:v for k,v in report.items() if k!='changes'},ensure_ascii=False,indent=2))
