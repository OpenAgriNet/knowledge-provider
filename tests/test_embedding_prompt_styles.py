from pipeline.vector_store.embeddings import format_embedding_input


def test_embeddinggemma_has_distinct_query_and_passage_prompts(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROMPT_STYLE", "embeddinggemma")
    assert format_embedding_input("water", "query", use_e5_prefix=False) == "task: search result | query: water"
    assert format_embedding_input("water", "passage") == "title: none | text: water"


def test_plain_does_not_add_e5_prompt(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROMPT_STYLE", "plain")
    assert format_embedding_input("water", "query") == "water"
