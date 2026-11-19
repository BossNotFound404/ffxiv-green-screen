"""Extend the audited white floor MDL with four inside walls and a ceiling."""
import hashlib
import struct


FLOOR_SHA256 = "af85360f0307ae777de0302877ee4b7e59e52c5bbb4e13230dfc4d173ae3a850"


def add_white_cyclorama(floor: bytes) -> bytes:
    if hashlib.sha256(floor).hexdigest() != FLOOR_SHA256:
        raise ValueError("Expected the single-mesh 46/floor.mdl from Matched Stage v0.15")

    # This floor has one material, one LOD, one mesh, no submeshes, no edge
    # geometry. Stream 0 contains half4 positions; stream 1 contains all other
    # attributes. The five new faces can share the original draw and material.
    runtime = 68 + struct.unpack_from("<I", floor, 4)[0]
    model = runtime + 8 + struct.unpack_from("<I", floor, runtime + 4)[0]
    lod = model + 56
    mesh = lod + 3 * 60
    vertex_at = struct.unpack_from("<I", floor, 16)[0]
    index_at = struct.unpack_from("<I", floor, 28)[0]
    vertex_count = struct.unpack_from("<H", floor, mesh)[0]
    index_count = struct.unpack_from("<I", floor, mesh + 4)[0]
    attr_at = vertex_at + struct.unpack_from("<I", floor, mesh + 24)[0]
    assert (vertex_count, index_count) == (449, 2496)
    assert attr_at == vertex_at + 8 * vertex_count and index_at == attr_at + 20 * vertex_count
    assert floor[mesh + 32:mesh + 36] == bytes((8, 20, 0, 2))
    assert index_at + 2 * index_count == len(floor)

    # The placement is at the origin; the recording mark is at world (100,0,100).
    # The walls reach just below the receiving floor and there is no lower face.
    corners = [(x, y, z) for y in (-12, 256) for x, z in
               [(-156, -156), (356, -156), (356, 356), (-156, 356)]]
    faces = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7), (4, 5, 6, 7)]
    positions = b"".join(struct.pack("<4e", *point, 1) for point in corners)
    # Use the center vertex's upright shading normal, color and UV on every face
    # so the flat backdrop samples the same material as the center of the floor.
    center_attrs = floor[index_at - 20:index_at]
    assert struct.unpack("<4e4B4e", center_attrs) == (0, 1, 0, 0, 127, 127, 127, 0, .5, .5, .5, .5)
    added_indices = [vertex_count + i for a, b, c, d in faces for i in (a, b, c, a, c, d)]
    indices = floor[index_at:] + struct.pack("<30H", *added_indices)
    vertices = floor[vertex_at:attr_at] + positions + floor[attr_at:index_at] + center_attrs * len(corners)

    header = bytearray(floor[:vertex_at])
    # The roof shares this emissive model's draw. Disable its global and local
    # light shadow passes so the enclosure cannot cast a roof shadow on actors.
    # This changes neither material, visible mesh depth nor the decal receiver.
    assert header[model + 23] == 0x04
    header[model + 23] |= 0x03  # ModelFlags1.ShadowDisabled | LightShadowDisabled
    new_index_at = vertex_at + len(vertices)
    for field, value in [(28, new_index_at), (40, len(vertices)), (52, len(indices)),
                         (lod + 32, new_index_at), (lod + 44, len(vertices)), (lod + 48, len(indices)),
                         (lod + 56, new_index_at), (mesh + 4, index_count + len(added_indices)),
                         (mesh + 24, attr_at - vertex_at + len(positions))]:
        struct.pack_into("<I", header, field, value)
    struct.pack_into("<H", header, mesh, vertex_count + len(corners))
    # Update the two populated bounding boxes. All new corners already fit
    # within their X/Z bounds and the native model's 1405-unit culling radius.
    for box in (vertex_at - 128, vertex_at - 96):
        struct.pack_into("<f", header, box + 4, -12)
        struct.pack_into("<f", header, box + 20, 256)
    return bytes(header) + vertices + indices
