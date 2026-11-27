"""Windows discovery sources, shortcut inspection, and Shell launching."""

import base64
import ctypes
import hashlib
import json
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import replace
from pathlib import Path

from .discovery import (
    epic_games,
    galaxy_shortcut,
    glob_files,
    internet_shortcut,
    is_directory,
    is_file,
    merge_games,
    shortcut_icon,
    steam_games,
)
from .standalone import standalone_games

SHELL_FOLDERS = r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders'
XBOX_APP = r'shell:AppsFolder\Microsoft.GamingApp_8wekyb3d8bbwe!Microsoft.Xbox.App'


def registry_path(hive, key, name, view=0):
    import winreg

    try:
        with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
            value, _ = winreg.QueryValueEx(handle, name)
        if isinstance(value, str):
            path = Path(os.path.expandvars(value))
            if path.is_absolute():
                return path
    except OSError:
        pass
    return None


def env_path(name, *parts):
    value = os.environ.get(name)
    return Path(value).joinpath(*parts) if value and Path(value).is_absolute() else None


def steam_root():
    import winreg

    candidates = [registry_path(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath')]
    for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
        candidates.append(
            registry_path(winreg.HKEY_LOCAL_MACHINE, r'Software\Valve\Steam', 'InstallPath', view)
        )
    candidates.extend(env_path(name, 'Steam') for name in ('ProgramFiles(x86)', 'ProgramFiles'))
    return next((path for path in candidates if path and is_directory(path / 'steamapps')), None)


def shortcut_files(include_links=True):
    import winreg

    locations = [
        (
            registry_path(winreg.HKEY_CURRENT_USER, SHELL_FOLDERS, 'Desktop')
            or env_path('USERPROFILE', 'Desktop'),
            '',
        ),
        (
            registry_path(winreg.HKEY_LOCAL_MACHINE, SHELL_FOLDERS, 'Common Desktop')
            or env_path('PUBLIC', 'Desktop'),
            '',
        ),
        (env_path('APPDATA', 'Microsoft', 'Windows', 'Start Menu', 'Programs'), '**/'),
        (env_path('ProgramData', 'Microsoft', 'Windows', 'Start Menu', 'Programs'), '**/'),
    ]
    extensions = ('url', 'lnk') if include_links else ('url',)
    return list(
        dict.fromkeys(
            path
            for folder, prefix in locations
            if folder
            for ext in extensions
            for path in glob_files(folder, f'{prefix}*.{ext}')
        )
    )


def powershell(script, data, timeout):
    executable = env_path('SystemRoot', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
    if not executable:
        return None
    prefix = '[Console]::OutputEncoding = [Text.Encoding]::UTF8\n$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json\n'
    command = base64.b64encode((prefix + script).encode('utf-16-le')).decode('ascii')
    try:
        result = subprocess.run(
            [str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', command],
            input=json.dumps(data),
            encoding='utf-8',
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=True,
        )
        return result.stdout.lstrip('\ufeff')
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return None


READ_LINKS = r"""
$shell = New-Object -ComObject WScript.Shell
$rows = @(foreach ($path in $payload.paths) {
    try {
        $link = $shell.CreateShortcut($path)
        if ([IO.Path]::GetFileName($link.TargetPath) -in 'GalaxyClient.exe', 'GalaxyClientService.exe') {
            [pscustomobject]@{ path = $path; target = $link.TargetPath; arguments = $link.Arguments;
                              icon = ($link.IconLocation -replace ',-?\d+$', '') }
        }
    } catch { }
})
ConvertTo-Json -InputObject $rows -Compress
"""


def shell_shortcuts(paths):
    if not paths:
        return
    result = powershell(READ_LINKS, {'paths': [str(path) for path in paths]}, timeout=12)
    if not result:
        return
    try:
        rows = json.loads(result)
    except ValueError:
        return
    if not isinstance(rows, list):
        return
    requested_paths = set(paths)
    for row in rows:
        if not isinstance(row, dict) or not all(
            isinstance(row.get(field), str) for field in ('path', 'target', 'arguments')
        ):
            continue
        path = Path(row['path'])
        if path in requested_paths:
            game = galaxy_shortcut(path, row['target'], row['arguments'], row.get('icon'))
            if game:
                yield game


def discover(game_key=None):
    """Discover the library, or only the sources needed to resolve a launch."""
    source = game_key.partition(':')[0] if game_key else None
    root = steam_root()
    epic = env_path('ProgramData', 'Epic', 'EpicGamesLauncher', 'Data', 'Manifests')
    installed = list(steam_games(root))
    if epic:
        installed.extend(epic_games(epic))
    shortcuts = []
    if source != 'Standalone':
        links = []
        for path in shortcut_files(include_links=source in (None, 'GOG')):
            if path.suffix.lower() == '.lnk':
                links.append(path)
            else:
                game = internet_shortcut(path)
                if game and (game_key is None or game.key == game_key):
                    shortcuts.append(game)
        if links:
            shortcuts.extend(shell_shortcuts(links))

    # Use a matching shortcut's launch URI while keeping the manifest's title and art.
    shortcut_launches = {}
    for game in shortcuts:
        shortcut_launches.setdefault(game.key, game.launch)
    games = [
        replace(game, launch=shortcut_launches[game.key]) if game.key in shortcut_launches else game
        for game in installed
    ]
    games.extend(shortcuts)
    if source in (None, 'Standalone') or (source == 'GOG' and game_key not in shortcut_launches):
        roots = [env_path(name) for name in ('ProgramW6432', 'ProgramFiles', 'ProgramFiles(x86)')]
        roots.append(env_path('LOCALAPPDATA', 'Programs'))
        games.extend(
            standalone_games(Path(r'C:\Games'), roots, (game.install_dir for game in installed))
        )
    return merge_games(game for game in games if game_key is None or game.key == game_key)


EXTRACT_ICONS = r"""
Add-Type -AssemblyName System.Drawing
foreach ($item in $payload.items) {
    $icon = $null
    $bitmap = $null
    try {
        $icon = [Drawing.Icon]::ExtractAssociatedIcon($item.source)
        if ($null -ne $icon) {
            $bitmap = $icon.ToBitmap()
            $bitmap.Save($item.output, [Drawing.Imaging.ImageFormat]::Png)
        }
    } catch { }
    finally {
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        if ($null -ne $icon) { $icon.Dispose() }
    }
}
"""


def artwork(games, cache):
    """Use local posters first, then shortcut or executable icons in one batch."""
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # Cached posters can still be displayed.
    images = {}
    candidates = {}
    jobs = {}
    for game in games:
        if game.poster:
            images[game.key] = ('poster', str(game.poster))
            continue
        for source in dict.fromkeys((game.icon, game.executable)):
            if (
                source is None
                or source.suffix.lower() not in ('.ico', '.exe')
                or shortcut_icon(str(source)) is None
            ):
                continue
            try:
                stat = source.stat()
            except OSError:
                continue
            key = f'{source}:{stat.st_mtime_ns}:{stat.st_size}'
            output = cache / (hashlib.sha256(key.encode('utf-8')).hexdigest() + '.png')
            candidates.setdefault(game.key, []).append(output)
            if is_file(output):
                break
            jobs[output] = {'source': str(source), 'output': str(output)}
    if jobs and is_directory(cache):
        powershell(EXTRACT_ICONS, {'items': list(jobs.values())}, timeout=15)
    for key, outputs in candidates.items():
        for output in outputs:
            if is_file(output):
                images[key] = ('icon', str(output))
                break
    return images


XBOX_ICONS = r"""
$apps = @{
    'xbox' = @('Microsoft.GamingApp', 'Microsoft.Xbox.App')
    'game-bar' = @('Microsoft.XboxGamingOverlay', 'App')
}
foreach ($item in $payload.items) {
    try {
        $name, $id = $apps[$item.key]
        $package = Get-AppxPackage -Name $name -ErrorAction Stop | Select-Object -First 1
        if (!$package) { continue }
        $manifest = Get-AppxPackageManifest -Package $package -ErrorAction Stop
        $app = $manifest.Package.Applications.Application | Where-Object { $_.Id -eq $id } | Select-Object -First 1
        foreach ($logo in @($app.VisualElements.Square150x150Logo, $app.VisualElements.Square44x44Logo)) {
            if (!$logo) { continue }
            $path = Join-Path $package.InstallLocation $logo
            $base = Join-Path ([IO.Path]::GetDirectoryName($path)) ([IO.Path]::GetFileNameWithoutExtension($path))
            $candidates = @(($base + '.scale-200.png'), ($base + '.targetsize-256_altform-unplated.png'),
                            $path, ($base + '.scale-100.png'), ($base + '.scale-400.png'))
            $asset = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
            if ($asset) {
                Copy-Item -LiteralPath $asset -Destination $item.output -ErrorAction Stop
                break
            }
        }
    } catch { }
}
"""


def xbox_artwork(cache):
    """Copy each installed Xbox app's logo into Kodi's accessible art cache."""
    outputs = {key: cache / (key + '.png') for key in ('xbox', 'game-bar')}
    missing = [
        {'key': key, 'output': str(path)} for key, path in outputs.items() if not is_file(path)
    ]
    checked = cache / 'xbox-logos-checked'
    if missing:
        try:
            retry = time.time() - checked.stat().st_mtime >= 86400
        except OSError:
            retry = True
        if retry:
            try:
                cache.mkdir(parents=True, exist_ok=True)
                if powershell(XBOX_ICONS, {'items': missing}, timeout=8) is not None:
                    checked.touch()
            except OSError:
                pass
    return {key: str(path) for key, path in outputs.items() if is_file(path)}


def launch(game):
    if game.working_dir is not None:
        subprocess.Popen([game.launch], cwd=str(game.working_dir))
    else:
        os.startfile(game.launch)


def open_xbox():
    explorer = env_path('SystemRoot', 'explorer.exe')
    if not explorer:
        raise OSError('Windows Explorer could not be located')
    subprocess.Popen([str(explorer), XBOX_APP])


# SendInput requires the full Win32 INPUT union, even for keyboard-only input.
class KeyboardInput(ctypes.Structure):
    _fields_ = [
        ('vk', wintypes.WORD),
        ('scan', wintypes.WORD),
        ('flags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('extra_info', ctypes.c_size_t),
    ]


class MouseInput(ctypes.Structure):
    _fields_ = [
        ('dx', wintypes.LONG),
        ('dy', wintypes.LONG),
        ('data', wintypes.DWORD),
        ('flags', wintypes.DWORD),
        ('time', wintypes.DWORD),
        ('extra_info', ctypes.c_size_t),
    ]


class InputData(ctypes.Union):
    _fields_ = [('keyboard', KeyboardInput), ('mouse', MouseInput)]


class Input(ctypes.Structure):
    _fields_ = [('kind', wintypes.DWORD), ('data', InputData)]


def open_game_bar():
    """Ask Windows to open its overlay for the foreground app, as Win+G does."""
    send_input = ctypes.WinDLL('user32', use_last_error=True).SendInput
    send_input.argtypes = (wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int)
    send_input.restype = wintypes.UINT

    def key(vk, flags=0):
        return Input(1, InputData(keyboard=KeyboardInput(vk, 0, flags, 0, 0)))

    def send(events):
        return send_input(len(events), (Input * len(events))(*events), ctypes.sizeof(Input))

    extended, key_up = 0x01, 0x02
    win_down, win_up = key(0x5B, extended), key(0x5B, extended | key_up)
    g_down, g_up = key(0x47), key(0x47, key_up)
    sent = send((win_down, g_down, g_up, win_up))
    if sent != 4:
        error = ctypes.get_last_error()
        # If a partial write left a key pressed, try to release it.
        if 0 < sent < 4:
            send((g_up, win_up) if sent == 2 else (win_up,))
        raise OSError(f'Windows did not accept Win+G ({sent} of 4 events, error {error}).')
