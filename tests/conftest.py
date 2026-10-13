import pytest

from app import create_app
from helpers import CsrfClient, sign_up

TEST_SECRET = "test-only-secret-key-never-used-in-production"


@pytest.fixture()
def make_app(tmp_path):
    """Build an isolated app (own SQLite file) with a fixed test secret."""
    def factory(**config):
        return create_app({"TESTING": True, "SECRET_KEY": TEST_SECRET, "DATABASE": str(tmp_path / "history.db"), **config})
    return factory


@pytest.fixture()
def guest(make_app):
    """An anonymous browser: has a session and CSRF token but is not signed in."""
    app = make_app()
    with app.test_client() as raw:
        yield CsrfClient(raw), app


@pytest.fixture()
def client(guest):
    """A signed-in browser. Most existing tests exercise history, which is per-account."""
    client, app = guest
    sign_up(client, "ada")
    yield client, app
