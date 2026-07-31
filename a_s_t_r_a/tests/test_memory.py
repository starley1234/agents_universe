"""Tests for memory subsystem — ontology graph."""

from __future__ import annotations

import uuid

from astra.memory.ontology import OntologyStore


def test_ontology_add_and_query():
    store = OntologyStore()
    pid = uuid.uuid4()

    store.add_entity(pid, "Python", "language")
    store.add_entity(pid, "FastAPI", "framework")
    store.add_relation(pid, "FastAPI", "Python", "built_with")

    neighbours = store.query_neighbors(pid, "FastAPI", depth=1)
    assert "FastAPI" in neighbours
    assert any(e["target"] == "Python" for e in neighbours["FastAPI"]["edges"])


def test_ontology_subgraph_text():
    store = OntologyStore()
    pid = uuid.uuid4()

    store.add_entity(pid, "A", "concept")
    store.add_entity(pid, "B", "concept")
    store.add_relation(pid, "A", "B", "connects_to")

    text = store.get_subgraph_text(pid, "A")
    assert "A" in text
    assert "B" in text
    assert "connects_to" in text


def test_ontology_empty_query():
    store = OntologyStore()
    pid = uuid.uuid4()

    result = store.query_neighbors(pid, "nonexistent", depth=1)
    assert result == {}

    text = store.get_subgraph_text(pid, "nonexistent")
    assert text == ""


def test_ontology_multi_hop():
    store = OntologyStore()
    pid = uuid.uuid4()

    store.add_entity(pid, "A")
    store.add_entity(pid, "B")
    store.add_entity(pid, "C")
    store.add_relation(pid, "A", "B", "knows")
    store.add_relation(pid, "B", "C", "knows")

    result = store.query_neighbors(pid, "A", depth=2)
    assert "A" in result
    assert "B" in result
    assert "C" in result


def test_ontology_save_load(tmp_path):
    store = OntologyStore()
    pid = uuid.uuid4()

    store.add_entity(pid, "X", "thing")
    store.add_entity(pid, "Y", "thing")
    store.add_relation(pid, "X", "Y", "related")

    path = tmp_path / "ontology.json"
    store.save(pid, path)

    store2 = OntologyStore()
    store2.load(pid, path)

    text = store2.get_subgraph_text(pid, "X")
    assert "X" in text
    assert "Y" in text
