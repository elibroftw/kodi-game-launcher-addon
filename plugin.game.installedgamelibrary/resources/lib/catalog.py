"""Cached Kodi tiles. Launch commands are never saved in the catalog."""

from __future__ import annotations

import errno
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass

from . import hero_catalog, online_art, windows
from .storage import write_json

FILENAME = 'game_catalog.json'
SCHEMA_VERSION = 7


@dataclass(frozen=True)
class GameTile:
    key: str
    title: str
    launcher: str
    image: str | None = None
    is_poster: bool = False
    fanart: str | None = None


@dataclass(frozen=True)
class Catalog:
    games: tuple[GameTile, ...]
    tool_icons: dict[str, str]


def load(profile):
    """Return None for a cache miss, distinct from a cached empty library."""
    try:
        data = json.loads((profile / FILENAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('version') != SCHEMA_VERSION:
        return None
    if data.get('hero_revision') != hero_catalog.revision():
        return None
    games = data.get('games')
    icons = data.get('tool_icons')
    if not isinstance(games, list) or not isinstance(icons, dict):
        return None
    tiles = []
    for entry in games:
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(key), str) and entry[key] for key in ('key', 'title', 'launcher')
        ):
            return None
        image = entry.get('image')
        is_poster = entry.get('is_poster', False)
        fanart = entry.get('fanart')
        if not isinstance(is_poster, bool) or any(
            path is not None and not isinstance(path, str) for path in (image, fanart)
        ):
            return None
        tiles.append(
            GameTile(entry['key'], entry['title'], entry['launcher'], image, is_poster, fanart)
        )
    return Catalog(
        tuple(tiles), {key: icon for key, icon in icons.items() if isinstance(icon, str) and icon}
    )


@contextmanager
def index_lock(profile):
    """Prevent overlapping Kodi invocations from indexing together on Windows."""
    import msvcrt

    profile.mkdir(parents=True, exist_ok=True)
    with (profile / 'game_catalog.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            yield False
        else:
            # Closing the handle releases the byte-range lock, even on failure.
            yield True


def rebuild(profile, *, force=False):
    """Index in the current invocation and publish only a complete catalog."""
    with index_lock(profile) as acquired:
        if not acquired or (not force and load(profile) is not None):
            return False
        hero_revision = hero_catalog.revision()
        games = online_art.enrich_games(windows.discover(), profile / 'artwork' / 'steam')
        cache = profile / 'icons'
        images = windows.artwork(games, cache) if games else {}
        tiles = []
        for game in games:
            kind, image = images.get(game.key, (None, None))
            tiles.append(
                GameTile(
                    game.key,
                    game.title,
                    game.launcher,
                    image,
                    kind == 'poster',
                    str(game.fanart) if game.fanart else None,
                )
            )
        snapshot = Catalog(tuple(tiles), windows.xbox_artwork(cache))
        write_json(
            profile / FILENAME,
            dict(version=SCHEMA_VERSION, hero_revision=hero_revision, **asdict(snapshot)),
        )
    return True
