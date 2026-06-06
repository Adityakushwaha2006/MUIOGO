"""
Fixtures shared across all tests.

API/ is on the path via pyproject.toml, so imports just work with no sys.path hacks.
"""

import pytest

from app import app as _flask_app


@pytest.fixture(scope="session")
def app():
    """
    The Flask app, set up for testing.

    TESTING is kept False on purpose. When it's True, Flask re-raises exceptions
    instead of returning 500, which means tests would crash instead of asserting
    the right status code. We want real HTTP responses, not stack traces.
    """
    _flask_app.config.update(
        {
            "SECRET_KEY": "test-secret-key",
            "TESTING": False,
        }
    )
    yield _flask_app


@pytest.fixture()
def client(app):
    """Return a test client for the Flask app."""
    with app.test_client() as c:
        yield c


# ── OG-Core fixtures (shared by all test_ogcore_* modules) ──────────────────

@pytest.fixture()
def isolated_storage(tmp_path, monkeypatch):
    """
    Redirect Config.OGC_DATA_STORAGE to a fresh temp dir for one test.

    OGCoreCase / OGCoreRunner read Config.OGC_DATA_STORAGE at construction, and
    the route layer reads it per request, so patching the attribute before the
    test runs fully isolates OG-Core disk state. Nothing touches the real store,
    and each test starts from an empty directory (deterministic, no flakiness).
    """
    from Classes.Base import Config

    store = tmp_path / "OGCore"
    store.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Config, "OGC_DATA_STORAGE", store)
    return store


@pytest.fixture()
def ogc_client(app, isolated_storage):
    """Test client whose OG-Core storage is the isolated temp dir."""
    with app.test_client() as c:
        yield c
