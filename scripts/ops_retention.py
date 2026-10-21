"""Explicit offline trace retention only. Never delete profiles, DB rows or containers."""
import argparse
import os
from pathlib import Path
import time


def clean(root, days, *, apply=False, now=None):
    if root.is_symlink() or not root.is_dir() or not 1 <= days <= 30:
        raise ValueError("explicit real trace directory and 1..30 days required")
    cutoff = (time.time() if now is None else now) - days * 86400
    candidates = []
    # Only known trace formats; do not recurse through symlink directories.
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
        for name in files:
            file = Path(directory) / name
            if file.is_symlink() or file.suffix not in {".jsonl", ".zip", ".trace"}:
                continue
            info = file.stat()
            if info.st_mtime < cutoff:
                candidates.append(file)
                if apply:
                    file.unlink()
    return len(candidates)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--days", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--quiesced", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.quiesced:
        parser.error("stop trace writers and pass --quiesced before applying")
    print("eligible trace files:", clean(args.root, args.days, apply=args.apply))
