#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2025 Calibre-Web contributors
# Copyright (C) 2024-2025 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""
KOReader Sync Server Implementation for Calibre-Web-Automated

This module provides a sync server compatible with KOReader's sync functionality,
allowing users to sync their reading progress across devices.

Protocol Specification:
    - Authentication, either:
        * HTTP Basic Auth (RFC 7617), sent by the CWA KOReader plugin
        * x-auth-user / x-auth-key headers (key = md5(password)), sent by KOReader's built-in
          progress sync. The key is verified against ub.KOSyncKey, captured at web login.
    - Endpoints:
        * POST /kosync/users/create - Registration (always refused with an explanation)
        * GET  /kosync/users/auth - Authenticate user
        * GET  /kosync/syncs/progress/<document> - Get reading progress
        * PUT  /kosync/syncs/progress - Update reading progress
        * GET  /kosync - Plugin download page

Security:
    - All API endpoints use HTTP Basic Authentication
    - Document identifiers validated to prevent injection attacks
    - Session management via SQLAlchemy with proper isolation
    - Rate limiting should be applied at reverse proxy level

Integration:
    - Syncs with Calibre library via BookFormatChecksum table
    - Updates ReadBook status based on reading percentage thresholds
    - Maintains KoboReadingState for compatibility with Kobo sync
    - Atomic commits ensure sync data integrity

Based on the reference implementation from koreader-sync-server
Reference: https://github.com/koreader/koreader-sync-server
"""

import base64
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Any, Tuple

from ...services import SyncToken as SyncToken, hardcover
from ...kobo import push_reading_state_to_hardcover

from flask import Blueprint, request, jsonify
from flask_babel import gettext as _
from werkzeug.security import check_password_hash
from sqlalchemy import func, desc
from sqlalchemy.exc import SQLAlchemyError

from ... import logger, ub, csrf, config, constants, services, usermanagement
from ...render_template import render_title_template
from ..checksums.filename import FILENAME_CHECKSUM_VERSION, filename_hashes
from ..models import KOSyncProgress
from ..settings import is_koreader_sync_enabled

log = logger.create()

# Create the blueprint
kosync = Blueprint('kosync', __name__)

# Error codes matching KOReader sync server specification
ERROR_NO_STORAGE = 1000
ERROR_INTERNAL = 2000
ERROR_UNAUTHORIZED_USER = 2001
ERROR_USER_EXISTS = 2002
ERROR_INVALID_FIELDS = 2003
ERROR_DOCUMENT_FIELD_MISSING = 2004

# Field names (constants for API contract)
PROGRESS_FIELD = "progress"
PERCENTAGE_FIELD = "percentage"
DEVICE_FIELD = "device"
DEVICE_ID_FIELD = "device_id"
TIMESTAMP_FIELD = "timestamp"

# Validation constants
MAX_DOCUMENT_LENGTH = 255  # Maximum document identifier length
MAX_PROGRESS_LENGTH = 255  # Maximum progress string length
MAX_DEVICE_LENGTH = 100    # Maximum device name length
MAX_DEVICE_ID_LENGTH = 100 # Maximum device ID length

UNAUTHORIZED_MESSAGE = ("Unauthorized. Use your Calibre-Web Automated username and password. "
                        "For KOReader's built-in progress sync, sign in to the web interface once "
                        "with that password first.")


def _require_kosync_enabled():
    # 401 is one of the few statuses KOReader understands, so the device shows this message
    # instead of "Unknown server error"
    if not is_koreader_sync_enabled():
        return create_sync_response({
            "error": ERROR_NO_STORAGE,
            "message": "KOReader sync is disabled on this server"
        }, 401)
    return None


class KOSyncError(Exception):
    """Custom exception for KOSync protocol errors"""
    def __init__(self, error_code: int, message: str):
        self.error_code = error_code
        self.message = message
        super().__init__(message)


def is_valid_field(field: Any) -> bool:
    """
    Check if a field is valid (not None, not empty string).

    Args:
        field: Value to validate

    Returns:
        True if field is a non-empty string
    """
    return isinstance(field, str) and len(field) > 0


def is_valid_key_field(field: Any, max_length: int = MAX_DOCUMENT_LENGTH) -> bool:
    """
    Check if a field is valid as a database key.

    Key fields must be non-empty strings without colons (reserved for internal use)
    and within specified length limits.

    Args:
        field: Value to validate
        max_length: Maximum allowed length

    Returns:
        True if field is valid for use as a key
    """
    return is_valid_field(field) and ":" not in field and len(field) <= max_length


def authenticate_user() -> Optional[ub.User]:
    """
    Authenticate user using HTTP Basic Authentication (RFC 7617).

    Expects Authorization header with format: 'Basic <base64(username:password)>'

    Security considerations:
        - Uses constant-time password comparison via check_password_hash
        - Case-insensitive username lookup for consistency with Calibre-Web
        - Validates credential format before database lookup

    Returns:
        User object if authentication succeeds, None otherwise
    """
    if config.config_allow_reverse_proxy_header_login:
        if user := usermanagement.load_user_from_reverse_proxy_header(request):
            return user

    auth_header = request.headers.get('Authorization')

    if (not auth_header or not auth_header.startswith('Basic ')) and \
            ('x-auth-user' in request.headers or 'x-auth-key' in request.headers):
        return _authenticate_kosync_key()

    if not auth_header or not auth_header.startswith('Basic '):
        log.debug("Missing or invalid Authorization header")
        return None

    try:
        # Extract and decode the base64 encoded credentials
        encoded_credentials = auth_header[6:]  # Remove 'Basic ' prefix
        decoded_credentials = base64.b64decode(encoded_credentials).decode('utf-8')

        # Split username and password (allow colons in password)
        if ':' not in decoded_credentials:
            log.debug("Invalid credential format (missing colon separator)")
            return None

        username, password = decoded_credentials.split(':', 1)

    except (ValueError, UnicodeDecodeError) as e:
        log.warning(f"Failed to decode credentials: {str(e)}")
        return None

    # Validate field formats before database lookup
    if not is_valid_field(password) or not is_valid_key_field(username, max_length=MAX_DEVICE_LENGTH):
        log.debug(f"Invalid username or password format")
        return None

    # Find user by username (case-insensitive for Calibre-Web compatibility)
    try:
        user = ub.session.query(ub.User).filter(
            func.lower(ub.User.name) == username.lower()
        ).first()
    except SQLAlchemyError as e:
        log.error(f"Database error during user lookup: {e}")
        return None

    if not user:
        log.debug(f"User not found: {username}")
        return None

    # Check if LDAP authentication is enabled
    if config.config_login_type == constants.LOGIN_LDAP and services.ldap:
        # Try LDAP authentication
        login_result, error = services.ldap.bind_user(user.name, password)
        if login_result:
            log.info(f"authenticate_user: Successfully authenticated user via LDAP: {user.name}")
            return user

        # Log LDAP failure but continue to local check (fallback)
        # We use debug level here because failure is expected if the user is using a local password
        if error:
            log.debug(f"authenticate_user: LDAP authentication failed for {user.name} (attempting local fallback): {error}")

    # Verify password using constant-time comparison
    # Check if user has a local password set before attempting verification
    if user.password and check_password_hash(str(user.password), password):
        log.info(f"User authenticated successfully: {username}")
        return user

    log.debug(f"Invalid password for user: {username}")
    return None


def _authenticate_kosync_key() -> Optional[ub.User]:
    """Authenticate KOReader's built-in progress sync (x-auth-user + x-auth-key = md5(password))."""
    username = request.headers.get('x-auth-user', '')
    key = request.headers.get('x-auth-key', '')
    if not is_valid_field(key) or not is_valid_key_field(username, max_length=MAX_DEVICE_LENGTH):
        return None
    try:
        user = ub.session.query(ub.User).filter(func.lower(ub.User.name) == username.lower()).first()
        if user and user.name != "Guest" and ub.verify_kosync_key(user, key):
            return user
    except SQLAlchemyError as e:
        log.error(f"Database error during KOReader key check: {e}")
        return None
    log.debug(f"KOReader sync key rejected for user: {username}")
    return None


def create_sync_response(data: Dict[str, Any], status_code: int = 200) -> tuple:
    """
    Create a standardized JSON sync response.

    Args:
        data: Response payload dictionary
        status_code: HTTP status code (default: 200)

    Returns:
        Tuple of (response, status_code) for Flask
    """
    return jsonify(data), status_code


def handle_sync_error(error: KOSyncError) -> tuple:
    """
    Handle sync errors and return appropriate response.

    Args:
        error: KOSyncError with error code and message

    Returns:
        JSON error response: 401 for authentication failures (shown by KOReader), 400 otherwise
    """
    log.error(f"KOSync Error {error.error_code}: {error.message}")
    status = 401 if error.error_code == ERROR_UNAUTHORIZED_USER else 400
    return create_sync_response({
        "error": error.error_code,
        "message": error.message
    }, status)


def get_book_by_checksum(document_checksum: str, version: str = None):
    """
    Lookup a book in the Calibre library by its partial MD5 checksum.

    Searches all stored checksums for the given checksum value, regardless of
    whether it came from library files or OPDS exports.

    Args:
        document_checksum: The partial MD5 checksum from KOReader
        version: Optional algorithm version to filter by (None = any version)

    Returns:
        Tuple of (book_id, book_format, book_title, book_path, version) or
        (None, None, None, None, None) if no match found

    Note:
        Uses parameterized queries to prevent SQL injection.
        Orders by created DESC (latest first), then version DESC.
    """
    from ... import calibre_db
    from ...db import BookFormatChecksum, Books

    try:
        query = calibre_db.session.query(
            BookFormatChecksum.book,
            BookFormatChecksum.format,
            BookFormatChecksum.version,
            Books.title,
            Books.path
        ).join(
            Books, BookFormatChecksum.book == Books.id
        ).filter(
            BookFormatChecksum.checksum == document_checksum
        )

        # Optionally filter by version
        if version is not None:
            query = query.filter(BookFormatChecksum.version == version)

        # Order by created DESC (latest first), then version DESC
        query = query.order_by(
            BookFormatChecksum.created.desc(),
            BookFormatChecksum.version.desc()
        )

        result = query.first()

        if result:
            book_id, book_format, checksum_version, book_title, book_path = result
            log.debug(f"Found book match: {book_title} (ID {book_id}, format {book_format}, checksum v{checksum_version})")
            return book_id, book_format, book_title, book_path, checksum_version

        if version in (None, FILENAME_CHECKSUM_VERSION):
            match = _filename_match(document_checksum)
            if match:
                return match

        # No match found
        log.debug(f"No book found for checksum: {document_checksum}")
        return None, None, None, None, None

    except SQLAlchemyError as e:
        log.error(f"Database error looking up book by checksum {document_checksum}: {e}")
        return None, None, None, None, None
    except Exception as e:
        log.error(f"Unexpected error looking up book by checksum {document_checksum}: {e}")
        return None, None, None, None, None


# Devices that identify documents by file name (CrossPoint's default, a KOReader option) send the MD5
# of the name they saved the book under. Those hashes are matched against names derived from the
# library metadata, indexed in memory and rebuilt at most every FILENAME_INDEX_RETRY seconds on a miss.
FILENAME_INDEX_TTL = 600
FILENAME_INDEX_RETRY = 60
_filename_cache: dict[str, Any] = {"index": None, "built": 0.0}
_filename_lock = threading.Lock()


def _book_filename_hashes(book) -> dict[str, str]:
    """{file-name hash: format} for one library book."""
    authors = [author.name for author in getattr(book, "authors", None) or []]
    hashes = {}
    for data in getattr(book, "data", None) or []:
        for checksum, book_format in filename_hashes(book.title, authors, [data.format], data.name, book.id).items():
            hashes.setdefault(checksum, book_format)
    return hashes


def _build_filename_index() -> dict[str, tuple[int, str] | None]:
    from sqlalchemy.orm import lazyload, selectinload

    from ... import calibre_db
    from ...db import Books

    index: dict[str, tuple[int, str] | None] = {}
    books = calibre_db.session.query(Books).options(
        lazyload("*"), selectinload(Books.authors), selectinload(Books.data)).all()
    for book in books:
        for checksum, book_format in _book_filename_hashes(book).items():
            # A name shared by two books (same title, no author) identifies neither.
            if checksum in index and (index[checksum] or (None,))[0] != book.id:
                index[checksum] = None
            else:
                index.setdefault(checksum, (book.id, book_format))
    return index


def _filename_index_lookup(checksum: str) -> tuple[int, str] | None:
    with _filename_lock:
        now = time.monotonic()
        index, age = _filename_cache["index"], now - _filename_cache["built"]
        if index is None or age > FILENAME_INDEX_TTL or (checksum not in index and age > FILENAME_INDEX_RETRY):
            try:
                index = _build_filename_index()
            except (SQLAlchemyError, ImportError, AttributeError) as e:
                log.warning(f"Could not index library file names: {e}")
                index = index or {}
            _filename_cache.update(index=index, built=now)
        return index.get(checksum)


def _filename_match(document_checksum: str):
    """Match a file-name document hash to a book, remembering it as one of the book's checksums."""
    from ... import calibre_db
    from ..checksums import store_checksum

    match = _filename_index_lookup(document_checksum)
    if not match:
        return None
    book_id, book_format = match
    book = calibre_db.get_book(book_id)
    if not book:
        return None
    store_checksum(book_id, book_format, document_checksum, version=FILENAME_CHECKSUM_VERSION)
    log.info(f"Matched file-name document {document_checksum} to book {book_id} ({book_format})")
    return book_id, book_format, book.title, book.path, FILENAME_CHECKSUM_VERSION


def enrich_response_with_book_info(response_data: Dict[str, Any], document_checksum: str) -> Dict[str, Any]:
    """
    Enrich a sync response with Calibre book information if the book is found.

    This adds Calibre-specific metadata to the response, allowing clients to
    display richer information about the synced document.

    Args:
        response_data: The response dictionary to enrich
        document_checksum: The document checksum to look up

    Returns:
        Tuple of (enriched_response_data, book_id, book_format, book_title, checksum_version)
    """
    book_id, book_format, book_title, book_path, checksum_version = get_book_by_checksum(document_checksum)

    if book_id:
        response_data["calibre_book_id"] = book_id
        response_data["calibre_book_title"] = book_title
        response_data["calibre_book_format"] = book_format
        response_data["calibre_checksum_version"] = checksum_version

    return response_data, book_id, book_format, book_title, checksum_version


def update_book_read_status(user, book_id: int, percentage: float) -> None:
    """
    Update the user's ReadBook status based on reading progress percentage.

    Status thresholds:
        - 0%: STATUS_UNREAD
        - 1-98%: STATUS_IN_PROGRESS
        - 99-100%: STATUS_FINISHED

    Behavior:
        - Creates ReadBook record if it doesn't exist
        - Increments times_started_reading when transitioning to IN_PROGRESS
        - Updates KoboBookmark progress_percent for Kobo sync compatibility
        - Handles status transitions gracefully

    Args:
        user_id: The ID of the user
        book_id: The ID of the book in the Calibre library
        percentage: Reading progress percentage (0.0 to 100.0)

    Raises:
        SQLAlchemyError: If database operation fails

    Note:
        Caller is responsible for committing the session.
    """
    # Determine the new read status based on percentage
    if percentage >= 99.0:
        new_status = ub.ReadBook.STATUS_FINISHED
    elif percentage > 0:
        new_status = ub.ReadBook.STATUS_IN_PROGRESS
    else:
        new_status = ub.ReadBook.STATUS_UNREAD

    user_id = user.id

    log.debug(f"update_book_read_status: user {user_id}, book {book_id}, "
              f"percentage {percentage:.2f}% -> status {new_status}")

    # Query for existing ReadBook record
    book_read = ub.session.query(ub.ReadBook).filter(
        ub.ReadBook.user_id == user_id,
        ub.ReadBook.book_id == book_id
    ).first()

    if book_read:
        # Update existing record
        old_status = book_read.read_status
        log.debug(f"Found existing ReadBook: old_status={old_status}, new_status={new_status}")

        # Increment times_started_reading when transitioning to IN_PROGRESS
        if new_status == ub.ReadBook.STATUS_IN_PROGRESS and old_status != ub.ReadBook.STATUS_IN_PROGRESS:
            book_read.times_started_reading += 1
            book_read.last_time_started_reading = datetime.now(timezone.utc)
            log.info(f"User {user_id} started reading book {book_id} "
                    f"(times started: {book_read.times_started_reading})")

        # Update status if changed
        if old_status != new_status:
            book_read.read_status = new_status
            log.info(f"User {user_id} book {book_id} status changed: "
                    f"{old_status} -> {new_status} (progress: {percentage:.1f}%)")
        else:
            log.debug(f"ReadBook status unchanged: {old_status}")

        book_read.last_modified = datetime.now(timezone.utc)

        # Update KoboBookmark progress_percent if it exists
        if book_read.kobo_reading_state and book_read.kobo_reading_state.current_bookmark:
            book_read.kobo_reading_state.current_bookmark.progress_percent = percentage
            book_read.kobo_reading_state.current_bookmark.last_modified = datetime.now(timezone.utc)

    else:
        # Create new ReadBook record
        book_read = ub.ReadBook(
            user_id=user_id,
            book_id=book_id,
            read_status=new_status
        )

        # Set started reading fields for IN_PROGRESS books
        # Note: Following Kobo/CWA convention, times_started_reading only increments
        # when status is IN_PROGRESS. Books that jump straight to FINISHED (e.g.,
        # syncing at 100% without intermediate syncs) will have times_started_reading=0
        if new_status == ub.ReadBook.STATUS_IN_PROGRESS:
            book_read.times_started_reading = 1
            book_read.last_time_started_reading = datetime.now(timezone.utc)
            log.info(f"User {user_id} started reading book {book_id} (new entry)")

        # Create associated KoboReadingState
        kobo_reading_state = ub.KoboReadingState(
            user_id=user_id,
            book_id=book_id
        )
        kobo_reading_state.current_bookmark = ub.KoboBookmark()
        kobo_reading_state.current_bookmark.progress_percent = percentage
        kobo_reading_state.statistics = ub.KoboStatistics()
        book_read.kobo_reading_state = kobo_reading_state

        ub.session.add(book_read)
        log.info(f"User {user_id} book {book_id} created with status {new_status} "
                f"(progress: {percentage:.1f}%)")

    # Merge the record (caller commits)
    ub.session.merge(book_read)


def get_progress_record(user_id, document_checksum, book_id) -> KOSyncProgress:
    """
    Look up and return the KOSyncProgress record associated with a user and document identifier(s).

    Behavior:
        Returns only the most recently updated record for the given criteria.
        Should still work with a document_checksum when no book_id is provided, and vice versa.

    Args:
        user_id: The ID of the user
        book_id: The ID of the book in the Calibre library
        document_checksum: The checksum of the document sought
    """
    progress_record = ub.session.query(KOSyncProgress).filter(
        KOSyncProgress.user_id == user_id,
        KOSyncProgress.document.in_((book_id, document_checksum))
    ).order_by(
        desc(KOSyncProgress.timestamp)
    ).first()

    if not progress_record:
        log.debug(f"No progress record found for user: {user_id}, document_checksum: {document_checksum}, book_id: {book_id}")
    log.debug(f"Progress found: {progress_record}")
    return progress_record


def store_progress(user, progress_record, document, book_id, book_title, progress, percentage_float,
                   device, device_id, timestamp, push_hardcover=True) -> KOSyncProgress:
    """
    Save a progress record, then update ReadBook/Kobo state and Hardcover for a matched book.

    Shared by KOReader devices and the web readers so both keep one record per user and book.
    Raises KOSyncError when the progress itself cannot be committed.
    """
    # Prefer the book_id as the identifier (if we have it) to ensure that all documents associated with the same
    # Calibre book share the same progress record even if they have different checksums.
    document = book_id or document

    if progress_record:
        progress_record.progress = progress
        progress_record.percentage = percentage_float
        progress_record.device = device
        progress_record.device_id = device_id
        progress_record.timestamp = timestamp
        log.debug(f"Updated kosync progress for user {user.id}, document {document}")
    else:
        progress_record = KOSyncProgress(
            user_id=user.id,
            document=str(document),
            progress=progress,
            percentage=percentage_float,
            device=device,
            device_id=device_id,
            timestamp=timestamp
        )
        ub.session.add(progress_record)
        log.debug(f"Created kosync progress for user {user.id}, document {document}")

    # CRITICAL: Always commit kosync_progress first before attempting ReadBook updates
    # This ensures sync location is persisted even if ReadBook update fails
    try:
        ub.session.commit()
        log.info(f"Saved kosync progress: user={user.id}, document={document}, "
                 f"progress={percentage_float:.2f}%")
    except SQLAlchemyError as e:
        log.error(f"Failed to commit kosync_progress: {e}")
        ub.session.rollback()
        raise KOSyncError(ERROR_INTERNAL, "Failed to save sync progress")

    # Update user's ReadBook status if we matched a book
    # This is done AFTER kosync_progress is committed, so sync location is always safe
    if book_id:
        try:
            update_book_read_status(user, book_id, percentage_float)
            ub.session.commit()
            log.info(f"Updated ReadBook status: user={user.id}, book={book_id} "
                     f"({book_title}), status based on {percentage_float:.1f}%")

            if push_hardcover:
                from ... import calibre_db
                book = calibre_db.get_book(book_id)
                log.debug(f"Going to sync book {book_id} to Hardcover.")
                push_reading_state_to_hardcover(user, book, int(percentage_float))

        except SQLAlchemyError as e:
            log.error(f"Failed to update ReadBook status for book {book_id}: {e}")
            # Rollback only affects the failed ReadBook update
            # kosync_progress was already committed and is safe
            ub.session.rollback()
        except Exception as e:
            log.error(f"Unexpected error updating ReadBook status for book {book_id}: {e}")
            ub.session.rollback()

    return progress_record


################################################################################
# API Endpoints
################################################################################

@kosync.route("/kosync")
def kosync_plugin_page():
    """
    Display the KOReader plugin download and installation page.

    This page provides:
        - Plugin download link
        - Installation instructions
        - Configuration guidance
        - Troubleshooting tips

    Returns:
        Rendered HTML page
    """
    return render_title_template(
        "kosync_plugin.html",
        title=_("KOReader Sync Plugin"),
        page="cwa-kosync",
        kosync_enabled=is_koreader_sync_enabled()
    )


@csrf.exempt
@kosync.route("/kosync/users/auth", methods=["GET"])
def auth_user():
    """
    Authenticate user endpoint (KOSync protocol).

    This endpoint verifies user credentials and is typically called once
    during KOReader sync setup to validate the connection.

    Returns:
        200: {"authorized": "OK"} if authentication succeeds
        401: {"error": 2001, "message": "Unauthorized"} if authentication fails

    Note:
        Rate limiting should be applied at reverse proxy level to prevent
        brute force attacks (suggested: 10 requests per minute per IP).
    """
    blocked = _require_kosync_enabled()
    if blocked:
        return blocked

    user = authenticate_user()
    if user:
        return create_sync_response({"authorized": "OK"})
    else:
        return create_sync_response({
            "error": ERROR_UNAUTHORIZED_USER,
            "message": UNAUTHORIZED_MESSAGE
        }, 401)


@csrf.exempt
@kosync.route("/kosync/users/create", methods=["POST"])
def create_user():
    """KOReader "Register": accounts come from Calibre-Web, so explain how to sign in instead.

    402 is the status KOReader displays for a refused registration.
    """
    return create_sync_response({
        "error": ERROR_USER_EXISTS,
        "message": "Registration is not available. Use Login with your Calibre-Web Automated "
                   "username and password."
    }, 402)


@csrf.exempt
@kosync.route("/kosync/syncs/progress/<document>", methods=["GET"])
def get_progress(document: str):
    """
    Get reading progress for a document (KOSync protocol).

    Returns the latest progress for the specified document identifier,
    enriched with Calibre library metadata if the book is matched.

    Args:
        document: Document identifier (KOReader partial MD5 hash)

    Returns:
        200: Progress data with optional Calibre metadata
        400: Error response if validation fails
        401: Unauthorized if authentication fails

    Response format:
        {
            "document": "abc123...",
            "progress": "location string",
            "percentage": 0.4567,  # Decimal fraction (0.4567 = 45.67%)
            "device": "KOReader",
            "device_id": "device123",
            "timestamp": 1699564800,
            "calibre_book_id": 42,  # Optional: if matched
            "calibre_book_title": "Book Title",  # Optional
            "calibre_book_format": "EPUB",  # Optional
            "calibre_checksum_version": "koreader"  # Optional
        }

    Note:
        Percentage is returned as decimal (0.4567 = 45.67%) as expected by KOReader.
        Internally stored as percentage (0-100) in database.
    """
    endpoint = f"GET /kosync/syncs/progress/{document}"
    log.debug(endpoint)
    try:
        blocked = _require_kosync_enabled()
        if blocked:
            return blocked

        user = authenticate_user()
        if not user:
            raise KOSyncError(ERROR_UNAUTHORIZED_USER, UNAUTHORIZED_MESSAGE)

        if not is_valid_key_field(document):
            raise KOSyncError(ERROR_DOCUMENT_FIELD_MISSING, "Invalid document field")

        # Create initial response with Calibre book information if available
        response_data, book_id, book_format, book_title, _ = enrich_response_with_book_info(
            {}, document
        )

        progress_record = get_progress_record(user.id, document, book_id)

        if not progress_record:
            return create_sync_response({})

        # KOReader expects percentage as a decimal fraction (0.9411 = 94.11%)
        # We store it as percentage (0-100), so convert back to decimal (0-1)
        percentage_decimal = progress_record.percentage / 100.0

        response_updates = {
            "document": document,
            "progress": progress_record.progress,
            "percentage": percentage_decimal,
            "device": progress_record.device,
            "device_id": progress_record.device_id,
            "timestamp": int(progress_record.timestamp.timestamp())
        }

        response_data = {**response_data, **response_updates}

        log.debug(endpoint + f" Response: {response_data}")

        return create_sync_response(response_data)

    except KOSyncError as e:
        return handle_sync_error(e)
    except SQLAlchemyError as e:
        log.error(f"get_progress: Database error: {str(e)}")
        return handle_sync_error(KOSyncError(ERROR_INTERNAL, "Database error"))
    except Exception as e:
        log.error(f"get_progress: Unexpected error: {str(e)}")
        return handle_sync_error(KOSyncError(ERROR_INTERNAL, "Internal server error"))


@csrf.exempt
@kosync.route("/kosync/syncs/progress", methods=["PUT"])
def update_progress():
    """
    Update reading progress for a document (KOSync protocol).

    This endpoint receives progress updates from KOReader devices and:
        1. Validates and stores the sync data in kosync_progress table
        2. Attempts to match the document to a Calibre library book
        3. Updates ReadBook status if a match is found

    The commit strategy ensures sync data is always persisted, even if
    ReadBook updates fail (preventing sync data loss).

    Request body:
        {
            "document": "abc123...",  # Required: Document identifier
            "progress": "location",   # Required: Current reading position
            "percentage": 0.4567,     # Required: Progress as decimal (0-1)
            "device": "KOReader",     # Required: Device name
            "device_id": "device123"  # Optional: Device identifier
        }

    Returns:
        200: Success with document and timestamp
        400: Validation error
        401: Unauthorized
        500: Internal error

    Response format:
        {
            "document": "abc123...",
            "timestamp": 1699564800,
            "calibre_book_id": 42,  # Optional: if matched
            "calibre_book_title": "Book Title",  # Optional
            "calibre_book_format": "EPUB",  # Optional
            "calibre_checksum_version": "koreader"  # Optional
        }

    Note:
        Percentage is converted from decimal (0.9411 = 94.11%) to percentage (94.11).
    """
    endpoint = f"PUT /kosync/syncs/progress/<document> with: {request.get_json()}"
    log.debug(endpoint)
    try:
        blocked = _require_kosync_enabled()
        if blocked:
            return blocked

        user = authenticate_user()
        if not user:
            raise KOSyncError(ERROR_UNAUTHORIZED_USER, UNAUTHORIZED_MESSAGE)

        data = request.get_json()
        if not data:
            raise KOSyncError(ERROR_INVALID_FIELDS, "Invalid request data")

        # Extract and validate required fields
        document = data.get("document")
        if not is_valid_key_field(document):
            raise KOSyncError(ERROR_DOCUMENT_FIELD_MISSING, "Invalid document field")

        progress = data.get("progress")
        percentage = data.get("percentage")
        device = data.get("device")
        device_id = data.get("device_id")

        # Validate required fields
        if not progress or percentage is None or not device:
            raise KOSyncError(ERROR_INVALID_FIELDS, "Missing required fields")

        # Validate field lengths
        if not is_valid_field(progress) or len(progress) > MAX_PROGRESS_LENGTH:
            raise KOSyncError(ERROR_INVALID_FIELDS, "Invalid progress field")
        if not is_valid_field(device) or len(device) > MAX_DEVICE_LENGTH:
            raise KOSyncError(ERROR_INVALID_FIELDS, "Invalid device field")
        if device_id and len(device_id) > MAX_DEVICE_ID_LENGTH:
            raise KOSyncError(ERROR_INVALID_FIELDS, "Invalid device_id field")

        # KOReader sends percentage as a decimal fraction (0.9411 = 94.11%)
        # Convert to actual percentage (0-100 range)
        try:
            percentage_float = float(percentage)
            if percentage_float <= 1.0:
                percentage_float *= 100.0
            if percentage_float < 0 or percentage_float > 100:
                raise ValueError("Percentage out of range")
        except (ValueError, TypeError) as e:
            raise KOSyncError(ERROR_INVALID_FIELDS, f"Invalid percentage value: {e}")

        timestamp = datetime.now(timezone.utc)

        response_data = {
            "document": document,
            "timestamp": int(timestamp.timestamp())
        }

        # Enrich response with Calibre book information if available
        response_data, book_id, book_format, book_title, _ = enrich_response_with_book_info(
            response_data, document
        )

        progress_record = get_progress_record(user.id, document, book_id)
        store_progress(user, progress_record, document, book_id, book_title, progress,
                       percentage_float, device, device_id, timestamp)

        log.debug(endpoint + f" Response: {response_data}")

        return create_sync_response(response_data)

    except KOSyncError as e:
        return handle_sync_error(e)
    except SQLAlchemyError as e:
        log.error(f"update_progress: Database error: {str(e)}")
        ub.session.rollback()
        return handle_sync_error(KOSyncError(ERROR_INTERNAL, "Database error"))
    except Exception as e:
        log.error(f"update_progress: Unexpected error: {str(e)}")
        ub.session.rollback()
        return handle_sync_error(KOSyncError(ERROR_INTERNAL, "Internal server error"))


################################################################################
# Web reader progress (browser session authentication)
################################################################################

WEB_DEVICE_NAME = "CWA Web Reader"


def _web_progress_record(user_id: int, book_id: int) -> KOSyncProgress | None:
    """Latest progress for a book, whether KOReader stored it by book id or by an unmatched checksum."""
    from ... import calibre_db
    from ...db import BookFormatChecksum

    documents = [str(book_id)]
    try:
        documents += [row[0] for row in calibre_db.session.query(BookFormatChecksum.checksum)
                      .filter(BookFormatChecksum.book == book_id).all()]
    except SQLAlchemyError as e:
        log.warning(f"Could not read checksums for book {book_id}: {e}")
    book = calibre_db.get_book(book_id)
    if book:
        documents += list(_book_filename_hashes(book))
    return ub.session.query(KOSyncProgress).filter(
        KOSyncProgress.user_id == user_id,
        KOSyncProgress.document.in_(documents)
    ).order_by(desc(KOSyncProgress.timestamp)).first()


def _serialize_web_progress(record: KOSyncProgress | None) -> dict[str, Any] | None:
    if not record:
        return None
    stamp = record.timestamp
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return {
        "progress": record.progress,
        "percentage": max(0.0, min(1.0, record.percentage / 100.0)),
        "device": record.device,
        "device_id": record.device_id,
        "timestamp": stamp.timestamp(),
    }


def reader_progress_config(book_id: int, book_format: str | None = None) -> dict[str, Any] | None:
    """Progress sync settings embedded in the web reader pages, or None when sync is unavailable."""
    from flask import url_for

    from ...cw_login import current_user

    if not current_user.is_authenticated or current_user.is_anonymous or not is_koreader_sync_enabled():
        return None
    candidates = []
    try:
        state = _serialize_web_progress(_web_progress_record(int(current_user.id), book_id))
        if (book_format or "").lower() in ("epub", "kepub"):
            candidates = [dict(_serialize_web_progress(record), document=record.document)
                          for record in _unmatched_progress(int(current_user.id), state)]
    except SQLAlchemyError as e:
        log.warning(f"Could not load reading progress for book {book_id}: {e}")
        state = None
    return {
        "url": url_for("kosync.reader_progress", book_id=book_id),
        "state": state,
        "device": WEB_DEVICE_NAME,
        "candidates": candidates,
        "linkUrl": url_for("kosync.link_reader_progress", book_id=book_id) if candidates else None,
        "format": (book_format or "").upper(),
        "messages": {
            "remote": _("Continue from %(percent)s on %(device)s?", percent="{percent}", device="{device}"),
            "jump": _("Continue"),
            "dismiss": _("Dismiss"),
            "error": _("Reading progress could not be synchronized."),
            "synced": _("Reading progress synchronized for %(user)s (%(percent)s).",
                        user=current_user.name, percent="{percent}"),
            "syncing": _("Synchronizing reading progress…"),
        },
    }


# KOReader identifies books by a hash of the file it holds. Copies downloaded before checksums were
# recorded (or edited since) never match a library file, so their progress is offered to the reader,
# which verifies the position against the open book before linking the hash to it.
MAX_LINK_CANDIDATES = 10


def _unmatched_progress(user_id: int, current: dict[str, Any] | None = None) -> list[KOSyncProgress]:
    """The user's newest EPUB progress records whose document hash matches no library file.

    With `current` (the book's serialized state), only records newer or further along are kept: merely
    opening a book in the web reader saves a fresh position near the start, which must not hide one.
    """
    from ... import calibre_db
    from ...db import BookFormatChecksum

    records = [record for record in ub.session.query(KOSyncProgress).filter(
        KOSyncProgress.user_id == user_id,
        KOSyncProgress.progress.like("/body/DocFragment%")
    ).order_by(desc(KOSyncProgress.timestamp)).limit(MAX_LINK_CANDIDATES * 5).all()
        if not record.document.isdigit()]
    if current is not None:
        records = [record for record in records
                   if (state := _serialize_web_progress(record))["timestamp"] > current["timestamp"]
                   or state["percentage"] > current["percentage"]]
    if not records:
        return []
    known = {row[0] for row in calibre_db.session.query(BookFormatChecksum.checksum).filter(
        BookFormatChecksum.checksum.in_([record.document for record in records])).all()}
    return [record for record in records if record.document not in known
            and not _filename_index_lookup(record.document)][:MAX_LINK_CANDIDATES]


@kosync.route("/kosync/reader/<int:book_id>/link", methods=["POST"])
@usermanagement.user_login_required
def link_reader_progress(book_id: int):
    """Attach an unmatched KOReader document hash to this book after the reader verified the position."""
    from ... import calibre_db
    from ...cw_login import current_user
    from ..checksums import store_checksum

    if not is_koreader_sync_enabled():
        return jsonify({"message": "Reading progress sync is disabled"}), 404
    book = calibre_db.get_filtered_book(book_id)
    if not book:
        return jsonify({"message": "Book not found"}), 404
    data = request.get_json(silent=True)
    document = data.get("document") if isinstance(data, dict) else None
    book_format = str(data.get("format") or "").upper() if isinstance(data, dict) else ""
    if not is_valid_key_field(document):
        return jsonify({"message": "Invalid document field"}), 400
    if book_format not in ("EPUB", "KEPUB") or not any(item.format.upper() == book_format for item in book.data):
        return jsonify({"message": "Invalid format field"}), 400
    try:
        linked = next((record for record in _unmatched_progress(int(current_user.id))
                       if record.document == document), None)
        if linked is None:
            return jsonify({"message": "Progress record not found"}), 404
        if not store_checksum(book_id, book_format, document):
            return jsonify({"message": "Could not link reading progress"}), 500
        # The reader chose this position: a later web reader position (e.g. saved on opening) must not win.
        ub.session.query(KOSyncProgress).filter(
            KOSyncProgress.user_id == int(current_user.id),
            KOSyncProgress.document == str(book_id),
            KOSyncProgress.device == WEB_DEVICE_NAME,
            KOSyncProgress.timestamp > linked.timestamp
        ).delete(synchronize_session=False)
        ub.session.commit()
        log.info(f"Linked KOReader document {document} to book {book_id} for user {current_user.id}")
        return jsonify(_serialize_web_progress(_web_progress_record(int(current_user.id), book_id)) or {})
    except SQLAlchemyError as e:
        log.error(f"link_reader_progress: Database error for book {book_id}: {e}")
        ub.session.rollback()
        return jsonify({"message": "Database error"}), 500


@kosync.route("/kosync/reader/<int:book_id>", methods=["GET", "PUT"])
@usermanagement.user_login_required
def reader_progress(book_id: int):
    """Reading progress for the signed-in user's web readers, stored with KOReader progress.

    Uses the browser session (and CSRF token for PUT) instead of device credentials. Positions
    use KOReader's format (XPointer for EPUB, 1-based page for PDF/comics) so devices can resume.
    """
    from ... import calibre_db
    from ...cw_login import current_user

    if not is_koreader_sync_enabled():
        return jsonify({"message": "Reading progress sync is disabled"}), 404
    book = calibre_db.get_filtered_book(book_id)
    if not book:
        return jsonify({"message": "Book not found"}), 404
    user = current_user

    try:
        record = _web_progress_record(int(user.id), book_id)
        if request.method == "GET":
            return jsonify(_serialize_web_progress(record) or {})

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"message": "Invalid request data"}), 400
        progress = data.get("progress")
        percentage = data.get("percentage")
        device = data.get("device") or WEB_DEVICE_NAME
        device_id = data.get("device_id")
        if not is_valid_field(progress) or len(progress) > MAX_PROGRESS_LENGTH:
            return jsonify({"message": "Invalid progress field"}), 400
        if not is_valid_field(device) or len(device) > MAX_DEVICE_LENGTH:
            return jsonify({"message": "Invalid device field"}), 400
        if device_id is not None and (not is_valid_field(device_id) or len(device_id) > MAX_DEVICE_ID_LENGTH):
            return jsonify({"message": "Invalid device_id field"}), 400
        # Web readers always send a fraction, so 1.0 is unambiguous here.
        if isinstance(percentage, bool) or not isinstance(percentage, (int, float)) or not 0 <= percentage <= 1:
            return jsonify({"message": "Invalid percentage value"}), 400

        percentage_float = float(percentage) * 100.0
        push_hardcover = record is None or int(record.percentage) != int(percentage_float)
        record = store_progress(user, record, str(book_id), book_id, book.title, progress, percentage_float,
                                device, device_id, datetime.now(timezone.utc), push_hardcover=push_hardcover)
        return jsonify(_serialize_web_progress(record))
    except KOSyncError as e:
        return jsonify({"message": e.message}), 500
    except SQLAlchemyError as e:
        log.error(f"reader_progress: Database error for book {book_id}: {e}")
        ub.session.rollback()
        return jsonify({"message": "Database error"}), 500


################################################################################
# Error Handlers
################################################################################

@kosync.errorhandler(400)
def handle_bad_request(error):
    """Handle HTTP 400 Bad Request errors"""
    return create_sync_response({
        "error": ERROR_INVALID_FIELDS,
        "message": "Bad request"
    }, 400)


@kosync.errorhandler(401)
def handle_unauthorized(error):
    """Handle HTTP 401 Unauthorized errors"""
    return create_sync_response({
        "error": ERROR_UNAUTHORIZED_USER,
        "message": "Unauthorized"
    }, 401)


@kosync.errorhandler(500)
def handle_internal_error(error):
    """Handle HTTP 500 Internal Server errors"""
    log.error(f"Internal server error: {error}")
    return create_sync_response({
        "error": ERROR_INTERNAL,
        "message": "Internal server error"
    }, 500)
