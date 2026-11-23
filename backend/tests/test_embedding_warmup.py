"""Embedding warm-up: non-blocking, switchable, and unable to fail loudly.

The warm-up exists to remove the cold-start cost of the first recall. These
tests pin the four properties that make it safe to ship:

1. It never runs when ``JARVISMIND_WARMUP_EMBEDDING`` is off.
2. Starting it returns immediately, even while the model load is still running.
3. An unavailable or raising provider is swallowed, not propagated.
4. It touches only ``container.embedding_provider`` — no database, no schema.
"""
from __future__ import annotations

import threading

import pytest

from jarvismind.config import Settings
from jarvismind.warmup import THREAD_NAME, start_embedding_warmup, warmup_embedding


class FakeProvider:
    def __init__(self, *, available: bool = True, raises: bool = False, gate: threading.Event | None = None):
        self.is_available = available
        self._raises = raises
        self._gate = gate
        self.texts: list[str] = []

    def embed_text(self, text: str) -> list[float]:
        if self._gate is not None:
            # Hold the "model load" open so the caller can prove it did not wait.
            self._gate.wait(timeout=5.0)
        if self._raises:
            raise RuntimeError("model unavailable")
        self.texts.append(text)
        return [0.0]


class FakeContainer:
    def __init__(self, provider):
        self._provider = provider

    @property
    def embedding_provider(self):
        if isinstance(self._provider, Exception):
            raise self._provider
        return self._provider


def _settings(**overrides) -> Settings:
    values = {"warmup_embedding": True}
    values.update(overrides)
    return Settings(**values)


def test_warmup_embeds_one_short_text_and_reports_ready():
    provider = FakeProvider()
    assert warmup_embedding(FakeContainer(provider)) is True
    assert provider.texts == ["warmup"]


def test_an_unavailable_provider_is_not_an_error():
    provider = FakeProvider(available=False)
    assert warmup_embedding(FakeContainer(provider)) is False
    # Nothing is embedded when the provider already knows it is unavailable.
    assert provider.texts == []


def test_a_raising_provider_is_contained():
    assert warmup_embedding(FakeContainer(FakeProvider(raises=True))) is False
    # Even a broken property must not escape: an offline machine is a normal state.
    assert warmup_embedding(FakeContainer(RuntimeError("no provider"))) is False


def test_disabled_by_configuration_starts_no_thread():
    before = {thread.ident for thread in threading.enumerate()}
    assert start_embedding_warmup(FakeContainer(FakeProvider()), _settings(warmup_embedding=False)) is None
    after = {thread.ident for thread in threading.enumerate()}
    assert after == before


def test_enabled_starts_a_daemon_thread_that_does_not_block_the_caller():
    gate = threading.Event()
    provider = FakeProvider(gate=gate)

    thread = start_embedding_warmup(FakeContainer(provider), _settings())

    assert thread is not None
    assert thread.name == THREAD_NAME
    # The caller returned while embed_text is still blocked inside the thread.
    assert thread.is_alive() is True
    assert provider.texts == []

    gate.set()
    thread.join(timeout=5.0)
    assert thread.is_alive() is False
    assert provider.texts == ["warmup"]


def test_a_failing_thread_never_raises_into_the_caller():
    thread = start_embedding_warmup(FakeContainer(FakeProvider(raises=True)), _settings())
    assert thread is not None
    thread.join(timeout=5.0)
    assert thread.is_alive() is False


@pytest.mark.allow_load_settings
def test_the_switch_is_read_from_the_environment(monkeypatch):
    """Pin the documented variable and its default.

    The real loader is not called here: the P0 gate deliberately forbids tests
    from invoking it, and this only needs the boolean parsing rule it uses.
    """
    from jarvismind.config import _env_bool

    name = "JARVISMIND_WARMUP_EMBEDDING"
    monkeypatch.setenv(name, "0")
    assert _env_bool(name, True) is False

    monkeypatch.setenv(name, "1")
    assert _env_bool(name, True) is True

    monkeypatch.delenv(name)
    # On by default: the failure it fixes is silent, so opting out is the choice.
    assert _env_bool(name, True) is True
    assert Settings().warmup_embedding is True


def test_create_app_never_starts_a_warmup_thread():
    """The warm-up belongs to ``serve`` only.

    ``create_app`` is what ``pytest`` and ``TestClient`` build. If it started the
    thread, every test session would try to load a real ONNX model.
    """
    import inspect

    from jarvismind import main as main_module

    source = inspect.getsource(main_module.create_app)
    assert "start_embedding_warmup" not in source
    assert "warmup_embedding" not in source