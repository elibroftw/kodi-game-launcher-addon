"""Discover game launch entries without invoking any launcher."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit


@dataclass(frozen=True)
class Game:
    launcher: str
    app_id: str
    title: str
    launch: str
    poster: Path | None = None
    icon: Path | None = None
    executable: Path | None = None
    install_dir: Path | None = None
    working_dir: Path | None = None  # Set only for direct executable launches.
    fanart: Path | None = None
    fallback_fanart: Path | None = None

    @property
    def key(self):
        return f'{self.launcher}:{self.app_id.casefold()}'


STEAM_REDISTRIBUTABLES = '228980'
TOKEN = re.compile(r'\s+|//[^\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')


def read_vdf(path):
    """Read Valve KeyValues as used by Steam's manifests and library list."""
    root = {}
    stack = [root]
    key = None
    try:
        text = path.read_text(encoding='utf-8-sig')
        pos = 0
        for match in TOKEN.finditer(text):
            if match.start() != pos:
                return {}
            pos = match.end()
            token = match.group()
            if token.isspace() or token.startswith('//'):
                continue
            if token == '}':
                if key is not None or len(stack) == 1:
                    return {}
                stack.pop()
            elif token == '{':
                if key is None:
                    return {}
                child = {}
                stack[-1][key] = child
                stack.append(child)
                key = None
            else:
                if token.startswith('"'):
                    token = re.sub(r'\\([\\"])', r'\1', token[1:-1])
                if key is None:
                    key = token.casefold()
                else:
                    stack[-1][key] = token
                    key = None
        return root if pos == len(text) and len(stack) == 1 and key is None else {}
    except (OSError, UnicodeError):
        return {}


def is_file(path):
    try:
        return path.is_file()
    except (OSError, ValueError):
        return False


def is_directory(path):
    try:
        return path.is_dir()
    except (OSError, ValueError):
        return False


def glob_files(directory, pattern):
    try:
        return sorted(directory.glob(pattern))
    except OSError:
        return []


def inside(directory, relative):
    """Resolve a launcher-provided relative path without escaping its install root."""
    relative = Path(relative)
    if relative.anchor:
        return None
    try:
        base = directory.resolve()
        path = (base / relative).resolve()
        path.relative_to(base)
        return path
    except (OSError, RuntimeError, ValueError):
        return None


def steam_art(root, app_id, *stems):
    cache = root / 'appcache' / 'librarycache'
    for stem in stems:
        for ext in ('jpg', 'png', 'webp'):
            name = f'{stem}.{ext}'
            for path in (cache / f'{app_id}_{name}', cache / app_id / name):
                if is_file(path):
                    return path
            for path in glob_files(cache / app_id, f'*/{name}'):
                if is_file(path):
                    return path
    return None


def steam_games(root):
    if not root:
        return
    libraries = [root]
    config = read_vdf(root / 'steamapps' / 'libraryfolders.vdf').get('libraryfolders')
    if isinstance(config, dict):
        for key, entry in config.items():
            if not key.isdecimal():
                continue
            path = entry.get('path') if isinstance(entry, dict) else entry
            if isinstance(path, str) and Path(path).is_absolute():
                libraries.append(Path(path))
    for library in dict.fromkeys(libraries):
        apps = library / 'steamapps'
        for path in glob_files(apps, 'appmanifest_*.acf'):
            manifest = read_vdf(path).get('appstate')
            if not isinstance(manifest, dict):
                continue
            app_id = manifest.get('appid')
            title = manifest.get('name')
            install = manifest.get('installdir')
            flags = manifest.get('stateflags', '')
            if (
                not isinstance(app_id, str)
                or not re.fullmatch(r'[0-9]+', app_id)
                or app_id == STEAM_REDISTRIBUTABLES
                or path.stem.lower() != f'appmanifest_{app_id}'
                or not isinstance(title, str)
                or not title.strip()
                or not isinstance(install, str)
                or not install
                or not isinstance(flags, str)
                or not re.fullmatch(r'[0-9]{1,10}', flags)
                or not int(flags) & 4
            ):
                continue
            folder = inside(apps / 'common', install)
            if not folder or not is_directory(folder):
                continue
            hero = steam_art(root, app_id, 'library_hero_2x', 'library_hero')
            yield Game(
                'Steam',
                app_id,
                title,
                'steam://rungameid/' + app_id,
                poster=steam_art(root, app_id, 'library_600x900'),
                install_dir=folder,
                fanart=hero,
                fallback_fanart=(
                    None if hero else steam_art(root, app_id, 'library_header_2x', 'library_header')
                ),
            )


def epic_games(manifests):
    for path in glob_files(manifests, '*.item'):
        try:
            item = json.loads(path.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError):
            continue
        if not isinstance(item, dict) or item.get('bIsIncompleteInstall'):
            continue
        keys = (
            'CatalogNamespace',
            'CatalogItemId',
            'AppName',
            'DisplayName',
            'InstallLocation',
            'LaunchExecutable',
        )
        values = [item.get(key) for key in keys]
        if not all(isinstance(value, str) and value.strip() for value in values):
            continue
        namespace, catalog_id, app_id, title, location, executable = values
        if not all(
            re.fullmatch(r'[\w.-]+', value, re.ASCII) for value in (namespace, catalog_id, app_id)
        ):
            continue
        if not Path(location).is_absolute():
            continue
        exe = inside(Path(location), executable)
        if not exe or not is_file(exe):
            continue
        app_path = quote(f'{namespace}:{catalog_id}:{app_id}', safe='')
        launch = f'com.epicgames.launcher://apps/{app_path}?action=launch&silent=true'
        yield Game('Epic Games', app_id, title, launch, executable=exe, install_dir=Path(location))


def launcher_uri(url):
    if not url or re.search(r'[\x00-\x20\x7f"<>\\|#]', url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    endpoint = parts.netloc.casefold()
    path = unquote(parts.path)
    if parts.scheme == 'steam' and endpoint == 'rungameid' and re.fullmatch(r'/[0-9]+', path):
        app_id = path[1:]
        if not parts.query and app_id != STEAM_REDISTRIBUTABLES:
            return 'Steam', app_id
    if (
        parts.scheme == 'com.epicgames.launcher'
        and endpoint == 'apps'
        and re.fullmatch(r'/[\w.-]+(?::[\w.-]+:[\w.-]+)?', path, re.ASCII)
        and parse_qs(parts.query, keep_blank_values=True).get('action') == ['launch']
    ):
        return 'Epic Games', path[1:].split(':')[-1]
    if parts.scheme == 'goggalaxy' and endpoint == 'launch' and re.fullmatch(r'/[0-9]+/?', path):
        return 'GOG', path.strip('/')
    if (
        parts.scheme == 'uplay'
        and endpoint == 'launch'
        and re.fullmatch(r'/[0-9]+(?:/[0-9]+)?/?', path)
    ):
        return 'Ubisoft Connect', path.split('/')[1]
    if (
        parts.scheme in ('origin', 'link2ea', 'ea')
        and endpoint == 'launchgame'
        and re.fullmatch(r'/[\w.:-]+/?', path, re.ASCII)
    ):
        return 'EA', path.strip('/')
    return None


def shortcut_icon(value):
    if not isinstance(value, str):
        return None
    path = Path(os.path.expandvars(value.strip().strip('"')))
    # Don't open UNC paths supplied by shortcuts.
    if path.is_absolute() and not path.drive.startswith(('\\\\', '//')) and is_file(path):
        return path
    return None


def internet_shortcut(path):
    try:
        raw = path.read_bytes()
        if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
            text = raw.decode('utf-16')
        else:
            try:
                text = raw.decode('utf-8-sig')
            except UnicodeError:
                text = raw.decode('cp1252', errors='replace')
    except (OSError, UnicodeError):
        return None
    fields = {}
    in_section = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('['):
            in_section = line.casefold() == '[internetshortcut]'
        elif in_section and '=' in line:
            key, value = line.split('=', 1)
            fields[key.strip().casefold()] = value.strip()
    url = fields.get('url')
    identity = launcher_uri(url)
    if identity:
        return Game(*identity, path.stem, url, icon=shortcut_icon(fields.get('iconfile')))
    return None


def galaxy_shortcut(path, target, arguments, icon):
    if Path(target).name.casefold() not in ('galaxyclient.exe', 'galaxyclientservice.exe'):
        return None
    game_id = re.search(
        r'(?:^|\s)/gameId=(?:"([0-9]+)"|([0-9]+))(?=\s|$)', arguments, re.IGNORECASE
    )
    if not game_id or not re.search(r'(?:^|\s)/command=runGame(?=\s|$)', arguments, re.IGNORECASE):
        return None
    return Game(
        'GOG', game_id.group(1) or game_id.group(2), path.stem, str(path), icon=shortcut_icon(icon)
    )


def merge_games(games):
    """Keep the first launch entry for each key and fill in missing metadata."""
    merged = {}
    for game in games:
        prior = merged.get(game.key)
        if prior:
            merged[game.key] = replace(
                prior,
                poster=prior.poster or game.poster,
                icon=prior.icon or game.icon,
                executable=prior.executable or game.executable,
                install_dir=prior.install_dir or game.install_dir,
                fanart=prior.fanart or game.fanart,
                fallback_fanart=prior.fallback_fanart or game.fallback_fanart,
            )
        else:
            merged[game.key] = game
    return list(merged.values())
