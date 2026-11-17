# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Exposes the result of the startup database integrity check (scripts/db_integrity.py) to templates."""

import json
import os

from . import logger

log = logger.create()

_corrupted = None


def status_file():
    return os.path.join(os.environ.get("CALIBRE_DBPATH", "/config"), ".cwa_db_health.json")


def corrupted_databases():
    """Databases the startup check could not repair; the file only changes when the container starts."""
    global _corrupted
    if _corrupted is None:
        try:
            with open(status_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            _corrupted = [d.get("db") for d in data.get("databases", []) if d.get("status") == "corrupt"]
        except (OSError, ValueError):
            _corrupted = []
        if _corrupted:
            log.error("Corrupted database(s) detected at startup: %s", ", ".join(_corrupted))
    return _corrupted


def inject_db_health():
    return {"corrupted_databases": corrupted_databases()}
