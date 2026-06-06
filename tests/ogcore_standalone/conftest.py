"""
OG-Core-specific fixtures.

Scoped to this folder, so only the OG-Core suite pays for them. The app/client
fixtures come from the parent tests/conftest.py and are inherited here, which is
why ogc_client can depend on app without redefining it.
"""

import pytest


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
