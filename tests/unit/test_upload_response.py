"""Upload responses must work both for the XHR progress plugin and native form posts (Safari/iOS)."""

import json

import pytest
from flask import Flask

pytestmark = pytest.mark.unit


@pytest.fixture
def upload_response():
    from cps.editbooks import _upload_response
    return _upload_response


def test_xhr_upload_gets_json_location(upload_response):
    with Flask(__name__).test_request_context("/upload", method="POST",
                                              headers={"X-Requested-With": "XMLHttpRequest"}):
        resp = upload_response("/tasks")
    assert resp.mimetype == "application/json"
    assert json.loads(resp.get_data(as_text=True)) == {"location": "/tasks"}


def test_native_form_upload_is_redirected(upload_response):
    with Flask(__name__).test_request_context("/upload", method="POST"):
        resp = upload_response("/tasks")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/tasks")
