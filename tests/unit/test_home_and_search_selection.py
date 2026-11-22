# SPDX-License-Identifier: GPL-3.0-or-later

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask, redirect, request, url_for
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
from sqlalchemy import Boolean, Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def homepage():
    base = declarative_base()

    class MagicShelf(base):
        __tablename__ = 'magic_shelf'
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer)
        is_system = Column(Boolean)
        name = Column(String)

    engine = create_engine('sqlite://')
    base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    app = Flask(__name__)
    app.add_url_rule('/magicshelf/<int:shelf_id>/<sort_param>/<int:page>', 'web.render_magic_shelf', lambda **kwargs: '')
    user = SimpleNamespace(is_authenticated=True, id=1, role_admin=lambda: False)
    render_books_list = Mock(return_value='books fallback')
    namespace = {'current_user': user, 'ub': SimpleNamespace(session=session, MagicShelf=MagicShelf),
                 'magic_shelf': SimpleNamespace(SYSTEM_SHELF_TEMPLATES={'currently_reading': {'name': 'Currently Reading'}}),
                 'request': request, 'redirect': redirect, 'url_for': url_for,
                 'render_books_list': render_books_list}
    source = ast.parse((ROOT / 'cps' / 'web.py').read_text(encoding='utf-8'))
    index = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'index')
    index.decorator_list = []
    exec(compile(ast.Module(body=[index], type_ignores=[]), 'web_index', 'exec'), namespace)
    try:
        yield app, session, MagicShelf, user, namespace['index'], render_books_list
    finally:
        session.close()
        engine.dispose()


def test_homepage_uses_own_system_shelf_and_preserves_page(homepage):
    app, session, shelf, _, index, _ = homepage
    session.add_all([shelf(id=1, user_id=2, is_system=True, name='Currently Reading'),
                     shelf(id=2, user_id=1, is_system=False, name='Currently Reading'),
                     shelf(id=3, user_id=1, is_system=True, name='Currently Reading')])
    session.commit()
    with app.test_request_context('/page/2?sort=abc'):
        assert index(2).location == '/magicshelf/3/abc/2'


@pytest.mark.parametrize('authenticated', [False, True])
def test_homepage_without_own_system_shelf_falls_back_to_books(homepage, authenticated):
    app, session, shelf, user, index, render_books_list = homepage
    session.add(shelf(id=1, user_id=2, is_system=True, name='Currently Reading'))
    session.commit()
    user.is_authenticated = authenticated
    with app.test_request_context('/'):
        assert index(1) == 'books fallback'
    render_books_list.assert_called_once_with('newest', 'stored', 1, 1)


@pytest.mark.parametrize('page', ['search', 'advsearch'])
@pytest.mark.parametrize('can_edit', [True, False])
def test_search_selection_controls_follow_permissions(page, can_edit):
    loader = ChoiceLoader([DictLoader({
        'layout.html': '{% block header %}{% endblock %}{% block body %}{% endblock %}'
                       '{% block modal %}{% endblock %}{% block js %}{% endblock %}',
        'image.html': '{% macro book_cover(book) %}cover{% endmacro %}'
                      '{% macro format_pills(book) %}{% endmacro %}',
    }), FileSystemLoader(str(ROOT / 'cps' / 'templates'))])
    env = Environment(loader=loader, autoescape=True)
    env.globals.update(_=lambda text, **kwargs: text % kwargs if kwargs else text,
                       url_for=lambda endpoint, **kwargs: '/static/' + kwargs['filename'] if endpoint == 'static' else '/' + endpoint,
                       csrf_token=lambda: 'token')
    env.filters.update(shortentitle=lambda value, *args: value, music=lambda value: False,
                       formatfloat=lambda value, *args: value)
    user = SimpleNamespace(is_authenticated=True, role_admin=lambda: False, role_edit=lambda: can_edit,
                           shelf=SimpleNamespace(all=lambda: []))
    book = SimpleNamespace(id=42, title='Book', data=[], has_cover=True, authors=[], series=[], ratings=[])

    class Entry:
        Books = book

        def __getitem__(self, key):
            return False

    html = env.get_template('search.html').render(current_user=user, entries=[Entry()], page=page,
        g=SimpleNamespace(shelves_access=[], config_authors_max=0), result_count=1, adv_searchterm='book',
        search_filters=None, simple=True, order='stored', query='book')
    assert 'data-bulk-book-grid' in html
    assert 'data-book-id="42"' in html
    assert ('id="bulk-book-select"' in html) == can_edit
    assert ('id="bulkBookModal"' in html) == can_edit
    assert ('js/bulk-edit.js' in html) == can_edit
