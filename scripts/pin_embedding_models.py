"""Pin the local embedding models (product-design.md §17.4): resolve each model's current Hugging Face revision,
download every file at that revision, and record its SHA-256 and size in src/openmymemo/recall/model_pins.json.

OpenMyMemo refuses to download a model until it is pinned here (it fails closed), so a maintainer runs this once
for each model in `recall/local.py`'s list, and again after changing a model's repository or files:

    uv run python scripts/pin_embedding_models.py              # every model: print the table
    uv run python scripts/pin_embedding_models.py --write      # ... and write model_pins.json
    uv run python scripts/pin_embedding_models.py --model multilingual-e5-small --write

Review the printed table (repository, revision, sizes), then commit model_pins.json. Downloads go to a temporary
folder and are deleted afterwards; nothing is written into any OpenMyMemo folder. A model whose files can't be
fetched (a wrong path, or a repository gated behind terms) is reported and left unpinned. Set HF_TOKEN to fetch
from a gated repository you have accepted the terms of.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from openmymemo.recall import local  # noqa: E402


def _headers() -> dict[str, str]:
    h = {"User-Agent": "OpenMyMemo-pin-script"}
    token = os.environ.get("HF_TOKEN", "").strip()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def revision(client: httpx2.Client, model: local.LocalModel) -> str:
    r = client.get(f"{local.HF}/api/models/{model.repo}/revision/main", headers=_headers())
    r.raise_for_status()
    sha = r.json().get("sha")
    if not isinstance(sha, str) or len(sha) != 40:
        raise RuntimeError(f"no revision for {model.repo}")
    return sha


def fetch(client: httpx2.Client, url: str, folder: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with client.stream("GET", url, headers=_headers()) as r:
        r.raise_for_status()
        with open(folder / "download", "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                h.update(chunk)
                fh.write(chunk)
                size += len(chunk)
                print(f"\r    {size / 2**20:,.0f} MB", end="", file=sys.stderr, flush=True)
    print("", file=sys.stderr)
    (folder / "download").unlink()
    return h.hexdigest(), size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", action="append", choices=[m.id for m in local.MODELS],
                        help="Pin only this model (repeatable).")
    parser.add_argument("--write", action="store_true", help="Write src/openmymemo/recall/model_pins.json.")
    args = parser.parse_args()
    chosen = [m for m in local.MODELS if not args.model or m.id in args.model]
    pins = local.load_pins()
    failed = []
    with httpx2.Client(follow_redirects=True, timeout=httpx2.Timeout(120.0, connect=20.0)) as client, \
            tempfile.TemporaryDirectory() as tmp:
        for model in chosen:
            print(f"{model.id}  ({model.repo})", file=sys.stderr)
            try:
                rev = revision(client, model)
                files = {}
                for f in model.files:
                    print(f"  {f.path}", file=sys.stderr)
                    sha, size = fetch(client, model.url(f, rev), Path(tmp))
                    files[f.path] = {"sha256": sha, "size": size}
            except (httpx2.HTTPError, RuntimeError, OSError, ValueError) as e:
                print(f"  FAILED: {type(e).__name__}: {e}", file=sys.stderr)
                failed.append(model.id)
                continue
            pins[model.id] = {"repo": model.repo, "revision": rev, "files": files}
    print("\n| Model | Revision | File | Size | SHA-256 |\n| --- | --- | --- | --- | --- |")
    for model in chosen:
        pin = pins.get(model.id)
        if model.id in failed or not pin:
            print(f"| {model.id} | (not pinned) | | | |")
            continue
        for path, entry in pin["files"].items():
            print(f"| {model.id} | {pin['revision'][:12]} | {path} | {entry['size']:,} | {entry['sha256']} |")
    if args.write:
        out = {"format": 1, "models": {k: pins[k] for k in sorted(pins)}}
        local.PINS_PATH.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
        print(f"\nWrote {local.PINS_PATH.relative_to(ROOT)}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
