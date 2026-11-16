"""Tests for the embedding token counter used to size advisory chunks."""

import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

from pipeline.vector_store import embeddings


def test_count_tokens_local_uses_tokenizer(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "sentence_transformers")
    fake = SimpleNamespace(tokenizer=SimpleNamespace(encode=lambda text: text.split()))
    monkeypatch.setattr(embeddings, "_get_local_embedder", lambda: fake)
    # "passage: a b c" -> passage:, a, b, c
    assert embeddings.count_tokens("a b c") == 4


def test_count_tokens_remote_estimates_from_length(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai_compatible")
    text = "x" * 400
    assert embeddings.count_tokens(text) == len("passage: " + text) // 4


def test_count_tokens_remote_is_at_least_one(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai_compatible")
    assert embeddings.count_tokens("") >= 1


def test_parallel_requests_share_one_model_initialization(monkeypatch):
    started = Barrier(3)
    initialized = Event()
    proceed = Event()
    duplicate = Event()
    model = object()

    def load(_model_name):
        if initialized.is_set():
            duplicate.set()
        initialized.set()
        assert proceed.wait(timeout=5)
        return model

    def request_model():
        started.wait(timeout=5)
        return embeddings._get_local_embedder()

    monkeypatch.setattr(embeddings, "_local_embedder", None)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=load))

    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = [executor.submit(request_model) for _ in range(2)]
        try:
            started.wait(timeout=5)
            assert initialized.wait(timeout=5)
            assert not duplicate.wait(timeout=0.1)
        finally:
            proceed.set()
        assert all(result.result(timeout=5) is model for result in pending)
