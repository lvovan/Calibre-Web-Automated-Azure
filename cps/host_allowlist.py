# -*- coding: utf-8 -*-
# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

"""Reject requests whose Host header isn't in CWA_ALLOWED_HOSTS.

Used on Azure App Service to stop access through the default
*.azurewebsites.net hostname once a custom domain is bound. Disabled when
CWA_ALLOWED_HOSTS is empty or unset.
"""

import os

ENV_VAR = "CWA_ALLOWED_HOSTS"

# Hosts that can only come from inside the container (e.g. the Docker HEALTHCHECK).
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Paths served on any host so platform health probes keep working.
_EXEMPT_PATHS = {"/health", "/version"}


def parse_allowed_hosts(value):
    return {h.strip().lower().rstrip(".") for h in (value or "").split(",") if h.strip()}


def _hostname(host_header):
    host = (host_header or "").strip().lower()
    if host.startswith("["):  # [IPv6]:port
        return host[1:host.find("]")] if "]" in host else host
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


class HostAllowlist:
    """WSGI middleware. Must wrap the app outside ProxyFix so it checks the raw
    Host header the platform routed on, not a client-supplied X-Forwarded-Host."""

    def __init__(self, app, allowed_hosts):
        self.app = app
        self.allowed = set(allowed_hosts) | _LOOPBACK_HOSTS

    def __getattr__(self, name):
        # Expose the wrapped middleware's attributes (e.g. ReverseProxied.is_proxied,
        # read via app.wsgi_app by the admin page and Kobo sync).
        return getattr(self.app, name)

    def __call__(self, environ, start_response):
        if (environ.get("PATH_INFO") in _EXEMPT_PATHS
                or _hostname(environ.get("HTTP_HOST")) in self.allowed):
            return self.app(environ, start_response)
        body = b"Not Found"
        start_response("404 Not Found", [("Content-Type", "text/plain"),
                                         ("Content-Length", str(len(body)))])
        return [body]


def wrap(app):
    """Return app wrapped with HostAllowlist if CWA_ALLOWED_HOSTS is set, else app unchanged."""
    allowed = parse_allowed_hosts(os.environ.get(ENV_VAR))
    return HostAllowlist(app, allowed) if allowed else app
