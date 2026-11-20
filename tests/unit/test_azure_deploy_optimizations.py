# Calibre-Web Automated - fork of Calibre-Web
# Copyright (C) 2018-2026 Calibre-Web contributors
# Copyright (C) 2024-2026 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "azure-deploy.yml"


def test_code_only_deploy_can_skip_bicep():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "az webapp config container set" in workflow
    assert 'git diff --quiet "$infra_before" "${GITHUB_SHA}" -- infra' in workflow
    assert "az deployment group create" in workflow


def test_build_uses_persistent_registry_cache():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "type=registry,ref=${{ steps.meta.outputs.cache_ref }},ignore-error=true" in workflow
    assert "type=registry,ref=${{ steps.meta.outputs.cache_ref }},mode=min" in workflow


def test_health_polling_is_bounded_and_faster():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "seq 1 180" in workflow
    assert "sleep 5" in workflow
    assert "within 15 minutes" in workflow


def test_series_grid_prefers_volume_one_cover():
    web = Path(__file__).resolve().parents[2] / "cps" / "web.py"
    grid = Path(__file__).resolve().parents[2] / "cps" / "templates" / "grid.html"
    web_source = web.read_text(encoding="utf-8")
    grid_source = grid.read_text(encoding="utf-8")
    assert ".filter(db.Books.series_index == 1)" in web_source
    assert "representative_books.setdefault(series_id, book)" in web_source
    assert "image.book_cover(entry.book)" in grid_source
