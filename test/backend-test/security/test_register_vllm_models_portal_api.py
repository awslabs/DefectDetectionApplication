# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The vLLM registration script's Portal API checks
(security-scan-remediation-high, Requirements 9.1, 9.4 and 9.5).

``test/on-hardware/register_vllm_models.py`` sends a bearer value with
every request, so the base URL must be ``https`` and redirects aren't
followed. The script is loaded from its file with ``importlib``. No
outbound network: accepted URLs run with ``_request`` replaced, and the
redirect case uses a localhost server. Every value here is visibly fake.
"""
import importlib.util
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir,
    "on-hardware", "register_vllm_models.py")

FAKE_BEARER = "fake-bearer-value-for-tests"
SETTINGS = ("PORTAL_API", "PORTAL_TOKEN", "PORTAL_USECASE_ID")


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location(
        "register_vllm_models_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def no_settings(monkeypatch):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)


def _no_request(*args, **kwargs):
    raise AssertionError("the script must not call the Portal here")


def _argv(portal_api):
    return ["--portal-api", portal_api, "--token", FAKE_BEARER,
            "--usecase-id", "uc-fake-1"]


@pytest.mark.parametrize("template, rule", [
    ("http://portal.example.invalid/prod",
     "must be an https:// URL (got scheme 'http')"),
    ("ftp://portal.example.invalid/prod",
     "must be an https:// URL (got scheme 'ftp')"),
    ("portal.example.invalid/prod", "must be an https:// URL (got no scheme)"),
    ("https://{user}:{cred}@portal.example.invalid/prod",
     "must not contain user information"),
    ("https://portal.example.invalid/prod?api-key={cred}",
     "must not contain a query or fragment"),
    ("https://portal.example.invalid/prod#{cred}",
     "must not contain a query or fragment"),
], ids=["http", "ftp", "no-scheme", "userinfo", "query", "fragment"])
def test_rejected_portal_api_exits_2_without_echo(
        script, no_settings, monkeypatch, capsys, template, rule):
    value = template.format(user="fake-user", cred="fake-credential-value")
    monkeypatch.setattr(script, "_request", _no_request)
    with pytest.raises(SystemExit) as e:
        script.main(_argv(value))
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--portal-api / PORTAL_API " + rule in err
    assert FAKE_BEARER not in err
    assert value not in err
    for fragment in ("portal.example.invalid", "fake-user",
                     "fake-credential-value"):
        assert fragment not in err


@pytest.mark.parametrize("value, base", [
    ("https://portal.example.invalid/prod",
     "https://portal.example.invalid/prod"),
    ("HTTPS://portal.example.invalid/prod/",
     "HTTPS://portal.example.invalid/prod"),
])
def test_https_portal_api_is_accepted(script, no_settings, monkeypatch, value, base):
    calls = []

    def fake_request(method, url, bearer, body=None, timeout=30):
        calls.append((method, url, bearer))
        models = [{"name": m["model_name"], "model_type": "vllm"}
                  for m in script.MODELS]
        return 200, {"models": models}

    monkeypatch.setattr(script, "_request", fake_request)
    assert script.main(_argv(value)) == 0  # both models already registered
    assert calls == [
        ("GET", base + "/api/v1/models?usecase_id=uc-fake-1", FAKE_BEARER)]


def test_dry_run_still_needs_no_settings(script, no_settings, monkeypatch, capsys):
    monkeypatch.setattr(script, "_request", _no_request)
    assert script.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.count("-> POST /api/v1/models/vllm") == len(script.MODELS)
    assert '"usecase_id": "<usecase_id>"' in out


class _RedirectingHandler(BaseHTTPRequestHandler):
    """``/start`` answers 302 toward ``/recording``; every request's path
    and Authorization header are recorded."""

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        body = b'{"moved": true}' if self.path == "/start" else b"{}"
        self.send_response(302 if self.path == "/start" else 200)
        if self.path == "/start":
            self.send_header("Location", "/recording")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


@pytest.fixture
def redirect_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_redirect_is_returned_and_the_header_never_reaches_its_target(
        script, redirect_server):
    url = "http://127.0.0.1:{0}/start".format(redirect_server.server_address[1])
    status, payload = script._request("GET", url, FAKE_BEARER)
    assert status == 302
    assert payload == {"moved": True}
    assert redirect_server.seen == [("/start", "Bearer " + FAKE_BEARER)]
