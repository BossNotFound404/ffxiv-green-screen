from pathlib import Path
import os
import struct, zlib, re, json, hashlib

if not os.environ.get('FFXIV_SQPACK'):
    raise SystemExit('Set FFXIV_SQPACK to the local game/sqpack directory or use build.py --sqpack.')
ROOT = Path(os.environ['FFXIV_SQPACK'])
OUT = Path(__file__).resolve().parents[2] / 'output' / 'audit'
OUT.mkdir(parents=True, exist_ok=True)
INDEX = {}
for ix in (ROOT / 'ex4').glob('02*.index2'):
    b = ix.read_bytes()
    assert b[:6] == b'SqPack'
    h = struct.unpack_from('<I', b, 12)[0]
    offset, size = struct.unpack_from('<II', b, h + 8)
    assert size % 8 == 0 and offset + size <= len(b)
    for key, val in struct.iter_unpack('<II', b[offset:offset + size]):
        INDEX.setdefault(key, []).append((ix, val))

def read(path):
    hits = INDEX.get(zlib.crc32(path.lower().encode()) ^ 0xffffffff, [])
    if not hits:
        return None
    assert len(hits) == 1, ('ambiguous hash', path)
    ix, val = hits[0]
    assert not val & 1, ('synonym', path)
    pos = (val & ~15) * 8
    dat = ix.with_suffix('.dat' + str((val & 14) >> 1))
    with dat.open('rb') as f:
        f.seek(pos)
        header, kind, length, _, _, count = struct.unpack('<6I', f.read(24))
        assert kind == 2, (path, kind)
        blocks = [struct.unpack('<IHH', f.read(8)) for _ in range(count)]
        parts = []
        for offset, packed, unpacked in blocks:
            f.seek(pos + header + offset)
            size, _, compressed, actual = struct.unpack('<4I', f.read(16))
            assert size == 16 and actual == unpacked
            data = f.read(actual) if compressed == 32000 else zlib.decompress(f.read(compressed), -15)
            assert len(data) == actual
            parts.append(data)
    result = b''.join(parts)
    assert len(result) == length
    return result

def references(data):
    return sorted(set(x.decode('ascii') for x in re.findall(rb'(?:bg|bgcommon)/[a-zA-Z0-9_./-]+\.(?:sgb|pcb|mdl|mtrl|tex|avfx)', data)))

def mesh(data, geometry=False):
    version = struct.unpack_from('<i', data, 4)[0]
    assert version in (1, 4)
    stack, seen, vertices, triangles = [16], set(), [], []
    while stack:
        o = stack.pop()
        assert o not in seen and o + 48 <= len(data)
        seen.add(o)
        c1, c2 = struct.unpack_from('<ii', data, o + 8)
        limits = struct.unpack_from('<6f', data, o + 16)
        ncomp, nprim, nraw = struct.unpack_from('<HHH', data, o + 40)
        p = o + 48
        local = [struct.unpack_from('<3f', data, p + 12 * i) for i in range(nraw)]
        p += 12 * nraw
        for i in range(ncomp):
            q = struct.unpack_from('<3H', data, p + 6 * i)
            local.append(tuple(limits[j] + (limits[j+3] - limits[j]) * q[j] / 65535 for j in range(3)))
        p += 6 * ncomp
        for i in range(nprim):
            ids = struct.unpack_from('<3B', data, p + 12 * i)
            assert all(v < len(local) for v in ids)
            material = struct.unpack_from('<Q', data, p + 12 * i + 4)[0]
            triangles.append(([local[v] for v in ids], material))
        vertices.extend(local)
        stack.extend(o + child for child in (c1, c2) if child)
    if geometry:
        return vertices, triangles
    if not vertices:
        return {'version': version, 'vertices': 0}
    return {'version': version, 'nodes': len(seen), 'vertices': len(vertices), 'triangles': len(triangles),
            'min': [min(v[j] for v in vertices) for j in range(3)],
            'max': [max(v[j] for v in vertices) for j in range(3)],
            'distinct_y': sorted(set(v[1] for v in vertices))[:30],
            'material_counts': {str(k): sum(m == k for _, m in triangles) for k in set(m for _, m in triangles)}}

if __name__ == '__main__':
    base = 'bg/ex4/05_zon_z5/fld/z5fd/level/'
    queue = [base + f for f in ['z5fd.lvb','bg.lgb','planmap.lgb','planevent.lgb','planlive.lgb','planner.lgb','vfx.lgb']]
    report, seen = [], set()
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        data = read(path)
        if data is None:
            continue
        refs = references(data)
        item = {'path': path, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'references': refs}
        if path.endswith('.pcb'):
            item['mesh'] = mesh(data)
        for ref in refs:
            if ref.endswith(('.sgb', '.pcb')):
                queue.append(ref)
            elif ref.endswith('.mdl'):
                queue.append(ref.replace('/bgparts/', '/collision/').removesuffix('.mdl') + '.pcb')
        report.append(item)
    (OUT / 'resource_probe.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'files':len(report),'collision_meshes':[r for r in report if 'mesh' in r]}, ensure_ascii=False))
