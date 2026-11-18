"""JSONL-backed note store for devpulse.

Notes are appended to ``<data dir>/notes.jsonl``, one JSON object per line::

    {"ts": "2026-09-14T09:15:00Z", "tag": "fix", "text": "..."}

The data dir defaults to ``.devpulse`` and can be overridden with the
``DEVPULSE_DATA_DIR`` environment variable.
"""
import json
import os
from datetime import datetime, timedelta, timezone

DATA_DIR_ENV = 'DEVPULSE_DATA_DIR'
DEFAULT_DATA_DIR = '.devpulse'
NOTES_FILE = 'notes.jsonl'


def data_dir():
    """Notes directory: $DEVPULSE_DATA_DIR when set, else .devpulse/."""
    return os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR


def notes_path():
    return os.path.join(data_dir(), NOTES_FILE)


def parse_ts(value):
    """Parse an ISO-8601 timestamp (trailing Z allowed) to aware UTC."""
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_ts(dt):
    """Serialise a datetime back to the store's UTC 'Z' format."""
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class NoteStore:
    """Append-only store over a JSONL file."""

    def __init__(self, path=None):
        self.path = path or notes_path()

    def add(self, text, tag='note', ts=None):
        """Append a note and return it. `ts` is a datetime or None (now)."""
        note = {'ts': format_ts(ts or datetime.now(timezone.utc)),
                'tag': tag, 'text': text}
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(note) + '\n')
        return note

    def all(self):
        """Every note, oldest first. A missing file means no notes."""
        if not os.path.isfile(self.path):
            return []
        notes = []
        with open(self.path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    notes.append(json.loads(line))
        notes.sort(key=lambda n: n['ts'])
        return notes

    def latest_ts(self):
        """Timestamp of the newest note, or None when the store is empty."""
        notes = self.all()
        return parse_ts(notes[-1]['ts']) if notes else None

    def since(self, days, now=None):
        """Notes from the last `days` days relative to `now` (default: now)."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        return [n for n in self.all() if parse_ts(n['ts']) >= cutoff]

    def search(self, query):
        """Case-insensitive substring match over note text and tag."""
        q = query.lower()
        return [n for n in self.all()
                if q in n['text'].lower() or q in n['tag'].lower()]
