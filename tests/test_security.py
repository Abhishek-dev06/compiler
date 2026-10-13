"""CSRF validation, authentication/history isolation, session cookies, and Judge0-only execution."""
import re
import subprocess
from pathlib import Path

import pytest

from app import create_app
from conftest import TEST_SECRET
from helpers import PASSWORD, CsrfClient, install_fake_judge, sign_in, sign_up

RUN = {"source_code": "print('hi')", "language": "python", "stdin": ""}
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- CSRF validation

def test_run_without_csrf_token_is_rejected_before_judge0(guest, monkeypatch):
    client, _ = guest
    calls = install_fake_judge(monkeypatch)
    response = client.raw.post("/api/execute", json=RUN)  # no X-CSRFToken header
    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid or missing CSRF token.", "code": "csrf_failed"}
    assert calls == []  # nothing was submitted to Judge0


def test_run_with_wrong_csrf_token_is_rejected(guest, monkeypatch):
    client, _ = guest
    calls = install_fake_judge(monkeypatch)
    client.refresh_csrf()
    response = client.raw.post("/api/execute", json=RUN, headers={"X-CSRFToken": "not-the-session-token"})
    assert response.status_code == 400 and response.get_json()["code"] == "csrf_failed"
    assert calls == []


def test_token_from_another_session_is_rejected(make_app, monkeypatch):
    install_fake_judge(monkeypatch)
    app = make_app()
    first, second = app.test_client(), app.test_client()
    stolen = first.get("/api/csrf").get_json()["csrf_token"]
    second.get("/api/csrf")  # second browser has its own session and token
    response = second.post("/api/execute", json=RUN, headers={"X-CSRFToken": stolen})
    assert response.status_code == 400


def test_run_with_valid_csrf_token_goes_through_judge0(guest, monkeypatch):
    client, _ = guest
    calls = install_fake_judge(monkeypatch, stdout="hi\n")
    response = client.post("/api/execute", json=RUN)
    assert response.status_code == 200
    assert response.get_json()["status"] == "Accepted" and response.get_json()["stdout"] == "hi\n"
    assert [method for method, _ in calls] == ["POST", "GET"]  # Judge0 submit, then poll


def test_page_embeds_the_token_the_server_validates(guest, monkeypatch):
    """The real browser flow: read the token from the page's meta tag and send it in X-CSRFToken."""
    client, _ = guest
    install_fake_judge(monkeypatch)
    page = client.get("/compiler").get_data(as_text=True)
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page).group(1)
    assert client.raw.post("/api/execute", json=RUN, headers={"X-CSRFToken": token}).status_code == 200


def test_frontend_sends_the_header_the_backend_expects():
    script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "X-CSRFToken" in script and 'meta[name="csrf-token"]' in script and "/api/csrf" in script


def test_every_unsafe_method_needs_a_token(client):
    client, _ = client
    assert client.raw.delete("/api/history/1").status_code == 400  # CSRF is checked before the 404
    assert client.raw.post("/logout").status_code == 400
    assert client.get("/api/history").status_code == 200  # safe methods are not affected


def test_csrf_token_endpoint_is_per_session_and_not_cacheable(guest):
    client, _ = guest
    first = client.get("/api/csrf")
    assert first.headers["Cache-Control"] == "no-store"
    assert first.get_json()["csrf_token"] == client.get("/api/csrf").get_json()["csrf_token"]
    assert client.get("/compiler").headers["Cache-Control"] == "no-store"


# ---------------------------------------------------------------- history authentication

def test_guest_can_run_code_but_nothing_is_saved(guest, monkeypatch):
    client, _ = guest
    install_fake_judge(monkeypatch)
    run = client.post("/api/execute", json=RUN).get_json()
    assert run["history_saved"] is False and run["id"] is None and run["status"] == "Accepted"


def test_history_requires_authentication(guest):
    client, _ = guest
    for response in (client.get("/api/history"), client.get("/api/history/1"), client.delete("/api/history/1")):
        assert response.status_code == 401
        assert response.get_json() == {"error": "Authentication required."}


def test_me_reports_guest_state_and_signed_in_state(guest):
    client, _ = guest
    assert client.get("/api/me").get_json()["authenticated"] is False
    sign_up(client, "ada")
    me = client.get("/api/me").get_json()
    assert me["authenticated"] is True and me["user"]["username"] == "ada"


def test_history_is_isolated_per_account(make_app, monkeypatch):
    install_fake_judge(monkeypatch)
    app = make_app()
    alice, bob = CsrfClient(app.test_client()), CsrfClient(app.test_client())
    sign_up(alice, "alice")
    sign_up(bob, "bob")
    alice_run = alice.post("/api/execute", json=RUN).get_json()
    assert bob.get("/api/history").get_json() == []
    assert bob.get(f"/api/history/{alice_run['id']}").status_code == 404
    assert bob.delete(f"/api/history/{alice_run['id']}").status_code == 404
    assert [run["id"] for run in alice.get("/api/history").get_json()] == [alice_run["id"]]


def test_legacy_unowned_runs_are_never_exposed(client, monkeypatch):
    client, app = client
    with app.app_context():
        import sqlite3
        db = sqlite3.connect(app.config["DATABASE"])
        db.execute("INSERT INTO executions(source_code,language,status,created_at,user_id) VALUES('secret','python','Accepted','2024-01-01',NULL)")
        db.commit(); db.close()
    assert client.get("/api/history").get_json() == []


# ---------------------------------------------------------------- login / register flow

def test_login_and_register_pages_render_with_a_csrf_field(guest):
    client, _ = guest
    for path in ("/login", "/register"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'name="csrf_token"' in response.get_data(as_text=True)


def test_compiler_page_offers_sign_in_to_guests_and_sign_out_to_users(guest):
    client, _ = guest
    page = client.get("/compiler").get_data(as_text=True)
    assert "Sign in to see your run history" in page and 'href="/login?next=/compiler"' in page
    assert "window.CODERUN_AUTHENTICATED = false" in page
    sign_up(client, "ada")
    page = client.get("/compiler").get_data(as_text=True)
    assert "Sign out" in page and "window.CODERUN_AUTHENTICATED = true" in page


def test_login_post_without_csrf_is_rejected(guest):
    client, _ = guest
    sign_up(client, "ada")
    client.raw.post("/logout", headers={"X-CSRFToken": client.csrf_token})
    response = client.raw.post("/login", data={"identity": "ada", "password": PASSWORD})
    assert response.status_code == 400


def test_wrong_password_is_rejected_without_signing_in(guest):
    client, _ = guest
    sign_up(client, "ada")
    client.post("/logout")
    response = client.raw.post("/login", data={"identity": "ada", "password": "wrong-password", "csrf_token": client.refresh_csrf()})
    assert response.status_code == 200 and "Incorrect username/email or password." in response.get_data(as_text=True)
    assert client.get("/api/history").status_code == 401


def test_login_rotates_the_csrf_token_and_logout_ends_the_session(guest):
    client, _ = guest
    sign_up(client, "ada")
    client.post("/logout")
    before = client.refresh_csrf()
    sign_in(client, "ada")
    assert client.csrf_token != before  # session fixation protection: new session, new token
    assert client.get("/api/history").status_code == 200
    assert client.post("/logout").status_code == 302
    assert client.get("/api/history").status_code == 401


def test_login_required_pages_redirect_to_login_and_return_to_next(guest):
    client, _ = guest
    redirect = client.get("/history")
    assert redirect.status_code == 302 and "/login?next=" in redirect.headers["Location"]
    sign_up(client, "ada")
    client.post("/logout")
    sign_in_response = client.raw.post("/login?next=/compiler", data={"identity": "ada", "password": PASSWORD, "csrf_token": client.refresh_csrf()})
    assert sign_in_response.headers["Location"].endswith("/compiler")


@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example", "/\\evil.example", "javascript:alert(1)", "/ok\r\nSet-Cookie: x=1"])
def test_login_ignores_unsafe_next_targets(guest, target):
    client, _ = guest
    sign_up(client, "ada")
    client.post("/logout")
    response = client.raw.post("/login", query_string={"next": target}, data={"identity": "ada", "password": PASSWORD, "csrf_token": client.refresh_csrf()})
    assert response.status_code == 302 and response.headers["Location"].endswith("/compiler")


def test_signed_in_user_is_not_sent_to_missing_dashboard_template(guest):
    client, _ = guest
    sign_up(client, "ada")
    response = client.get("/")
    assert response.status_code == 302 and response.headers["Location"].endswith("/compiler")  # no 500 from a missing template


# ---------------------------------------------------------------- production cookies and secrets

def production_env(monkeypatch, secret="x" * 48):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    if secret is None: monkeypatch.delenv("SECRET_KEY", raising=False)
    else: monkeypatch.setenv("SECRET_KEY", secret)


@pytest.mark.parametrize("secret", [None, "", "local-development-change-this-secret"])
def test_production_refuses_to_start_without_a_real_secret_key(tmp_path, monkeypatch, secret):
    production_env(monkeypatch, secret)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"DATABASE": str(tmp_path / "x.db")})


def test_development_uses_a_random_key_not_the_old_public_default(tmp_path, monkeypatch):
    monkeypatch.delenv("RENDER", raising=False); monkeypatch.delenv("APP_ENV", raising=False); monkeypatch.delenv("SECRET_KEY", raising=False)
    first = create_app({"DATABASE": str(tmp_path / "a.db")}); second = create_app({"DATABASE": str(tmp_path / "b.db")})
    assert first.config["SECRET_KEY"] != second.config["SECRET_KEY"]
    assert first.config["SECRET_KEY"] != "local-development-change-this-secret"
    assert first.config["SESSION_COOKIE_SECURE"] is False  # plain-http local development keeps working


def test_production_cookie_is_secure_httponly_samesite_and_works_over_https(tmp_path, monkeypatch):
    production_env(monkeypatch)
    install_fake_judge(monkeypatch)
    app = create_app({"DATABASE": str(tmp_path / "prod.db")})
    assert app.config["SESSION_COOKIE_SECURE"] is True
    https = {"base_url": "https://coderun.example", "headers": {"X-Forwarded-Proto": "https"}}
    with app.test_client() as raw:
        client = CsrfClient(raw)
        cookie = raw.get("/compiler", **https).headers["Set-Cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
        # Full flow over HTTPS: token -> register -> run -> history.
        client.refresh_csrf(**https)
        response = raw.post("/register", data={"username": "ada", "email": "ada@example.com", "password": PASSWORD, "csrf_token": client.csrf_token}, **https)
        assert response.status_code == 302
        client.refresh_csrf(**https)
        run = raw.post("/api/execute", json=RUN, headers={"X-CSRFToken": client.csrf_token, "X-Forwarded-Proto": "https"}, base_url="https://coderun.example")
        assert run.status_code == 200 and run.get_json()["history_saved"] is True
        assert len(raw.get("/api/history", **https).get_json()) == 1


def test_secure_cookie_can_be_overridden_for_local_https_testing(tmp_path, monkeypatch):
    production_env(monkeypatch)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    assert create_app({"DATABASE": str(tmp_path / "p.db")}).config["SESSION_COOKIE_SECURE"] is False


# ---------------------------------------------------------------- code execution stays on Judge0

def test_flask_never_spawns_a_process_to_run_submitted_code(client, monkeypatch):
    client, _ = client
    install_fake_judge(monkeypatch)
    def forbidden(*args, **kwargs): raise AssertionError("submitted code must only run on Judge0")
    for name in ("run", "Popen", "call", "check_output", "check_call"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr("os.system", forbidden)
    assert client.post("/api/execute", json={"source_code": "import os; os.system('id')", "language": "python", "stdin": ""}).status_code == 200


def test_execute_validates_input_after_csrf(guest):
    client, _ = guest
    assert client.post("/api/execute", json={"source_code": "", "language": "python"}).status_code == 400
    assert client.post("/api/execute", json={"source_code": "x", "language": "brainfuck"}).status_code == 400
    assert client.post("/api/execute", data="not json", headers={"Content-Type": "text/plain"}).status_code == 400
