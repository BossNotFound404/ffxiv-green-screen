import struct,zlib,json,io
import materials_audit as a
Image = None

def texture(path):
    ix,val=a.probe.INDEX[zlib.crc32(path.encode())^0xffffffff][0]
    pos=(val&~15)*8
    with ix.with_suffix('.dat'+str((val&14)>>1)).open('rb') as f:
        f.seek(pos); header,kind,length,_,_,n=struct.unpack('<6I',f.read(24)); assert kind==4
        blocks=[struct.unpack('<5I',f.read(20)) for _ in range(n)]
        strides=struct.unpack('<'+'H'*sum(b[4] for b in blocks),f.read(2*sum(b[4] for b in blocks)))
        f.seek(pos+header); data=f.read(blocks[0][0]); bi=0
        for off,cs,ds,bo,count in blocks:
            p=pos+header+off; parts=[]
            for i in range(count):
                f.seek(p); hs,_,packed,size=struct.unpack('<4I',f.read(16)); assert hs==16
                d=f.read(size) if packed==32000 else zlib.decompress(f.read(packed),-15)
                assert len(d)==size
                parts.append(d); p+=strides[bi];bi+=1
            chunk=b''.join(parts); assert len(chunk)==ds
            data+=chunk
    assert len(data)==length
    return data

report=[]
for suffix in ['emv01','emv02','lsf01']:
    p=f'bg/ex4/05_zon_z5/fld/z5fd/texture/z5fd_a3_{suffix}_d.tex'
    d=texture(p); flags,fmt,w,h=struct.unpack_from('<IIHH',d)
    offset=struct.unpack_from('<I',d,28)[0]
    row={'path':p,'width':w,'height':h,'format':hex(fmt)}
    if fmt==0x3420:
        # Decode BC1 at sampled texel centres for an inspection preview only.
        pw,ph=min(w,512),min(h,512); rows=[]
        def rgb565(v): return ((v>>11)*255//31,((v>>5)&63)*255//63,(v&31)*255//31)
        for yy in range(ph):
            rowdata=bytearray([0])
            y=yy*h//ph
            for xx in range(pw):
                x=xx*w//pw
                at=offset+((y//4)*((w+3)//4)+x//4)*8
                c0,c1,bits=struct.unpack_from('<HHI',d,at)
                a0,a1=rgb565(c0),rgb565(c1)
                pal=[(*a0,255),(*a1,255)]
                if c0>c1:
                    pal.extend([(*( (2*a0[j]+a1[j])//3 for j in range(3)),255),(*( (a0[j]+2*a1[j])//3 for j in range(3)),255)])
                else: pal.extend([(*( (a0[j]+a1[j])//2 for j in range(3)),255),(0,0,0,0)])
                rowdata.extend(pal[(bits>>(2*((y%4)*4+x%4)))&3])
            rows.append(rowdata)
        def chunk(tag,data): return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data))
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',pw,ph,8,6,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(rows)))+chunk(b'IEND',b'')
        (a.probe.OUT/f'preview_{suffix}.png').write_bytes(png)
        row['preview']=f'preview_{suffix}.png'
    if Image is not None and fmt in (0x3420,0x3430,0x3431,0x6432):
        fourcc={0x3420:b'DXT1',0x3430:b'DXT3',0x3431:b'DXT5',0x6432:b'DX10'}[fmt]
        head=struct.pack('<7I',124,0x81007,h,w,((w+3)//4)*((h+3)//4)*(8 if fmt==0x3420 else 16),0,1)+bytes(44)
        head+=struct.pack('<II4s5I',32,4,fourcc,0,0,0,0,0)+struct.pack('<5I',0x1000,0,0,0,0)
        if fmt==0x6432: head+=struct.pack('<5I',98,3,0,1,0)
        im=Image.open(io.BytesIO(b'DDS '+head+d[offset:])).convert('RGBA')
        row['channel_extrema']=im.getextrema()
        im.thumbnail((640,640)); im.save(a.probe.OUT/f'preview_{suffix}.png')
        row['preview']=f'preview_{suffix}.png'
    report.append(row)
(a.probe.OUT/'texture_report.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
