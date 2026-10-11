from listen import library


def _vecs():
    rows = [
        ("A", "task", "machine translation", 1.0), ("A", "method", "head pruning", 1.0),
        ("A", "keyword", "attention", 1.0),
        ("B", "task", "machine translation", 1.0), ("B", "method", "head pruning", 1.0),
        ("B", "keyword", "transformers", 1.0),
        ("C", "task", "sleep consolidation", 1.0), ("C", "method", "electrophysiology", 1.0),
        ("C", "keyword", "memory", 0.5),
    ]
    return library.vectors(rows)


def test_vectors_are_normalized_and_weighted_by_kind():
    vecs = _vecs()
    for vec in vecs.values():
        assert round(sum(v * v for v in vec.values()), 9) == 1.0
    # a task term counts more than a plain keyword of the same row weight
    assert vecs["A"]["machine translation"] > vecs["A"]["attention"]


def test_similar_papers_are_grouped_and_unrelated_are_not():
    groups = sorted(sorted(g) for g in library.clusters(_vecs()))
    assert groups == [["A", "B"], ["C"]]


def test_group_label_prefers_terms_shared_by_members():
    vecs = _vecs()
    assert set(library.label(["A", "B"], vecs)[:2]) == {"head pruning", "machine translation"}


def test_edges_connect_nearest_neighbours_above_threshold():
    vecs = _vecs()
    links = library.edges(vecs)
    assert [(a, b) for a, b, _ in links] == [("A", "B")]
    assert library.shared_terms(vecs["A"], vecs["B"])[0] in {"head pruning", "machine translation"}
    assert library.edges({"X": {"alone": 1.0}}) == []


def test_empty_library_has_no_groups():
    assert library.clusters({}) == [] and library.edges({}) == []


def test_publication_dates_keep_their_precision():
    assert library.publication_date("2024-05-02") == ("2024-05-02", "day")
    assert library.publication_date("May 2021") == ("2021-01-01", "year")  # month names are not parsed
    assert library.publication_date("2019") == ("2019-01-01", "year")
    assert library.publication_date("2023-13") == ("2023-01-01", "year")  # invalid month falls back to the year
    assert library.publication_date("Updated 2022-08") == ("2022-08-01", "month")
    assert library.publication_date("") is None and library.publication_date(None) is None
