"""Newsletters: double opt-in, issues from research, sending, unsubscribe, public archive, scoping."""

import re

import pytest
from fastapi.testclient import TestClient

from atlas import auth


@pytest.fixture()
def secured(seeded, monkeypatch):
    monkeypatch.setenv("ATLAS_AUTH", "required")
    from atlas import newsletters
    from atlas.api.main import app

    monkeypatch.setattr(newsletters, "BACKGROUND", False)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def outbox(monkeypatch):
    from atlas import mailer

    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, **kw: sent.append({"to": to, "subject": subject, "body": body, **kw}) or True)
    return sent


def _signup(client, email, workspace):
    r = client.post("/api/v1/auth/signup", json={"email": email, "password": "correct horse battery", "name": email.split("@")[0], "workspace_name": workspace})
    assert r.status_code == 201, r.text
    return r.json()


def _link(body: str, path: str) -> str:
    m = re.search(rf"https?://\S+?{re.escape(path)}\S*", body)
    assert m, body
    return m.group(0).split("8010", 1)[1]


def _completed_run(acct) -> str:
    from atlas.research import engine

    run_id = engine.create_run("Sugar price outlook", {"engine": "deterministic", "formats": []}, acct["user"]["user_id"],
                               acct["workspace"]["workspace_id"])
    engine.execute_run(run_id)
    return run_id


SETTINGS = {"slug": "sugar-weekly", "name": "Sugar Weekly", "description": "Sugar markets, verified.", "from_name": "Sugar Desk",
            "reply_to": "desk@pub.test", "postal_address": ""}


def test_double_opt_in_issue_send_unsubscribe_and_archive(secured, outbox):
    pub = _signup(secured, "editor@pub.test", "Publisher")
    visitor = TestClient(secured.app)
    assert visitor.get("/api/v1/public/newsletters/sugar-weekly").status_code == 404
    assert secured.put("/api/v1/newsletter", json={**SETTINGS, "slug": "No Spaces"}).status_code == 422
    r = secured.put("/api/v1/newsletter", json=SETTINGS)
    assert r.status_code == 200 and r.json()["public_url"].endswith("/n/sugar-weekly")
    other = TestClient(secured.app)
    _signup(other, "rival@pub.test", "Rival")
    assert other.put("/api/v1/newsletter", json=SETTINGS).status_code == 422  # address taken

    # Subscribing answers identically whether or not the address is known, and sends a confirmation.
    for _ in range(2):
        assert visitor.post("/api/v1/public/newsletters/sugar-weekly/subscribe", json={"email": "Reader@Mail.test"}).json() == {"status": "check_your_email"}
    assert len([m for m in outbox if m["to"] == "reader@mail.test"]) == 1  # no resend flood
    assert visitor.post("/api/v1/public/newsletters/sugar-weekly/subscribe", json={"email": "not-an-email"}).status_code == 422
    assert visitor.post("/api/v1/public/newsletters/missing/subscribe", json={"email": "a@b.test"}).status_code == 404
    assert secured.get("/api/v1/newsletter").json()["newsletter"]["subscribers"] == {"active": 0, "pending": 1, "unsubscribed": 0}
    assert visitor.get("/newsletter/confirm?token=bogus").status_code == 400
    confirm = _link(outbox[-1]["body"], "/newsletter/confirm")
    assert "subscribed to Sugar Weekly" in visitor.get(confirm).text
    assert visitor.get(confirm).status_code == 400  # single use
    secured.post("/api/v1/newsletter/subscribers", json={"email": "invited@mail.test"})
    assert outbox[-1]["to"] == "invited@mail.test" and "Confirm" in outbox[-1]["subject"]  # invites still need consent

    run_id = _completed_run(pub)
    issue = secured.post("/api/v1/newsletter/issues", json={"run_id": run_id, "intro": "This week: <b>sugar</b>."}).json()
    assert issue["status"] == "draft" and issue["subject"] and "Sources:" in issue["body_markdown"]
    preview = secured.get(f"/api/v1/newsletter/issues/{issue['issue_id']}").json()["html"]
    assert "<b>" not in preview and "&lt;b&gt;" in preview and "not personalised investment advice" in preview
    assert secured.patch(f"/api/v1/newsletter/issues/{issue['issue_id']}", json={"subject": "Sugar: week 43"}).json()["subject"] == "Sugar: week 43"

    t = secured.post(f"/api/v1/newsletter/issues/{issue['issue_id']}/test").json()
    assert t["to"] == "editor@pub.test" and outbox[-1]["subject"] == "Sugar: week 43"
    r = secured.post(f"/api/v1/newsletter/issues/{issue['issue_id']}/send")
    assert r.status_code == 422 and "postal address" in r.text  # anti-spam laws: address first
    secured.put("/api/v1/newsletter", json={**SETTINGS, "postal_address": "1 Main St, La Paz, Bolivia"})
    before = len(outbox)
    r = secured.post(f"/api/v1/newsletter/issues/{issue['issue_id']}/send")
    assert r.status_code == 200 and r.json()["subscribers"] == 1
    sent = outbox[before:]
    assert [m["to"] for m in sent] == ["reader@mail.test"]  # only confirmed subscribers
    mail = sent[0]
    assert mail["from_name"] == "Sugar Desk" and mail["headers"]["Reply-To"] == "desk@pub.test"
    assert mail["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "1 Main St, La Paz, Bolivia" in mail["body"] and "1 Main St" in mail["html"]
    assert secured.post(f"/api/v1/newsletter/issues/{issue['issue_id']}/send").status_code == 422  # once
    assert secured.patch(f"/api/v1/newsletter/issues/{issue['issue_id']}", json={"subject": "x"}).status_code == 422
    listed = secured.get("/api/v1/newsletter/issues").json()[0]
    assert listed["status"] == "sent" and listed["recipients"] == 1

    # Public archive.
    info = visitor.get("/api/v1/public/newsletters/sugar-weekly").json()
    assert info["name"] == "Sugar Weekly" and [i["subject"] for i in info["issues"]] == ["Sugar: week 43"]
    page = visitor.get(f"/api/v1/public/newsletters/sugar-weekly/issues/{issue['issue_id']}").json()
    assert page["subject"] == "Sugar: week 43" and "Sources:" in page["html"]

    # One-click unsubscribe (POST, as mail clients do), and a tampered link fails.
    unsub = mail["headers"]["List-Unsubscribe"].strip("<>").split("8010", 1)[1]
    assert visitor.get(unsub[:-2] + "00").status_code == 400
    assert "receive Sugar Weekly any more" in visitor.post(unsub).text
    issue2 = secured.post("/api/v1/newsletter/issues", json={"run_id": run_id}).json()
    r = secured.post(f"/api/v1/newsletter/issues/{issue2['issue_id']}/send")
    assert r.status_code == 422 and "no confirmed subscribers" in r.text
    csv_text = secured.get("/api/v1/newsletter/subscribers?format=csv").text
    assert "reader@mail.test,unsubscribed" in csv_text

    # Archive off: issue pages disappear.
    secured.put("/api/v1/newsletter", json={**SETTINGS, "postal_address": "1 Main St", "public_archive": False})
    assert visitor.get(f"/api/v1/public/newsletters/sugar-weekly/issues/{issue['issue_id']}").status_code == 404
    assert visitor.get("/api/v1/public/newsletters/sugar-weekly").json()["issues"] == []


def test_newsletter_is_scoped_capped_and_purged(secured, outbox, monkeypatch):
    from atlas import newsletters
    from atlas.data.db import Newsletter, NewsletterSubscriber, Workspace, session_scope, utcnow

    pub = _signup(secured, "small@pub.test", "Small")
    ws = pub["workspace"]["workspace_id"]
    secured.put("/api/v1/newsletter", json={**SETTINGS, "slug": "small-list"})
    run_id = _completed_run(pub)
    issue = secured.post("/api/v1/newsletter/issues", json={"run_id": run_id}).json()

    outsider = TestClient(secured.app)
    _signup(outsider, "nosy@pub.test", "Nosy")
    assert outsider.get("/api/v1/newsletter").json()["newsletter"] is None
    assert outsider.get(f"/api/v1/newsletter/issues/{issue['issue_id']}").status_code == 404
    assert outsider.post("/api/v1/newsletter/issues", json={"run_id": run_id}).status_code == 404  # no newsletter of their own
    outsider.put("/api/v1/newsletter", json={**SETTINGS, "slug": "nosy-list"})
    assert outsider.post("/api/v1/newsletter/issues", json={"run_id": run_id}).status_code == 422  # not their research

    auth.set_plan(ws, "investor")
    token = secured.post("/api/v1/team/invitations", json={"email": "writer@pub.test"}).json()["link"].split("token=")[1]
    member = TestClient(secured.app)
    member.post("/api/v1/auth/invitations/accept", json={"token": token, "password": "member password!"})
    assert member.get("/api/v1/newsletter").status_code == 403

    # Subscriber cap: new addresses beyond the plan's limit are quietly not added.
    monkeypatch.setitem(auth.PLANS["investor"], "newsletter_subscribers", 2)
    visitor = TestClient(secured.app)
    for i in range(3):
        visitor.post("/api/v1/public/newsletters/small-list/subscribe", json={"email": f"r{i}@mail.test"})
    assert [r["email"] for r in secured.get("/api/v1/newsletter/subscribers").json()] == ["r1@mail.test", "r0@mail.test"]
    assert secured.get("/api/v1/account").json()["usage"]["newsletter_subscribers"] == 2

    # Suspended workspaces' newsletters vanish publicly.
    with session_scope() as s:
        s.get(Workspace, ws).suspended_at = utcnow()
    assert visitor.get("/api/v1/public/newsletters/small-list").status_code == 404
    with session_scope() as s:
        s.get(Workspace, ws).suspended_at = None

    # A send interrupted by a restart is finished by the scheduler, without duplicates.
    with session_scope() as s:
        for sub in s.query(NewsletterSubscriber).filter(NewsletterSubscriber.email.in_(["r0@mail.test", "r1@mail.test"])):
            sub.status = "active"
    secured.put("/api/v1/newsletter", json={**SETTINGS, "slug": "small-list", "postal_address": "PO Box 1"})
    monkeypatch.setattr(newsletters, "BACKGROUND", True)
    monkeypatch.setattr(newsletters.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())  # "crash" before sending
    secured.post(f"/api/v1/newsletter/issues/{issue['issue_id']}/send")
    with session_scope() as s:
        sub = s.query(NewsletterSubscriber).filter(NewsletterSubscriber.email == "r0@mail.test").one()
        sub.last_issue_id = issue["issue_id"]  # r0 had already received it
    before = len(outbox)
    from datetime import timedelta

    assert newsletters.resume_stalled(utcnow() + timedelta(minutes=20)) == 1
    assert [m["to"] for m in outbox[before:]] == ["r1@mail.test"]
    assert newsletters.resume_stalled(utcnow() + timedelta(minutes=40)) == 0

    # Deleting the publisher's account (sole member once the writer is removed) removes the newsletter and its list.
    writer_id = member.get("/api/v1/account").json()["user"]["user_id"]
    assert secured.delete(f"/api/v1/team/members/{writer_id}").status_code == 204
    r = secured.post("/api/v1/account/delete", json={"password": "correct horse battery", "confirm_email": "small@pub.test"})
    assert r.status_code in (200, 204), r.text
    with session_scope() as s:
        assert s.query(Newsletter).filter(Newsletter.slug == "small-list").count() == 0
        assert s.query(NewsletterSubscriber).filter(NewsletterSubscriber.email == "r1@mail.test").count() == 0
