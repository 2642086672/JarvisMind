"""Background warm-up of the embedding model.

Why this exists
---------------
The embedding provider is created lazily, on the first request that needs a
vector. With Fastembed that first call downloads (if needed) and loads an ONNX
model, which can take several seconds. A client whose timeout is shorter than
that sees a failed recall on the very first turn and concludes that memory was
lost — the worst possible first impression for a memory service.

Loading it once, in a daemon thread, after the listener is up removes that
first-request cost without making startup wait for it.

Rules this module keeps
-----------------------
* It runs **only** from the ``serve`` command. ``create_app`` never starts it,
  so ``pytest`` / ``TestClient`` sessions pay nothing and never download a model.
* Nothing is ever awaited: the thread is a daemon and the return value of
  :func:`start_embedding_warmup` is the thread (or ``None`` when disabled), never
  a join.
* Every failure is contained. An offline machine has no model cache; the
  provider factory already degrades to a deterministic fallback, and anything
  that still raises is logged once at debug level and dropped. A warm-up must
  never be able to take the service down.
* It logs no content and no credentials — only a provider name and a boolean.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .config import Settings

logger = logging.getLogger("jme.warmup")

#: The text embedded to force the model into memory. It is never stored and
#: never leaves the process; a short string is enough to exercise the path.
_WARMUP_TEXT = "warmup"

#: Thread name, so a stack dump or profiler shows what this thread is doing.
THREAD_NAME = "jarvismind-embedding-warmup"


def warmup_embedding(container: Any) -> bool:
    """Force the embedding provider to load and run one tiny embedding.

    Returns ``True`` when the provider was usable, ``False`` otherwise. It never
    raises: an unavailable provider is an expected state on an offline machine,
    not an error the operator needs to act on.
    """
    try:
        provider = container.embedding_provider
        if not getattr(provider, "is_available", False):
            return False
        provider.embed_text(_WARMUP_TEXT)
        return True
    except Exception:  # pragma: no cover - defensive: no failure may escape
        return False


def _run(container: Any) -> None:
    try:
        ready = warmup_embedding(container)
    except BaseException:  # pragma: no cover - a daemon thread must not crash
        ready = False
    if ready:
        logger.debug("embedding model warmed up")
    else:
        # One low-noise line. "Offline" is a normal state, not a warning storm.
        logger.debug("embedding warm-up skipped (provider unavailable)")


def start_embedding_warmup(
    container: Any,
    settings: Settings,
    *,
    name: str = THREAD_NAME,
) -> Optional[threading.Thread]:
    """Start the warm-up thread, or return ``None`` when it is switched off.

    The caller must not join the returned thread; it exists to run beside the
    server, not to delay it.
    """
    if not getattr(settings, "warmup_embedding", False):
        return None
    thread = threading.Thread(target=_run, args=(container,), name=name, daemon=True)
    thread.start()
    return thread


__all__ = ["THREAD_NAME", "start_embedding_warmup", "warmup_embedding"]