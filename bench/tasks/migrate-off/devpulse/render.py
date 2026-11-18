"""Render devpulse notes as markdown digests and plain lists."""
from collections import OrderedDict

from devpulse.store import parse_ts

TAG_ORDER = ['blocker', 'feat', 'fix', 'chore', 'note']


def tag_key(tag):
    """Sort key: TAG_ORDER first, anything else alphabetical at the end."""
    return (TAG_ORDER.index(tag) if tag in TAG_ORDER else len(TAG_ORDER), tag)


def group_by_tag(notes):
    """Notes grouped by tag, ordered by TAG_ORDER then alphabetically."""
    groups = {}
    for note in notes:
        groups.setdefault(note['tag'], []).append(note)
    return OrderedDict(sorted(groups.items(), key=lambda kv: tag_key(kv[0])))


def render_note(note):
    """One markdown line: `- **tag** text (YYYY-MM-DD)`."""
    day = parse_ts(note['ts']).strftime('%Y-%m-%d')
    return '- **%s** %s _(%s)_' % (note['tag'], note['text'], day)


def render_digest(notes, days):
    """A markdown digest: header line, then one section per tag."""
    lines = ['# Digest — last %d day%s' % (days, '' if days == 1 else 's'), '']
    if not notes:
        lines.append('No notes in the window.')
        return '\n'.join(lines) + '\n'
    for tag, tagged in group_by_tag(notes).items():
        lines.append('## %s (%d)' % (tag, len(tagged)))
        lines.extend(render_note(n) for n in tagged)
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def render_list(notes):
    """Plain list of notes, one markdown line each."""
    if not notes:
        return 'No notes.\n'
    return '\n'.join(render_note(n) for n in notes) + '\n'
