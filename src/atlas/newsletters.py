"""Newsletters: a workspace publishes its verified research as an email newsletter.

One newsletter per workspace. People subscribe on its public page and confirm by email (double
opt-in); owners and admins can also invite someone, which sends the same confirmation. An issue is a
snapshot of a completed research run (its narrative, sources and the standard disclaimer) with an
editable subject and introduction. Every email carries a one-click unsubscribe link (also as
List-Unsubscribe headers) and the publisher's postal address, which anti-spam laws require; sending
is refused until the address is set. Sent issues can be published in a public archive.
"""

from __future__ import annotations

import csv
import hashlib
import html as _html
import io
import logging
import os
import re
import secrets
import threading
from datetime import timedelta
from typing import Any

import markdown as md
from sqlalchemy import delete, func, select

from atlas import secretbox
from atlas.auth import PLANS, Principal, audit
from atlas.data.db import (
    Newsletter,
    NewsletterIssue,
    NewsletterSubscriber,
    ResearchRun,
    User,
    Workspace,
    session_scope,
    utcnow,
)

log = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])$")
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,250}\.[A-Za-z]{2,24}$")
CONFIRM_TTL = timedelta(days=7)
RESEND_AFTER = timedelta(minutes=10)
STALLED = timedelta(minutes=15)
DISCLAIMER = ("Based on public reference data published with a lag; not a live quote. "
              "General research, not personalised investment advice.")
BACKGROUND = True  # tests send synchronously


class NewsletterError(ValueError):
    pass


def _iso(d) -> str | None:
    return d.isoformat(timespec="seconds") + "Z" if d else None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def web_url(path: str) -> str:
    return os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/") + path


def api_url(path: str) -> str:
    return os.getenv("ATLAS_API_PUBLIC_URL", "http://localhost:8010").rstrip("/") + path


def subscriber_cap(workspace_id: str) -> int:
    with session_scope() as s:
        ws = s.get(Workspace, workspace_id)
        plan = ws.plan if ws else "explorer"
    return int(PLANS.get(plan, PLANS["explorer"])["newsletter_subscribers"])


def safe_html(markdown_text: str) -> str:
    """Markdown to HTML with raw HTML escaped and only http(s)/mailto links kept."""
    out = md.markdown(markdown_text.replace("&", "&amp;").replace("<", "&lt;"), extensions=["tables", "sane_lists"])
    return re.sub(r'href="(?!https?://|mailto:)[^"]*"', 'href="#"', out)


# ---------------------------------------------------------------- settings


def _settings(n: Newsletter, s) -> dict[str, Any]:
    counts = dict(s.execute(select(NewsletterSubscriber.status, func.count()).where(
        NewsletterSubscriber.newsletter_id == n.newsletter_id).group_by(NewsletterSubscriber.status)).all())
    return {"newsletter_id": n.newsletter_id, "slug": n.slug, "name": n.name, "description": n.description,
            "from_name": n.from_name, "reply_to": n.reply_to, "postal_address": n.postal_address,
            "public_archive": n.public_archive, "public_url": web_url(f"/n/{n.slug}"),
            "subscribers": {"active": counts.get("active", 0), "pending": counts.get("pending", 0),
                            "unsubscribed": counts.get("unsubscribed", 0)}}


def get_settings(workspace_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        n = s.execute(select(Newsletter).where(Newsletter.workspace_id == workspace_id)).scalar_one_or_none()
        if n is None:
            return None
        out = _settings(n, s)
    out["subscriber_limit"] = subscriber_cap(workspace_id)
    return out


def save_settings(p: Principal, *, slug: str, name: str, description: str = "", from_name: str = "",
                  reply_to: str | None = None, postal_address: str = "", public_archive: bool = True) -> dict[str, Any]:
    slug, name = slug.strip().lower(), name.strip()
    if not SLUG_RE.match(slug):
        raise NewsletterError("the address must be 3-40 lowercase letters, digits or hyphens")
    if not name:
        raise NewsletterError("give the newsletter a name")
    reply_to = (reply_to or "").strip() or None
    if reply_to and not EMAIL_RE.match(reply_to):
        raise NewsletterError("reply-to must be an email address")
    with session_scope() as s:
        taken = s.execute(select(Newsletter).where(Newsletter.slug == slug)).scalar_one_or_none()
        n = s.execute(select(Newsletter).where(Newsletter.workspace_id == p.workspace_id)).scalar_one_or_none()
        if taken is not None and (n is None or taken.newsletter_id != n.newsletter_id):
            raise NewsletterError("that address is taken")
        created = n is None
        if n is None:
            n = Newsletter(workspace_id=p.workspace_id, slug=slug, name=name)
            s.add(n)
        n.slug, n.name, n.description = slug, name[:120], description.strip()[:2000]
        n.from_name, n.reply_to = from_name.strip()[:120], reply_to
        n.postal_address, n.public_archive = postal_address.strip()[:300], public_archive
        s.flush()
        newsletter_id = n.newsletter_id
    audit("newsletter.created" if created else "newsletter.updated", p.user_id, p.workspace_id, newsletter_id=newsletter_id, slug=slug)
    out = get_settings(p.workspace_id)
    assert out is not None
    return out


def _mine(s, workspace_id: str) -> Newsletter:
    n = s.execute(select(Newsletter).where(Newsletter.workspace_id == workspace_id)).scalar_one_or_none()
    if n is None:
        raise KeyError("newsletter")
    return n


# ---------------------------------------------------------------- subscribers


def _send_confirmation(name: str, email: str, token: str) -> None:
    from atlas import mailer

    link = api_url(f"/newsletter/confirm?token={token}")
    try:
        mailer.send(email, f"Confirm your subscription to {name}",
                    f"Someone (hopefully you) asked to receive {name} at this address.\n\nConfirm: {link}\n\n"
                    "If this wasn't you, ignore this email and you won't hear from us. The link is valid for 7 days.")
    except Exception:
        log.exception("could not send newsletter confirmation")


def subscribe(slug: str, email: str, *, source: str = "form") -> None:
    """Start a double opt-in. Says nothing about whether the address is already on the list."""
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise NewsletterError("enter a valid email address")
    with session_scope() as s:
        n = s.execute(select(Newsletter).where(Newsletter.slug == slug)).scalar_one_or_none()
        ws = s.get(Workspace, n.workspace_id) if n else None
        if n is None or ws is None or ws.suspended_at is not None:
            raise KeyError("newsletter")
        sub = s.execute(select(NewsletterSubscriber).where(NewsletterSubscriber.newsletter_id == n.newsletter_id,
                                                           NewsletterSubscriber.email == email)).scalar_one_or_none()
        if sub is not None and sub.status == "active":
            return
        if sub is not None and sub.confirm_sent_at and utcnow() - sub.confirm_sent_at < RESEND_AFTER:
            return  # don't let the form be used to flood someone's inbox
        listed = s.execute(select(func.count()).select_from(NewsletterSubscriber).where(
            NewsletterSubscriber.newsletter_id == n.newsletter_id, NewsletterSubscriber.status != "unsubscribed")).scalar() or 0
        workspace_id, name = n.workspace_id, n.name
        # A pending address is already counted; a new or returning one needs a free place.
        cap_hit = (sub is None or sub.status == "unsubscribed") and listed >= subscriber_cap(workspace_id)
        if not cap_hit:
            if sub is None:
                sub = NewsletterSubscriber(newsletter_id=n.newsletter_id, email=email, source=source)
                s.add(sub)
            token = secrets.token_urlsafe(32)
            sub.status, sub.confirm_token_hash, sub.confirm_sent_at, sub.unsubscribed_at = "pending", _hash(token), utcnow(), None
    if cap_hit:
        audit("newsletter.limit_reached", None, workspace_id, slug=slug)
        return
    _send_confirmation(name, email, token)


def invite(p: Principal, email: str) -> None:
    with session_scope() as s:
        slug = _mine(s, p.workspace_id).slug
    subscribe(slug, email, source="invited")
    audit("newsletter.invited", p.user_id, p.workspace_id, email=email.strip().lower())


def confirm(token: str) -> str | None:
    """Confirm a subscription; returns the newsletter's name, or None for a bad or expired link."""
    with session_scope() as s:
        sub = s.execute(select(NewsletterSubscriber).where(NewsletterSubscriber.confirm_token_hash == _hash(token))).scalar_one_or_none()
        if sub is None or sub.confirm_sent_at is None or utcnow() - sub.confirm_sent_at > CONFIRM_TTL:
            return None
        sub.status, sub.confirmed_at, sub.confirm_token_hash = "active", utcnow(), None
        n = s.get(Newsletter, sub.newsletter_id)
        return n.name if n else None


def unsubscribe_link(subscriber_id: str) -> str:
    return api_url(f"/newsletter/unsubscribe?s={subscriber_id}&sig={secretbox.sign('newsletter|' + subscriber_id)}")


def unsubscribe(subscriber_id: str, signature: str) -> str | None:
    if not secretbox.verify_signature("newsletter|" + subscriber_id, signature):
        return None
    with session_scope() as s:
        sub = s.get(NewsletterSubscriber, subscriber_id)
        if sub is None:
            return None
        if sub.status != "unsubscribed":
            sub.status, sub.unsubscribed_at, sub.confirm_token_hash = "unsubscribed", utcnow(), None
        n = s.get(Newsletter, sub.newsletter_id)
        return n.name if n else ""


def list_subscribers(workspace_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        n = _mine(s, workspace_id)
        rows = s.execute(select(NewsletterSubscriber).where(NewsletterSubscriber.newsletter_id == n.newsletter_id)
                         .order_by(NewsletterSubscriber.created_at.desc())).scalars()
        return [{"subscriber_id": r.subscriber_id, "email": r.email, "status": r.status, "source": r.source,
                 "created_at": _iso(r.created_at), "confirmed_at": _iso(r.confirmed_at), "unsubscribed_at": _iso(r.unsubscribed_at)}
                for r in rows]


def subscribers_csv(workspace_id: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "status", "source", "subscribed_at", "confirmed_at", "unsubscribed_at"])
    for r in list_subscribers(workspace_id):
        w.writerow([r["email"], r["status"], r["source"], r["created_at"], r["confirmed_at"] or "", r["unsubscribed_at"] or ""])
    return buf.getvalue()


def remove_subscriber(p: Principal, subscriber_id: str) -> None:
    """Erase a subscriber entirely (e.g. a deletion request)."""
    with session_scope() as s:
        n = _mine(s, p.workspace_id)
        sub = s.get(NewsletterSubscriber, subscriber_id)
        if sub is None or sub.newsletter_id != n.newsletter_id:
            raise KeyError(subscriber_id)
        s.delete(sub)
    audit("newsletter.subscriber_removed", p.user_id, p.workspace_id)


# ---------------------------------------------------------------- issues


def _issue_dict(i: NewsletterIssue, with_body: bool = False) -> dict[str, Any]:
    out = {"issue_id": i.issue_id, "run_id": i.run_id, "subject": i.subject, "intro": i.intro, "status": i.status,
           "created_at": _iso(i.created_at), "sent_at": _iso(i.sent_at) if i.status == "sent" else None, "recipients": i.recipients}
    if with_body:
        out["body_markdown"] = i.body_markdown
    return out


def _snapshot(result: dict[str, Any]) -> str:
    narrative = result.get("narrative_markdown", "").strip()
    lines = narrative.splitlines()
    if lines and lines[0].startswith("# "):  # the subject replaces the report title
        narrative = "\n".join(lines[1:]).strip()
    sources = result.get("sources") or []
    if sources:
        narrative += "\n\n**Sources:** " + "; ".join(
            f"{src.get('name') or src.get('dataset_id')} ({src.get('provider', 'n/a')})" for src in sources[:12])
    return narrative


def create_issue(p: Principal, run_id: str, subject: str | None = None, intro: str = "") -> dict[str, Any]:
    with session_scope() as s:
        n = _mine(s, p.workspace_id)
        run = s.get(ResearchRun, run_id)
        if run is None or run.workspace_id != p.workspace_id or run.status == "deleted":
            raise NewsletterError("research run not found")
        if run.status != "complete" or not run.result:
            raise NewsletterError("only completed research can become an issue")
        result = dict(run.result)
        i = NewsletterIssue(newsletter_id=n.newsletter_id, run_id=run_id, subject=(subject or result.get("title") or run.query)[:200].strip(),
                            intro=intro.strip()[:5000], body_markdown=_snapshot(result), created_by=p.user_id)
        s.add(i)
        s.flush()
        out = _issue_dict(i, True)
    audit("newsletter.issue_created", p.user_id, p.workspace_id, issue_id=out["issue_id"], run_id=run_id)
    return out


def _issue(s, workspace_id: str, issue_id: str) -> tuple[Newsletter, NewsletterIssue]:
    n = _mine(s, workspace_id)
    i = s.get(NewsletterIssue, issue_id)
    if i is None or i.newsletter_id != n.newsletter_id:
        raise KeyError(issue_id)
    return n, i


def update_issue(p: Principal, issue_id: str, *, subject: str | None = None, intro: str | None = None) -> dict[str, Any]:
    with session_scope() as s:
        _, i = _issue(s, p.workspace_id, issue_id)
        if i.status != "draft":
            raise NewsletterError("a sent issue cannot be edited")
        if subject is not None:
            if not subject.strip():
                raise NewsletterError("the subject cannot be empty")
            i.subject = subject.strip()[:200]
        if intro is not None:
            i.intro = intro.strip()[:5000]
        return _issue_dict(i, True)


def delete_issue(p: Principal, issue_id: str) -> None:
    with session_scope() as s:
        _, i = _issue(s, p.workspace_id, issue_id)
        if i.status != "draft":
            raise NewsletterError("only drafts can be deleted")
        s.delete(i)


def list_issues(workspace_id: str) -> list[dict[str, Any]]:
    with session_scope() as s:
        n = _mine(s, workspace_id)
        rows = s.execute(select(NewsletterIssue).where(NewsletterIssue.newsletter_id == n.newsletter_id)
                         .order_by(NewsletterIssue.created_at.desc())).scalars()
        return [_issue_dict(i) for i in rows]


def _content(n: Newsletter, i: NewsletterIssue) -> str:
    return (f"{i.intro}\n\n---\n\n" if i.intro else "") + i.body_markdown + f"\n\n*{DISCLAIMER}*"


def get_issue(workspace_id: str, issue_id: str) -> dict[str, Any]:
    with session_scope() as s:
        n, i = _issue(s, workspace_id, issue_id)
        return {**_issue_dict(i, True), "html": safe_html(_content(n, i))}


def render_email(n: Newsletter, i: NewsletterIssue, unsubscribe: str) -> tuple[str, str]:
    content = _content(n, i)
    archive = web_url(f"/n/{n.slug}/{i.issue_id}") if n.public_archive else None
    footer = (f"You receive {n.name} because you subscribed and confirmed your address."
              f"\nUnsubscribe: {unsubscribe}\n{n.postal_address}")
    text = f"{i.subject}\n\n{content}\n\n" + (f"Read online: {archive}\n\n" if archive else "") + "--\n" + footer
    e = _html.escape
    html = (f"<!doctype html><html><body style=\"margin:0;padding:24px;background:#f6f7f9;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d2330\">"
            f"<div style=\"max-width:640px;margin:0 auto;background:#fff;border-radius:8px;padding:28px;line-height:1.55;font-size:15px\">"
            f"<div style=\"font-size:12px;color:#667;text-transform:uppercase;letter-spacing:.04em\">{e(n.name)}</div>"
            f"<h1 style=\"font-size:22px;margin:6px 0 18px\">{e(i.subject)}</h1>{safe_html(content)}"
            + (f"<p><a href=\"{e(archive)}\">Read online</a></p>" if archive else "")
            + f"</div><p style=\"max-width:640px;margin:16px auto 0;font-size:12px;color:#667;text-align:center\">"
            f"You receive {e(n.name)} because you subscribed and confirmed your address. "
            f"<a href=\"{e(unsubscribe)}\" style=\"color:#667\">Unsubscribe</a><br>{e(n.postal_address)}</p></body></html>")
    return text, html


def _send_one(n: Newsletter, i: NewsletterIssue, to: str, subscriber_id: str | None) -> bool:
    from atlas import mailer

    unsub = unsubscribe_link(subscriber_id) if subscriber_id else web_url(f"/n/{n.slug}")
    text, html = render_email(n, i, unsub)
    headers = {"List-Unsubscribe": f"<{unsub}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"} if subscriber_id else {}
    if n.reply_to:
        headers["Reply-To"] = n.reply_to
    return mailer.send(to, i.subject, text, html=html, headers=headers, from_name=n.from_name or n.name)


def send_test(p: Principal, issue_id: str) -> dict[str, Any]:
    with session_scope() as s:
        n, i = _issue(s, p.workspace_id, issue_id)
        user = s.get(User, p.user_id)
        if user is None:
            raise NewsletterError("single-user mode has no email address")
        s.expunge(n)
        s.expunge(i)
        to = user.email
    sent = _send_one(n, i, to, None)
    return {"to": to, "sent": sent}


def send_issue(p: Principal, issue_id: str) -> dict[str, Any]:
    with session_scope() as s:
        n, i = _issue(s, p.workspace_id, issue_id)
        if i.status != "draft":
            raise NewsletterError("this issue was already sent")
        if not n.postal_address.strip():
            raise NewsletterError("add your postal address in the newsletter settings first; anti-spam laws require it in every email")
        active = s.execute(select(func.count()).select_from(NewsletterSubscriber).where(
            NewsletterSubscriber.newsletter_id == n.newsletter_id, NewsletterSubscriber.status == "active")).scalar() or 0
        if not active:
            raise NewsletterError("there are no confirmed subscribers yet")
        i.status, i.sent_at = "sending", utcnow()
    audit("newsletter.issue_sent", p.user_id, p.workspace_id, issue_id=issue_id, subscribers=active)
    if BACKGROUND:
        threading.Thread(target=deliver, args=(issue_id,), daemon=True).start()
    else:
        deliver(issue_id)
    return {"issue_id": issue_id, "status": "sending", "subscribers": active}


def deliver(issue_id: str) -> int:
    """Send an issue to every active subscriber who hasn't had it; safe to resume after a crash."""
    with session_scope() as s:
        i = s.get(NewsletterIssue, issue_id)
        if i is None or i.status != "sending":
            return 0
        n = s.get(Newsletter, i.newsletter_id)
        assert n is not None
        s.expunge(n)
        s.expunge(i)
    sent = 0
    while True:
        with session_scope() as s:
            batch = s.execute(select(NewsletterSubscriber.subscriber_id, NewsletterSubscriber.email).where(
                NewsletterSubscriber.newsletter_id == n.newsletter_id, NewsletterSubscriber.status == "active",
                (NewsletterSubscriber.last_issue_id.is_(None)) | (NewsletterSubscriber.last_issue_id != issue_id)).limit(100)).all()
        if not batch:
            break
        for sid, email in batch:
            try:
                _send_one(n, i, email, sid)
                sent += 1
            except Exception:
                log.exception("newsletter issue %s to subscriber %s failed", issue_id, sid)
            with session_scope() as s:  # marked even on failure so one bad address cannot stall the send
                sub = s.get(NewsletterSubscriber, sid)
                if sub is not None:
                    sub.last_issue_id = issue_id
                row = s.get(NewsletterIssue, issue_id)
                if row is not None:
                    row.recipients += 1
                    row.sent_at = utcnow()  # heartbeat while sending
    with session_scope() as s:
        row = s.get(NewsletterIssue, issue_id)
        if row is not None:
            row.status, row.sent_at = "sent", utcnow()
    return sent


def resume_stalled(now=None) -> int:
    """Finish sends interrupted by a restart (run by the scheduler)."""
    now = now or utcnow()
    with session_scope() as s:
        ids = list(s.execute(select(NewsletterIssue.issue_id).where(
            NewsletterIssue.status == "sending", NewsletterIssue.sent_at < now - STALLED)).scalars())
    for issue_id in ids:
        deliver(issue_id)
    return len(ids)


# ---------------------------------------------------------------- public pages


def _public(s, slug: str) -> Newsletter | None:
    n = s.execute(select(Newsletter).where(Newsletter.slug == slug)).scalar_one_or_none()
    ws = s.get(Workspace, n.workspace_id) if n else None
    return None if n is None or ws is None or ws.suspended_at is not None else n


def public_info(slug: str) -> dict[str, Any] | None:
    with session_scope() as s:
        n = _public(s, slug)
        if n is None:
            return None
        issues = []
        if n.public_archive:
            issues = [{"issue_id": i.issue_id, "subject": i.subject, "sent_at": _iso(i.sent_at)} for i in s.execute(
                select(NewsletterIssue).where(NewsletterIssue.newsletter_id == n.newsletter_id, NewsletterIssue.status == "sent")
                .order_by(NewsletterIssue.sent_at.desc()).limit(100)).scalars()]
        return {"slug": n.slug, "name": n.name, "description": n.description, "issues": issues, "public_archive": n.public_archive}


def public_issue(slug: str, issue_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        n = _public(s, slug)
        if n is None or not n.public_archive:
            return None
        i = s.get(NewsletterIssue, issue_id)
        if i is None or i.newsletter_id != n.newsletter_id or i.status != "sent":
            return None
        return {"newsletter": n.name, "slug": n.slug, "subject": i.subject, "sent_at": _iso(i.sent_at), "html": safe_html(_content(n, i))}


def purge_workspace(s, workspace_id: str) -> None:
    ids = list(s.execute(select(Newsletter.newsletter_id).where(Newsletter.workspace_id == workspace_id)).scalars())
    if ids:
        s.execute(delete(NewsletterSubscriber).where(NewsletterSubscriber.newsletter_id.in_(ids)))
        s.execute(delete(NewsletterIssue).where(NewsletterIssue.newsletter_id.in_(ids)))
        s.execute(delete(Newsletter).where(Newsletter.newsletter_id.in_(ids)))
