"""Embedders behind one interface (product-design.md §17.4): the local ONNX model, or an OpenAI-compatible
``/embeddings`` endpoint at the Assistant's own endpoint.

An embedder turns texts into L2-normalized vectors. ``id`` names the model (and for local models, the exact pinned
file), so an index built by one is never read by another. ``may_embed(memory)`` says which memories it may see: the
local model sees everything (nothing leaves the computer); an endpoint only what the Assistant may send there —
never a private topic to a remote model (``Assistant.may_send``).

``active_embedder(vault)`` picks the one in use: the local model when it is on and ready, else the endpoint's
embeddings when the Assistant is on, its recall switch is on and an embeddings model is set, else None (keyword
search only). MyMemo's hosted model has no embeddings.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from . import local

if TYPE_CHECKING:
    from ..assistant import Assistant
    from ..vault import Memory, Vault

log = logging.getLogger("openmymemo.recall")

ENDPOINT_BATCH = 64
ENDPOINT_MIN_SIMILARITY = 0.25  # OpenAI-style embeddings: unrelated text scores near 0.1, related 0.3 and up


class EmbedError(Exception):
    """The embedder couldn't produce vectors now (runtime missing, endpoint down, limit reached). Search falls
    back to keywords."""


class Embedder(Protocol):
    id: str
    remote: bool
    min_similarity: float

    def may_embed(self, memory: Memory) -> bool: ...

    def embed(self, texts: Sequence[str], kind: str, memory_ids: Sequence[str] = ()) -> list[list[float]]:
        """Vectors for ``texts``; ``kind`` is ``query`` or ``passage`` (``memory_ids``: whose texts they are, for
        the activity log of a remote embedder). Raises EmbedError."""
        ...


def normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else [0.0 for _ in vec]


# ---- the local ONNX model ------------------------------------------------------------------------------------


class OnnxEmbedder:
    """A model from ``local.MODELS`` run with ONNX Runtime on this computer. ``session`` and ``tokenizer`` can be
    given (tests); otherwise they are loaded from the downloaded files on first use."""

    remote = False

    def __init__(self, model: local.LocalModel, session: Any = None, tokenizer: Any = None):
        self.model = model
        pin = local.pin_of(model)
        main = next(f for f in model.files if f.role == "model")
        digest = pin["files"][main.path]["sha256"][:12] if pin else "unpinned"
        self.id = f"local/{model.id}@{digest}"
        self.min_similarity = model.min_similarity
        self._session = session
        self._tokenizer = tokenizer
        self._lock = threading.Lock()

    def may_embed(self, memory: Memory) -> bool:
        return True

    def _load(self) -> None:
        if self._session is not None and self._tokenizer is not None:
            return
        if not local.runtime_available():
            raise EmbedError("ONNX Runtime isn't installed.")
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:
            raise EmbedError("ONNX Runtime isn't installed.") from e
        files = {f.role: local.model_path(self.model, f) for f in self.model.files}
        try:
            tok = Tokenizer.from_file(str(files["tokenizer"]))
            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            session = ort.InferenceSession(str(files["model"]), sess_options=opts,
                                           providers=["CPUExecutionProvider"])
        except Exception as e:  # noqa: BLE001 - a broken download or an unsupported model: no vectors, keywords work
            log.warning("semantic: couldn't load %s (%s)", self.model.id, type(e).__name__)
            raise EmbedError(f"{self.model.name} couldn't be loaded.") from None
        self._tokenizer, self._session = tok, session

    def embed(self, texts: Sequence[str], kind: str, memory_ids: Sequence[str] = ()) -> list[list[float]]:
        if not texts:
            return []
        with self._lock:
            self._load()
            out: list[list[float]] = []
            for i in range(0, len(texts), 16):
                out.extend(self._batch(texts[i:i + 16], kind))
            return out

    def _batch(self, texts: Sequence[str], kind: str) -> list[list[float]]:
        import numpy as np

        m = self.model
        prefix = m.query_prefix if kind == "query" else m.passage_prefix
        tok = self._tokenizer
        tok.enable_truncation(max_length=m.max_tokens)
        pad_id = tok.token_to_id("<pad>")
        if pad_id is None:
            pad_id = tok.token_to_id(m.append_eos) if m.append_eos else 0
        tok.enable_padding(pad_id=pad_id or 0)
        encodings = tok.encode_batch([prefix + t for t in texts])
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        if m.append_eos:  # the last real token must be the end-of-text token (last-token pooling)
            eos = tok.token_to_id(m.append_eos)
            if eos is not None:
                lengths = mask.sum(axis=1)
                for row, n in enumerate(lengths):
                    if n == 0 or ids[row, n - 1] != eos:
                        if n < ids.shape[1]:
                            ids[row, n], mask[row, n] = eos, 1
                        else:
                            ids[row, n - 1] = eos
        feeds: dict[str, Any] = {}
        for inp in self._session.get_inputs():
            if inp.name == "input_ids":
                feeds[inp.name] = ids
            elif inp.name == "attention_mask":
                feeds[inp.name] = mask
            elif inp.name == "token_type_ids":
                feeds[inp.name] = np.zeros_like(ids)
            elif inp.name == "position_ids":
                feeds[inp.name] = np.clip(np.cumsum(mask, axis=1) - 1, 0, None).astype(np.int64)
            else:
                raise EmbedError(f"{m.name} needs an input this version doesn't provide ({inp.name}).")
        outputs = self._session.get_outputs()
        names = [o.name for o in outputs]
        if m.pooling == "output":
            want = "sentence_embedding" if "sentence_embedding" in names else names[-1]
            vecs = np.asarray(self._session.run([want], feeds)[0], dtype=np.float32)
        else:
            want = "last_hidden_state" if "last_hidden_state" in names else names[0]
            hidden = np.asarray(self._session.run([want], feeds)[0], dtype=np.float32)
            vecs = pool(hidden, mask, m.pooling)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        vecs = vecs / np.where(norms > 0, norms, 1.0)
        return [[float(x) for x in row] for row in vecs]


def pool(hidden, mask, how: str):
    """Pool token vectors ``hidden`` [batch, tokens, dims] with ``mask`` [batch, tokens]: mean over real tokens,
    the first token (CLS), or the last real token."""
    import numpy as np

    if how == "cls":
        return hidden[:, 0, :]
    if how == "last":
        last = np.maximum(mask.sum(axis=1) - 1, 0)
        return hidden[np.arange(hidden.shape[0]), last, :]
    weights = mask[:, :, None].astype(np.float32)
    return (hidden * weights).sum(axis=1) / np.maximum(weights.sum(axis=1), 1e-9)


_local: dict[str, OnnxEmbedder] = {}
_local_lock = threading.Lock()


def local_embedder() -> OnnxEmbedder | None:
    """The local model when it is on, downloaded and its runtime installed; one per process (loading takes a
    moment)."""
    s = local.load_semantic()
    if not s.enabled:
        return None
    model = s.local_model
    if not local.is_downloaded(model) or not local.runtime_available():
        return None
    with _local_lock:
        e = _local.get(model.id)
        if e is None:
            e = _local[model.id] = OnnxEmbedder(model)
        return e


def forget_local() -> None:
    with _local_lock:
        _local.clear()


# ---- an OpenAI-compatible /embeddings endpoint -----------------------------------------------------------------


class EndpointEmbedder:
    """The Assistant's own endpoint (a server on this computer, or a provider with the person's key), when it has
    an OpenAI-compatible ``/embeddings`` and an embeddings model is set. Requests count against the Assistant's
    monthly limits and are logged as its calls (feature ``recall``), with the memory ids sent, never the text."""

    def __init__(self, assistant: Assistant):
        self.assistant = assistant
        s = assistant.settings
        self.remote = not s.local
        self.id = f"endpoint/{s.host}/{s.embeddings_model}"
        self.min_similarity = ENDPOINT_MIN_SIMILARITY
        self._private = assistant.vault.private_topics()
        self._connector = assistant.connector()

    def may_embed(self, memory: Memory) -> bool:
        return self._connector is not None and self.assistant.may_send(memory, self._private, self._connector)

    def embed(self, texts: Sequence[str], kind: str, memory_ids: Sequence[str] = ()) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), ENDPOINT_BATCH):
            out.extend(self._request(texts[i:i + ENDPOINT_BATCH], memory_ids[i:i + ENDPOINT_BATCH]))
        return out

    def _request(self, texts: Sequence[str], memory_ids: Sequence[str]) -> list[list[float]]:
        from .. import __version__
        from ..assistant import client
        from ..assistant.core import CONNECTOR_ID
        from ..assistant.settings import is_loopback, record_usage, reserve_call

        a, s = self.assistant, self.assistant.settings
        if not texts:
            return []
        if not a.available("recall") or not s.embeddings_available:
            raise EmbedError(a.unavailable_reason("recall") or "No embeddings model.")
        if s.kind == "local" and not is_loopback(s.endpoint):
            raise EmbedError("A model on this computer must have a local address.")
        capped = reserve_call(s.caps)
        if capped:
            raise EmbedError(f"This month's limit on {capped} is reached.")
        headers = {"User-Agent": f"OpenMyMemo/{__version__}", "Accept": "application/json"}
        if s.secret:
            headers["Authorization"] = f"Bearer {s.secret}"
        result, payload, status = "ok", None, None
        try:
            with client._client(s, min(s.timeout, 30.0)) as http:
                r = http.post(s.endpoint + "/embeddings", json={"model": s.embeddings_model, "input": list(texts)},
                              headers=headers)
                status = r.status_code
                payload = r.json() if r.status_code == 200 else None
        except Exception as e:  # noqa: BLE001 - any transport failure: no vectors this time
            result = "timeout" if "Timeout" in type(e).__name__ else "unreachable"
        vectors: list[list[float]] | None = None
        if result == "ok":
            vectors = _parse_embeddings(payload, len(texts))
            if vectors is None:
                result = "http" if status != 200 else "invalid"
        tokens = None
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            t = payload["usage"].get("total_tokens") or payload["usage"].get("prompt_tokens")
            tokens = t if isinstance(t, int) and t >= 0 else None
        if tokens:
            record_usage(tokens=tokens)
        try:
            a.vault.log_activity(CONNECTOR_ID, "call", feature="recall", model=s.embeddings_model,
                                 endpoint=s.host or None, kind=s.kind, result=result, tokens_in=tokens,
                                 memory_ids=sorted(set(memory_ids)) or None, embeddings=len(texts))
        except OSError:
            pass
        if vectors is None:
            raise EmbedError("The endpoint didn't return embeddings.")
        return vectors


def _parse_embeddings(payload: Any, n: int) -> list[list[float]] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) != n:
        return None
    rows: list[tuple[int, list[float]]] = []
    for pos, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            return None
        vec = item["embedding"]
        if not vec or not all(isinstance(x, int | float) and not isinstance(x, bool) for x in vec):
            return None
        idx = item.get("index", pos)
        rows.append((idx if isinstance(idx, int) else pos, normalize([float(x) for x in vec])))
    rows.sort(key=lambda r: r[0])
    dims = {len(v) for _, v in rows}
    return [v for _, v in rows] if len(dims) == 1 else None


def endpoint_embedder(vault: Vault) -> EndpointEmbedder | None:
    from ..assistant import Assistant

    a = Assistant.from_settings(vault)
    if not a.settings.embeddings_available or not a.available("recall"):
        return None
    return EndpointEmbedder(a)


def active_embedder(vault: Vault) -> Embedder | None:
    """The embedder search uses now, or None (keyword search only). Never raises."""
    try:
        return local_embedder() or endpoint_embedder(vault)
    except Exception as e:  # noqa: BLE001 - settings that can't be read mean no vectors, not a broken search
        log.warning("semantic: no embedder (%s)", type(e).__name__)
        return None
