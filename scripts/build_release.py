"""Build a deterministic, component-only manual installation archive."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "sse_ezona"


def build(tag=None):
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    version = manifest["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("Manifest needs a stable semantic version")
    if tag is not None and tag != f"v{version}":
        raise SystemExit("Tag does not match manifest version")
    changelog = (ROOT / "CHANGELOG.md").read_text()
    match = re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if not match:
        raise SystemExit("Missing dated changelog entry for this version")
    files = []
    for file in sorted(COMPONENT.rglob("*")):
        if not file.is_file() or "__pycache__" in file.parts or file.suffix == ".pyc":
            continue
        parts = file.relative_to(COMPONENT).parts
        allowed = (len(parts) == 1 and (file.suffix == ".py" or file.name in {"manifest.json", "strings.json"})
                   or len(parts) == 2 and parts[0] == "translations" and file.suffix == ".json"
                   or len(parts) == 2 and parts[0] == "brand" and file.suffix in {".png", ".svg"})
        if not allowed or file.is_symlink():
            raise SystemExit(f"Unexpected component file: {file.relative_to(COMPONENT)}")
        files.append(file)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    archive = dist / f"ha-sse-ezona-{version}.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zipped:
        for file in files:
            item = ZipInfo(str(file.relative_to(ROOT)), date_time=(2026, 1, 1, 0, 0, 0))
            item.compress_type = ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            zipped.writestr(item, file.read_bytes())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (dist / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n")
    (dist / "RELEASE_NOTES.md").write_text(match.group(1).strip() + "\n")
    print(f"Built {archive.name}: {len(files)} component files; SHA-256 {digest}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Require this vX.Y.Z tag to match the manifest")
    build(parser.parse_args().tag)
