"""Conservative discovery of directly launchable Windows game installs."""

import hashlib
import json
import os
import re
import stat
from pathlib import PureWindowsPath

from .discovery import Game

SKIP_DIRS = {
    'commonfiles',
    'windowsapps',
    'modifiablewindowsapps',
    'windowskits',
    'steam',
    'steamlibrary',
    'steamapps',
    'epicgames',
    'epicgameslauncher',
    'ubisoftgamelauncher',
    'eadesktop',
    'goggalaxy',
    'bin',
    'bin32',
    'bin64',
    'binaries',
    'engine',
    'content',
    'plugins',
    'redist',
    'commonredist',
    'support',
    'installers',
}
BIN_DIRS = (
    (),
    ('bin',),
    ('bin32',),
    ('bin64',),
    ('bin', 'x64'),
    ('bin', 'Win64'),
    ('Binaries', 'Win64'),
    ('Binaries', 'Win32'),
)
HELPER = re.compile(
    r'^(?:setup|install|unins|uninst|update|patcher|crash|unitycrash|vc(?:redist|runtime)|'
    r'dxsetup|prereq|config|repair|diagnostic|benchmark|ue[45]prereq|unrealeditor)|'
    r'(?:crashreporter|crashreportclient|crashhandler(?:32|64)?|dedicatedserver|uninstaller|'
    r'configtool|configurator|settings|benchmark|patcher)$'
)
SHIPPING = re.compile(r'.+-win(?:32|64)-shipping\.exe$', re.IGNORECASE)


def normalize(name):
    return ''.join(char for char in name.casefold() if char.isalnum())


def kind(path, test):
    try:
        status = path.lstat()
    except (OSError, ValueError):
        return False
    return test(status.st_mode) and not (
        getattr(status, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def directory(path):
    return kind(path, stat.S_ISDIR)


def file(path):
    return kind(path, stat.S_ISREG)


def children(path):
    try:
        return sorted(path.iterdir())
    except (OSError, ValueError):
        return []


def child_dirs(entries):
    return [
        path
        for path in entries
        if normalize(path.name) not in SKIP_DIRS
        and not path.name.startswith(('.', '$'))
        and directory(path)
    ]


def beneath(root, parts):
    """Walk only real directories, including intermediate components."""
    for part in parts:
        root = root / part
        if not directory(root):
            return None
    return root


def plausible(path):
    name = normalize(path.stem)
    return (
        path.suffix.casefold() == '.exe'
        and name not in {'launcher', 'launch', 'start'}
        and not HELPER.search(name)
        and file(path)
    )


def local_art(folder, *stems):
    for stem in stems:
        for ext in ('.jpg', '.jpeg', '.png', '.webp'):
            path = folder / (stem + ext)
            if file(path):
                return path
    return None


def gog_tasks(folder, entries):
    """Return the declared primary offline GOG tasks, including unusable ones."""
    for path in entries:
        name = re.fullmatch(r'goggame-([0-9]+)\.info', path.name, re.IGNORECASE)
        if not name or not file(path):
            continue
        try:
            info = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict) or str(info.get('gameId')) != name[1]:
            continue
        tasks = info.get('playTasks')
        if isinstance(tasks, list):
            for task in tasks:
                if isinstance(task, dict) and task.get('isPrimary') is True:
                    yield name[1], info.get('name') or folder.name, task


def relative_parts(value):
    if not isinstance(value, str) or any(char in value for char in '\x00:*?"<>|'):
        return None
    path = PureWindowsPath(value)
    if path.anchor or any(part.endswith(('.', ' ')) for part in path.parts):
        return None
    return path.parts


def gog_game(folder, primary):
    game_id, title, task = primary
    parts = relative_parts(task.get('path'))
    if not parts or task.get('type') != 'FileTask' or task.get('arguments'):
        return None
    parent = beneath(folder, parts[:-1])
    if not parent:
        return None
    exe = parent / parts[-1]
    working = task.get('workingDir')
    work_parts = relative_parts(working) if working else None
    cwd = (
        beneath(folder, work_parts) if work_parts is not None else (parent if not working else None)
    )
    if (
        not cwd
        or exe.suffix.casefold() != '.exe'
        or HELPER.search(normalize(exe.stem))
        or not file(exe)
    ):
        return None
    return Game(
        'GOG',
        game_id,
        title if isinstance(title, str) else folder.name,
        str(exe),
        poster=local_art(folder, 'poster', 'cover'),
        executable=exe,
        install_dir=folder,
        working_dir=cwd,
    )


def unity_player(exe):
    data = beneath(exe.parent, (exe.stem + '_Data',))
    if (
        not data
        or not file(exe.parent / 'UnityPlayer.dll')
        or not file(data / 'globalgamemanagers')
    ):
        return False, False
    plugin_dirs = [
        exe.parent,
        beneath(data, ('Plugins', 'x86_64')),
        beneath(data, ('Plugins', 'x86')),
    ]
    steamworks = any(
        file(path / dll)
        for path in plugin_dirs
        if path
        for dll in ('steam_api64.dll', 'steam_api.dll')
    )
    return True, steamworks


def unreal_players(project):
    paks = beneath(project, ('Content', 'Paks'))
    if not paks or not any(
        path.suffix.casefold() in ('.pak', '.utoc') and file(path) for path in children(paks)
    ):
        return
    for platform in ('Win64', 'Win32'):
        binaries = beneath(project, ('Binaries', platform))
        if binaries:
            yield from (
                exe for exe in children(binaries) if SHIPPING.fullmatch(exe.name) and plausible(exe)
            )


def excluded(path, managed):
    return any(parent in managed for parent in (path, *path.parents))


def inspect_install(folder, entries, allow_unmarked, managed):
    """Return (game, recognized_install), even if a game cannot be launched safely."""
    primary = list(gog_tasks(folder, entries))
    if primary:
        # Do not replace a declared launch task with a guess if unsupported or ambiguous.
        game = gog_game(folder, primary[0]) if len(primary) == 1 else None
        if game and excluded(game.executable, managed):
            game = None
        return game, True

    shipping = set()
    title_key = normalize(folder.name)
    bootstraps = [exe for exe in entries if normalize(exe.stem) == title_key and plausible(exe)]
    for project in (folder, *child_dirs(entries)):
        if (
            project == folder
            or allow_unmarked
            or bootstraps
            or normalize(project.name) == title_key
        ):
            shipping.update(exe for exe in unreal_players(project) if not excluded(exe, managed))
    ranked = dict.fromkeys(shipping, 100)
    if len(shipping) == 1:
        ranked.update(dict.fromkeys(bootstraps, 110))

    for relative in BIN_DIRS:
        binary_dir = beneath(folder, relative)
        if not binary_dir or excluded(binary_dir, managed):
            continue
        for exe in entries if not relative else children(binary_dir):
            if not plausible(exe):
                continue
            unity, steamworks = unity_player(exe)
            if allow_unmarked or steamworks:
                score = (80 if normalize(exe.stem) == title_key else 0) + (20 if unity else 1)
                ranked[exe] = max(ranked.get(exe, 0), score)

    if not ranked:
        return None, False
    best = max(ranked.values())
    winners = [exe for exe, score in ranked.items() if score == best]
    if len(winners) != 1:
        return None, True
    exe = winners[0]
    identity = hashlib.sha256(os.fsencode(os.path.normcase(str(folder)))).hexdigest()
    game = Game(
        'Standalone',
        identity,
        folder.name,
        str(exe),
        poster=local_art(folder, 'poster', 'cover'),
        executable=exe,
        install_dir=folder,
        working_dir=exe.parent,
    )
    return game, True


def canonical(path):
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return path


def standalone_games(games_root, program_roots, managed_dirs=()):
    managed = {canonical(path) for path in managed_dirs if path is not None}
    seen = set()
    sources = [(games_root, True)] + [(path, False) for path in program_roots]
    for root, dedicated in sources:
        if not root or not directory(root):
            continue
        root = canonical(root)
        if root in seen:
            continue
        seen.add(root)
        for folder in child_dirs(children(root)):
            if excluded(folder, managed):
                continue
            entries = children(folder)
            collection = not dedicated and normalize(folder.name) == 'games'
            game, is_install = (
                (None, False)
                if collection
                else inspect_install(folder, entries, dedicated, managed)
            )
            if game:
                yield game
            if not dedicated and not is_install:
                # A publisher or Games collection can contain separate installs.
                for subfolder in child_dirs(entries):
                    if not excluded(subfolder, managed):
                        game, _ = inspect_install(
                            subfolder, children(subfolder), collection, managed
                        )
                        if game:
                            yield game
