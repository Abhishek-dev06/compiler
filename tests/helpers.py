"""Shared test helpers: a CSRF-aware client that behaves like static/app.js, and a fake Judge0."""
from __future__ import annotations

import base64

PASSWORD = "correct horse battery"


class CsrfClient:
    """Wraps Flask's test client and sends X-CSRFToken on unsafe methods, as the browser code does."""

    def __init__(self, client):
        self.raw = client
        self.csrf_token: str | None = None

    def __getattr__(self, name):  # get(), cookie_jar, etc. pass straight through
        return getattr(self.raw, name)

    def refresh_csrf(self, **kwargs) -> str:
        self.csrf_token = self.raw.get("/api/csrf", **kwargs).get_json()["csrf_token"]
        return self.csrf_token

    def _unsafe(self, method: str, *args, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        if "X-CSRFToken" not in headers:
            if self.csrf_token is None:
                self.refresh_csrf()
            headers["X-CSRFToken"] = self.csrf_token
        return getattr(self.raw, method)(*args, headers=headers, **kwargs)

    def post(self, *args, **kwargs): return self._unsafe("post", *args, **kwargs)
    def put(self, *args, **kwargs): return self._unsafe("put", *args, **kwargs)
    def patch(self, *args, **kwargs): return self._unsafe("patch", *args, **kwargs)
    def delete(self, *args, **kwargs): return self._unsafe("delete", *args, **kwargs)


def sign_up(client: CsrfClient, username: str = "ada", password: str = PASSWORD, **kwargs):
    response = client.raw.post(
        "/register",
        data={"username": username, "email": f"{username}@example.com", "password": password, "csrf_token": client.refresh_csrf(**kwargs)},
        **kwargs,
    )
    assert response.status_code == 302, response.get_data(as_text=True)
    client.refresh_csrf(**kwargs)  # registering rotates the session, so the old token is no longer valid
    return response


def sign_in(client: CsrfClient, username: str = "ada", password: str = PASSWORD, **kwargs):
    response = client.raw.post(
        "/login",
        data={"identity": username, "password": password, "csrf_token": client.refresh_csrf(**kwargs)},
        **kwargs,
    )
    assert response.status_code == 302, response.get_data(as_text=True)
    client.refresh_csrf(**kwargs)
    return response


def encoded_judge_result(result):
    """Create the text shape returned by Judge0 with base64_encoded=true."""
    return {
        key: base64.b64encode(value.encode("utf-8")).decode("ascii")
        if key in {"stdout", "stderr", "compile_output", "message"} and isinstance(value, str)
        else value
        for key, value in result.items()
    }


def install_fake_judge(monkeypatch, stdout: str = "Hello, World!\n"):
    """Patch the requests boundary with a complete Judge0 submit-then-poll sequence. Returns the call log."""
    calls: list[tuple[str, str]] = []

    class Response:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if method == "POST":
            return Response({"token": "abc"})
        return Response(encoded_judge_result({"stdout": stdout, "status": {"id": 3, "description": "Accepted"}, "time": "0.01", "memory": 8500}))

    monkeypatch.setattr("app.requests.request", fake_request)
    return calls
