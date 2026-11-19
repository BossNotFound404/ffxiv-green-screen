"""Check rebuilt PMP contents against the original milestone snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def fingerprint(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names) or archive.testzip() is not None:
            raise ValueError(f"Invalid archive: {path.name}")
        member_hashes = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in names}
        for name in names:
            if name.endswith(".json"):
                doc = json.loads(archive.read(name))
                for container in [doc, *doc.get("Options", [])]:
                    for local in container.get("Files", {}).values():
                        if local not in member_hashes:
                            raise ValueError(f"Missing {local} in {path.name}")
    data = json.dumps(member_hashes, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--all", action="store_true", help="Require the complete set of ten packages")
    args = parser.parse_args()
    reference = Path(__file__).resolve().parent / "docs/evidence/packages.json"
    records = json.loads(reference.read_text(encoding="utf-8"))
    packages = sorted(args.directory.glob("*.pmp"))
    if not packages or (args.all and {p.name for p in packages} != records.keys()):
        parser.error("No packages found or collection incomplete.")
    for path in packages:
        if path.name not in records or fingerprint(path) != records[path.name]["contents_sha256"]:
            raise SystemExit(f"Different from archived contents: {path.name}")
        print("OK", path.name)


if __name__ == "__main__":
    main()
