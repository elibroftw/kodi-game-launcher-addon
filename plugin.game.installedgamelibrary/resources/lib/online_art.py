"""Cache curated hero images and missing Steam artwork in the add-on profile."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from http.client import HTTPException
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from . import hero_catalog
from .discovery import is_file
from .hero_catalog import normalized_title
from .standalone import local_art
from .storage import write_bytes, write_json

SEARCH_URL = 'https://store.steampowered.com/api/storesearch/'
DETAILS_URL = 'https://store.steampowered.com/api/appdetails'
SCREENSHOT_HOSTS = frozenset(
    {
        'shared.akamai.steamstatic.com',
        'shared.cloudflare.steamstatic.com',
        'shared.fastly.steamstatic.com',
        'cdn.cloudflare.steamstatic.com',
        'cdn.akamai.steamstatic.com',
        'steamcdn-a.akamaihd.net',
    }
)
IMAGE_URL = 'https://cdn.cloudflare.steamstatic.com/steam/apps'
REQUEST_TIMEOUT = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def fetch(url, limit):
    request = Request(url, headers={'User-Agent': 'PC Game Library for Kodi'})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            data = response.read(limit + 1)
    except (OSError, HTTPException):
        return None
    return data if len(data) <= limit else None


def fetch_json(url):
    raw = fetch(url, 256 * 1024)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def valid_app_id(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 10
        and value.isascii()
        and value.isdecimal()
        and 0 < int(value) < 2**32
    )


def match_title(title, cache):
    expected = normalized_title(title)
    if not expected or len(title) > 200:
        return None
    try:
        digest = hashlib.sha256(expected.encode('utf-8')).hexdigest()
        query = urlencode({'term': title, 'l': 'english', 'cc': 'us'})
    except UnicodeError:
        return None
    path = cache / 'matches' / f'{digest}.json'
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cached = None
    if valid_app_id(cached):
        return cached

    items = fetch_json(f'{SEARCH_URL}?{query}').get('items')
    if not isinstance(items, list):
        return None
    matches = set()
    for item in items:
        if not isinstance(item, dict) or item.get('type') != 'app':
            continue
        name = item.get('name')
        app_id = str(item.get('id'))
        if isinstance(name, str) and normalized_title(name) == expected and valid_app_id(app_id):
            matches.add(app_id)
    if len(matches) != 1:
        return None
    app_id = matches.pop()
    try:
        write_json(path, app_id)
    except OSError:
        pass  # A failed artwork cache write must not block the library.
    return app_id


def image_suffix(data):
    if data and data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):
        return '.jpg'
    if data and data.startswith(b'\x89PNG\r\n\x1a\n') and data.endswith(b'\0\0\0\0IEND\xaeB`\x82'):
        return '.png'
    if (
        data
        and len(data) >= 20
        and data.startswith(b'RIFF')
        and data[8:12] == b'WEBP'
        and int.from_bytes(data[4:8], 'little') + 8 == len(data)
        and data[12:16] in (b'VP8 ', b'VP8L', b'VP8X')
    ):
        return '.webp'
    return None


def save_image(path, data):
    try:
        write_bytes(path, data)
    except OSError:
        return None
    return path


def save_jpeg(url, path):
    data = fetch(url, MAX_IMAGE_BYTES)
    return save_image(path, data) if image_suffix(data) == '.jpg' else None


def cached_image(cache, app_id, filename, *assets):
    path = cache / app_id / filename
    if is_file(path):
        return path
    for asset in assets:
        saved = save_jpeg(f'{IMAGE_URL}/{app_id}/{asset}', path)
        if saved:
            return saved
    return None


def is_screenshot_url(url, app_id):
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != 'https' or parts.netloc.lower() not in SCREENSHOT_HOSTS:
        return False
    prefix = f'/steam/apps/{app_id}/'
    name = parts.path.rsplit('/', 1)[-1]
    return (
        parts.path.startswith((prefix, f'/store_item_assets{prefix}'))
        and name.startswith('ss_')
        and name.lower().endswith('.jpg')
    )


def cached_screenshot(cache, app_id):
    path = cache / app_id / 'screenshot.jpg'
    if is_file(path):
        return path
    query = urlencode({'appids': app_id, 'filters': 'screenshots', 'l': 'english', 'cc': 'us'})
    app = fetch_json(f'{DETAILS_URL}?{query}').get(app_id)
    data = app.get('data') if isinstance(app, dict) and app.get('success') else None
    screenshots = data.get('screenshots') if isinstance(data, dict) else None
    if isinstance(screenshots, list):
        for entry in screenshots[:3]:
            url = entry.get('path_full') if isinstance(entry, dict) else None
            if is_screenshot_url(url, app_id) and save_jpeg(url, path):
                return path
    return None


def cached_curated_hero(url, cache):
    digest = hashlib.sha256(url.encode('utf-8')).hexdigest()
    for suffix in ('.jpg', '.png', '.webp'):
        path = cache / f'{digest}{suffix}'
        if is_file(path):
            return path
    data = fetch(url, MAX_IMAGE_BYTES)
    suffix = image_suffix(data)
    return save_image(cache / f'{digest}{suffix}', data) if suffix else None


def enrich_game(game, cache, heroes=()):
    override = (
        local_art(game.install_dir, 'landscape', 'fanart', 'background')
        if game.install_dir
        else None
    )
    if not override:
        curated = hero_catalog.match(game, heroes)
        if curated is not None:
            override = cached_curated_hero(curated.url, cache.parent / 'curated')
    if override:
        game = replace(game, fanart=override)
        if game.poster:
            return game
    if game.launcher == 'Steam':
        app_id = game.app_id if valid_app_id(game.app_id) else None
    else:
        app_id = match_title(game.title, cache)
    if not app_id:
        return game
    poster = game.poster or cached_image(cache, app_id, 'poster.jpg', 'library_600x900.jpg')
    fanart = (
        override
        or cached_screenshot(cache, app_id)
        or game.fanart
        or cached_image(cache, app_id, 'hero.jpg', 'library_hero_2x.jpg', 'library_hero.jpg')
        or game.fallback_fanart
        or cached_image(cache, app_id, 'landscape.jpg', 'capsule_616x353.jpg', 'header.jpg')
    )
    return replace(game, poster=poster, fanart=fanart)


def enrich_games(games, cache):
    if not games:
        return games
    enrich = partial(enrich_game, cache=cache, heroes=hero_catalog.load())
    with ThreadPoolExecutor(max_workers=4) as workers:
        return list(workers.map(enrich, games))
