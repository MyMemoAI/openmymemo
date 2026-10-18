"""Recall: search that understands meaning (product-design.md §17.4). Everything here is optional and falls back
to keyword search; with no index and no model help, ``search`` returns exactly what ``context.search`` returns.

* ``search`` (``hybrid.py``): hybrid search (keywords fused with vectors by reciprocal rank fusion), with the Assistant's query
  expansion and rerank when ``model=True``. Used by ``memory_search``, ``openmymemo search`` and the panel.
* ``recall``: ranked, budget-cut text for prompts and hooks (``openmymemo recall``).
* ``similar``: the memories closest in meaning to a text, for checking proposals and tidy-up; ``[]`` without an
  embedder (fall back to ``textutil.similarity``).
* ``build_index`` / ``EmbeddingIndex``: the plain-file cache in ``.openmymemo/index/`` (``index.py``).
* ``active_embedder``: the local ONNX model or the Assistant endpoint's ``/embeddings`` (``embed.py``).
* ``local``: the list of local models, pinned downloads, the optional runtime; ``semantic``: the switch.
"""

from .embed import Embedder, EmbedError, active_embedder
from .hybrid import Hit, Result, build_index, recall, rrf, search, similar
from .index import EmbeddingIndex, Refreshed, Snapshot, text_hash

__all__ = [
    "EmbedError", "Embedder", "EmbeddingIndex", "Hit", "Refreshed", "Result", "Snapshot", "active_embedder",
    "build_index", "recall", "rrf", "search", "similar", "text_hash",
]
