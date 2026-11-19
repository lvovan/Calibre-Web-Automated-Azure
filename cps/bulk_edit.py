# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Patch-based bulk editing; filesystem-aware metadata helpers remain in editbooks."""

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from flask import jsonify, request, url_for
from flask_babel import get_locale, gettext as _
from sqlalchemy import func, or_
from sqlalchemy.exc import SQLAlchemyError

from . import calibre_db, config, db, helper, isoLanguages, logger, ub
from .cw_login import current_user

log = logger.create()
MAX_BOOKS = 100


class InvalidEdit(ValueError):
    pass


def _ids(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_BOOKS
            or any(type(value) is not int or value <= 0 for value in values)
            or len(set(values)) != len(values)):
        raise InvalidEdit(_('Select between 1 and %(limit)s distinct books.', limit=MAX_BOOKS))
    return values


def _books(ids):
    books = [calibre_db.get_filtered_book(book_id, allow_show_archived=True) for book_id in ids]
    if any(book is None for book in books):
        raise InvalidEdit(_('A selected book is unavailable or you no longer have access to it.'))
    return books


def _date(value):
    return value.isoformat() if value else ''


def _columns():
    return calibre_db.session.query(db.CustomColumns).order_by(db.CustomColumns.id).all()


def _shelves():
    return ub.session.query(ub.Shelf).filter(or_(
        ub.Shelf.user_id == current_user.id, ub.Shelf.is_public == 1)).order_by(ub.Shelf.name).all()


def _can_edit_shelf(shelf):
    return bool(current_user.role_edit_shelfs()) if shelf.is_public else shelf.user_id == int(current_user.id)


def _field(key, label, group, kind='text', **kwargs):
    return dict(key=key, label=label, group=group, type=kind, **kwargs)


def schema(columns, shelves):
    fields = [
        _field('title', _('Shared title (optional)'), 'details', wide=True, clearable=False),
        _field('authors', _('Authors'), 'details', wide=True, clearable=False),
        _field('publisher', _('Publisher'), 'details'),
        _field('pubdate', _('Published Date'), 'details', 'date'),
        _field('languages', _('Languages'), 'details'),
        _field('rating', _('Rating'), 'details', 'number', min=0, max=5, step=.5),
        _field('series', _('Series'), 'series'),
        _field('series_index', _('Series ID'), 'series', 'number', min=0, step=.01, clearable=False),
        _field('tags', _('Tags'), 'organize', wide=True),
        _field('shelves', _('Shelves'), 'organize', 'multiple', wide=True,
               options=[dict(value=str(s.id), label=s.name) for s in shelves if _can_edit_shelf(s)]),
        _field('read_status', _('Read status'), 'organize', 'select', clearable=False, options=[
            dict(value='0', label=_('Unread')), dict(value='1', label=_('Read'))
        ] + ([] if config.config_read_column else [dict(value='2', label=_('In progress'))]),
               help=_('Uses the configured shared read-status column.') if config.config_read_column
               else _('Changes your reading status only.')),
        _field('comments', _('Description'), 'content', 'textarea', wide=True),
        _field('cover_url', _('Cover image URL'), 'content', 'url', wide=True),
    ]
    if current_user.check_visibility(32768):
        fields.append(_field('archived', _('Archive status'), 'organize', 'select', clearable=False, options=[
            dict(value='0', label=_('Not archived')), dict(value='1', label=_('Archived'))]))
    for column in columns:
        if column.id == config.config_read_column:
            continue
        kind = {'bool': 'select', 'enumeration': 'select', 'datetime': 'date',
                'comments': 'textarea', 'int': 'number', 'float': 'number', 'rating': 'number'}.get(
                    column.datatype, 'text')
        options = {}
        if column.datatype == 'bool':
            options['options'] = [dict(value='True', label=_('Yes')), dict(value='False', label=_('No'))]
        elif column.datatype == 'enumeration':
            options['options'] = [dict(value=v, label=v) for v in column.get_display_dict().get('enum_values', [])]
        if kind == 'number':
            options['step'] = 1 if column.datatype == 'int' else .5 if column.datatype == 'rating' else .01
        if column.datatype == 'rating':
            options.update(min=0, max=5)
        fields.append(_field('custom_column_' + str(column.id), column.name, 'custom', kind,
                             readonly=column.datatype in db.cc_exceptions,
                             wide=kind == 'textarea', **options))
    return fields


def _custom_value(book, column):
    values = getattr(book, 'custom_column_' + str(column.id), [])
    if not values:
        return ''
    if column.datatype == 'bool':
        return 'True' if values[0].value else 'False'
    if column.datatype == 'datetime':
        return values[0].value.strftime('%Y-%m-%d') if values[0].value else ''
    if column.datatype == 'rating':
        return str(float(values[0].value) / 2)
    if column.datatype == 'series':
        return ', '.join('{} [{}]'.format(v.value, v.extra) for v in values)
    text = [str(v.value) for v in values]
    return ', '.join(sorted(text) if column.is_multiple else text)


def serialize_book(book, columns, shelves):
    read = ub.session.query(ub.ReadBook).filter_by(user_id=current_user.id, book_id=book.id).first()
    archived = ub.session.query(ub.ArchivedBook).filter_by(user_id=current_user.id, book_id=book.id).first()
    state = ub.session.query(ub.KoboReadingState).filter_by(user_id=current_user.id, book_id=book.id).first()
    bookmark = state.current_bookmark if state else None
    links = ub.session.query(ub.BookShelf).filter(ub.BookShelf.book_id == book.id,
                                                ub.BookShelf.shelf.in_([s.id for s in shelves])).all()
    values = dict(
        title=book.title, authors=' & '.join(a.name.replace('|', ',') for a in calibre_db.order_authors([book])),
        publisher=book.publishers[0].name if book.publishers else '',
        pubdate=book.pubdate.strftime('%Y-%m-%d') if book.pubdate and book.pubdate.year > 101 else '',
        languages=', '.join(sorted(isoLanguages.get_language_name(get_locale(), v.lang_code) for v in book.languages)),
        rating=str(book.ratings[0].rating / 2) if book.ratings else '',
        series=book.series[0].name if book.series else '', series_index=str(book.series_index),
        tags=', '.join(sorted(t.name for t in book.tags)),
        shelves=sorted(str(link.shelf) for link in links if any(s.id == link.shelf and _can_edit_shelf(s) for s in shelves)),
        read_status=str(read.read_status if read else ub.ReadBook.STATUS_UNREAD),
        archived='1' if archived and archived.is_archived else '0',
        comments=book.comments[0].text if book.comments else '', cover_url='',
    )
    for column in columns:
        values['custom_column_' + str(column.id)] = _custom_value(book, column)
    if config.config_read_column:
        values['read_status'] = '1' if values.get('custom_column_' + str(config.config_read_column)) == 'True' else '0'
    return dict(id=book.id, version=_date(book.last_modified), values=values,
                identifiers={i.type.lower(): i.val for i in book.identifiers},
                cover=url_for('web.get_cover', book_id=book.id, c=_date(book.last_modified)),
                system=dict(uuid=book.uuid, added=_date(book.timestamp), modified=_date(book.last_modified),
                            formats=', '.join('{} ({:.2f} MiB)'.format(f.format, f.uncompressed_size / 1048576)
                                              for f in book.data),
                            progress=bookmark.progress_percent if bookmark else None,
                            progress_updated=_date(bookmark.last_modified) if bookmark else ''))


def load_workbench():
    try:
        raw = request.args.get('ids', '').split(',')
        if any(not re.fullmatch(r'[1-9][0-9]*', value) for value in raw):
            raise InvalidEdit(_('Invalid book selection.'))
        ids = _ids([int(value) for value in raw])
        books, columns, shelves = _books(ids), _columns(), _shelves()
        return jsonify(fields=schema(columns, shelves), books=[serialize_book(b, columns, shelves) for b in books])
    except InvalidEdit as error:
        return jsonify(error=str(error)), 400


def _number(value, label, minimum=None, maximum=None, step=None):
    if not isinstance(value, str):
        raise InvalidEdit(_('Invalid value for %(field)s.', field=label))
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise InvalidEdit(_('Invalid number for %(field)s.', field=label)) from error
    if (not number.is_finite() or abs(number) > 1000000000
            or (minimum is not None and number < Decimal(str(minimum)))
            or (maximum is not None and number > Decimal(str(maximum)))
            or (step and number % Decimal(str(step)))):
        raise InvalidEdit(_('Invalid number for %(field)s.', field=label))
    return number


def validate_payload(payload, books, fields):
    if not isinstance(payload, dict) or set(payload) - {'book_ids', 'changes', 'titles', 'numbering', 'versions'}:
        raise InvalidEdit(_('Invalid bulk edit request.'))
    changes, titles, versions = (payload.get(key, {}) for key in ('changes', 'titles', 'versions'))
    if not all(isinstance(value, dict) for value in (changes, titles, versions)):
        raise InvalidEdit(_('Invalid bulk edit fields.'))
    ids = {str(b.id) for b in books}
    if set(titles) - ids or set(versions) != ids:
        raise InvalidEdit(_('The selection has changed. Close the editor and try again.'))
    for book in books:
        if versions[str(book.id)] != _date(book.last_modified):
            raise InvalidEdit(_('Book %(id)s changed since the editor opened. Reload before saving.', id=book.id))
    allowed = {f['key']: f for f in fields if not f.get('readonly')}
    identifier_types = {identifier.type.lower() for book in books for identifier in book.identifiers}
    for key, value in changes.items():
        if key.startswith('identifier:'):
            if not re.fullmatch(r'identifier:[a-z][a-z0-9_-]{0,63}', key) and key[11:] not in identifier_types:
                raise InvalidEdit(_('Invalid identifier type.'))
            field = dict(label=_('Identifier'), type='text')
        elif key in allowed:
            field = allowed[key]
        else:
            raise InvalidEdit(_('An edited field is unavailable or read-only.'))
        if field['type'] == 'multiple':
            permitted = {o['value'] for o in field['options']}
            if not isinstance(value, list) or any(not isinstance(v, str) or v not in permitted for v in value):
                raise InvalidEdit(_('You cannot modify one of the selected shelves.'))
            continue
        if not isinstance(value, str) or len(value) > 1000000:
            raise InvalidEdit(_('Invalid value for %(field)s.', field=field['label']))
        if value and field['type'] == 'number':
            _number(value, field['label'], field.get('min'), field.get('max'), field.get('step'))
        if value and field['type'] == 'date':
            try:
                datetime.strptime(value, '%Y-%m-%d')
            except ValueError as error:
                raise InvalidEdit(_('Invalid date for %(field)s.', field=field['label'])) from error
        if field['type'] == 'select' and value not in ['', *[o['value'] for o in field['options']]]:
            raise InvalidEdit(_('Invalid choice for %(field)s.', field=field['label']))
    if 'authors' in changes and not any(name.strip() for name in changes['authors'].split('&')):
        raise InvalidEdit(_('Authors cannot be empty.'))
    if 'read_status' in changes and changes['read_status'] == '':
        raise InvalidEdit(_('Choose a read status.'))
    if 'archived' in changes and changes['archived'] == '':
        raise InvalidEdit(_('Choose an archive status.'))
    if 'series_index' in changes and changes['series_index'] == '':
        raise InvalidEdit(_('Series ID cannot be empty.'))
    if changes.get('cover_url') and not re.match(r'^https?://', changes['cover_url'], re.I):
        raise InvalidEdit(_('Cover URLs must use HTTP or HTTPS.'))
    if 'languages' in changes:
        invalid = []
        isoLanguages.get_language_code_from_name(get_locale(), changes['languages'].split(','), invalid)
        if invalid:
            raise InvalidEdit(_('Invalid languages: %(languages)s', languages=', '.join(invalid)))
    for book in books:
        title = titles.get(str(book.id), changes.get('title', book.title))
        if not isinstance(title, str) or not title.strip() or len(title) > 10000:
            raise InvalidEdit(_('A title is required for book %(id)s.', id=book.id))
    numbering = payload.get('numbering')
    if numbering is not None:
        if not isinstance(numbering, dict) or set(numbering) != {'start', 'increment'}:
            raise InvalidEdit(_('Invalid series numbering.'))
        start = _number(numbering['start'], _('Start at'), 0, step=.01)
        increment = _number(numbering['increment'], _('Increment'), .01, step=.01)
        if start + increment * (len(books) - 1) > 1000000000:
            raise InvalidEdit(_('Series numbering is too large.'))
        if any(not changes.get('series', b.series[0].name if b.series else '').strip() for b in books):
            raise InvalidEdit(_('Set a series for all selected books before auto-numbering.'))
    return changes, titles, numbering


def _apply_user_state(book, changes, shelves):
    now = datetime.now(timezone.utc)
    if 'read_status' in changes and not config.config_read_column:
        read = ub.session.query(ub.ReadBook).filter_by(user_id=current_user.id, book_id=book.id).first()
        if read is None:
            read = ub.ReadBook(user_id=current_user.id, book_id=book.id)
            ub.session.add(read)
        read.read_status = int(changes['read_status'])
        read.last_modified = now
    if 'archived' in changes:
        archived = ub.session.query(ub.ArchivedBook).filter_by(user_id=current_user.id, book_id=book.id).first()
        if archived is None:
            archived = ub.ArchivedBook(user_id=current_user.id, book_id=book.id)
            ub.session.add(archived)
        archived.is_archived = changes['archived'] == '1'
        archived.last_modified = now
    if 'shelves' in changes:
        desired = set(changes['shelves'])
        for shelf in shelves:
            if not _can_edit_shelf(shelf):
                continue
            link = ub.session.query(ub.BookShelf).filter_by(book_id=book.id, shelf=shelf.id).first()
            if link and str(shelf.id) not in desired:
                ub.session.delete(link)
                shelf.last_modified = now
            elif link is None and str(shelf.id) in desired:
                order = ub.session.query(func.max(ub.BookShelf.order)).filter_by(shelf=shelf.id).scalar() or 0
                shelf.books.append(ub.BookShelf(book_id=book.id, shelf=shelf.id, order=order + 1))
                shelf.last_modified = now


def _apply_metadata(book, changes, columns, cover_file):
    from . import editbooks as edits

    rename = False
    authors = [a.name for a in calibre_db.order_authors([book])]
    if 'title' in changes:
        rename |= edits.handle_title_on_edit(book, changes['title'])
    if 'authors' in changes:
        previous = authors
        assigned = _replace_named(book, 'authors', db.Authors, changes['authors'].split('&'))
        authors = [author.name for author in assigned]
        book.author_sort = ' & '.join(author.sort or helper.get_sorted_author(author.name) for author in assigned)
        rename |= previous != authors
    # Renames may flush the session: do them before relationship-heavy edits.
    if rename:
        error = helper.update_dir_structure(book.id, config.get_book_path(), authors[0])
        if error:
            raise InvalidEdit(str(error))
    for key, relationship, model in [('tags', 'tags', db.Tags), ('series', 'series', db.Series),
                                      ('publisher', 'publishers', db.Publishers)]:
        if key in changes:
            _replace_named(book, relationship, model, changes[key].split(',') if key == 'tags' else [changes[key]])
    for key, edit in [('comments', edits.edit_book_comments), ('languages', edits.edit_book_languages)]:
        if key in changes:
            edit(changes[key], book)
    if 'rating' in changes:
        edits.edit_book_ratings(changes, book)
    if 'series_index' in changes:
        edits.edit_book_series_index(format(Decimal(changes['series_index']), 'f'), book)
    if 'series' in changes and not changes['series'].strip():
        book.series_index = 1
    if 'pubdate' in changes:
        book.pubdate = datetime.strptime(changes['pubdate'], '%Y-%m-%d') if changes['pubdate'] else db.Books.DEFAULT_PUBDATE
    identifier_changes = {key.split(':', 1)[1]: value.strip() for key, value in changes.items()
                          if key.startswith('identifier:')}
    if identifier_changes:
        identifiers = {i.type.lower(): i.val for i in book.identifiers}
        identifiers.update(identifier_changes)
        edits.modify_identifiers([db.Identifiers(value, key, book.id) for key, value in identifiers.items() if value],
                                 book.identifiers, calibre_db.session)
    custom = {key: value for key, value in changes.items() if key.startswith('custom_column_')}
    if 'read_status' in changes and config.config_read_column:
        custom['custom_column_' + str(config.config_read_column)] = 'True' if changes['read_status'] == '1' else 'False'
    for column in columns:
        key = 'custom_column_' + str(column.id)
        if key in custom and not custom[key]:
            for value in list(getattr(book, key)):
                getattr(book, key).remove(value)
                if not getattr(value, 'books', []):
                    calibre_db.session.delete(value)
            del custom[key]
    if custom:
        edits.edit_cc_data(book.id, book, dict(custom), columns)
    if cover_file:
        cover_file.stream.seek(0)
        success, error = helper.save_cover(cover_file, book.path)
        if not success:
            raise InvalidEdit(str(error))
        book.has_cover = 1
    elif 'cover_url' in changes:
        if changes['cover_url']:
            success, error = helper.save_cover_from_url(changes['cover_url'], book.path)
            if not success:
                raise InvalidEdit(str(error))
            book.has_cover = 1
        else:
            book.has_cover = 0
    book.last_modified = datetime.now(timezone.utc)
    calibre_db.set_metadata_dirty(book.id)


def _replace_named(book, relationship, model, names):
    # Single-book helpers can rename shared records (and unselected books). Reuse
    # canonical records here without changing their spelling or other memberships.
    previous, assigned = list(getattr(book, relationship)), []
    for name in dict.fromkeys(name.strip().replace(',', '|') if relationship == 'authors'
                              else name.strip() for name in names):
        if not name:
            continue
        value = calibre_db.session.query(model).filter(model.name == name).first()
        if value is None:
            if relationship == 'authors':
                value = model(name, helper.get_sorted_author(name.replace('|', ',')))
            elif relationship == 'series':
                value = model(name, name)
            elif relationship == 'publishers':
                value = model(name, None)
            else:
                value = model(name)
            calibre_db.session.add(value)
        if all(value is not item for item in assigned):
            assigned.append(value)
    setattr(book, relationship, assigned)
    for value in previous:
        if not value.books:
            calibre_db.session.delete(value)
    return assigned


def _export_changes(book, changes):
    payload = dict(changes, title=book.title, authors=' & '.join(a.name for a in book.authors))
    payload['_cwa_meta'] = dict(modify_date=True, change_count=len(changes),
                                has_content=any(v != '' for v in changes.values()),
                                timestamp=datetime.now(timezone.utc).isoformat())
    folder = '/app/calibre-web-automated/metadata_change_logs'
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, '{}-{}.json'.format(datetime.now().strftime('%Y%m%d%H%M%S%f'), book.id))
    with open(path, 'w', encoding='utf-8') as output:
        json.dump(payload, output, ensure_ascii=False)


def save_workbench():
    try:
        payload = json.loads(request.form.get('payload', '{}'))
        if not isinstance(payload, dict):
            raise InvalidEdit(_('Invalid bulk edit request.'))
        books = _books(_ids(payload.get('book_ids')))
        columns, shelves = _columns(), _shelves()
        changes, titles, numbering = validate_payload(payload, books, schema(columns, shelves))
        cover_file = request.files.get('cover_file')
        if cover_file and cover_file.mimetype not in {'image/jpeg', 'image/png', 'image/webp'}:
            raise InvalidEdit(_('Choose a JPEG, PNG, or WebP cover image.'))
        if not changes and not titles and not numbering and not cover_file:
            raise InvalidEdit(_('No changes selected.'))
    except (InvalidEdit, json.JSONDecodeError) as error:
        log.warning('Bulk edit rejected: %s', error)
        return jsonify(error=str(error)), 400

    results, updated, failed = [], [], False
    for index, book in enumerate(books):
        if failed:
            results.append(dict(id=book.id, status='skipped', error=_('Not attempted after the previous error.')))
            continue
        patch = dict(changes)
        if str(book.id) in titles:
            patch['title'] = titles[str(book.id)]
        if numbering:
            patch['series_index'] = format(Decimal(numbering['start']) + index * Decimal(numbering['increment']), 'f')
        if not patch and not cover_file:
            results.append(dict(id=book.id, status='unchanged'))
            continue
        metadata = any(key not in {'shelves', 'archived', 'read_status'} for key in patch) or bool(cover_file)
        metadata |= bool('read_status' in patch and config.config_read_column)
        metadata_saved = False
        try:
            calibre_db.create_functions(config)
            calibre_db.session.refresh(book)
            if payload['versions'][str(book.id)] != _date(book.last_modified):
                raise InvalidEdit(_('Book %(id)s changed since the editor opened. Reload before saving.', id=book.id))
            if metadata:
                _apply_metadata(book, patch, columns, cover_file)
                calibre_db.session.commit()
                metadata_saved = True
                updated.append(book.id)
            _apply_user_state(book, patch, shelves)
            sync_query = ub.session.query(ub.KoboSyncedBooks).filter_by(book_id=book.id)
            if not metadata:
                sync_query = sync_query.filter_by(user_id=current_user.id)
            sync_query.delete(synchronize_session=False)
            ub.session.commit()
            warnings = []
            if metadata:
                try:
                    _export_changes(book, patch)
                except OSError as error:
                    log.error('Bulk edit metadata export failed for %s: %s', book.id, error)
                    warnings.append(_('Metadata saved, but writing the enforcement log failed.'))
            if 'cover_url' in patch or cover_file:
                helper.replace_cover_thumbnail_cache(book.id, book_path=book.path, last_modified=book.last_modified)
            if metadata and config.config_use_google_drive:
                from . import gdriveutils
                gdriveutils.updateGdriveCalibreFromLocal()
            results.append(dict(id=book.id, status='saved', warnings=warnings))
        except (InvalidEdit, SQLAlchemyError, OSError, ValueError) as error:
            calibre_db.session.rollback()
            ub.session.rollback()
            log.error('Bulk edit failed for book %s: %s', book.id, error)
            results.append(dict(id=book.id, status='error', metadata_saved=metadata_saved,
                                error=str(error), warning=_('Some changes or file operations may already have completed. Reload before retrying.')))
            failed = True
    if updated:
        from .editbooks import _queue_duplicate_scan_after_change
        _queue_duplicate_scan_after_change(updated)
    return jsonify(success=not failed, results=results), 207 if failed else 200
