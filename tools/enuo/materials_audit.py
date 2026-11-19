"""Read-only model/material metadata audit. Never writes game resources."""
import json, struct, zlib, re, math
import probe

for ix in (probe.ROOT / 'ffxiv').glob('01*.index2'):
    b=ix.read_bytes(); h=struct.unpack_from('<I',b,12)[0]
    off,size=struct.unpack_from('<II',b,h+8)
    for key,val in struct.iter_unpack('<II',b[off:off+size]):
        probe.INDEX.setdefault(key,[]).append((ix,val))

def model(path, *, native=False):
    hits=probe.INDEX.get(zlib.crc32(path.lower().encode())^0xffffffff,[])
    assert len(hits)==1,(path,hits)
    ix,val=hits[0]; pos=(val&~15)*8
    with ix.with_suffix('.dat'+str((val&14)>>1)).open('rb') as f:
        f.seek(pos); h=f.read(208)
        header,kind,raw=struct.unpack_from('<III',h)
        assert kind==3
        offsets=struct.unpack_from('<11I',h,112)
        indices=struct.unpack_from('<11H',h,156)
        counts=struct.unpack_from('<11H',h,178)
        decls,materials=struct.unpack_from('<HH',h,200)
        sizes=struct.unpack('<'+'H'*sum(counts),f.read(2*sum(counts)))
        segments=[]
        for off,index,count in zip(offsets,indices,counts):
            parts=[]; p=pos+header+off
            for i in range(count):
                f.seek(p); bs,_,compressed,actual=struct.unpack('<4I',f.read(16)); assert bs==16
                d=f.read(actual) if compressed==32000 else zlib.decompress(f.read(compressed),-15)
                assert len(d)==actual
                parts.append(d); p+=sizes[index+i]
            segments.append(b''.join(parts))
    assert sum(map(len,segments))+68==raw,(path,raw,list(map(len,segments)))
    if native:
        # Native header for the single-LOD, no-edge studio models.
        assert h[204] == 1 and all(not segments[i] for i in (3,4,5,6,7,9,10))
        stack,runtime,vertices,indices=(segments[i] for i in (0,1,2,8))
        vertex_at=68+len(stack)+len(runtime)
        index_at=vertex_at+len(vertices)
        file_header=(h[20:24]+struct.pack('<2I2H12I',len(stack),len(runtime),decls,materials,
            vertex_at,0,0,index_at,0,0,len(vertices),0,0,len(indices),0,0)+h[204:207]+b'\0')
        data=file_header+stack+runtime+vertices+indices
        assert len(data)==raw and index_at+len(indices)==raw
        return data
    stack,runtime=segments[:2]
    assert len(stack)==decls*136
    sc,_,ss=struct.unpack_from('<HHI',runtime)
    strings=runtime[8:8+ss]
    names=[s.decode('utf-8') for s in strings.split(b'\0') if s]
    mh=8+ss
    radius=struct.unpack_from('<f',runtime,mh)[0]
    meshes,attrs,subs,mats,bones,bt,shapes,sm,sv=struct.unpack_from('<9H',runtime,mh+4)
    eid=struct.unpack_from('<H',runtime,mh+24)[0]
    flags2=runtime[mh+27]
    lp=mh+56+eid*32
    lods=[struct.unpack_from('<HH',runtime,lp+60*i) for i in range(3)]
    mp=lp+180+(120 if flags2&16 else 0)
    geoms=[]
    for lod,(first,count) in enumerate(lods):
        for mi in range(first,first+count):
            mo=mp+mi*36
            vc=struct.unpack_from('<H',runtime,mo)[0]
            ic=struct.unpack_from('<I',runtime,mo+4)[0]
            material=struct.unpack_from('<H',runtime,mo+8)[0]
            vo=struct.unpack_from('<3I',runtime,mo+20)
            stride=runtime[mo+32:mo+35]
            declaration=stack[mi*136:(mi+1)*136]
            elements=[]; vertices=[]
            for ei in range(17):
                stream,offset,typ,usage,ui=struct.unpack_from('<5B',declaration,ei*8)
                if stream==255: break
                elements.append([stream,offset,typ,usage,ui])
                if usage==0:
                    assert typ in (2,3,13,14),typ
                    fmt={2:'<3f',3:'<4f',13:'<2e',14:'<4e'}[typ]
                    assert typ!=13,'two component position'
                    for vi in range(vc):
                        v=struct.unpack_from(fmt,segments[2+lod],vo[stream]+vi*stride[stream]+offset)[:3]
                        assert all(math.isfinite(x) for x in v)
                        vertices.append(v)
            geoms.append({'lod':lod,'mesh':mi,'material_index':material,'vertex_count':vc,'index_count':ic,'elements':elements,
                          'min':[min(v[j] for v in vertices) for j in range(3)] if vertices else None,
                          'max':[max(v[j] for v in vertices) for j in range(3)] if vertices else None,
                          'central_500_vertex_y':sorted(set(v[1] for v in vertices if abs(v[0])<=250 and abs(v[2])<=250)),
                          'y_counts':{str(y):sum(v[1]==y for v in vertices) for y in sorted(set(v[1] for v in vertices))} if 'a3_stg00' in path else None})
    return {'path':path,'string_count':sc,'strings':names,'material_count':materials,'radius':radius,'geometries':geoms}

if __name__=='__main__':
    objects=json.loads((probe.OUT/'layout_objects.json').read_text(encoding='utf-8'))
    paths=sorted(set(o['model'] for o in objects if o['type']==1))
    result=[]
    for p in paths:
        try: result.append(model(p))
        except Exception as e: result.append({'path':p,'error':repr(e)})
    env='bgcommon/env/global/ex4_genv/genv_zon_z5/genv_z5_fld/genv_z5fd.envb'
    eb=probe.read(env)
    report={'models':result,'environment':{'path':env,'bytes':len(eb) if eb else None,'strings':[s.decode('ascii') for s in re.findall(rb'[\x20-\x7e]{8,}',eb or b'')]}}
    (probe.OUT/'materials_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
