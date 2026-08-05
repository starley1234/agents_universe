from app.services.embeddings import cosine_similarity, deterministic_embedding
from app.services.memory import chunk_text, format_memories_for_prompt


def test_deterministic_embedding_is_stable_and_similar():
    a = deterministic_embedding("OpenSCAD render error code missing", dimensions=64)
    b = deterministic_embedding("OpenSCAD render error missing code", dimensions=64)
    c = deterministic_embedding("banana orange fruit", dimensions=64)
    assert a == deterministic_embedding("OpenSCAD render error code missing", dimensions=64)
    assert cosine_similarity(a, b) > cosine_similarity(a, c)


def test_chunk_text_and_prompt_format():
    chunks = chunk_text("A" * 100 + "\n\n" + "B" * 100, chunk_chars=120)
    assert len(chunks) >= 2
    prompt = format_memories_for_prompt([
        {"content": "remember this", "score": 0.9, "metadata": {"source": "test"}}
    ])
    assert "remember this" in prompt
    assert "source=test" in prompt
