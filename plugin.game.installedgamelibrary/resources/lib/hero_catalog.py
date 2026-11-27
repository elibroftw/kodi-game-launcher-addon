"""Curated hero image URLs shipped with the add-on."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

CATALOG_PATH = Path(__file__).parents[1] / 'hero_images.json'


@dataclass(frozen=True)
class Hero:
    names: tuple[str, ...]
    url: str
    steam_app_id: str | None = None


def normalized_title(title):
    return ' '.join(unicodedata.normalize('NFKC', title).casefold().split())


def contents():
    try:
        return CATALOG_PATH.read_bytes()
    except OSError:
        return b''


def revision():
    return hashlib.sha256(contents()).hexdigest()


def is_hero_url(url):
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        filename = parts.path.rsplit('/', 1)[-1]
        return (
            parts.scheme == 'https'
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not parts.fragment
            and bool(filename)
        )
    except ValueError:
        return False


def load():
    try:
        data = json.loads(contents())
    except ValueError:
        return ()
    rows = data.get('games') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return ()
    heroes = []
    for row in rows:
        if not isinstance(row, dict) or not is_hero_url(row.get('hero_url')):
            continue
        title, aliases = row.get('title'), row.get('aliases', [])
        if (
            not isinstance(title, str)
            or not normalized_title(title)
            or not isinstance(aliases, list)
        ):
            continue
        app_id = row.get('steam_app_id')
        if app_id is not None and not (
            isinstance(app_id, str) and app_id.isascii() and app_id.isdecimal()
        ):
            continue
        names = tuple(normalized_title(name) for name in [title, *aliases] if isinstance(name, str))
        heroes.append(Hero(names, row['hero_url'], app_id))
    return tuple(heroes)


def match(game, heroes):
    if game.launcher == 'Steam':
        by_id = [hero for hero in heroes if hero.steam_app_id == game.app_id]
        if by_id:
            return by_id[0] if len(by_id) == 1 else None
        heroes = tuple(hero for hero in heroes if hero.steam_app_id is None)
    title = normalized_title(game.title)
    by_title = [hero for hero in heroes if title and title in hero.names]
    return by_title[0] if len(by_title) == 1 else None
