"""The "Search by meaning on this computer" switch for one memory folder: what the panel and ``openmymemo semantic``
call. ``local`` does the per-computer part (model, runtime, settings); this adds the folder's index."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import local
from .embed import active_embedder, endpoint_embedder, local_embedder
from .index import EmbeddingIndex

if TYPE_CHECKING:
    from ..vault import Vault


def status(vault: Vault) -> dict[str, Any]:
    """Everything the panel shows: the switch, the chosen model, the list, the runtime, the job in progress, the
    embedder in use and this folder's index."""
    # The job first: when it finished meanwhile, it still reads as running and the panel asks once more, so a
    # "done" never comes with an index read before the job's last write.
    job = local.job().as_dict()
    s = local.load_semantic()
    emb = active_embedder(vault)
    if not s.enabled:
        _drop_local_index(vault)
    idx = EmbeddingIndex(vault).status(emb)
    total = len(vault.memories())
    runtime = local.runtime_available()
    return {
        "enabled": s.enabled, "model": s.model, "accepted_terms": sorted(s.accepted_terms),
        "ready": s.enabled and local_embedder() is not None,
        "models": local.models_info(), "runtime": {"installed": runtime,
                                                   "command": None if runtime else local.manual_command()},
        "job": job,
        "embedder": None if emb is None else ("local" if not emb.remote else "endpoint"),
        "embedder_id": emb.id if emb is not None else None,
        "endpoint": endpoint_embedder(vault) is not None,
        "index": {**idx, "memories": total},
    }


def _drop_local_index(vault: Vault) -> None:
    """With the local model off, an index it built is deleted (in every folder, when that folder is next used)."""
    idx = EmbeddingIndex(vault)
    snap = idx.load()
    if snap is not None and snap.model.startswith("local/"):
        idx.clear()


def turn_on(vault: Vault, model_id: str | None = None, *, accept_terms: bool = False, progress=None) -> dict:
    """Turn it on and build this folder's index (for the CLI; the panel uses ``start``). Raises
    ``local.SemanticError``."""
    before = local.load_semantic()
    phase = progress or (lambda *_: None)
    s = local.turn_on(model_id, accept_terms=accept_terms, on_phase=phase)
    if before.model != s.model or not before.enabled:
        _drop_local_index(vault)
    from .hybrid import build_index

    emb = local_embedder()
    if emb is not None:
        build_index(vault, lambda done, total: phase("index", done, total), embedder=emb)
    return status(vault)


def start(vault: Vault, model_id: str | None = None, *, accept_terms: bool = False) -> dict:
    """``turn_on`` in the background for the panel, which polls ``status``. Refuses at once (SemanticError) what
    can't start: an unknown or unpinned model, or terms not accepted."""
    current = local.load_semantic()
    model = local.BY_ID.get(model_id or current.model)
    if model is None:
        raise local.SemanticError(f"Unknown model {model_id}.", "unknown_model")
    if local.pin_of(model) is None:
        raise local.SemanticError(f"{model.name} isn't available yet.", "unpinned")
    if model.terms_url and model.id not in current.accepted_terms and not accept_terms:
        raise local.SemanticError(f"Accept the {model.license} to download {model.name}.", "terms")
    switching = current.model != model.id or not current.enabled

    def after() -> None:
        from .hybrid import build_index

        if switching:
            _drop_local_index(vault)
        emb = local_embedder()
        if emb is not None:
            job = local.job()

            def progress(done: int, total: int) -> None:
                job.done, job.total = done, total

            build_index(vault, progress, embedder=emb)

    local.start_turn_on(model.id, accept_terms, after)
    return status(vault)


def turn_off(vault: Vault) -> dict:
    """Turn it off: delete the model and this folder's index (the choice of model is kept)."""
    local.turn_off()
    _drop_local_index(vault)
    return status(vault)
