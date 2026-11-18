# SPDX-License-Identifier: GPL-3.0-or-later

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from cps import db
from cps.library_search import SearchSyntaxError, parse_library_search


pytestmark = pytest.mark.unit


@pytest.fixture
def library(monkeypatch):
    engine = create_engine("sqlite://").execution_options(schema_translate_map={"calibre": None})
    db.Base.metadata.create_all(engine)
    with Session(engine) as session:
        rows = [
            {"id": 949, "title": "When the Moon Hits Your Eye", "sort": "Moon"},
            {"id": 1, "title": "Hits Eye", "sort": "Hits Eye"},
            {"id": 2, "title": "A warrior in Montréal", "sort": "Warrior"},
            {"id": 3, "title": "949 EPUB", "sort": "949"},
            {"id": 4, "title": "Other", "sort": "Other"},
            {"id": 5, "title": "50% Done", "sort": "50% Done"},
            {"id": 6, "title": "50X Done", "sort": "50X Done"},
        ]
        session.execute(db.Books.__table__.insert(), rows)
        session.add_all([db.Data(949, "EPUB", 0, "book"), db.Data(2, "AZW3", 0, "book"),
                         db.Data(4, "AZW", 0, "book")])
        session.execute(db.Tags.__table__.insert(), [{"id": 1, "name": "Eye"}])
        session.execute(db.books_tags_link.insert(), [{"book": 4, "tag": 1}])
        session.execute(db.Authors.__table__.insert(), [{"id": 1, "name": "Hits"}])
        session.execute(db.books_authors_link.insert(), [{"book": 4, "author": 1}])
        session.commit()
        catalog = db.CalibreDB(init=False)
        catalog.session = session
        catalog.create_functions()
        monkeypatch.setattr(catalog, "common_filters", lambda *_: db.true())
        yield catalog
    engine.dispose()


def ids(library, query, relevance=True):
    return [book.id for book in library._natural_search_query(
        library.session.query(db.Books), parse_library_search(query), [], relevance).all()]


@pytest.mark.parametrize(("query", "expected"), [
    ("hits eye", [1, 949, 4]),
    ('"hits eye"', [1]),
    ('"hits your eye"', [949]),
    ("eye hits format:.EPUB", [949]),
    ("epub", [949]),
    ('"EPUB"', [3]),
    ("AZW", [4]),
    ("format:azw3", [2]),
    ("949", [949, 3]),
    ("#949", [949]),
    ("id:949", [949]),
    ("id:1 ID:949 iD:1 #999", [1, 949]),
    ("#1 #949 format:epub", [949]),
    ("id:999", []),
    ("warriors montreal", [2]),
    ('"warriors" montreal', []),
    ("50%", [5]),
])
def test_search_filters_and_relevance(library, query, expected):
    assert ids(library, query) == expected


def test_exact_id_does_not_bypass_visibility(library, monkeypatch):
    monkeypatch.setattr(library, "common_filters", lambda *_: db.Books.id != 949)
    assert ids(library, "id:949") == []
    assert ids(library, "id:1 ID:949") == [1]
    assert ids(library, "949") == [3]
    assert ids(library, "epub") == []


def test_explicit_sort_and_selection(library):
    result = library._natural_search_query(library.session.query(db.Books),
                                          parse_library_search("hits eye"), [], False)
    assert [b.id for b in result.order_by(db.Books.id.desc()).all()] == [949, 4, 1]
    assert [b.id for b in library.get_checkbox_sorted(result.order_by(db.Books.id).all(),
                                                       [949, 2, 949], 0, 10, "desc")] == [949, 1, 4]


@pytest.mark.parametrize("query", ["id:", "id:-7", "id:1 ID:0", "format:", "format:epub%",
                                   '"hits eye', "id:9223372036854775808", "a " * 13, "x" * 257])
def test_invalid_syntax(query):
    with pytest.raises(SearchSyntaxError):
        parse_library_search(query)


@pytest.mark.parametrize(("plural", "singular"), [
    ("warriors", "warrior"), ("boxes", "box"), ("stories", "story"),
    ("chevaux", "cheval"), ("étoiles", "étoile"),
])
def test_plural_matching_in_both_directions(plural, singular):
    from cps.library_search import normalize_search_text, search_text_score

    assert search_text_score(plural, normalize_search_text(singular), False) == 1
    assert search_text_score(singular, normalize_search_text(plural), False) == 1
