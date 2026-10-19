"""Check that every pinned local embedding model (product-design.md §17.4) loads and embeds with the installed
ONNX Runtime: download it the way the app does (checksums verified), embed a question and two memories, and check
that the matching memory scores higher.

    uv run --with onnxruntime --with tokenizers python scripts/check_local_models.py [<folder>]

CI runs it on an Intel Mac, where the installer picks ONNX Runtime 1.23, its last release for Intel Macs. The
models go into <folder> (default: a temporary one) as OpenMyMemo's settings folder, so a cache can keep them.
"""

import os
import sys
import tempfile


def report(level: str, text: str) -> None:
    """Print a line, as a GitHub annotation when running in GitHub Actions (so it shows without the log)."""
    print(f"::{level}::{text}" if os.environ.get("GITHUB_ACTIONS") else text, flush=True)


def main() -> None:
    os.environ["OPENMYMEMO_CLOUD_DIR"] = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()
    import onnxruntime

    from openmymemo.recall import embed, local

    report("notice", f"ONNX Runtime {onnxruntime.__version__}, Python {sys.version.split()[0]}")
    failed = []
    for model in local.MODELS:
        if local.pin_of(model) is None:
            report("notice", f"{model.id}: not pinned, skipped")
            continue
        try:
            local.ensure_downloaded(model)
            embedder = embed.OnnxEmbedder(model)
            [query] = embedder.embed(["What do I like to drink?"], "query")
            tea, city = embedder.embed(["Prefers tea over coffee", "Lives in Berlin"], "document")
        except Exception as e:  # noqa: BLE001 - every model is checked and reported
            report("error", f"{model.id}: {type(e).__name__}: {str(e)[:300]}")
            failed.append(model.id)
            continue
        score = [sum(a * b for a, b in zip(query, doc, strict=True)) for doc in (tea, city)]
        ok = score[0] > score[1]
        report("notice" if ok else "error",
               f"{model.id}: {len(query)} dimensions, tea {score[0]:.3f} vs Berlin {score[1]:.3f}")
        if not ok:
            failed.append(model.id)
    if failed:
        sys.exit(f"Failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
