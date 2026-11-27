"""Bundled hero overrides and title-to-Steam-ID mappings."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

CATALOG_PATH = Path(__file__).parents[1] / 'hero_images.json'
STEAM_TITLES_PATH = CATALOG_PATH.with_name('steam_titles.json')


@dataclass(frozen=True)
class Hero:
    names: tuple[str, ...]
    url: str | None
    steam_app_id: str | None = None
    alternate_steam_app_ids: tuple[str, ...] = ()


def normalized_title(title):
    return ' '.join(unicodedata.normalize('NFKC', title).casefold().split())


def contents(path):
    try:
        return path.read_bytes()
    except OSError:
        return b''


def revision():
    return hashlib.sha256(contents(CATALOG_PATH) + b'\0' + contents(STEAM_TITLES_PATH)).hexdigest()


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


def is_steam_id(value):
    return isinstance(value, str) and value.isascii() and value.isdecimal()


def load_entries(path, *, curated):
    try:
        data = json.loads(contents(path))
    except ValueError:
        return ()
    rows = data.get('games') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return ()
    heroes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get('hero_url') if curated else None
        if curated and not is_hero_url(url):
            continue
        title, aliases = row.get('title'), row.get('aliases', [])
        if (
            not isinstance(title, str)
            or not normalized_title(title)
            or not isinstance(aliases, list)
        ):
            continue
        app_id = row.get('steam_app_id')
        alternate_ids = row.get('alternate_steam_app_ids', [])
        if (app_id is not None or not curated) and not is_steam_id(app_id):
            continue
        if not isinstance(alternate_ids, list) or not all(map(is_steam_id, alternate_ids)):
            continue
        names = tuple(normalized_title(name) for name in [title, *aliases] if isinstance(name, str))
        heroes.append(Hero(names, url, app_id, tuple(alternate_ids)))
    return tuple(heroes)


def load():
    return load_entries(CATALOG_PATH, curated=True)


def load_steam_titles():
    return load_entries(STEAM_TITLES_PATH, curated=False)


def match(game, heroes):
    if game.launcher == 'Steam':
        by_id = [
            hero
            for hero in heroes
            if game.app_id == hero.steam_app_id or game.app_id in hero.alternate_steam_app_ids
        ]
        if by_id:
            return by_id[0] if len(by_id) == 1 else None
        heroes = tuple(
            hero
            for hero in heroes
            if hero.steam_app_id is None and not hero.alternate_steam_app_ids
        )
    title = normalized_title(game.title)
    by_title = [hero for hero in heroes if title and title in hero.names]
    return by_title[0] if len(by_title) == 1 else None
