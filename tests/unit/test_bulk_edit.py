# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Exercise bulk patches against separate, real in-memory Calibre and app databases."""

import ast
import importlib
import io
import json
import logging
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import Flask, flash, render_template
from flask_babel import Babel, get_locale
from markupsafe import Markup
from sqlalchemy import create_engine, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workbench(monkeypatch):
    original = {key: value for key, value in sys.modules.items() if key == 'cps' or key.startswith('cps.')}
    for key in original:
        del sys.modules[key]
    package = types.ModuleType('cps')
    package.__path__ = [str(ROOT / 'cps')]
    logger = types.ModuleType('cps.logger')
    logger.create = lambda: logging.getLogger('bulk-test')
    user = types.SimpleNamespace(id=1, role_edit_shelfs=lambda: False, check_visibility=lambda flag: True)
    login = types.ModuleType('cps.cw_login')
    login.current_user = user
    login.AnonymousUserMixin = type('AnonymousUserMixin', (), {})
    login.user_logged_in = types.SimpleNamespace(connect=lambda function: None)
    sys.modules.update({'cps': package, 'cps.logger': logger, 'cps.cw_login': login})
    package.logger = logger
    engines = []
    try:
        ub = importlib.import_module('cps.ub')
        db = importlib.import_module('cps.db')
        for base in (db.Base, ub.Base):
            engine = create_engine('sqlite://', execution_options={'schema_translate_map': {'calibre': None}})
            base.metadata.create_all(engine)
            engines.append(engine)
        library = db.CalibreDB()
        library.session = sessionmaker(bind=engines[0])()
        library.create_functions = Mock()
        accessible = {1, 2, 3}
        library.get_filtered_book = lambda book_id, **kwargs: library.session.get(db.Books, book_id) if book_id in accessible else None
        ub.session = sessionmaker(bind=engines[1])()
        package.calibre_db = library
        package.config = types.SimpleNamespace(config_read_column=0, config_use_google_drive=False, get_book_path=lambda: 'library')
        package.helper = types.SimpleNamespace(
            get_sorted_author=lambda value: value,
            uniq=lambda values: list(dict.fromkeys(values)),
            update_dir_structure=Mock(return_value=False),
            replace_cover_thumbnail_cache=Mock(), save_cover=Mock(return_value=(True, None)))
        bulk = importlib.import_module('cps.bulk_edit')
        from cps.clean_html import clean_string
        from cps.string_helper import strip_whitespaces

        # Load the unchanged single-book helpers without starting unrelated services.
        source = ast.parse((ROOT / 'cps' / 'editbooks.py').read_text(encoding='utf-8'))
        edits = types.ModuleType('cps.editbooks')
        edits.__dict__.update(db=db, calibre_db=library, helper=package.helper, config=package.config,
                              isoLanguages=bulk.isoLanguages, get_locale=get_locale, clean_string=clean_string,
                              strip_whitespaces=strip_whitespaces, Markup=Markup, datetime=datetime,
                              timezone=timezone, func=func, flash=flash, log=logger.create(), _=bulk._)
        functions = [node for node in source.body if isinstance(node, ast.FunctionDef) and not node.decorator_list]
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'editbooks_helpers', 'exec'), edits.__dict__)
        edits._queue_duplicate_scan_after_change = Mock()
        sys.modules['cps.editbooks'] = edits
        package.editbooks = edits
        bulk._export_changes = Mock()
        app = Flask(__name__, template_folder=str(ROOT / 'cps' / 'templates'), static_folder=str(ROOT / 'cps' / 'static'))
        app.secret_key = 'unit-test-only'
        Babel(app)
        app.add_url_rule('/ajax/bulk-books', 'edit-book.bulk_books', lambda: bulk.load_workbench() if bulk.request.method == 'GET' else bulk.save_workbench(), methods=['GET', 'POST'])
        app.add_url_rule('/cover/<int:book_id>', 'web.get_cover', lambda book_id: '')
        author = db.Authors('Original Author', 'Original Author')
        tag = db.Tags('Original tag')
        series = db.Series('Existing Series', 'Existing Series')
        for book_id in (1, 2, 3):
            book = db.Books('Title ' + str(book_id), '', 'Original Author', datetime(2026, 1, 1),
                            datetime(2020, 1, 1), str(book_id + 4), datetime(2026, 1, 2),
                            'Original Author/Title ({})'.format(book_id), 1, [author], [tag])
            book.id = book_id
            book.uuid = 'book-' + str(book_id)
            book.authors = [author]
            book.tags = [tag]
            book.series = [series]
            book.comments = [db.Comments('Original description', book_id)]
            book.identifiers = [db.Identifiers('unique-' + str(book_id), 'isbn', book_id), db.Identifiers('old', 'asin', book_id)]
            library.session.add(book)
        library.session.commit()
        context = app.app_context()
        context.push()
        try:
            yield types.SimpleNamespace(app=app, client=app.test_client(), bulk=bulk, library=library, db=db, ub=ub,
                                        helper=package.helper, edits=edits, user=user, accessible=accessible)
        finally:
            context.pop()
            library.session.close()
            ub.session.close()
    finally:
        for engine in engines:
            engine.dispose()
        for key in list(sys.modules):
            if key == 'cps' or key.startswith('cps.'):
                del sys.modules[key]
        sys.modules.update(original)


def draft(workbench, ids=(1, 2)):
    response = workbench.client.get('/ajax/bulk-books', query_string={'ids': ','.join(map(str, ids))})
    assert response.status_code == 200
    return {'book_ids': list(ids), 'changes': {}, 'titles': {}, 'numbering': None,
            'versions': {str(book['id']): book['version'] for book in response.json['books']}}


def save(workbench, payload):
    return workbench.client.post('/ajax/bulk-books', data={'payload': json.dumps(payload)})


def test_modal_localization_renders_client_placeholders(workbench):
    with workbench.app.test_request_context('/'):
        markup = render_template('bulk_edit.html')
    assert '%(count)s selected' in markup
    assert '%(id)s' in markup
    assert 'bulk-book-config' in markup


def test_ordered_numbering_individual_titles_and_untouched_values(workbench):
    patch = draft(workbench, (2, 1))
    patch['changes'] = {'authors': 'New Author', 'series': 'New Series', 'title': 'Shared title'}
    patch['titles'] = {'1': 'Individual title'}
    patch['numbering'] = {'start': '0.5', 'increment': '1.25'}
    response = save(workbench, patch)
    assert response.status_code == 200, response.json
    first, second, untouched = (workbench.library.session.get(workbench.db.Books, value) for value in (1, 2, 3))
    assert (first.title, second.title, untouched.title) == ('Individual title', 'Shared title', 'Title 3')
    assert (first.series_index, second.series_index, untouched.series_index) == ('1.75', '0.50', '7')
    assert [a.name for a in first.authors] == ['New Author']
    assert first.comments[0].text == 'Original description'
    assert first.tags[0].name == 'Original tag'
    assert first.pubdate == datetime(2020, 1, 1)
    assert [a.name for a in untouched.authors] == ['Original Author']
    workbench.edits._queue_duplicate_scan_after_change.assert_called_once_with([2, 1])


def test_rename_precedes_relationship_edits_and_reuses_canonical_names(workbench):
    observed = []

    def rename(book_id, path, author):
        book = workbench.library.session.get(workbench.db.Books, book_id)
        observed.append((book.title, author, book.comments[0].text, book.tags[0].name))
        return False

    workbench.helper.update_dir_structure.side_effect = rename
    patch = draft(workbench, (1,))
    patch['changes'] = {'title': 'Renamed', 'authors': 'original author', 'tags': 'Replacement', 'comments': ''}
    assert save(workbench, patch).status_code == 200
    assert observed == [('Renamed', 'Original Author', 'Original description', 'Original tag')]
    other = workbench.library.session.get(workbench.db.Books, 3)
    assert other.authors[0].name == 'Original Author'
    assert other.tags[0].name == 'Original tag'


def test_identifier_patch_preserves_unique_types_and_clear_is_explicit(workbench):
    patch = draft(workbench)
    patch['changes'] = {'identifier:asin': 'shared-asin', 'publisher': '', 'rating': '3.5', 'tags': ''}
    assert save(workbench, patch).status_code == 200
    for book_id in patch['book_ids']:
        book = workbench.library.session.get(workbench.db.Books, book_id)
        assert {value.type: value.val for value in book.identifiers} == {'isbn': 'unique-' + str(book_id), 'asin': 'shared-asin'}
        assert book.tags == []
        assert book.ratings[0].rating == 7
        assert book.comments[0].text == 'Original description'


def test_title_only_edit_leaves_other_selected_books_and_sync_records_untouched(workbench):
    patch = draft(workbench)
    patch['titles'] = {'1': 'Only this title'}
    workbench.ub.session.add(workbench.ub.KoboSyncedBooks(user_id=1, book_id=2))
    workbench.ub.session.commit()
    response = save(workbench, patch)
    assert response.status_code == 200
    assert [result['status'] for result in response.json['results']] == ['saved', 'unchanged']
    assert workbench.ub.session.query(workbench.ub.KoboSyncedBooks).one().book_id == 2
    assert workbench.library.session.get(workbench.db.Books, 2).last_modified == datetime(2026, 1, 2)


@pytest.mark.parametrize('change', [
    {'titles': {'1': ''}}, {'titles': {'3': 'Not selected'}},
    {'changes': {'rating': 'NaN'}}, {'changes': {'series_index': '-1'}},
    {'changes': {'authors': ' & '}}, {'changes': {'read_status': ''}},
    {'changes': {'cover_url': 'file:///etc/passwd'}}, {'changes': {'languages': 'Not a language'}},
    {'changes': {'custom_column_999': 'invalid'}},
    {'numbering': {'start': '1', 'increment': '0'}},
    {'changes': {'series': ''}, 'numbering': {'start': '1', 'increment': '1'}},
])
def test_invalid_batch_rejected_before_any_writes(workbench, change):
    patch = draft(workbench)
    patch.update(change)
    response = save(workbench, patch)
    assert response.status_code == 400, response.json
    assert workbench.library.session.get(workbench.db.Books, 1).title == 'Title 1'
    workbench.helper.update_dir_structure.assert_not_called()
    workbench.bulk._export_changes.assert_not_called()


def test_inaccessible_selection_and_stale_snapshot_do_not_write(workbench):
    patch = draft(workbench)
    patch['changes'] = {'title': 'Will not save'}
    workbench.accessible.remove(2)
    assert save(workbench, patch).status_code == 400
    workbench.accessible.add(2)
    workbench.library.session.get(workbench.db.Books, 2).last_modified = datetime(2026, 2, 1)
    workbench.library.session.commit()
    assert save(workbench, patch).status_code == 400
    assert workbench.library.session.get(workbench.db.Books, 1).title == 'Title 1'


def test_user_state_is_explicit_and_unmanageable_shelves_are_preserved(workbench):
    ub = workbench.ub
    owned = ub.Shelf(name='Own shelf', user_id=1, is_public=0)
    public = ub.Shelf(name='Someone else', user_id=2, is_public=1)
    ub.session.add_all([owned, public])
    ub.session.flush()
    owned.books.append(ub.BookShelf(book_id=1, shelf=owned.id, order=1))
    public.books.append(ub.BookShelf(book_id=1, shelf=public.id, order=1))
    ub.session.add_all([ub.ArchivedBook(user_id=1, book_id=1, is_archived=True),
                        ub.ReadBook(user_id=2, book_id=1, read_status=1)])
    ub.session.commit()
    patch = draft(workbench, (1,))
    patch['changes'] = {'shelves': [], 'read_status': '0', 'archived': '0'}
    assert save(workbench, patch).status_code == 200
    assert ub.session.query(ub.ReadBook).filter_by(user_id=1).one().read_status == 0
    assert ub.session.query(ub.ReadBook).filter_by(user_id=2).one().read_status == 1
    assert ub.session.query(ub.ArchivedBook).one().is_archived is False
    assert [link.shelf for link in ub.session.query(ub.BookShelf).filter_by(book_id=1)] == [public.id]
    assert workbench.library.session.get(workbench.db.Books, 1).last_modified == datetime(2026, 1, 2)
    patch = draft(workbench, (2,))
    patch['changes'] = {'shelves': [str(owned.id)]}
    assert save(workbench, patch).status_code == 200
    assert ub.session.query(ub.BookShelf).filter_by(book_id=2).one().shelf == owned.id


def test_partial_failure_reports_saved_error_and_unattempted_books(workbench):
    patch = draft(workbench, (1, 2, 3))
    patch['changes'] = {'title': 'New title'}
    workbench.helper.update_dir_structure.side_effect = [False, 'Permission denied']
    response = save(workbench, patch)
    assert response.status_code == 207, response.json
    assert [result['status'] for result in response.json['results']] == ['saved', 'error', 'skipped']
    assert response.json['success'] is False
    assert workbench.library.session.get(workbench.db.Books, 1).title == 'New title'
    assert workbench.library.session.get(workbench.db.Books, 2).title == 'Title 2'
    assert workbench.library.session.get(workbench.db.Books, 3).title == 'Title 3'


def test_shared_cover_upload_is_rewound_for_each_book(workbench):
    patch = draft(workbench)
    contents = []

    def store_cover(upload, path):
        contents.append(upload.stream.read())
        return True, None

    workbench.helper.save_cover.side_effect = store_cover
    response = workbench.client.post('/ajax/bulk-books', data={
        'payload': json.dumps(patch), 'cover_file': (io.BytesIO(b'cover bytes'), 'cover.jpg', 'image/jpeg')})
    assert response.status_code == 200, response.json
    assert contents == [b'cover bytes', b'cover bytes']
    assert workbench.helper.replace_cover_thumbnail_cache.call_count == 2


def test_app_commit_failure_reports_that_metadata_was_already_saved(workbench, monkeypatch):
    patch = draft(workbench)
    patch['changes'] = {'title': 'Committed title', 'read_status': '1'}
    monkeypatch.setattr(workbench.ub.session, 'commit', Mock(side_effect=SQLAlchemyError('app database busy')))
    response = save(workbench, patch)
    assert response.status_code == 207
    assert response.json['results'][0]['metadata_saved'] is True
    assert response.json['results'][1]['status'] == 'skipped'
    assert workbench.library.session.get(workbench.db.Books, 1).title == 'Committed title'
    assert workbench.ub.session.query(workbench.ub.ReadBook).count() == 0


def test_enforcement_failure_surfaces_a_warning_without_claiming_rollback(workbench):
    patch = draft(workbench, (1,))
    patch['changes'] = {'publisher': 'Publisher'}
    workbench.bulk._export_changes.side_effect = OSError('disk full')
    response = save(workbench, patch)
    assert response.status_code == 200
    assert response.json['results'][0]['warnings']
    assert workbench.library.session.get(workbench.db.Books, 1).publishers[0].name == 'Publisher'


def test_later_book_revision_is_checked_again_after_earlier_saves(workbench):
    patch = draft(workbench)
    patch['changes'] = {'title': 'Updated'}

    def concurrent_update(book, changes):
        later = workbench.library.session.get(workbench.db.Books, 2)
        later.last_modified = datetime(2026, 2, 1)
        workbench.library.session.commit()

    workbench.bulk._export_changes.side_effect = concurrent_update
    response = save(workbench, patch)
    assert response.status_code == 207
    assert [result['status'] for result in response.json['results']] == ['saved', 'error']
    assert 'changed since' in response.json['results'][1]['error']


def test_custom_columns_clear_boolean_zero_and_enumeration(workbench):
    db = workbench.db
    columns = [db.CustomColumns(id=1, name='Flag', datatype='bool', is_multiple=False, display='{}'),
               db.CustomColumns(id=2, name='Count', datatype='int', is_multiple=False, display='{}'),
               db.CustomColumns(id=3, name='Choice', datatype='enumeration', is_multiple=False,
                                display='{"enum_values":["One","Two"]}'),
               db.CustomColumns(id=4, name='Stars', datatype='rating', is_multiple=False, display='{}')]
    db.CalibreDB.setup_db_cc_classes(columns)
    db.Base.metadata.create_all(workbench.library.session.bind)
    workbench.library.session.add_all(columns)
    book = workbench.library.session.get(db.Books, 1)
    book.custom_column_1 = [db.cc_classes[1](value=1, book=1)]
    book.custom_column_2 = [db.cc_classes[2](value=5, book=1)]
    book.custom_column_4 = [db.cc_classes[4](value='7')]
    workbench.library.session.commit()
    patch = draft(workbench, (1,))
    patch['changes'] = {'custom_column_1': '', 'custom_column_2': '0', 'custom_column_3': 'Two', 'custom_column_4': '4.5'}
    assert save(workbench, patch).status_code == 200
    workbench.library.session.refresh(book)
    assert book.custom_column_1 == []
    assert book.custom_column_2[0].value == 0
    assert book.custom_column_3[0].value == 'Two'
    assert book.custom_column_4[0].value == '9'


def test_shared_read_column_updates_metadata_not_user_read_record(workbench):
    db = workbench.db
    column = db.CustomColumns(id=1, name='Read', datatype='bool', is_multiple=False, display='{}')
    db.CalibreDB.setup_db_cc_classes([column])
    db.Base.metadata.create_all(workbench.library.session.bind)
    workbench.library.session.add(column)
    workbench.library.session.commit()
    workbench.bulk.config.config_read_column = 1
    patch = draft(workbench, (1,))
    patch['changes'] = {'read_status': '0'}
    response = save(workbench, patch)
    assert response.status_code == 200, response.json
    assert workbench.library.session.get(db.Books, 1).custom_column_1[0].value is False
    assert workbench.ub.session.query(workbench.ub.ReadBook).count() == 0
