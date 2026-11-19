"""Remove duplicate blended sky triangles from the four AVFX backdrops."""
import argparse
import io
import json
import struct
import zipfile
from pathlib import Path

from avfx_chunks import chunk, chunks, rewrite
from build_448_material_pair import encode, read_package


STABLE_CONTENTS = "8618f1233859f01379f806a11ac4e0c946d37deec959bd0a535aa5814f728696"


def single_pass_sky(effect):
    (root, body), = chunks(effect)
    assert root == "AVFX"
    particles = [dict(chunks(data)) for tag, data in chunks(body) if tag == "Ptcl"]
    floor, sky = particles
    assert "SS" in dict(chunks(floor["Data"]))
    assert dict(chunks(sky["Data"])) == {"MNO": struct.pack("<i", 0)}
    # VFXEditor calls these settings Cull=None, Draw Mode=Blend and Depth Write=off.
    for name in ("CulT", "RMT", "DsDw"):
        assert sky[name] == struct.pack("<i", 0)
    model, = [data for tag, data in chunks(body) if tag == "Modl"]
    fields = dict(chunks(model))
    assert len(fields["VDrw"]) == 8 * 36
    triangles = list(struct.iter_unpack("<3H", fields["VIdx"]))
    assert len(triangles) == 20
    front = []
    for start in range(0, len(triangles), 4):
        first, second, third, fourth = triangles[start:start + 4]
        assert third == first[::-1] and fourth == second[::-1]
        front.extend(first + second)
    # Cull=None already draws each triangle from either side. Submitting its
    # reverse as well composites sky opacity twice while the floor fades once.
    model = rewrite(model, {"VIdx": struct.pack("<30H", *front)})
    return chunk(root, rewrite(body, {"Modl": model}))


def build(baseline):
    assets = read_package(baseline, STABLE_CONTENTS)
    for color in ("black", "red", "green", "blue"):
        local = f"sky/{color}.avfx"
        assets[local] = single_pass_sky(assets[local])
    meta = json.loads(assets["meta.json"])
    meta.update(Name="Round Table Studio - Capture Stage v0.17", Version="0.17.0",
                Description="修正黑、红、绿、蓝棚天幕在渐变时重复叠色。白棚沿用 v0.16 的实体布景与原生过渡。")
    assets["meta.json"] = encode(meta)
    return assets


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    assets = build(args.baseline)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in sorted(assets.items()):
            archive.writestr(zipfile.ZipInfo(name), data, zipfile.ZIP_DEFLATED, compresslevel=9)
    with args.output.open("xb") as output:
        output.write(buf.getvalue())
