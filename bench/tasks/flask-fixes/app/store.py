"""In-memory note store.

Notes are dicts: {"id", "title", "body", "tags", "created"}.
All public methods return copies so callers can't mutate stored state.
"""
import threading
import time


class NoteStore:
    def __init__(self):
        self._notes = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def add(self, title, body="", tags=None):
        """Create a note and return it."""
        with self._lock:
            note = {
                "id": self._next_id,
                "title": title,
                "body": body,
                "tags": list(tags or []),
                "created": int(time.time()),
            }
            self._notes[note["id"]] = note
            self._next_id += 1
            return dict(note)

    def get(self, note_id):
        """One note by id, or None."""
        note = self._notes.get(note_id)
        return dict(note) if note is not None else None

    def list(self):
        """All notes, newest first."""
        notes = sorted(self._notes.values(),
                       key=lambda n: n["id"], reverse=True)
        return [dict(n) for n in notes]

    def update(self, note_id, title=None, body=None, tags=None):
        """Update fields of a note. Returns the note, or None if missing."""
        with self._lock:
            note = self._notes.get(note_id)
            if note is None:
                return None
            if title is not None:
                note["title"] = title
            if body is not None:
                note["body"] = body
            if tags is not None:
                note["tags"] = list(tags)
            return dict(note)

    def delete(self, note_id):
        """Delete a note. Returns True if it existed."""
        with self._lock:
            return self._notes.pop(note_id, None) is not None

    def search(self, query):
        """Notes whose title or body contains `query` (case-insensitive)."""
        return [dict(n) for n in self.list()
                if query in n["title"] or query in n["body"]]

    def clear(self):
        """Drop all notes. Used by tests."""
        with self._lock:
            self._notes.clear()
            self._next_id = 1
