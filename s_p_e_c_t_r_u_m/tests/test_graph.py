"""Тесты семантического графа: узлы, связи, персистентность."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_graph_add_node():
    """Добавление узла."""
    from spectrum.storage.graph import SemanticGraph, GraphNode

    g = SemanticGraph()
    node = GraphNode(node_id="n1", node_type="document", label="Test Doc")
    g.add_node(node)

    assert g.get_node("n1") is not None
    assert g.get_node("n1").label == "Test Doc"
    assert g.get_node("nonexistent") is None


def test_graph_add_document():
    """Добавление документа."""
    from spectrum.storage.graph import SemanticGraph

    g = SemanticGraph()
    doc = g.add_document("/path/to/file.pdf", "abc123def456")

    assert doc.node_type == "document"
    assert doc.label == "file.pdf"
    assert g.get_node(doc.node_id) is not None


def test_graph_add_chunk_node():
    """Добавление чанка с привязкой к документу."""
    from spectrum.storage.graph import SemanticGraph

    g = SemanticGraph()
    doc = g.add_document("/path/to/file.pdf", "abc123")
    chunk = g.add_chunk_node("chunk-1", "Текст чанка для проверки", doc.node_id)

    assert chunk.node_type == "chunk"
    assert g.get_node(chunk.node_id) is not None

    # Проверяем связь документ → чанк
    edges = g.get_edges_from(doc.node_id)
    assert len(edges) == 1
    assert edges[0].edge_type == "contains"
    assert edges[0].target_id == chunk.node_id


def test_graph_add_entity():
    """Добавление сущности."""
    from spectrum.storage.graph import SemanticGraph

    g = SemanticGraph()
    ent = g.add_entity("ООО ТехноСтрой", "organization")

    assert ent.node_type == "entity"
    assert ent.label == "ООО ТехноСтрой"

    # Повторное добавление — тот же узел
    ent2 = g.add_entity("ООО ТехноСтрой", "organization")
    assert ent.node_id == ent2.node_id


def test_graph_edges():
    """Связи между узлами."""
    from spectrum.storage.graph import SemanticGraph

    g = SemanticGraph()
    doc = g.add_document("/path/to/file.pdf", "abc123")
    chunk = g.add_chunk_node("chunk-1", "text", doc.node_id)
    ent = g.add_entity("ТехноСтрой", "organization")

    # Связь чанк → сущность
    g.add_edge(chunk.node_id, ent.node_id, "mentions")

    # Проверяем
    neighbors = g.get_neighbors(chunk.node_id)
    assert len(neighbors) >= 1

    # Фильтр по типу
    mentions = g.get_neighbors(chunk.node_id, edge_type="mentions")
    assert len(mentions) == 1
    assert mentions[0].label == "ТехноСтрой"


def test_graph_find_documents_for_entity():
    """Поиск документов по сущности."""
    from spectrum.storage.graph import SemanticGraph

    g = SemanticGraph()
    doc = g.add_document("/path/to/contract.pdf", "abc123")
    chunk = g.add_chunk_node("chunk-1", "Поставщик: ТехноСтрой", doc.node_id)
    ent = g.add_entity("ТехноСтрой", "organization")
    g.add_edge(chunk.node_id, ent.node_id, "mentions")

    docs = g.find_documents_for_entity("ТехноСтрой")
    assert len(docs) == 1
    assert docs[0].node_id == doc.node_id


def test_graph_stats():
    """Статистика графа."""
    from spectrum.storage.graph import SemanticGraph

    g = SemanticGraph()
    g.add_document("/a.pdf", "hash_a")
    g.add_document("/b.pdf", "hash_b")
    g.add_chunk_node("c1", "text", "doc:hash_a")
    g.add_entity("Entity1", "org")

    stats = g.stats()
    assert stats["total_nodes"] == 4
    assert stats["nodes_document"] == 2
    assert stats["nodes_chunk"] == 1
    assert stats["nodes_entity"] == 1


def test_graph_persistence():
    """Персистентность: save/load."""
    from spectrum.storage.graph import SemanticGraph

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "graph.json"

        # Создаём и сохраняем
        g1 = SemanticGraph(persist_path=path)
        g1.add_document("/test.pdf", "hash123")
        g1.add_chunk_node("c1", "test text", "doc:hash123")
        g1.save()

        assert path.exists()

        # Загружаем
        g2 = SemanticGraph(persist_path=path)
        assert g2.get_node("doc:hash123") is not None
        assert g2.get_node("chunk:c1") is not None


def test_graph_clear():
    """Очистка графа."""
    from spectrum.storage.graph import SemanticGraph

    g = SemanticGraph()
    g.add_document("/a.pdf", "hash")
    g.add_entity("Test", "org")
    assert len(g.stats()) > 0

    g.clear()
    assert g.stats()["total_nodes"] == 0
    assert g.stats()["total_edges"] == 0


def test_graph_node_to_dict():
    """Сериализация узла."""
    from spectrum.storage.graph import GraphNode

    n = GraphNode(
        node_id="test",
        node_type="document",
        label="Test",
        properties={"key": "value"},
    )
    d = n.to_dict()
    assert d["node_id"] == "test"
    assert d["properties"]["key"] == "value"


def test_graph_edge_to_dict():
    """Сериализация связи."""
    from spectrum.storage.graph import GraphEdge

    e = GraphEdge(
        source_id="a",
        target_id="b",
        edge_type="contains",
        weight=0.5,
    )
    d = e.to_dict()
    assert d["source_id"] == "a"
    assert d["target_id"] == "b"
    assert d["edge_type"] == "contains"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  ✅ {test_fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test_fn.__name__}: {e}")
            failed += 1
    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
