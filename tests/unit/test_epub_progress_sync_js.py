# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import json
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
PROGRESS_SYNC_JS = Path(__file__).resolve().parents[2] / "cps" / "static" / "js" / "reading" / "progress-sync.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for JavaScript unit tests")
def test_local_epub_position_must_match_server_xpointer():
    cases = [
        ({"timestamp": 20, "xpointer": "/body/old"}, {"timestamp": 20, "progress": "/body/new"}, False),
        ({"timestamp": 21, "xpointer": "/body/old"}, {"timestamp": 20, "progress": "/body/new"}, False),
        ({"timestamp": 20, "xpointer": "/body/same"}, {"timestamp": 20, "progress": "/body/same"}, True),
        ({"timestamp": 19, "xpointer": "/body/same"}, {"timestamp": 20, "progress": "/body/same"}, False),
        ({"pending": True, "savedAt": 21001, "xpointer": "/body/local"},
         {"timestamp": 21, "progress": "/body/remote"}, True),
    ]
    script = f"""
        require({json.dumps(str(PROGRESS_SYNC_JS))});
        const cases = {json.dumps(cases)};
        for (const [record, state, expected] of cases) {{
            const actual = globalThis.CwaProgressSync.localPositionIsCurrent(record, state);
            if (actual !== expected) throw new Error(JSON.stringify({{record, state, expected, actual}}));
        }}
    """
    subprocess.run(["node", "-e", script], check=True)
