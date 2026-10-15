"""Migration registry (migrations.jsonl): one JSON line per migrated project."""

import json
import os

from . import Abort
from .paths import same_path, write_text_atomic


class Registry:
    """Reads and updates migrations.jsonl inside `state_dir`."""

    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "migrations.jsonl")

    def all(self):
        """All migrations, from oldest to newest."""
        if not os.path.isfile(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def save(self, record):
        """Adds a migration at the end, or replaces it in place (by `id`): the order is chronological."""
        records = self.all()
        ids = [r["id"] for r in records]
        if record["id"] in ids:
            records[ids.index(record["id"])] = record
        else:
            records.append(record)
        write_text_atomic(self.path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

    def find(self, path):
        """The most recent migration whose source, destination or archive is `path`, preferring those not undone
        (after undoing a rename, `undo` must reach the migration before it)."""
        matches = [r for r in self.all()
                   if any(r.get(k) and same_path(r[k], path) for k in ("dst", "src", "archive"))]
        if not matches:
            raise Abort(f"No migration is registered for {path}")
        active = [r for r in matches if r["phase1"] != "undone"]
        return (active or matches)[-1]

    def previous_move(self, record):
        """The completed migration whose destination is this one's source (the move before it), or None.
        Uses the `previous` field (cc-move 0.4+) or, for older records, compares the paths."""
        records = self.all()
        if record.get("previous"):
            return next((r for r in records if r["id"] == record["previous"]), None)
        ids = [r["id"] for r in records]
        earlier = records[:ids.index(record["id"])] if record.get("id") in ids else records
        matches = [r for r in earlier if r["phase1"].startswith("ok") and same_path(r["dst"], record["src"])]
        return matches[-1] if matches else None

    def next_move(self, record):
        """The completed migration that later started from this one's destination (the move after it), or None."""
        records = self.all()
        ids = [r["id"] for r in records]
        later = records[ids.index(record["id"]) + 1:] if record.get("id") in ids else []
        return next((r for r in later if r["phase1"].startswith("ok") and same_path(r["src"], record["dst"])), None)

    def chain(self, record):
        """The earlier migrations that led to this one's source, oldest first ([] if it is the first move)."""
        chain, current = [], self.previous_move(record)
        while current:
            chain.insert(0, current)
            current = self.previous_move(current)
        return chain


def source_is_copy(chain):
    """True if a migration's source was itself made by an earlier copy (not only by a rename in place):
    its archive is then just an intermediate copy, and the true original is archived further back."""
    return any(r.get("kind") != "case-rename" for r in chain)
