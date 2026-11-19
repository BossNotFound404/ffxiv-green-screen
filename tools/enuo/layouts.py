import json, struct, math
import probe

rows = []
for name in ['bg','planmap','planevent','planlive','planner','vfx']:
    path = f'bg/ex4/05_zon_z5/fld/z5fd/level/{name}.lgb'
    b = probe.read(path)
    assert b[:4] == b'LGB1' and b[12:16] == b'LGP1'
    def u(at): return struct.unpack_from('<I', b, at)[0]
    def text(at):
        assert 0 <= at < len(b)
        return b[at:b.index(b'\0', at)].decode('utf-8')
    for i in range(u(32)):
        layer = 36 + u(36 + 4 * i)
        lid, no, io, count = struct.unpack_from('<4I', b, layer)
        lname = text(layer + no)
        table = layer + io
        for j in range(count):
            o = table + u(table + 4 * j)
            kind, iid, no = struct.unpack_from('<3I', b, o)
            tr = struct.unpack_from('<9f', b, o + 12)
            assert all(math.isfinite(x) for x in tr)
            row = dict(file=name,layer_id=lid,layer=lname,type=kind,id=iid,name=text(o+no),position=tr[:3],rotation=tr[3:6],scale=tr[6:],offset=o)
            if kind == 1:
                row.update(model=text(o+u(o+48)),collision=text(o+u(o+52)),collision_type=u(o+56),attribute_mask=u(o+60),attribute=u(o+64))
            elif kind == 57:
                row.update(shape=u(o+48),enabled=b[o+54],attribute_mask=u(o+60),attribute=u(o+64),collision=text(o+u(o+72)))
            elif kind in (4,6):
                row['asset']=text(o+u(o+48))
            rows.append(row)
(probe.OUT/'layout_objects.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
selected=[r for r in rows if r['type']==57 or (r['type']==1 and ('stg' in r['model'] or max(map(abs,r['scale']))>=100))]
print(json.dumps({'objects':len(rows),'selected':selected},ensure_ascii=False))
