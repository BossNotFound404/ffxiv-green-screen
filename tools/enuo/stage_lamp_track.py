"""Add an optional, constant neutral color to a single active 448 stage lamp."""
import hashlib
import struct


# The four spotlights have authored intensity 20. A common white gain of 14
# leaves headroom when the original saturated color becomes neutral.
STUDIO_GAIN = 14.0
SOURCE_HASHES = {
    47: "ee29a0f70b6c96db6d260a6686f97ea3136906d88330d27b625cd5293e4dd244",
    45: "2f56f215e64a5f129d079a3889e61f20812d65f48a4bae372623bafa194bcf7b",
    48: "36f5207534d87a19cd2a384b4b90f92ac9a01568c44958d6c3600c9302a036de",
    56: "279b7bb9f860a417474707519efb4e1341a65269bd166c17c733f06430f0a7c5",
}
EXAMPLE_PATH = "bgcommon/env/obset/ffxiv_obset/obset_lak_l1/obset_l1_rad/obset_l1r2_pass2.obsb"


def _rows(data):
    table, count = struct.unpack_from("<2I", data, 24)
    for index in range(count):
        entry = 20 + table + 16 * index
        start, channels, weather, footer = struct.unpack_from("<4I", data, entry)
        yield entry, entry + start, channels, weather, entry + footer


def _mixed_row(visibility_key, rgb, gain):
    # l1r2's bg.lgb binds lamp 4741747 to l1r2_pass2. Its weather 1 has this
    # channel pair. Both key lists and the color pointer are owner-relative.
    key_time = visibility_key[:4]
    return (struct.pack("<6I", 24, 1, 33, 32, 1, 29)
            + struct.pack("<I", 4) + key_time + struct.pack("<I", 8)
            + struct.pack("<4Bf", *rgb, 255, gain)
            + struct.pack("<I", 4) + visibility_key)


def verify_native_lamp_example(read):
    """Check the mixed row representation against the local game data."""
    data = read(EXAMPLE_PATH)
    assert hashlib.sha256(data).hexdigest() == "c199f63e0b75107fe5899ec7f3ce3e38f77b7e1051eda0d5484c27327473c8ca"
    ((_, start, channels, _, end),) = (row for row in _rows(data) if row[3] == 1)
    key = struct.pack("<fHBB", 61200.0, 0, 0, 0)
    assert channels == 2 and data[start:end] == _mixed_row(key, (255, 173, 68), 15)
    assert data[20 + struct.unpack_from("<I", data, 32)[0]:] == bytes(12)


def add_neutral_lamp_track(data, weather):
    if hashlib.sha256(data).hexdigest() != SOURCE_HASHES.get(weather):
        raise ValueError("Expected the matching v0.15 active stage-lamp table.")
    rows = list(_rows(data))
    ((_, start, _, _, footer),) = (row for row in rows if row[3] == weather)
    old = data[start:footer]
    assert len(old) == 24 and struct.unpack_from("<4I", old) == (12, 1, 29, 4)
    assert old[22:] == bytes((1, 0))
    mixed = _mixed_row(old[16:], (255, 255, 255), STUDIO_GAIN)
    result = bytearray(data[:start] + mixed + data[footer:])
    growth = len(mixed) - len(old)
    old_strings = len(data) - 13
    assert data[old_strings:] == bytes(13)
    assert 20 + struct.unpack_from("<I", data, 32)[0] == old_strings + 1
    struct.pack_into("<I", result, 4, len(result))
    struct.pack_into("<I", result, 32, old_strings + 1 + growth - 20)

    # The table entries precede all row data. Each 20-byte footer references
    # the same empty string just before the unchanged 12-byte auxiliary block.
    for entry, row_start, channels, owner, row_footer in rows:
        assert channels == 1 and start > entry
        new_start = row_start + (growth if row_start > start else 0)
        new_footer = row_footer + (growth if row_footer >= footer else 0)
        struct.pack_into("<4I", result, entry, new_start - entry,
                         2 if owner == weather else 1, owner, new_footer - entry)
        for offset in (12, 16):
            assert row_footer + struct.unpack_from("<i", data, row_footer + offset)[0] == old_strings
            struct.pack_into("<i", result, new_footer + offset, old_strings + growth - new_footer)
    return bytes(result)
