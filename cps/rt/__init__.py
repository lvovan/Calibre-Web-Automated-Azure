# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later

"""Isolated, automatically selected IE11 user interface."""

from flask import Blueprint

rt = Blueprint("rt", __name__, url_prefix="/rt", template_folder="templates",
               static_folder="static", static_url_path="/static")

from . import routing, routes  # noqa: E402,F401
