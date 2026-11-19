"""Read and write the aligned tag-length-value blocks of an embedded AVFX."""
import struct


def chunks(data):
    offset = 0
    while offset < len(data):
        tag, size = struct.unpack_from("<4sI", data, offset)
        start = offset + 8
        offset = start + ((size + 3) & ~3)
        assert offset <= len(data)
        yield tag[::-1].strip(b"\0").decode("ascii"), data[start:start + size]


def chunk(tag, value):
    key = tag.encode("ascii").rjust(4, b"\0")[::-1]
    # Native tags are reversed after padding on the left (e.g. RGB -> BGR\0).
    return struct.pack("<4sI", key, len(value)) + value + bytes(-len(value) % 4)


def rewrite(data, replacements):
    result = []
    seen = set()
    for tag, value in chunks(data):
        if tag in replacements:
            assert tag not in seen
            seen.add(tag)
            value = replacements[tag]
        result.append(chunk(tag, value))
    assert seen == replacements.keys()
    return b"".join(result)
