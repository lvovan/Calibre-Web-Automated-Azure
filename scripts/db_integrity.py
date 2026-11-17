# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Startup integrity check for the SQLite databases in /config.

Runs from cwa-init before any service opens the databases. A corrupted database
is rebuilt with VACUUM INTO and swapped in only when the rebuilt copy passes
integrity_check and holds exactly the same rows; the original is kept as
<db>.corrupt-<timestamp>. Databases that cannot be repaired losslessly are left
untouched and reported in a status file that the web UI turns into a banner.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

STATUS_FILE = "/config/.cwa_db_health.json"
OK, REPAIRED, CORRUPT, MISSING = "ok", "repaired", "corrupt", "missing"


def _integrity(path):
    # Read-write so that a hot journal left by a killed process is rolled back first
    conn = sqlite3.connect(path, timeout=30)
    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    finally:
        conn.close()
    return rows == ["ok"], "; ".join(rows[:5])


def _row_counts(conn):
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def _repair(path, stamp):
    """Rebuild path losslessly; return the backup path, or raise if that is not possible."""
    tmp = f"{path}.repair-{stamp}"
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        src = sqlite3.connect(path, timeout=30)
        try:
            before = _row_counts(src)
            src.execute("VACUUM INTO ?", (tmp,))
        finally:
            src.close()
        ok, detail = _integrity(tmp)
        if not ok:
            raise RuntimeError(f"rebuilt copy is still corrupted: {detail}")
        dst = sqlite3.connect(tmp)
        try:
            after = _row_counts(dst)
        finally:
            dst.close()
        if after != before:
            lost = sorted(t for t in before if after.get(t) != before[t])
            raise RuntimeError(f"rebuilt copy would lose rows in: {', '.join(lost)}")
        backup = f"{path}.corrupt-{stamp}"
        shutil.copy2(path, backup)
        # A leftover journal belongs to the corrupted file and must not be replayed onto the rebuilt one
        for suffix in ("-journal", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.replace(path + suffix, backup + suffix)
        os.replace(tmp, path)
        return backup
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def check_database(path, repair=True, stamp=None):
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    result = {"db": os.path.basename(path), "path": path}
    if not os.path.exists(path):
        return {**result, "status": MISSING}
    try:
        ok, detail = _integrity(path)
    except sqlite3.DatabaseError as e:
        ok, detail = False, str(e)
    if ok:
        return {**result, "status": OK}
    result["detail"] = detail
    if not repair:
        return {**result, "status": CORRUPT}
    try:
        result["backup"] = _repair(path, stamp)
        return {**result, "status": REPAIRED}
    except (sqlite3.Error, OSError, RuntimeError) as e:
        return {**result, "status": CORRUPT, "repair_error": str(e)}


def run(paths, status_file=STATUS_FILE, repair=True):
    results = [check_database(p, repair=repair) for p in paths]
    payload = {"checked_at": datetime.now(timezone.utc).isoformat(), "databases": results}
    tmp = status_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, status_file)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("databases", nargs="+")
    parser.add_argument("--status-file", default=STATUS_FILE)
    parser.add_argument("--no-repair", action="store_true")
    args = parser.parse_args(argv)
    results = run(args.databases, args.status_file, repair=not args.no_repair)
    for r in results:
        line = f"[db-integrity] {r['db']}: {r['status']}"
        if r.get("detail"):
            line += f" ({r['detail'][:200]})"
        if r.get("backup"):
            line += f"; original kept as {r['backup']}"
        if r.get("repair_error"):
            line += f"; automatic repair failed: {r['repair_error']}"
        print(line, flush=True)
    return 1 if any(r["status"] == CORRUPT for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
