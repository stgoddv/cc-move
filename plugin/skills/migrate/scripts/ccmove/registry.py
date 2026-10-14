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
