"""Feedback on research answers: readers rate an answer and say what was wrong; operators review it.

Submitting shares the rating, reasons, comment, the question and the answer's title with the people
who run this ATLAS deployment (the page says so). The answer itself stays in the workspace.
Negative feedback emails the operators (ATLAS_OPERATOR_EMAILS) so pilots get quick answers.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from atlas.auth import Principal, audit, operator_emails
from atlas.data.db import ResearchFeedback, ResearchRun, User, Workspace, session_scope, utcnow

log = logging.getLogger(__name__)

REASONS = {
    "wrong_number": "A number is wrong",
    "wrong_source": "A source or citation is wrong",
    "outdated": "The data is out of date",
    "missed_question": "It didn't answer my question",
    "unclear": "Hard to follow",
    "other": "Something else",
}


class FeedbackError(ValueError):
    pass


def _iso(d) -> str | None:
    return d.isoformat(timespec="seconds") + "Z" if d else None


def _mine_dict(f: ResearchFeedback) -> dict[str, Any]:
    return {"feedback_id": f.feedback_id, "run_id": f.run_id, "rating": f.rating, "reasons": f.reasons, "comment": f.comment,
            "status": f.status, "operator_note": f.operator_note, "created_at": _iso(f.created_at), "updated_at": _iso(f.updated_at)}


def submit(p: Principal, run_id: str, rating: str, reasons: list[str] | None = None, comment: str = "") -> dict[str, Any]:
    """Create or replace this person's feedback on a run."""
    if rating not in ("up", "down"):
        raise FeedbackError("rating must be up or down")
    reasons = sorted(set(reasons or []))
    unknown = [r for r in reasons if r not in REASONS]
    if unknown:
        raise FeedbackError(f"unknown reasons: {', '.join(unknown)}")
    if rating == "up":
        reasons = []
    comment = comment.strip()[:2000]
    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        if run is None or run.workspace_id != p.workspace_id or run.status == "deleted":
            raise KeyError(run_id)
        if run.status not in ("complete", "failed"):
            raise FeedbackError("rate a run once it has finished")
        result = run.result or {}
        f = s.execute(select(ResearchFeedback).where(ResearchFeedback.run_id == run_id, ResearchFeedback.user_id == p.user_id)).scalar_one_or_none()
        was = f.rating if f else None
        if f is None:
            f = ResearchFeedback(run_id=run_id, workspace_id=p.workspace_id, user_id=p.user_id, rating=rating, question=run.query)
            s.add(f)
        f.rating, f.reasons, f.comment = rating, reasons, comment
        f.title, f.engine = (result.get("title") or None), run.engine
        f.verified = (result.get("verification") or {}).get("passed")
        f.updated_at = utcnow()
        if rating == "down":
            f.status, f.resolved_at = "open", None  # a new complaint reopens
        elif was is None:
            f.status = "resolved"  # praise needs no follow-up
        s.flush()
        out = _mine_dict(f)
        workspace = s.get(Workspace, p.workspace_id)
        ws_name = workspace.name if workspace else p.workspace_id
    audit("research.feedback", p.user_id, p.workspace_id, run_id=run_id, rating=rating)
    if rating == "down" and was != "down":
        _notify_operators(out, run_id, ws_name, p.email)
    return out


def _notify_operators(f: dict[str, Any], run_id: str, workspace: str, reader: str | None) -> None:
    import os

    from atlas import mailer

    to = sorted(operator_emails())
    if not to:
        return
    with session_scope() as s:
        row = s.get(ResearchFeedback, f["feedback_id"])
        question = row.question if row else ""
    reasons = ", ".join(REASONS[r] for r in f["reasons"]) or "no reason given"
    base = os.getenv("ATLAS_PUBLIC_URL", "http://localhost:3010").rstrip("/")
    body = (f"{reader or 'A reader'} in '{workspace}' marked an answer as wrong.\n\nQuestion: {question}\nReasons: {reasons}\n"
            + (f"Comment: {f['comment']}\n" if f["comment"] else "") + f"\nReview it in the operator console: {base}/admin\n")
    for email in to:
        try:
            mailer.send(email, "ATLAS feedback: an answer was marked wrong", body)
        except Exception:
            log.exception("could not email operator about feedback %s", f["feedback_id"])


def get_mine(p: Principal, run_id: str) -> dict[str, Any] | None:
    with session_scope() as s:
        f = s.execute(select(ResearchFeedback).where(ResearchFeedback.run_id == run_id, ResearchFeedback.user_id == p.user_id,
                                                     ResearchFeedback.workspace_id == p.workspace_id)).scalar_one_or_none()
        return _mine_dict(f) if f else None


def withdraw(p: Principal, run_id: str) -> bool:
    with session_scope() as s:
        f = s.execute(select(ResearchFeedback).where(ResearchFeedback.run_id == run_id, ResearchFeedback.user_id == p.user_id,
                                                     ResearchFeedback.workspace_id == p.workspace_id)).scalar_one_or_none()
        if f is None:
            return False
        s.delete(f)
    return True


# ---------------------------------------------------------------- operators


def list_for_operators(status: str | None = "open", limit: int = 200) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = select(ResearchFeedback, User.email, Workspace.name).join(User, User.user_id == ResearchFeedback.user_id, isouter=True) \
            .join(Workspace, Workspace.workspace_id == ResearchFeedback.workspace_id, isouter=True)
        if status:
            q = q.where(ResearchFeedback.status == status)
        rows = s.execute(q.order_by(ResearchFeedback.updated_at.desc()).limit(limit)).all()
        return [{**_mine_dict(f), "question": f.question, "title": f.title, "engine": f.engine, "verified": f.verified,
                 "reader": email, "workspace": ws_name, "workspace_id": f.workspace_id, "reason_labels": [REASONS[r] for r in f.reasons if r in REASONS]}
                for f, email, ws_name in rows]


def resolve(operator: Principal, feedback_id: str, status: str, note: str | None = None) -> dict[str, Any]:
    if status not in ("open", "resolved"):
        raise FeedbackError("status must be open or resolved")
    with session_scope() as s:
        f = s.get(ResearchFeedback, feedback_id)
        if f is None:
            raise KeyError(feedback_id)
        f.status, f.resolved_at = status, (utcnow() if status == "resolved" else None)
        if note is not None:
            f.operator_note = note.strip()[:2000] or None
        out = _mine_dict(f)
        ws = f.workspace_id
    audit("admin.feedback_" + status, operator.user_id, ws, feedback_id=feedback_id, operator=operator.email)
    return out


def summary(days: int = 30) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    with session_scope() as s:
        counts = dict(s.execute(select(ResearchFeedback.rating, func.count()).where(ResearchFeedback.updated_at >= since)
                                .group_by(ResearchFeedback.rating)).all())
        open_ = s.execute(select(func.count()).select_from(ResearchFeedback).where(ResearchFeedback.status == "open")).scalar() or 0
    up, down = counts.get("up", 0), counts.get("down", 0)
    return {"up_30d": up, "down_30d": down, "open": open_, "satisfaction_30d": round(up / (up + down), 3) if up + down else None}
