"""Profile-local game launch history, newest successful handoff first."""

import json

from .storage import write_json


def read_history(path):
    try:
        entries = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    return list(dict.fromkeys(key for key in entries if isinstance(key, str) and key))


def order_games(games, path):
    ranks = {key: index for index, key in enumerate(read_history(path))}
    return sorted(
        games, key=lambda game: (ranks.get(game.key, len(ranks)), game.title.casefold(), game.key)
    )


def record_launch(path, key):
    """Move a game to the front; return whether the order changed."""
    entries = read_history(path)
    if entries and entries[0] == key:
        return False
    entries = [key] + [entry for entry in entries if entry != key]
    write_json(path, entries)
    return True
