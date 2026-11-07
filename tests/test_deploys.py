import base64
import logging
import secrets

import pytest
from fastapi.testclient import TestClient
from test_auth import sign_up

import argus.app as appmod
from argus.alerts import Alerter
from argus.deploys import env_text
from argus.runtime import Runtime


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "stacks"
    (root / "hosts").mkdir(parents=True)
    (root / "hosts" / "nas").write_text("# what nas runs\nsites/home/web\nsites/home/missing\n")
    (root / "hosts" / "pi").write_text("sites/home/web\n")
    web = root / "sites" / "home" / "web"
    (web / "conf").mkdir(parents=True)
    (web / "compose.yml").write_text("services: {web: {image: nginx}}\n")
    (web / "conf" / "site.conf").write_text("listen 80;\n")
    return root


@pytest.fixture
def client(tmp_path, prober, repo, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="argus")
    monkeypatch.setenv("ARGUS_DEPLOY_DIR", str(repo))
    rt = Runtime(tmp_path / "none.yml", tmp_path / "argus.db", prober=prober)
    with TestClient(appmod.create_app(rt, start_scheduler=False)) as c:
        c.portal.call(c.app.state.deploys.refresh)
        yield c


def enroll(client, csrf, name, read_only=False):
    code = client.post("/api/companions/codes", headers=csrf, json={
        "url": "https://argus.lan", "name": name, "read_only": read_only}).json()["code"]
    return client.post("/companion/enroll", json={"code": code, "hostname": name}).json()


def report(client, token, **fields):
    return client.post("/companion/report", json={"nonce": secrets.token_urlsafe(16), **fields},
                       headers={"Authorization": f"Bearer {token}"}).json()


def test_a_host_gets_its_stacks_with_secrets_and_reports_back(client, caplog):
    csrf = {"X-CSRF-Token": sign_up(client, caplog)["csrf"]}
    nas = enroll(client, csrf, "nas")
    r = client.put("/api/deploys/secrets", headers=csrf,
                   json={"path": "sites/home/web", "env": {"TOKEN": "s3cret$x", "bad key": "x"}})
    assert r.status_code == 422  # names are checked
    r = client.put("/api/deploys/secrets", headers=csrf,
                   json={"path": "sites/home/web", "env": {"TOKEN": "s3cret$x"}})
    assert r.json() == {"path": "sites/home/web", "secrets": ["TOKEN"]}  # names, never values

    plan = report(client, nas["token"])["deploy"]
    (web,) = plan["stacks"]  # the missing folder is skipped
    assert web["name"] == "web" and web["env"] == "TOKEN='s3cret$x'\n"
    assert base64.b64decode(web["files"]["conf/site.conf"]) == b"listen 80;\n"
    assert plan["pull"] is None  # no read token saved: public images only

    # Changing a secret changes the digest, so the stack redeploys.
    client.put("/api/deploys/secrets", headers=csrf,
               json={"path": "sites/home/web", "env": {"TOKEN": "new"}})
    again = report(client, nas["token"])["deploy"]["stacks"][0]
    assert again["digest"] != web["digest"]

    view = client.get("/api/companions").json()["companions"][0]
    assert view["stacks"][0]["status"] == "pending"
    report(client, nas["token"], deploys=[{"path": "sites/home/web", "digest": again["digest"],
                                           "result": "ok", "at": 1.0}])
    view = client.get("/api/companions").json()["companions"][0]
    assert view["stacks"][0]["status"] == "deployed" and view["stacks"][0]["secrets"] == ["TOKEN"]
    assert "new" not in str(client.get("/api/companions").json())


def test_monitoring_only_hosts_get_no_deploys(client, caplog):
    csrf = {"X-CSRF-Token": sign_up(client, caplog)["csrf"]}
    pi = enroll(client, csrf, "pi", read_only=True)
    assert "deploy" not in report(client, pi["token"])
    (pi_view,) = client.get("/api/companions").json()["companions"]
    assert pi_view["stacks"][0]["status"] == "read_only"


def test_a_pull_credential_only_when_something_is_due(client, caplog):
    csrf = {"X-CSRF-Token": sign_up(client, caplog)["csrf"]}
    client.put("/api/deploys", headers=csrf, json={"pull_token": "ghp_" + "a" * 36})
    nas = enroll(client, csrf, "nas")
    plan = report(client, nas["token"])["deploy"]
    assert plan["pull"]["registry"] == "ghcr.io"
    done = [{"path": "sites/home/web", "digest": plan["stacks"][0]["digest"], "result": "ok"}]
    assert report(client, nas["token"], deploys=done)["deploy"]["pull"] is None
    assert client.get("/api/deploys").json()["pull_token_set"] is True
    assert "ghp_" not in str(client.get("/api/deploys").json())


async def test_a_failed_or_refused_deploy_pages(client, caplog):
    csrf = {"X-CSRF-Token": sign_up(client, caplog)["csrf"]}
    nas = enroll(client, csrf, "nas")
    plan = report(client, nas["token"])["deploy"]
    report(client, nas["token"], deploys=[{
        "path": "sites/home/web", "digest": plan["stacks"][0]["digest"], "result": "refused",
        "detail": "this host's policy refuses it: web: privileged"}])
    alerter: Alerter = client.app.state.alerter
    (p,) = [p for k, p in alerter.problems().items() if k.startswith("deploy:")]
    assert p.title == "deploy of web on nas was refused by its policy"
    assert "privileged" in p.detail


def test_env_text_keeps_dollars_and_hashes_literal():
    assert env_text({"B": "a$b#c", "A": "it's"}) == "A=\"it's\"\nB='a$b#c'\n"


def test_deploy_settings_need_the_admin(client):
    assert client.put("/api/deploys", json={"repo": "a/b"}).status_code == 401
    assert client.put("/api/deploys/secrets",
                      json={"path": "a", "env": {}}).status_code == 401


async def test_a_saved_read_token_comes_before_the_apps_token(tmp_path):
    # A GitHub App cannot read a personal account's packages (mars-flink pulls
    # failed with "installation not allowed"); the owner's saved token can.
    import sqlite3
    from types import SimpleNamespace

    from argus.deploys import PULL_TOKEN, REPO, Deploys
    from argus.ghauth import AppCredentials
    from argus.settings import Settings, load_key

    app = object.__new__(AppCredentials)

    async def pull_token(owner):
        return "app-installation-token"
    app.pull_token = pull_token
    s = Settings(sqlite3.connect(":memory:"), load_key(tmp_path / "k"))
    s.set(REPO, "ann/stacks")
    d = Deploys(s, SimpleNamespace(creds=app), lambda: 0.0)
    assert (await d.pull_credential())["password"] == "app-installation-token"  # noqa: S105
    s.set_secret(PULL_TOKEN, "ghp_" + "r" * 36)
    assert (await d.pull_credential()) == {"registry": "ghcr.io", "username": "argus",
                                           "password": "ghp_" + "r" * 36}
