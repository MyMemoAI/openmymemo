"""The recall test set in CI (product-design.md §17.4): hit@k for keyword search, keyword + vectors (a deterministic
stand-in embedder), and with the Assistant's expansion and rerank (recorded answers). A change that makes any of
them worse than baseline.json fails here; the table is printed (``pytest -s``) so improvements are visible too."""

import json
from pathlib import Path

from recall_harness import build_vault, load_set, run_all, table

from openmymemo.assistant import client

BASELINE = json.loads((Path(__file__).parent / "baseline.json").read_text(encoding="utf-8"))


def test_recall_hit_rates_do_not_fall_below_the_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "TRANSPORT", None)  # restored after the replay endpoint the harness sets
    _, questions = load_set()
    assert len([q for q in questions if q["lang"] == "en"]) >= 15 and len([q for q in questions if q["lang"] == "zh"]) >= 12
    results = run_all(build_vault(tmp_path / "vault"), questions)
    print("\n" + table(results))
    for mode, want in BASELINE.items():
        for k, langs in want.items():
            for lang, value in langs.items():
                assert results[mode][k][lang] >= value, (mode, k, lang, results[mode][k][lang], value)
    # What the Assistant and vectors are for: they find what keywords miss.
    assert results["vectors"]["hit@5"]["all"] > results["keyword"]["hit@5"]["all"]
    assert results["expand+rerank"]["hit@5"]["all"] > results["keyword"]["hit@5"]["all"]
