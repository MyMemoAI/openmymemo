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

    def embed(self, texts: Sequence[str], kind: str, memory_ids: Sequence[str] = ()) -> list[list[float] | None]:
        """Vectors for ``texts``; ``kind`` is ``query`` or ``passage`` (``memory_ids``: whose texts they are,
        required for passages sent to an endpoint). A None vector: that text may not be sent now, so it wasn't.
        Raises EmbedError."""
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
            elif inp.name.startswith("past_key_values."):
                # A decoder exported with its key/value cache (Qwen3): there is no past, so each one is empty.
                feeds[inp.name] = np.zeros(empty_past(inp.shape, len(ids)),
                                           dtype=np.float16 if inp.type == "tensor(float16)" else np.float32)
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


def empty_past(shape: Sequence[Any], batch: int) -> list[int]:
    """The shape of an empty key/value cache input, [batch, heads, past tokens, head dims]: the first named
    dimension is the batch, any other named one (the past tokens) is 0, and fixed ones stay."""
    return [d if isinstance(d, int) else (batch if i == 0 else 0) for i, d in enumerate(shape)]


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


def endpoint_id(settings) -> str:
    """The embedder id for an endpoint: the full normalized address (scheme, host, port, path; never credentials)
    and the model, so two servers that use the same model name never share an index or cached query vectors."""
    from urllib.parse import urlsplit

    parts = urlsplit(settings.endpoint)
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    path = parts.path.rstrip("/")
    return f"endpoint/{parts.scheme.lower()}://{host}{port}{path}/{settings.embeddings_model}"


class EndpointEmbedder:
    """The Assistant's own endpoint (a server on this computer, or a provider with the person's key), when it has
    an OpenAI-compatible ``/embeddings`` and an embeddings model is set. Requests count against the Assistant's
    monthly limits and are logged as its calls (feature ``recall``), with the memory ids sent, never the text.

    What may be sent is decided again for every request, from the settings, connector and memory files as they are
    at that moment (``_live``): a long indexing run never sends a batch on an earlier decision. Memories that may
    no longer be sent are left out of the request (their vector is None); a change of endpoint, model or locality
    stops the embedder."""

    def __init__(self, assistant: Assistant):
        self.assistant = assistant
        s = assistant.settings
        self.remote = not s.local
        self.id = endpoint_id(s)
        self.min_similarity = ENDPOINT_MIN_SIMILARITY
        self._guard = assistant.guard()  # the send rules when this embedder was made (may_embed only)
        self._withheld: list[str] | None = None

    def _withheld_texts(self) -> list[str]:
        if self._withheld is None:
            self._withheld = self._guard.withheld()[2] if self._guard is not None else []
        return self._withheld

    def may_embed(self, memory: Memory) -> bool:
        """What the Assistant may send: active, in its read scope, not private (a model on this computer gets the
        private topics the person opted in), and not containing text it may not send. Checked again, live, before
        each request."""
        from ..assistant.guard import contains_any

        g = self._guard
        return (g is not None and memory.status == "active" and g.memory_ok(memory)
                and not contains_any(memory.content, self._withheld_texts()))

    def _live(self):
        """The Assistant, its settings and its send guard as they are now; EmbedError when this embedder is no
        longer the one configured (another endpoint, model or locality) or nothing may be sent."""
        from ..assistant import Assistant
        from ..assistant.settings import is_loopback, load_settings

        s = load_settings()
        a = Assistant(self.assistant.vault, s)
        if not s.embeddings_available or not a.available("recall"):
            raise EmbedError(a.unavailable_reason("recall") or "No embeddings model.")
        if endpoint_id(s) != self.id or (not s.local) != self.remote:
            raise EmbedError("The embeddings settings changed.")
        if s.kind == "local" and not is_loopback(s.endpoint):
            raise EmbedError("A model on this computer must have a local address.")
        g = a.guard()
        if g is None:
            raise EmbedError("The Assistant's access was removed.")
        return a, s, g

    def embed(self, texts: Sequence[str], kind: str, memory_ids: Sequence[str] = ()) -> list[list[float] | None]:
        out: list[list[float] | None] = []
        for i in range(0, len(texts), ENDPOINT_BATCH):
            out.extend(self._request(texts[i:i + ENDPOINT_BATCH], memory_ids[i:i + ENDPOINT_BATCH], kind))
        return out

    def _log(self, a, s, result: str, memory_ids: Sequence[str], n: int, tokens: int | None = None) -> None:
        from ..assistant.core import CONNECTOR_ID

        try:
            a.vault.log_activity(CONNECTOR_ID, "call", feature="recall", model=s.embeddings_model,
                                 endpoint=s.host or None, kind=s.kind, result=result, tokens_in=tokens,
                                 memory_ids=sorted(set(memory_ids)) or None, embeddings=n)
        except OSError:
            pass

    def _request(self, texts: Sequence[str], memory_ids: Sequence[str], kind: str) -> list[list[float] | None]:
        from .. import __version__
        from ..assistant import Data, client
        from ..assistant.guard import contains_any
        from ..assistant.settings import record_usage, reserve_call
        from ..vault import NotFound

        if not texts:
            return []
        a, s, g = self._live()
        withheld = g.withheld()[2]
        vault = a.vault
        keep: list[int] = []
        by_memory = bool(memory_ids)  # memories' texts: each checked against the memory as it is now
        if by_memory:
            if len(memory_ids) != len(texts):
                raise EmbedError("Each text needs its memory id.")
            for i, (text, mid) in enumerate(zip(texts, memory_ids, strict=True)):
                try:
                    m = vault.get(mid)
                except NotFound:
                    continue
                if (m.status == "active" and m.content == text and g.memory_ok(m)
                        and not contains_any(text, withheld)):
                    keep.append(i)
        else:
            keep = [i for i, text in enumerate(texts) if not contains_any(text, withheld)]
        dropped = len(texts) - len(keep)
        if dropped:
            self._log(a, s, "refused_private", [memory_ids[i] for i in range(len(memory_ids)) if i not in keep],
                      dropped)
        if not keep:
            if not by_memory:
                raise EmbedError("That includes text that may not be sent to this model.")
            return [None] * len(texts)
        send = [texts[i] for i in keep]
        ids = [memory_ids[i] for i in keep] if by_memory else []
        blocks = [Data(t, memory_ids=(mid,)) for t, mid in zip(send, ids, strict=True)] if ids else [Data(t) for t in send]
        if g.refusal(blocks) is not None:  # the Assistant's own last check of everything going out
            self._log(a, s, "refused_private", ids, len(send))
            raise EmbedError("That includes text that may not be sent to this model.")
        capped = reserve_call(s.caps)
        if capped:
            raise EmbedError(f"This month's limit on {capped} is reached.")
        headers = {"User-Agent": f"OpenMyMemo/{__version__}", "Accept": "application/json"}
        if s.secret:
            headers["Authorization"] = f"Bearer {s.secret}"
        result, payload, status = "ok", None, None
        try:
            with client._client(s, min(s.timeout, 30.0)) as http:
                r = http.post(s.endpoint + "/embeddings", json={"model": s.embeddings_model, "input": send},
                              headers=headers)
                status = r.status_code
                payload = r.json() if r.status_code == 200 else None
        except Exception as e:  # noqa: BLE001 - any transport failure: no vectors this time
            result = "timeout" if "Timeout" in type(e).__name__ else "unreachable"
        vectors: list[list[float]] | None = None
        if result == "ok":
            vectors = _parse_embeddings(payload, len(send))
            if vectors is None:
                result = "http" if status != 200 else "invalid"
        tokens = None
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            t = payload["usage"].get("total_tokens") or payload["usage"].get("prompt_tokens")
            tokens = t if isinstance(t, int) and t >= 0 else None
        if tokens:
            record_usage(tokens=tokens)
        self._log(a, s, result, ids, len(send), tokens)
        if vectors is None:
            raise EmbedError("The endpoint didn't return embeddings.")
        out: list[list[float] | None] = [None] * len(texts)
        for i, vec in zip(keep, vectors, strict=True):
            out[i] = vec
        return out


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
