"""Report authored AVFX particle settings and texture slots. No GPU draws are observed."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

from avfx_chunks import chunks


# VFXEditor/Formats/AvfxFormat/Base/Enums.cs and Particle/AvfxParticle.cs,
# https://github.com/0ceal0t/Dalamud-VFXEditor/tree/49012104303caae373ac4b37b385a3affedca472
PARTICLE_TYPES = (
    "Parameter", "Powder", "Windmill", "Line", "Laser", "Model", "Polyline", "Reserve0",
    "Quad", "Polygon", "Decal", "DecalRing", "Disc", "LightModel", "ModelSkin", "Dissolve",
)
DRAW_MODES = (
    "Blend", "Multiply", "Add", "Subtract", "Screen", "Reverse", "Min", "Max", "Opacity",
    "Unknown_Multiply_9", "Unknown_Add_10", "Unknown_Subtract_11", "Unknown_Screen_12",
)


def enum_field(fields, tag, names):
    if tag not in fields:
        return {"present": False, "value": None, "name": None}
    value, = struct.unpack("<i", fields[tag])
    name = names[value] if 0 <= value < len(names) else "Unknown"
    return {"present": True, "value": value, "name": name}


def inspect(data):
    roots = list(chunks(data))
    if len(roots) != 1 or roots[0][0] != "AVFX":
        raise ValueError("expected a single AVFX root")
    particles, textures = [], []
    for tag, contents in chunks(roots[0][1]):
        if tag == "Ptcl":
            fields = {}
            has_alpha_curve = False
            for key, item in chunks(contents):
                if key in ("PrVT", "RMT"):
                    if key in fields:
                        raise ValueError(f"duplicate {key} in particle {len(particles)}")
                    fields[key] = item
                elif key == "Col":
                    has_alpha_curve |= any(channel == "A" for channel, _ in chunks(item))
            particles.append({"index": len(particles),
                              "particle_type": enum_field(fields, "PrVT", PARTICLE_TYPES),
                              "draw_mode": enum_field(fields, "RMT", DRAW_MODES),
                              "has_color_alpha_curve": has_alpha_curve})
        elif tag == "Tex":
            textures.append(contents.rstrip(b"\0").decode("utf-8"))
    return {"scope": "Authored AVFX metadata only", "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "particle_count": len(particles), "particles": particles,
            "texture_count": len(textures), "unique_texture_count": len(set(textures)),
            "textures": textures}


def read_vfx(root, game_path):
    # probe.py indexes expansion backgrounds and creates an audit directory at import.
    # Scan only the VFX indexes, using the same standard-file block format.
    normalized = game_path.lower().replace("\\", "/")
    if not normalized.startswith("vfx/") or not normalized.endswith(".avfx"):
        raise ValueError("expected vfx/.../*.avfx in SqPack mode")
    wanted = zlib.crc32(normalized.encode("utf-8")) ^ 0xffffffff
    matches = []
    for index in root.glob("*/08*.win32.index2"):
        table = index.read_bytes()
        if table[:6] != b"SqPack":
            raise ValueError(f"invalid SqPack index: {index.name}")
        header, = struct.unpack_from("<I", table, 12)
        offset, size = struct.unpack_from("<II", table, header + 8)
        if offset + size > len(table):
            raise ValueError(f"truncated SqPack index: {index.name}")
        for key, entry in struct.iter_unpack("<II", memoryview(table)[offset:offset + size]):
            if key == wanted:
                matches.append((index, entry))
    if not matches:
        raise ValueError(f"VFX resource not found in {root}: {game_path}")
    if len(matches) != 1 or matches[0][1] & 1:
        raise ValueError("ambiguous or synonym SqPack entry; inspect an exported AVFX instead")

    index, entry = matches[0]
    pos = (entry & ~15) * 8
    with index.with_suffix(f".dat{(entry & 14) >> 1}").open("rb") as handle:
        handle.seek(pos)
        header, kind, length, _, _, count = struct.unpack("<6I", handle.read(24))
        if kind != 2:
            raise ValueError(f"expected a standard SqPack entry, got {kind}")
        blocks = list(struct.iter_unpack("<IHH", handle.read(8 * count)))
        result = bytearray()
        for offset, _, expected in blocks:
            handle.seek(pos + header + offset)
            block_header, _, compressed, size = struct.unpack("<4I", handle.read(16))
            if block_header != 16 or size != expected:
                raise ValueError("invalid SqPack block header")
            block = handle.read(size if compressed == 32000 else compressed)
            if compressed != 32000:
                block = zlib.decompress(block, -15)
            if len(block) != size:
                raise ValueError("truncated SqPack block")
            result.extend(block)
    if len(blocks) != count or len(result) != length:
        raise ValueError("truncated SqPack entry")
    return bytes(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset", help="exported .avfx file, or vfx/.../*.avfx when using --sqpack")
    parser.add_argument("--sqpack", type=Path, help="local game/sqpack directory")
    args = parser.parse_args()
    try:
        data = read_vfx(args.sqpack, args.asset) if args.sqpack else Path(args.asset).read_bytes()
        print(json.dumps({"source": args.asset, **inspect(data)}, indent=2, ensure_ascii=False))
    except (OSError, ValueError, struct.error, zlib.error, AssertionError) as exc:
        parser.exit(1, f"AVFX inspection failed: {exc or 'malformed chunk'}\n")


if __name__ == "__main__":
    main()
