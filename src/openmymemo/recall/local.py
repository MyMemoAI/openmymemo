"""Local embedding models: "Search by meaning on this computer" (product-design.md §17.4). Opt-in, never bundled.

* **The list** (``MODELS``) names each model's Hugging Face repository and files, its download size, languages,
  license, and how it is used (prefixes, pooling). The revision and the SHA-256 of every file are pinned in
  ``model_pins.json`` next to this file, written by ``scripts/pin_embedding_models.py``. A model without a pin is
  listed but can't be turned on ("not available yet"): the downloader fails closed.
* **Downloads** go into the per-computer application folder (``cloud.cloud_dir()/models/<id>/``), never into the
  memory folder. A file is written as ``.part``, hashed while it streams, and kept only when its size and SHA-256
  match the pin; otherwise it is deleted.
* **The runtime** (``onnxruntime`` + ``tokenizers``, the ``openmymemo[semantic]`` extra) is used from the
  environment OpenMyMemo runs in when it's there. Otherwise turning the switch on installs it into a folder of its
  own next to the models (``semantic-runtime/<python>-<platform>/``) with ``uv pip install --target`` (or pip),
  which survives OpenMyMemo's own updates and works however OpenMyMemo was installed. Without uv or pip the person
  is shown the exact command (``install_command``).
* **The choice** is kept per computer in ``semantic.json`` next to ``assistant.json``: on or off, the model, and
  which models' terms the person accepted. Turning it off deletes the downloaded model (and the caller deletes the
  index); the model choice stays for next time.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2

log = logging.getLogger("openmymemo.recall")

PINS_PATH = Path(__file__).with_name("model_pins.json")
HF = "https://huggingface.co"
RUNTIME_REQUIREMENTS = ("onnxruntime>=1.20", "tokenizers>=0.20")
INSTALL_TIMEOUT = 900.0
HTTP_TRANSPORT: httpx2.BaseTransport | None = None  # tests put a fake server here
CHUNK = 1 << 20


class SemanticError(Exception):
    """Why the local model can't be turned on or used; the message is for the person. ``code`` is stable."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ModelFile:
    path: str  # inside the repository, e.g. "onnx/model_quantized.onnx"
    role: str  # "model", "data" (the model's external weights) or "tokenizer"


@dataclass(frozen=True)
class LocalModel:
    """One choice in Settings → Assistant → "Search by meaning on this computer"."""

    id: str
    name: str
    repo: str
    files: tuple[ModelFile, ...]
    size_mb: int  # approximate download, shown before starting (the pin has the exact bytes)
    dims: int
    languages: str
    license: str
    license_url: str
    pooling: str  # "mean", "cls", "last" (last real token), or "output" (the model outputs sentence_embedding)
    query_prefix: str = ""
    passage_prefix: str = ""
    max_tokens: int = 512
    append_eos: str | None = None  # a token to end every input with, when the model card asks for it
    min_similarity: float = 0.3  # below this, a memory isn't "related" to the query (cosine, normalized vectors)
    terms_url: str | None = None  # terms the person must accept in the app before the download
    recommended: bool = False
    note: str = ""

    def url(self, file: ModelFile, revision: str) -> str:
        return f"{HF}/{self.repo}/resolve/{revision}/{file.path}"


# Usage per model card (as known at the time of writing; the recall test set and the pin script are where a
# maintainer confirms them):
# * multilingual-e5-small: "query: " / "passage: " prefixes, mean pooling over the attention mask, L2-normalize.
#   Its cosine scores sit high (unrelated text is often ~0.7), hence the high threshold.
# * granite-embedding-97m-multilingual-r2: no prefixes, CLS pooling, normalize (IBM's model cards for the granite
#   embedding family). The ONNX file path in IBM's repository is the least certain entry here.
# * EmbeddingGemma-300m: task prompts "task: search result | query: " and "title: none | text: "; the ONNX export
#   from onnx-community outputs the pooled, projected and normalized `sentence_embedding` itself.
# * Qwen3-Embedding-0.6B: an instruction for queries only ("Instruct: …\nQuery:"), last-token pooling with
#   <|endoftext|> at the end, normalize.
MODELS: tuple[LocalModel, ...] = (
    LocalModel(
        id="multilingual-e5-small", name="multilingual-e5-small (int8)", repo="Xenova/multilingual-e5-small",
        files=(ModelFile("onnx/model_quantized.onnx", "model"), ModelFile("tokenizer.json", "tokenizer")),
        size_mb=135, dims=384, languages="About 100 languages, including English and Chinese", license="MIT",
        license_url="https://huggingface.co/intfloat/multilingual-e5-small", pooling="mean",
        query_prefix="query: ", passage_prefix="passage: ", min_similarity=0.8, recommended=True,
        note="Recommended. Small and fast: embeds a 1,000-memory folder in well under a minute on a laptop."),
    LocalModel(
        id="granite-embedding-97m-multilingual-r2", name="Granite Embedding 97M multilingual R2 (IBM)",
        repo="ibm-granite/granite-embedding-97m-multilingual-r2",
        files=(ModelFile("onnx/model_quantized.onnx", "model"), ModelFile("tokenizer.json", "tokenizer")),
        size_mb=100, dims=384, languages="Multilingual, including English and Chinese", license="Apache 2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0", pooling="cls", min_similarity=0.5,
        note="Similar size. Better multilingual retrieval scores reported by IBM; Chinese quality unpublished."),
    LocalModel(
        id="embeddinggemma-300m", name="EmbeddingGemma 300M (Google)", repo="onnx-community/embeddinggemma-300m-ONNX",
        files=(ModelFile("onnx/model_q4.onnx", "model"), ModelFile("onnx/model_q4.onnx_data", "data"),
               ModelFile("tokenizer.json", "tokenizer")),
        size_mb=200, dims=768, languages="Over 100 languages", license="Gemma Terms of Use",
        license_url="https://ai.google.dev/gemma/terms", pooling="output",
        query_prefix="task: search result | query: ", passage_prefix="title: none | text: ", max_tokens=2048,
        min_similarity=0.35, terms_url="https://ai.google.dev/gemma/terms",
        note="Mid-size, built for on-device use. Requires accepting Google's Gemma Terms of Use."),
    LocalModel(
        id="qwen3-embedding-0.6b", name="Qwen3-Embedding 0.6B", repo="onnx-community/Qwen3-Embedding-0.6B-ONNX",
        files=(ModelFile("onnx/model_quantized.onnx", "model"), ModelFile("tokenizer.json", "tokenizer")),
        size_mb=640, dims=1024, languages="Over 100 languages; strongest of these for Chinese", license="Apache 2.0",
        license_url="https://www.apache.org/licenses/LICENSE-2.0", pooling="last",
        query_prefix="Instruct: Given a question, retrieve memories about the person that answer it\nQuery:",
        append_eos="<|endoftext|>", max_tokens=1024, min_similarity=0.35,
        note="Best quality, larger download."),
)
BY_ID = {m.id: m for m in MODELS}
DEFAULT_MODEL = next(m.id for m in MODELS if m.recommended)


# ---- pins ------------------------------------------------------------------------------------------------------


def load_pins(path: Path | None = None) -> dict[str, Any]:
    """``{model id: {"revision", "files": {path: {"sha256", "size"}}}}`` from ``model_pins.json``."""
    try:
        data = json.loads((path or PINS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def pin_of(model: LocalModel, pins: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The model's pin when every file has a SHA-256 and a size; None otherwise (the model can't be downloaded)."""
    pin = (load_pins() if pins is None else pins).get(model.id)
    if not isinstance(pin, dict) or not isinstance(pin.get("revision"), str) or not pin["revision"]:
        return None
    files = pin.get("files")
    if not isinstance(files, dict):
        return None
    for f in model.files:
        entry = files.get(f.path)
        if not (isinstance(entry, dict) and isinstance(entry.get("sha256"), str) and len(entry["sha256"]) == 64
                and isinstance(entry.get("size"), int) and entry["size"] > 0):
            return None
    return pin


# ---- where things go -------------------------------------------------------------------------------------------


def _cloud_dir() -> Path:
    from ..cloud import cloud_dir

    return cloud_dir()


def models_dir() -> Path:
    return _cloud_dir() / "models"


def model_dir(model: LocalModel) -> Path:
    return models_dir() / model.id


def model_path(model: LocalModel, file: ModelFile) -> Path:
    """Where a model file is kept: by its file name, all in one folder, since a model's external weights
    (``model.onnx_data``) must sit next to the model under the name it refers to."""
    return model_dir(model) / Path(file.path).name


def settings_path() -> Path:
    return _cloud_dir() / "semantic.json"


def runtime_dir() -> Path:
    tag = f"py{sys.version_info.major}{sys.version_info.minor}-{sys.platform}-{platform.machine().lower() or 'any'}"
    return _cloud_dir() / "semantic-runtime" / tag


# ---- the person's choice ---------------------------------------------------------------------------------------


@dataclass
class SemanticSettings:
    enabled: bool = False
    model: str = DEFAULT_MODEL
    accepted_terms: dict[str, str] = field(default_factory=dict)  # model id → when the terms were accepted

    @property
    def local_model(self) -> LocalModel:
        return BY_ID.get(self.model) or BY_ID[DEFAULT_MODEL]


def load_semantic() -> SemanticSettings:
    s = SemanticSettings()
    try:
        raw = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return s
    if not isinstance(raw, dict):
        return s
    s.enabled = raw.get("enabled") is True
    if raw.get("model") in BY_ID:
        s.model = raw["model"]
    terms = raw.get("accepted_terms")
    if isinstance(terms, dict):
        s.accepted_terms = {k: str(v) for k, v in terms.items() if k in BY_ID}
    return s


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    from ..vault import _flock

    folder = _cloud_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(folder / ".semantic.lock", "a+") as fh:
        _flock(fh, True)
        try:
            yield
        finally:
            _flock(fh, False)


def save_semantic(*, enabled: bool | None = None, model: str | None = None,
                  accept_terms_for: str | None = None) -> SemanticSettings:
    """Change ``semantic.json``; keys this version doesn't know are kept."""
    from ..cloud import _write_private

    with _lock():
        try:
            raw = json.loads(settings_path().read_text(encoding="utf-8"))
            raw = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            raw = {}
        if model is not None:
            if model not in BY_ID:
                raise SemanticError(f"Unknown model {model}.", "unknown_model")
            raw["model"] = model
        if enabled is not None:
            raw["enabled"] = bool(enabled)
        if accept_terms_for is not None:
            terms = raw.get("accepted_terms") if isinstance(raw.get("accepted_terms"), dict) else {}
            terms[accept_terms_for] = datetime.now(UTC).isoformat(timespec="seconds")
            raw["accepted_terms"] = terms
        _write_private(settings_path(), json.dumps({"format": 1, **{k: v for k, v in raw.items() if k != "format"}},
                                                  ensure_ascii=False, indent=1) + "\n")
    return load_semantic()


# ---- the runtime (onnxruntime + tokenizers) --------------------------------------------------------------------

_run = subprocess.run  # replaced in tests


def _activate_runtime_dir() -> None:
    folder = runtime_dir()
    if folder.is_dir() and str(folder) not in sys.path:
        sys.path.append(str(folder))  # after the environment's own packages, so those always win
        importlib.invalidate_caches()


def runtime_available() -> bool:
    """Whether ``onnxruntime`` and ``tokenizers`` can be imported (from the environment or the runtime folder)."""
    _activate_runtime_dir()
    try:
        return (importlib.util.find_spec("onnxruntime") is not None
                and importlib.util.find_spec("tokenizers") is not None)
    except (ImportError, ValueError):
        return False


def _uv() -> str | None:
    for candidate in (os.environ.get("OPENMYMEMO_UV"), os.environ.get("UV"), shutil.which("uv")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def install_command(display: bool = False) -> list[str] | None:
    """The command that installs the runtime into ``runtime_dir()`` for this Python: with uv when it can be found,
    else with pip when this Python has it; None when neither (``display`` returns the uv form anyway, for the
    person to run)."""
    target = str(runtime_dir())
    uv = _uv()
    if uv or display:
        return [uv or "uv", "pip", "install", "--python", sys.executable, "--target", target, *RUNTIME_REQUIREMENTS]
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--target", target,
                *RUNTIME_REQUIREMENTS]
    return None


def manual_command() -> str:
    """The exact command for the person, when the app can't install the runtime itself."""
    return " ".join(f'"{a}"' if any(c in a for c in " <>=") else a for a in install_command(display=True) or [])


INTEL_MAC = ("Search by meaning on this computer needs ONNX Runtime, which doesn't support Intel Macs. With the "
             "Assistant on a server on this computer or your own key, you can set an embeddings model instead.")


def runtime_unsupported() -> bool:
    """Whether ONNX Runtime can't be installed here: it publishes no packages for Intel Macs."""
    return sys.platform == "darwin" and platform.machine().lower() == "x86_64"


def install_runtime() -> None:
    """Install ``onnxruntime`` and ``tokenizers`` into the runtime folder; raises SemanticError (code ``runtime``
    with the command to run, when there is no uv or pip, or the install failed)."""
    if runtime_available():
        return
    if runtime_unsupported():
        raise SemanticError(INTEL_MAC, "runtime")
    cmd = install_command()
    if cmd is None:
        raise SemanticError("Search by meaning needs ONNX Runtime and tokenizers. Install them with: "
                            + manual_command(), "runtime")
    runtime_dir().mkdir(parents=True, exist_ok=True)
    try:
        r = _run(cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("semantic: runtime install failed to start (%s)", type(e).__name__)
        raise SemanticError("Couldn't install ONNX Runtime. Install it with: " + manual_command(), "runtime") from None
    if r.returncode != 0:
        log.warning("semantic: runtime install failed: %s", (r.stderr or "")[-500:])
        raise SemanticError("Couldn't install ONNX Runtime (check the internet connection). Or install it with: "
                            + manual_command(), "runtime")
    if not runtime_available():
        raise SemanticError("ONNX Runtime was installed but can't be loaded. Install it with: " + manual_command(),
                            "runtime")


# ---- downloads -------------------------------------------------------------------------------------------------

Progress = Callable[[int, int], None]  # (bytes so far, bytes in all)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, sha256: str, size: int, progress: Progress | None = None,
             done_before: int = 0, total: int | None = None) -> None:
    """Fetch ``url`` (https only, redirects to https only) into ``dest``. The file is kept only when its size and
    SHA-256 match; otherwise it is deleted and SemanticError raised (``checksum`` or ``network``)."""
    if urlsplit(url).scheme != "https":
        raise SemanticError("Model downloads must use https.", "network")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    got = 0
    try:
        with httpx2.Client(transport=HTTP_TRANSPORT, follow_redirects=True, max_redirects=5,
                           timeout=httpx2.Timeout(60.0, connect=15.0)) as client:
            with client.stream("GET", url, headers={"User-Agent": "OpenMyMemo"}) as r:
                if urlsplit(str(r.url)).scheme != "https":
                    raise SemanticError("The download was redirected to an insecure address.", "network")
                if r.status_code != 200:
                    raise SemanticError(f"The model couldn't be downloaded (HTTP {r.status_code}).", "network")
                with open(part, "wb") as fh:
                    for chunk in r.iter_bytes(CHUNK):
                        got += len(chunk)
                        if got > size:
                            raise SemanticError("The downloaded file doesn't match the expected one, so it was "
                                                "deleted.", "checksum")
                        h.update(chunk)
                        fh.write(chunk)
                        if progress:
                            progress(done_before + got, total or size)
    except SemanticError:
        part.unlink(missing_ok=True)
        raise
    except (httpx2.HTTPError, OSError) as e:
        part.unlink(missing_ok=True)
        log.warning("semantic: download failed (%s)", type(e).__name__)
        raise SemanticError("The model couldn't be downloaded. Check the internet connection and try again.",
                            "network") from None
    if got != size or h.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise SemanticError("The downloaded file doesn't match the expected one, so it was deleted.", "checksum")
    os.replace(part, dest)


def _verified_path(model: LocalModel) -> Path:
    return model_dir(model) / "verified.json"


def is_downloaded(model: LocalModel, pins: dict[str, Any] | None = None) -> bool:
    """Every file is there with the pinned size and was verified (the record of each file's size, mtime and hash
    is checked, so a replaced file counts as missing)."""
    pin = pin_of(model, pins)
    if pin is None:
        return False
    try:
        record = json.loads(_verified_path(model).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    for f in model.files:
        p = model_path(model, f)
        entry = pin["files"][f.path]
        seen = record.get(f.path) if isinstance(record, dict) else None
        try:
            st = p.stat()
        except OSError:
            return False
        if (not isinstance(seen, dict) or st.st_size != entry["size"] or seen.get("sha256") != entry["sha256"]
                or seen.get("mtime_ns") != st.st_mtime_ns):
            return False
    return True


def ensure_downloaded(model: LocalModel, progress: Progress | None = None) -> None:
    """Download whatever of the model is missing or doesn't match its pin. Refuses unpinned models."""
    pin = pin_of(model)
    if pin is None:
        raise SemanticError(f"{model.name} isn't available yet.", "unpinned")
    if is_downloaded(model):
        return
    total = sum(pin["files"][f.path]["size"] for f in model.files)
    done = 0
    record: dict[str, Any] = {}
    for f in model.files:
        entry = pin["files"][f.path]
        dest = model_path(model, f)
        if not (dest.is_file() and dest.stat().st_size == entry["size"] and _sha256(dest) == entry["sha256"]):
            dest.unlink(missing_ok=True)
            download(model.url(f, pin["revision"]), dest, entry["sha256"], entry["size"], progress, done, total)
        done += entry["size"]
        if progress:
            progress(done, total)
        record[f.path] = {"sha256": entry["sha256"], "mtime_ns": dest.stat().st_mtime_ns}
    from ..vault import _atomic_write

    _atomic_write(_verified_path(model), json.dumps(record, indent=1) + "\n")


def delete_models() -> None:
    """Delete every downloaded model (turning the switch off)."""
    shutil.rmtree(models_dir(), ignore_errors=True)


def delete_other_models(keep: LocalModel) -> None:
    folder = models_dir()
    if folder.is_dir():
        for child in folder.iterdir():
            if child.name != keep.id and child.is_dir():
                shutil.rmtree(child, ignore_errors=True)


# ---- the switch, with progress for the panel ----------------------------------------------------------------


@dataclass
class Job:
    """What turning it on is doing now, for the panel to poll: ``phase`` is idle, runtime, download, index, done or
    error."""

    phase: str = "idle"
    model: str | None = None
    done: int = 0
    total: int = 0
    error: str | None = None
    code: str | None = None
    started: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "model": self.model, "done": self.done, "total": self.total,
                "error": self.error, "code": self.code}


_job = Job()
_job_lock = threading.Lock()


def job() -> Job:
    return _job


def turn_on(model_id: str | None = None, *, accept_terms: bool = False,
            on_phase: Callable[[str, int, int], None] | None = None) -> SemanticSettings:
    """Turn the local model on: check the choice and its terms, install the runtime if needed, download and verify
    the model, then save the choice. Raises SemanticError. The caller builds the index (``search.build_index``)."""
    current = load_semantic()
    model = BY_ID.get(model_id or current.model)
    if model is None:
        raise SemanticError(f"Unknown model {model_id}.", "unknown_model")
    if model.terms_url and model.id not in current.accepted_terms and not accept_terms:
        raise SemanticError(f"{model.name} is under {model.license} ({model.terms_url}); accept them to download it.",
                            "terms")
    if pin_of(model) is None:
        raise SemanticError(f"{model.name} isn't available yet.", "unpinned")
    phase = on_phase or (lambda *_: None)
    if not runtime_available():
        phase("runtime", 0, 0)
        install_runtime()
    phase("download", 0, model.size_mb << 20)
    ensure_downloaded(model, lambda done, total: phase("download", done, total))
    if model.terms_url and accept_terms:
        save_semantic(accept_terms_for=model.id)
    s = save_semantic(enabled=True, model=model.id)
    delete_other_models(model)
    from . import embed

    embed.forget_local()
    return s


def turn_off() -> SemanticSettings:
    """Turn the local model off and delete it (the model choice is kept). The caller deletes the index."""
    s = save_semantic(enabled=False)
    from . import embed

    embed.forget_local()
    delete_models()
    return s


def start_turn_on(model_id: str | None, accept_terms: bool, after: Callable[[], None] | None = None) -> Job:
    """``turn_on`` in a background thread, for the panel; ``after`` (building the index) runs when it succeeded."""
    global _job
    with _job_lock:
        if _job.phase in ("runtime", "download", "index"):
            return _job
        _job = Job(phase="runtime" if not runtime_available() else "download", model=model_id, started=time.time())
        current = _job

    def phase(name: str, done: int, total: int) -> None:
        current.phase, current.done, current.total = name, done, total

    def work() -> None:
        try:
            turn_on(model_id, accept_terms=accept_terms, on_phase=phase)
            if after:
                current.phase, current.done, current.total = "index", 0, 0
                after()
            current.phase = "done"
        except SemanticError as e:
            current.phase, current.error, current.code = "error", str(e), e.code
        except Exception as e:  # noqa: BLE001 - reported to the panel, never raised into the server
            log.warning("semantic: turning on failed (%s)", type(e).__name__)
            current.phase, current.error, current.code = "error", "Something went wrong turning it on.", "error"

    threading.Thread(target=work, name="openmymemo-semantic", daemon=True).start()
    return current


def models_info() -> list[dict[str, Any]]:
    """The list for the panel and ``openmymemo semantic status``."""
    pins = load_pins()
    out = []
    for m in MODELS:
        pin = pin_of(m, pins)
        out.append({
            "id": m.id, "name": m.name, "size_mb": round(sum(pin["files"][f.path]["size"] for f in m.files) / 2**20)
            if pin else m.size_mb, "dims": m.dims, "languages": m.languages, "license": m.license,
            "license_url": m.license_url, "terms_url": m.terms_url, "recommended": m.recommended, "note": m.note,
            "available": pin is not None, "downloaded": is_downloaded(m, pins),
        })
    return out
