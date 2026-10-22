"""Workspace watchlist: the instruments a team follows, shown first on Overview."""

from __future__ import annotations

from sqlalchemy import delete, func, select

from atlas.data.db import WatchlistItem, session_scope
from atlas.instruments import get_dataset

MAX_ITEMS = 40


class WatchlistError(ValueError):
    pass


def _check(dataset_id: str) -> None:
    ds = get_dataset(dataset_id)
    if ds is None or ds.kind not in ("market", "macro") or not ds.has_source:
        raise WatchlistError(f"'{dataset_id}' cannot be watched (market and macro series with a data source only)")


def items(workspace_id: str) -> list[str]:
    with session_scope() as s:
        return list(s.execute(select(WatchlistItem.dataset_id).where(WatchlistItem.workspace_id == workspace_id)
                              .order_by(WatchlistItem.position, WatchlistItem.added_at)).scalars())


def add(workspace_id: str, dataset_id: str, user_id: str) -> list[str]:
    _check(dataset_id)
    with session_scope() as s:
        if s.get(WatchlistItem, (workspace_id, dataset_id)) is None:
            n = s.execute(select(func.count()).select_from(WatchlistItem).where(WatchlistItem.workspace_id == workspace_id)).scalar() or 0
            if n >= MAX_ITEMS:
                raise WatchlistError(f"a watchlist holds up to {MAX_ITEMS} instruments")
            s.add(WatchlistItem(workspace_id=workspace_id, dataset_id=dataset_id, position=n, added_by=user_id))
    return items(workspace_id)


def remove(workspace_id: str, dataset_id: str) -> list[str]:
    with session_scope() as s:
        s.execute(delete(WatchlistItem).where(WatchlistItem.workspace_id == workspace_id, WatchlistItem.dataset_id == dataset_id))
    return items(workspace_id)


def replace(workspace_id: str, dataset_ids: list[str], user_id: str) -> list[str]:
    """Set the whole list in order (used to reorder)."""
    ids = list(dict.fromkeys(dataset_ids))
    if len(ids) > MAX_ITEMS:
        raise WatchlistError(f"a watchlist holds up to {MAX_ITEMS} instruments")
    for d in ids:
        _check(d)
    with session_scope() as s:
        existing = {w.dataset_id: w for w in s.execute(select(WatchlistItem).where(WatchlistItem.workspace_id == workspace_id)).scalars()}
        for d, w in existing.items():
            if d not in ids:
                s.delete(w)
        for i, d in enumerate(ids):
            if d in existing:
                existing[d].position = i
            else:
                s.add(WatchlistItem(workspace_id=workspace_id, dataset_id=d, position=i, added_by=user_id))
    return items(workspace_id)
