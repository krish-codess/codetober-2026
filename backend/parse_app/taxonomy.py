"""Taxonomy: normalising source categories into paths, the in-memory tree the classifier uses,
and the change operations (add / rename / merge / split / move / retire).

Every change operation is one transaction and one taxonomy_versions row, keyed by an idempotency
key, and reports how many labels it remapped automatically vs flagged for targeted review.
No operation ever requires relabelling items outside the affected node.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property, wraps
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import Connection, text

TAXONOMY_FILE = Path(__file__).with_name("taxonomy_v1.json")
MIN_SUPPORT = 8  # nodes with fewer pool examples are folded into their parent

Bools = NDArray[np.bool_]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:63]


def canonical_path(domain: str, raw_category: str) -> tuple[str, ...] | None:
    """Three upstream spellings -> one path. Returns None for categories that are not aspects.

    'rooms comfort' / 'LAPTOP#GENERAL' / 'Seller Service#Attitude' -> (domain, entity, attribute)
    'Instructor' (single level upstream)                           -> (domain, entity)
    'polarity negative' (sentiment leaked into the category field) -> None
    """
    raw = raw_category.strip()
    if "#" in raw:
        entity, _, attribute = raw.partition("#")
    else:
        entity, _, attribute = raw.partition(" ")
    parts = [slug(domain), slug(entity)] + ([slug(attribute)] if attribute.strip() else [])
    if not all(parts) or parts[1] == "polarity":
        return None
    return tuple(parts)


def build_paths(
    leaf_counts: Counter[tuple[str, ...]], domains: Iterable[str], min_support: int = MIN_SUPPORT
) -> list[str]:
    """Keep a node only if it (including descendants) has >= min_support examples."""
    support: Counter[tuple[str, ...]] = Counter()
    for path, n in leaf_counts.items():
        for depth in range(1, len(path) + 1):
            support[path[:depth]] += n
    keep: set[tuple[str, ...]] = {(slug(d),) for d in domains}
    for p in sorted(support, key=len):  # parents before children: a child survives only if its parent did
        if len(p) > 1 and support[p] >= min_support and p[:-1] in keep:
            keep.add(p)
    return sorted("/".join(p) for p in keep)


def resolve(path: Sequence[str], known: set[str] | dict[str, Any]) -> str | None:
    """Longest known prefix: a folded or not-yet-existing leaf falls back to its parent."""
    for depth in range(len(path), 0, -1):
        candidate = "/".join(path[:depth])
        if candidate in known:
            return candidate
    return None


def title_of(name: str) -> str:
    return name.replace("_", " ").capitalize()


# --- in-memory tree -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Tree:
    """Nodes in topological order (every parent precedes its children). `parent[j] == -1` for roots."""

    node_ids: NDArray[np.int64]
    parent: NDArray[np.int64]

    @classmethod
    def from_edges(cls, edges: Iterable[tuple[int, int | None]]) -> Tree:
        """edges: (node_id, parent_id or None), any order."""
        parent_of = dict(edges)
        depth: dict[int, int] = {}

        def d(n: int) -> int:
            if n not in depth:
                p = parent_of[n]
                depth[n] = 1 if p is None else d(p) + 1
            return depth[n]

        ordered = sorted(parent_of, key=lambda n: (d(n), n))
        pos = {n: i for i, n in enumerate(ordered)}
        parents = [(-1 if parent_of[n] is None else pos[parent_of[n]]) for n in ordered]  # type: ignore[index]
        return cls(np.array(ordered, dtype=np.int64), np.array(parents, dtype=np.int64))

    def __len__(self) -> int:
        return len(self.node_ids)

    @cached_property
    def depth(self) -> NDArray[np.int64]:
        out = np.ones(len(self), dtype=np.int64)
        for j, p in enumerate(self.parent):
            if p >= 0:
                out[j] = out[p] + 1
        return out

    @cached_property
    def is_leaf(self) -> Bools:
        out = np.ones(len(self), dtype=bool)
        out[self.parent[self.parent >= 0]] = False
        return out

    @cached_property
    def index(self) -> dict[int, int]:
        return {int(n): i for i, n in enumerate(self.node_ids)}

    def close(self, y: Bools) -> Bools:
        """Ancestor closure: a set child implies its parent."""
        out = y.copy()
        for j in range(len(self) - 1, -1, -1):
            if self.parent[j] >= 0:
                out[:, self.parent[j]] |= out[:, j]
        return out

    def is_consistent(self, y: Bools) -> Bools:
        """Per row: no node set without its parent."""
        child = self.parent >= 0
        orphan = y[:, child] & ~y[:, self.parent[child]]
        return np.asarray(~orphan.any(axis=1), dtype=bool)

    def encode(self, node_sets: Sequence[Iterable[int]]) -> Bools:
        y = np.zeros((len(node_sets), len(self)), dtype=bool)
        for i, nodes in enumerate(node_sets):
            for n in nodes:
                if n in self.index:  # nodes retired since labelling are ignored
                    y[i, self.index[n]] = True
        return self.close(y)


def load_tree(conn: Connection) -> Tree:
    rows = conn.execute(text("SELECT id, parent_id FROM taxonomy_nodes WHERE retired_version IS NULL")).all()
    return Tree.from_edges((r.id, r.parent_id) for r in rows)


def current_version(conn: Connection) -> int:
    v = conn.execute(text("SELECT max(id) FROM taxonomy_versions")).scalar()
    if v is None:
        raise TaxonomyError("taxonomy is not initialised", status=503)
    return int(v)


def active_paths(conn: Connection) -> dict[str, int]:
    rows = conn.execute(text("SELECT path, id FROM taxonomy_nodes WHERE retired_version IS NULL")).all()
    return {r.path: r.id for r in rows}


# --- change operations --------------------------------------------------------------------------


class TaxonomyError(Exception):
    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


def atomic(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Run a change inside a savepoint: if it fails part-way, nothing of it survives - not even
    its version row - whatever the caller does with the exception."""

    @wraps(fn)
    def wrapper(conn: Connection, **kwargs: Any) -> dict[str, Any]:
        with conn.begin_nested():
            return fn(conn, **kwargs)

    return wrapper


def _begin(conn: Connection, op: str, params: dict[str, Any], key: str, actor: str) -> int | None:
    """Insert the version row. Returns None if this idempotency key was already applied."""
    conn.execute(text("LOCK TABLE taxonomy_nodes IN SHARE ROW EXCLUSIVE MODE"))  # one change at a time
    return conn.execute(
        text(
            """INSERT INTO taxonomy_versions (op, params, idempotency_key, actor)
               VALUES (:op, CAST(:params AS jsonb), :key, :actor)
               ON CONFLICT (idempotency_key) DO NOTHING RETURNING id"""
        ),
        {"op": op, "params": json.dumps(params, sort_keys=True), "key": key, "actor": actor},
    ).scalar()


def _result(conn: Connection, key: str, replayed: bool) -> dict[str, Any]:
    row = conn.execute(
        text(
            """SELECT id AS version, op, params, labels_remapped, labels_flagged
               FROM taxonomy_versions WHERE idempotency_key = :key"""
        ),
        {"key": key},
    ).one()
    return {**row._asdict(), "replayed": replayed}


def _node(conn: Connection, path: str) -> Any:
    row = conn.execute(
        text("SELECT id, parent_id, name, path FROM taxonomy_nodes WHERE path = :p AND retired_version IS NULL"),
        {"p": path},
    ).one_or_none()
    if row is None:
        raise TaxonomyError(f'no active node with path "{path}"', status=404)
    return row


def _insert_node(conn: Connection, parent_id: int | None, name: str, title: str, version: int) -> int:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,62}", name):
        raise TaxonomyError(f'invalid node name "{name}": use lowercase letters, digits and underscores')
    clash = conn.execute(
        text(
            """SELECT 1 FROM taxonomy_nodes WHERE retired_version IS NULL AND name = :n
               AND parent_id IS NOT DISTINCT FROM :p"""
        ),
        {"n": name, "p": parent_id},
    ).first()
    if clash:
        raise TaxonomyError(f'a sibling named "{name}" already exists', status=409)
    return int(
        conn.execute(
            text(
                """INSERT INTO taxonomy_nodes (parent_id, name, title, depth, path, created_version)
                   VALUES (:p, :n, :t, 1, '', :v) RETURNING id"""
            ),
            {"p": parent_id, "n": name, "t": title, "v": version},
        ).scalar_one()
    )


def _flag_most_specific(conn: Connection, node_id: int, reason: str) -> int:
    """Flag labels on `node_id` for items that have no label on any child of it: exactly the
    items whose answer could change. Everything else is untouched."""
    return conn.execute(
        text(
            """UPDATE labels l SET review_reason = :reason
               WHERE l.node_id = :n AND NOT EXISTS (
                   SELECT 1 FROM labels c JOIN taxonomy_nodes cn ON cn.id = c.node_id
                   WHERE c.feedback_id = l.feedback_id AND cn.parent_id = :n)"""
        ),
        {"n": node_id, "reason": reason},
    ).rowcount


def _finish(conn: Connection, version: int, remapped: int = 0, flagged: int = 0) -> None:
    conn.execute(
        text("UPDATE taxonomy_versions SET labels_remapped = :r, labels_flagged = :f WHERE id = :v"),
        {"r": remapped, "f": flagged, "v": version},
    )


def seed(conn: Connection, paths: Sequence[str], actor: str = "seed") -> dict[str, Any]:
    """Create version 1 from a list of paths. No-op if the taxonomy already exists."""
    if conn.execute(text("SELECT 1 FROM taxonomy_versions LIMIT 1")).first():
        return {"seeded": False}
    version = _begin(conn, "init", {"n_nodes": len(paths)}, "init", actor)
    assert version is not None
    ids: dict[str, int] = {}
    for path in sorted(paths, key=lambda p: (p.count("/"), p)):
        parent, _, name = path.rpartition("/")
        ids[path] = _insert_node(conn, ids[parent] if parent else None, name, title_of(name), version)
    return {"seeded": True, "n_nodes": len(ids)}


@atomic
def add(conn: Connection, *, parent: str | None, name: str, title: str, key: str, actor: str) -> dict[str, Any]:
    version = _begin(conn, "add", {"parent": parent, "name": name, "title": title}, key, actor)
    if version is None:
        return _result(conn, key, replayed=True)
    parent_id = _node(conn, parent).id if parent else None
    _insert_node(conn, parent_id, name, title, version)
    flagged = _flag_most_specific(conn, parent_id, f"new_child:{parent}/{name}") if parent_id else 0
    _finish(conn, version, flagged=flagged)
    return _result(conn, key, replayed=False)


@atomic
def split(conn: Connection, *, path: str, children: Sequence[dict[str, str]], key: str, actor: str) -> dict[str, Any]:
    """`path` becomes the parent of new, finer children. Its existing labels stay valid at the
    parent level; only items whose most specific label is `path` are queued for review."""
    if len(children) < 2:
        raise TaxonomyError("a split needs at least two children")
    version = _begin(conn, "split", {"path": path, "children": list(children)}, key, actor)
    if version is None:
        return _result(conn, key, replayed=True)
    node = _node(conn, path)
    for child in children:
        _insert_node(conn, node.id, child["name"], child.get("title") or title_of(child["name"]), version)
    _finish(conn, version, flagged=_flag_most_specific(conn, node.id, f"split:{path}"))
    return _result(conn, key, replayed=False)


@atomic
def rename(conn: Connection, *, path: str, name: str, title: str | None, key: str, actor: str) -> dict[str, Any]:
    version = _begin(conn, "rename", {"path": path, "name": name, "title": title}, key, actor)
    if version is None:
        return _result(conn, key, replayed=True)
    node = _node(conn, path)
    if name != node.name:
        clash = conn.execute(
            text(
                """SELECT 1 FROM taxonomy_nodes WHERE retired_version IS NULL AND name = :n AND id <> :id
                   AND parent_id IS NOT DISTINCT FROM :p"""
            ),
            {"n": name, "id": node.id, "p": node.parent_id},
        ).first()
        if clash:
            raise TaxonomyError(f'a sibling named "{name}" already exists', status=409)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,62}", name):
        raise TaxonomyError(f'invalid node name "{name}"')
    conn.execute(
        text("UPDATE taxonomy_nodes SET name = :n, title = COALESCE(:t, title) WHERE id = :id"),
        {"n": name, "t": title, "id": node.id},
    )
    _finish(conn, version)  # labels reference node ids: nothing to remap, nothing to review
    return _result(conn, key, replayed=False)


@atomic
def merge(conn: Connection, *, source: str, target: str, key: str, actor: str) -> dict[str, Any]:
    """All labels on `source` move to `target`; `source` (which must be a leaf) is retired."""
    version = _begin(conn, "merge", {"source": source, "target": target}, key, actor)
    if version is None:
        return _result(conn, key, replayed=True)
    src, dst = _node(conn, source), _node(conn, target)
    if src.id == dst.id or dst.path.startswith(src.path + "/"):
        raise TaxonomyError("cannot merge a node into itself or its own descendant")
    # Insert on target first (trigger closes its ancestors), then delete from source. Source's
    # old ancestors keep their labels: they were true before and merging does not falsify them
    # when source and target share them; otherwise the stale ancestor is flagged below.
    remapped = conn.execute(
        text(
            """INSERT INTO labels (feedback_id, node_id)
               SELECT feedback_id, :dst FROM labels WHERE node_id = :src ON CONFLICT DO NOTHING"""
        ),
        {"src": src.id, "dst": dst.id},
    ).rowcount
    flagged = _flag_stale_ancestors(conn, src.id, src.parent_id, f"merged:{source}->{target}")
    conn.execute(text("DELETE FROM labels WHERE node_id = :src"), {"src": src.id})
    _retire(conn, src.id, version)
    _finish(conn, version, remapped=remapped, flagged=flagged)
    return _result(conn, key, replayed=False)


@atomic
def move(conn: Connection, *, path: str, new_parent: str | None, key: str, actor: str) -> dict[str, Any]:
    version = _begin(conn, "move", {"path": path, "new_parent": new_parent}, key, actor)
    if version is None:
        return _result(conn, key, replayed=True)
    node = _node(conn, path)
    if new_parent and (new_parent == path or new_parent.startswith(path + "/")):
        raise TaxonomyError("cannot move a node under itself or its own descendant")
    new_parent_id = _node(conn, new_parent).id if new_parent else None
    clash = conn.execute(
        text(
            """SELECT 1 FROM taxonomy_nodes WHERE retired_version IS NULL AND name = :n AND id <> :id
               AND parent_id IS NOT DISTINCT FROM :p"""
        ),
        {"n": node.name, "id": node.id, "p": new_parent_id},
    ).first()
    if clash:
        raise TaxonomyError(f'"{new_parent}" already has a child named "{node.name}"', status=409)
    conn.execute(text("UPDATE taxonomy_nodes SET parent_id = :p WHERE id = :id"), {"p": new_parent_id, "id": node.id})
    remapped = 0
    if new_parent_id is not None:  # re-establish closure under the new ancestors
        remapped = conn.execute(
            text(
                """INSERT INTO labels (feedback_id, node_id)
                   SELECT feedback_id, :p FROM labels WHERE node_id = :n ON CONFLICT DO NOTHING"""
            ),
            {"p": new_parent_id, "n": node.id},
        ).rowcount
    flagged = _flag_stale_ancestors(conn, node.id, node.parent_id, f"moved:{path}")
    _finish(conn, version, remapped=remapped, flagged=flagged)
    return _result(conn, key, replayed=False)


@atomic
def retire(conn: Connection, *, path: str, key: str, actor: str) -> dict[str, Any]:
    """Retire a node and its subtree. Items keep their labels on the surviving ancestors."""
    version = _begin(conn, "retire", {"path": path}, key, actor)
    if version is None:
        return _result(conn, key, replayed=True)
    node = _node(conn, path)
    subtree = (
        conn.execute(
            text(
                """SELECT id FROM taxonomy_nodes WHERE retired_version IS NULL
               AND (id = :id OR starts_with(path, :prefix)) ORDER BY depth DESC"""
            ),
            {"id": node.id, "prefix": node.path + "/"},
        )
        .scalars()
        .all()
    )
    remapped = conn.execute(text("SELECT count(*) FROM labels WHERE node_id = :n"), {"n": node.id}).scalar_one()
    conn.execute(text("DELETE FROM labels WHERE node_id = :n"), {"n": node.id})  # trigger removes descendants
    for node_id in subtree:  # deepest first, as the trigger requires
        _retire(conn, node_id, version)
    _finish(conn, version, remapped=remapped)
    return _result(conn, key, replayed=False)


def _retire(conn: Connection, node_id: int, version: int) -> None:
    conn.execute(text("UPDATE taxonomy_nodes SET retired_version = :v WHERE id = :id"), {"v": version, "id": node_id})


def _flag_stale_ancestors(conn: Connection, moved_id: int, old_parent_id: int | None, reason: str) -> int:
    """After a merge/move, the old parent's label is only still justified if the item has another
    label under it. Otherwise flag it: it may or may not still apply, and only a person can say."""
    if old_parent_id is None:
        return 0
    return conn.execute(
        text(
            """UPDATE labels l SET review_reason = :reason
               WHERE l.node_id = :old_parent
                 AND EXISTS (SELECT 1 FROM labels m WHERE m.feedback_id = l.feedback_id AND m.node_id = :moved)
                 AND NOT EXISTS (
                     SELECT 1 FROM labels c JOIN taxonomy_nodes cn ON cn.id = c.node_id
                     WHERE c.feedback_id = l.feedback_id AND cn.parent_id = :old_parent
                       AND c.node_id <> :moved)"""
        ),
        {"reason": reason, "old_parent": old_parent_id, "moved": moved_id},
    ).rowcount
