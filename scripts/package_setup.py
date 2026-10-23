"""Generate or verify the ASCII console chunks; never execute installers."""
import argparse
import base64
import gzip
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "system-setup"


def files(archives=None):
    result = {}
    for script, folder, remote in (
        ("01-init-alpine.sh", "01-INIT-CHUNKS", "/tmp/01-init-alpine.sh"),
        ("02-install-debian13.sh", "02-DEBIAN-CHUNKS", "/root/02-install-debian13.sh"),
    ):
        data = (ROOT / script).read_bytes()
        assert data.isascii() and b"\r" not in data
        compressed = (archives or {}).get(script)
        if compressed is None:
            compressed = bytearray(gzip.compress(data, mtime=0))
            compressed[9] = 255
            compressed = bytes(compressed)
        assert gzip.decompress(compressed) == data
        parts = []
        for index, offset in enumerate(range(0, len(compressed), 1050), 1):
            chunk = compressed[offset:offset + 1050]
            target = f"/tmp/{script}.{index:02d}.part"
            parts.append(target)
            command = f"base64 -d > {target} <<'DATA_END'\n{base64.encodebytes(chunk).decode()}DATA_END\n"
            command += f"echo '{hashlib.sha256(chunk).hexdigest()}  {target}' | sha256sum -c -\n"
            result[f"{folder}/{index:02d}-PART.txt"] = command.encode("ascii")
        command = f"cat {' '.join(parts)} | gzip -dc > {remote} &&\n"
        command += f"echo '{hashlib.sha256(data).hexdigest()}  {remote}' | sha256sum -c - &&\nsh {remote}\n"
        result[f"{folder}/99-CHECK-AND-RUN.txt"] = command.encode("ascii")
    assert all(len(data) < 2000 for data in result.values())
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    archives = {}
    if args.check:
        # zlib versions produce different valid deflate streams; check content.
        for script, folder in (("01-init-alpine.sh", "01-INIT-CHUNKS"), ("02-install-debian13.sh", "02-DEBIAN-CHUNKS")):
            parts = []
            for path in sorted((ROOT / folder).glob("*-PART.txt")):
                encoded = path.read_bytes().split(b"'DATA_END'\n", 1)[1].split(b"DATA_END\n", 1)[0]
                parts.append(base64.b64decode(b"".join(encoded.split()), validate=True))
            archives[script] = b"".join(parts)
    expected = files(archives)
    for name, content in expected.items():
        path = ROOT / name
        if args.check:
            assert path.read_bytes() == content, name
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    actual = {p.relative_to(ROOT).as_posix() for folder in ("01-INIT-CHUNKS", "02-DEBIAN-CHUNKS") for p in (ROOT / folder).glob("*.txt")}
    assert actual == set(expected), "Unexpected stale chunk files"
    manifest = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(ROOT).as_posix()}\n" for p in sorted(ROOT.rglob("*"), key=lambda p: p.relative_to(ROOT).as_posix()) if p.is_file() and p.name != "SHA256SUMS")
    if args.check:
        assert (ROOT / "SHA256SUMS").read_text(encoding="utf-8") == manifest
    else:
        (ROOT / "SHA256SUMS").write_text(manifest, encoding="utf-8", newline="\n")
    print("PASS: ASCII chunks, size limits, exact contents and manifest")


if __name__ == "__main__":
    main()
