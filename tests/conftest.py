from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from stagescreen.config import AppConfig
from stagescreen.container import Services
from stagescreen.i18n import set_language
from stagescreen.infrastructure.network import NetworkService
from stagescreen.infrastructure.privileged import NullRunner
from stagescreen.web.app import create_app
from tests.fakes import fake_nmcli, make_drm


@pytest.fixture()
def services(tmp_path: Path) -> Services:
    runner = NullRunner()
    config = AppConfig(data_dir=tmp_path / "data", system_backend="null", drm_dir=make_drm(tmp_path), port=0)
    return Services(config, privileged=runner, network=NetworkService(runner, read=fake_nmcli))


@pytest.fixture()
def client(services: Services):
    with TestClient(create_app(services)) as cl:
        cl.headers["X-ESS"] = "1"
        yield cl
    set_language("en")


@pytest.fixture()
def authed(client):
    r = client.post("/api/auth/setup", json={"username": "admin", "password": "secret123"})
    assert r.status_code == 200, r.text
    return client
