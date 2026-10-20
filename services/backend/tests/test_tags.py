"""Custom tags on a paper: each user's own, cleaned on the way in, and gone when the paper leaves their library."""
import pytest

from listen import tagging

from .test_library_scope import AUTH, db, make_client, pdf, upload  # noqa: F401  (fixtures and helpers of the library tests)


def test_tags_are_cleaned_and_sorted():
    assert tagging.clean(["  Graph   Nets ", "graph nets", "NLP", "", "  "]) == ["graph nets", "nlp"]
    assert tagging.clean(["state-space", "ml_ops", "2024"]) == ["2024", "ml_ops", "state-space"]


@pytest.mark.parametrize("bad", ["x" * 33, "c++", "a/b", "é?", "tag#1"])
def test_unusable_tags_are_refused(bad):
    with pytest.raises(ValueError):
        tagging.clean([bad])


def test_a_paper_has_at_most_twenty_tags():
    assert len(tagging.clean([f"t{i}" for i in range(tagging.MAX_TAGS)])) == tagging.MAX_TAGS
    with pytest.raises(ValueError):
        tagging.clean([f"t{i}" for i in range(tagging.MAX_TAGS + 1)])


def test_tags_are_saved_shown_and_replaced(make_client, pdf):
    ann = make_client("ann@example.org")
    doc_id = upload(ann, pdf).json()["id"]
    r = ann.put(f"/api/documents/{doc_id}/tags", json={"tags": ["  Pruning ", "pruning", "to read"]}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"tags": ["pruning", "to read"]}
    assert ann.get(f"/api/documents/{doc_id}", headers=AUTH).json()["tags"] == ["pruning", "to read"]
    assert ann.get("/api/documents", headers=AUTH).json()[0]["tags"] == ["pruning", "to read"]
    assert ann.put(f"/api/documents/{doc_id}/tags", json={"tags": []}, headers=AUTH).json() == {"tags": []}
    assert ann.get(f"/api/documents/{doc_id}", headers=AUTH).json()["tags"] == []


def test_tags_belong_to_the_user_who_added_them(make_client, pdf):
    ann = make_client("ann@example.org")
    bea = make_client("bea@example.org")
    doc_id = upload(ann, pdf).json()["id"]
    assert upload(bea, pdf).json()["id"] == doc_id  # the same stored paper, in both libraries
    ann.put(f"/api/documents/{doc_id}/tags", json={"tags": ["mine"]}, headers=AUTH)
    bea.put(f"/api/documents/{doc_id}/tags", json={"tags": ["hers"]}, headers=AUTH)
    assert ann.get(f"/api/documents/{doc_id}", headers=AUTH).json()["tags"] == ["mine"]
    assert bea.get(f"/api/documents/{doc_id}", headers=AUTH).json()["tags"] == ["hers"]


def test_a_paper_outside_the_library_takes_no_tags(make_client, pdf):
    ann = make_client("ann@example.org")
    bea = make_client("bea@example.org")
    doc_id = upload(ann, pdf).json()["id"]
    r = bea.put(f"/api/documents/{doc_id}/tags", json={"tags": ["x"]}, headers=AUTH)
    assert r.status_code == 404


def test_invalid_tags_are_refused_and_nothing_is_saved(make_client, pdf):
    ann = make_client("ann@example.org")
    doc_id = upload(ann, pdf).json()["id"]
    ann.put(f"/api/documents/{doc_id}/tags", json={"tags": ["kept"]}, headers=AUTH)
    r = ann.put(f"/api/documents/{doc_id}/tags", json={"tags": ["ok", "c++"]}, headers=AUTH)
    assert r.status_code == 422
    assert ann.get(f"/api/documents/{doc_id}", headers=AUTH).json()["tags"] == ["kept"]


def test_tags_go_when_the_paper_leaves_the_library(make_client, settings, pdf):
    ann = make_client("ann@example.org")
    doc_id = upload(ann, pdf).json()["id"]
    ann.put(f"/api/documents/{doc_id}/tags", json={"tags": ["gone"]}, headers=AUTH)
    assert ann.delete(f"/api/documents/{doc_id}", headers=AUTH).status_code == 204
    with db(settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM library_tags").fetchone()["n"] == 0
