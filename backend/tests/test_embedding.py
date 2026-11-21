"""Tests for Embedding providers in JME-4."""
import pytest

from jarvismind.core.errors import EmbeddingUnavailableError
from jarvismind.services.embedding import (
    BaseEmbeddingProvider,
    DeterministicEmbeddingProvider,
    DisabledEmbeddingProvider,
    FastembedEmbeddingProvider,
)


def test_deterministic_embedding_generation():
    provider = DeterministicEmbeddingProvider(dimension=512)
    assert provider.name == "deterministic"
    assert provider.dimension == 512
    assert provider.is_available is True

    v1 = provider.embed_text("今天天气很好")
    v2 = provider.embed_text("今天天气很好")
    assert len(v1) == 512
    assert v1 == v2  # 100% deterministic

    # Verify unit length normalization
    norm = sum(x * x for x in v1)
    assert pytest.approx(norm, 0.01) == 1.0


def test_deterministic_similarity():
    provider = DeterministicEmbeddingProvider(dimension=512)
    # Similar phrases sharing tokens/bigrams
    v_base = provider.embed_text("舟山海边风景")
    v_similar = provider.embed_text("舟山的海边")
    v_different = provider.embed_text("量子计算机编译器优化")

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b))

    sim_high = cosine(v_base, v_similar)
    sim_low = cosine(v_base, v_different)
    assert sim_high > sim_low


def test_deterministic_batch_generation():
    provider = DeterministicEmbeddingProvider(dimension=256)
    texts = ["第一条记忆", "第二条记忆", ""]
    vectors = provider.embed_batch(texts)
    assert len(vectors) == 3
    assert len(vectors[0]) == 256
    assert len(vectors[1]) == 256
    assert len(vectors[2]) == 256


def test_fastembed_real_onnx_generation():
    # Test real ONNX model inference using cached bge-small-zh-v1.5
    provider = FastembedEmbeddingProvider(
        model_name="BAAI/bge-small-zh-v1.5",
    )
    if not provider.is_available:
        pytest.skip("Fastembed model not locally cached or unavailable")

    assert provider.dimension == 512
    vec = provider.embed_text("物流客服质管")
    assert len(vec) == 512
    assert isinstance(vec[0], float)

    batch = provider.embed_batch(["测试文本一", "测试文本二"])
    assert len(batch) == 2
    assert len(batch[0]) == 512


def test_embedding_initialization_failure(monkeypatch):
    import fastembed

    def raise_init(*args, **kwargs):
        raise RuntimeError("Simulated Fastembed ONNX model load failure")

    monkeypatch.setattr(fastembed, "TextEmbedding", raise_init)

    provider = FastembedEmbeddingProvider(model_name="any-model")
    assert provider.is_available is False
    info = provider.get_info()
    assert info.is_available is False
    assert "Simulated Fastembed" in str(info.error)

    with pytest.raises(EmbeddingUnavailableError):
        provider.embed_text("hello")

    with pytest.raises(EmbeddingUnavailableError):
        provider.embed_batch(["hello"])


def test_disabled_embedding_provider():
    provider = DisabledEmbeddingProvider("Custom disabled reason", dimension=384)
    assert provider.is_available is False
    assert provider.dimension == 384
    with pytest.raises(EmbeddingUnavailableError) as exc_info:
        provider.embed_text("test")
    assert "Custom disabled reason" in str(exc_info.value)
