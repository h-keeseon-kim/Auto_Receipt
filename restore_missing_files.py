#!/usr/bin/env python3
"""Restore four missing project files, without replacing settings.py or deleting anything."""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path
import sys

EXPECTED = {'__init__.py': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 'asgi.py': 'babaea838a9d967a949d1beb3746c190ed6dc88a2f67a3cf962d968436e38ae2', 'urls.py': 'b818b11c7a5df7bd12cdc4c6647e2f9e91654b7d76b4ba966aeb3b4a5b42df15', 'wsgi.py': '3c874ddc534080c5d3978324a26730f617edf90a634e44e2f3e6a335069a75ce', 'settings.py': '8a0b202a83c27878d25713ffa95b3a7eba43d59c29792d8e882760fdeb14eb2f'}
RESTORE = ("__init__.py", "asgi.py", "urls.py", "wsgi.py")

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="Project directory containing manage.py")
    parser.add_argument("--check", action="store_true", help="Validate and show planned actions without changing files")
    args = parser.parse_args()
    root = args.project.expanduser().resolve()
    source = Path(__file__).resolve().parent / "auto_receipt"
    target = root / "auto_receipt"
    if not (root / "manage.py").is_file() or not (root / "receipts" / "urls.py").is_file():
        parser.error("The project directory must contain manage.py and receipts/urls.py.")
    if target.is_symlink() or not target.is_dir():
        parser.error("auto_receipt must be an existing ordinary directory, not a symlink.")
    if not (target / "settings.py").is_file():
        parser.error("Existing settings.py is missing. This script intentionally does not replace settings.")
    missing = []
    # Complete validation before the first write. Different existing files are never overwritten.
    for name in RESTORE:
        payload = source / name
        dest = target / name
        if not payload.is_file() or digest(payload) != EXPECTED[name]:
            parser.error("Invalid recovery payload: " + name)
        if dest.is_symlink():
            parser.error("Refusing symlink destination: " + name)
        if dest.exists():
            if not dest.is_file() or digest(dest) != EXPECTED[name]:
                parser.error("Existing file differs from the known v1.16.4 file; no files changed: " + name)
            print("UNCHANGED " + str(dest))
        else:
            missing.append(name)
            print(("WOULD RESTORE " if args.check else "RESTORE ") + str(dest))
    if not args.check:
        for name in missing:
            # Exclusive creation also prevents a concurrently created file being overwritten.
            with (target / name).open("xb") as out:
                out.write((source / name).read_bytes())
    print("settings.py preserved; database, migrations, receipts, and Railway configuration untouched.")
    print("Check complete." if args.check else "Restore complete. Commit the four restored files to your repository.")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as exc:
        print("Restore stopped: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
