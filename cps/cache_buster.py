# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2025 Calibre-Web contributors
# Copyright (C) 2024-2025 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

# Inspired by https://github.com/ChrisTM/Flask-CacheBust
# Uses query strings so CSS font files are found without having to resort to absolute URLs

import os
import hashlib

from . import logger
from . import constants


log = logger.create()


def init_cache_busting(app):
    """
    Configure `app` to so that `url_for` adds a unique query string to URLs generated
    for the `'static'` endpoint.

    This allows setting long cache expiration values on static resources
    because whenever the resource changes, so does its URL.
    """

    static_folder = os.path.join(app.static_folder, '')  # path to the static file folder, with trailing slash

    hash_table = {}
    build_version = constants.BUILD_NUMBER or "development"
    if app.debug:
        log.debug('Computing cache-busting values...')
        for dirpath, __, filenames in os.walk(static_folder):
            for filename in filenames:
                rooted_filename = os.path.join(dirpath, filename)
                try:
                    with open(rooted_filename, 'rb') as f:
                        file_hash = hashlib.md5(f.read()).hexdigest()[:7]  # nosec
                    file_path = rooted_filename.replace(static_folder, "").replace("\\", "/")
                    hash_table[file_path] = file_hash
                except PermissionError:
                    log.error("No permission to access {} file.".format(rooted_filename))
        log.debug('Finished computing cache-busting values')

    def bust_filename(file_name):
        return hash_table.get(file_name, build_version)

    def unbust_filename(file_name):
        return file_name.split("?", 1)[0]

    @app.url_defaults
    # pylint: disable=unused-variable
    def reverse_to_cache_busted_url(endpoint, values):
        """
        Make `url_for` produce busted filenames when using the 'static' endpoint.
        """
        if endpoint == "static":
            file_hash = bust_filename(values["filename"])
            if file_hash:
                values["q"] = file_hash

    def debusting_static_view(filename):
        """
        Serve a request for a static file having a busted name.
        """
        return original_static_view(filename=unbust_filename(filename))

    # Replace the default static file view with our debusting view.
    original_static_view = app.view_functions["static"]
    app.view_functions["static"] = debusting_static_view

    @app.after_request
    def cache_static_assets(response):
        if response.status_code == 200 and response.content_type:
            content_type = response.content_type.split(";", 1)[0]
            if content_type in (
                    "text/css", "text/javascript", "application/javascript", "font/woff",
                    "font/woff2", "image/svg+xml", "image/webp", "image/png",
                    "image/jpeg"):
                if request_path_is_static():
                    response.cache_control.public = True
                    response.cache_control.max_age = 31536000 if versioned_static_request() else 3600
                    if versioned_static_request():
                        response.cache_control.immutable = True
        return response

    def request_path_is_static():
        from flask import request
        return request.endpoint == "static"

    def versioned_static_request():
        from flask import request
        return bool(request.args.get("q"))
