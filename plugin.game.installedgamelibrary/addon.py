"""Kodi entry point for PC Game Library."""

import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs
from resources.lib import catalog, history, windows
from resources.lib.discovery import is_file

NAME = 'PC Game Library'
DEFAULT_ICON = 'DefaultAddonGame.png'
BLANK_FANART = str(Path(__file__).parent / 'resources' / 'media' / 'blank-fanart.png')
GAMES_WINDOW = 10821
ESTUARY_POSTER_VIEW = 51
HISTORY_FILE = 'last_launched.json'
LISTING_PROPERTY = 'GameLibraryListing'
TOOLS = {
    'xbox': ('Open Xbox app', windows.open_xbox),
    'game-bar': ('Open Xbox Game Bar', windows.open_game_bar),
}


def notify(message):
    xbmcgui.Dialog().notification(NAME, message, DEFAULT_ICON)


def profile_directory():
    return Path(xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo('profile')))


def focus_initial_item(entry_count, listing_token):
    if not entry_count or xbmcgui.getCurrentWindowId() != GAMES_WINDOW:
        return
    index = min(len(TOOLS), entry_count - 1)
    estuary = xbmc.getSkinDir() == 'skin.estuary'
    monitor = xbmc.Monitor()
    # EndOfDirectory unblocks Kodi; the new list may not be bound to the view yet.
    for _ in range(25):
        if xbmcgui.getCurrentWindowId() != GAMES_WINDOW:
            return
        offset = int(xbmc.getCondVisibility('Container.ListItemAbsolute(0).IsParentFolder'))
        first_item = f'.ListItemAbsolute({offset}).Property({LISTING_PROPERTY})'
        if xbmc.getInfoLabel('Container' + first_item) == listing_token:
            if estuary:
                control = ESTUARY_POSTER_VIEW
                xbmc.executebuiltin(f'Container.SetViewMode({control})')
            else:
                try:
                    control = xbmcgui.Window(GAMES_WINDOW).getFocusId()
                except RuntimeError:
                    return
                # Don't try to select an item in a skin's sidebar or menu button.
                if xbmc.getInfoLabel(f'Container({control})' + first_item) != listing_token:
                    return
            xbmc.executebuiltin(f'SetFocus({control},{index + offset},absolute)')
            return
        if monitor.waitForAbort(0.04):
            return


def backdrop_or_blank(path):
    return path if path and is_file(Path(path)) else BLANK_FANART


def library_rows(snapshot):
    games = (
        history.order_games(snapshot.games, profile_directory() / HISTORY_FILE) if snapshot else ()
    )
    tool_icons = snapshot.tool_icons if snapshot else {}
    xbox_logo = tool_icons.get('xbox')
    xbox_fanart = backdrop_or_blank(snapshot.xbox_fanart if snapshot else None)
    for key, (label, _) in TOOLS.items():
        item = xbmcgui.ListItem(label=label)
        icon = xbox_logo or tool_icons.get(key, DEFAULT_ICON)
        item.setArt({'thumb': icon, 'icon': icon, 'poster': icon, 'fanart': xbox_fanart})
        yield item, None, key
    for game in games:
        item = xbmcgui.ListItem(label=game.title, label2=game.launcher)
        image = game.image or DEFAULT_ICON
        art = {'thumb': image, 'icon': image, 'fanart': backdrop_or_blank(game.fanart)}
        if game.is_poster:
            art['poster'] = image
        item.setArt(art)
        yield item, game.key, None


def show_games(handle, base_url, snapshot):
    listing_token = uuid4().hex
    rescan_url = base_url + '?index=manual'
    menu = [('Rescan installed games', f'RunPlugin({rescan_url})')]
    entries = []
    for item, game, tool in library_rows(snapshot):
        if not entries:
            item.setProperty(LISTING_PROPERTY, listing_token)
        item.addContextMenuItems(menu)
        params = {'tool': tool} if tool is not None else {'game': game}
        entries.append((base_url + '?' + urlencode(params), item, False))
    if snapshot is None:
        item = xbmcgui.ListItem(label='Indexing installed games…')
        item.setArt({'thumb': DEFAULT_ICON, 'icon': DEFAULT_ICON, 'fanart': BLANK_FANART})
        entries.append((rescan_url, item, False))
    xbmcplugin.setPluginCategory(handle, NAME)
    xbmcplugin.setContent(handle, 'games')
    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_UNSORTED)
    xbmcplugin.addDirectoryItems(handle, entries, len(entries))
    xbmcplugin.endOfDirectory(handle, cacheToDisc=False)
    focus_initial_item(len(entries), listing_token)


def run_action(selected, tool):
    if tool is not None:
        action = TOOLS.get(tool)
        if action:
            label, open_tool = action
            try:
                open_tool()
            except OSError as error:
                xbmc.log(f'{NAME}: {label}: {error}', xbmc.LOGWARNING)
                if tool == 'game-bar':
                    notify('Could not send Win+G. Try it on your keyboard.')
                else:
                    notify('Could not open Xbox. Check that the app is installed.')
        return False

    game = next((game for game in windows.discover(selected) if game.key == selected), None)
    if not game:
        notify('Game not found. Run Rescan installed games and try again.')
        return False
    try:
        windows.launch(game)
    except OSError as error:
        xbmc.log(f'{NAME}: could not launch {game.key}: {error}', xbmc.LOGWARNING)
        hint = (
            'Check the game install.'
            if game.working_dir
            else 'Check that its launcher is installed.'
        )
        notify(f'Could not open {game.title}. {hint}')
        return False
    try:
        return history.record_launch(profile_directory() / HISTORY_FILE, game.key)
    except OSError as error:
        xbmc.log(f'{NAME}: could not save launch history: {error}', xbmc.LOGWARNING)
        return False


def refresh_library(base_url):
    if xbmcgui.getCurrentWindowId() == GAMES_WINDOW and xbmc.getInfoLabel(
        'Container.FolderPath'
    ).rstrip('/') == base_url.rstrip('/'):
        xbmc.executebuiltin('Container.Refresh')


def index_library(base_url, *, force):
    if force:
        notify('Scanning for installed games…')
    profile = profile_directory()
    try:
        changed = catalog.rebuild(profile, force=force)
    except OSError as error:
        xbmc.log(f'{NAME}: could not index games: {error}', xbmc.LOGWARNING)
        notify('Could not save the game list. Try Rescan installed games.')
        return
    if changed:
        refresh_library(base_url)
        snapshot = catalog.load(profile)
        if snapshot is not None and not snapshot.games:
            notify('No supported installed games or shortcuts found.')


def finish_action(handle):
    # Kodi asks for a playable URL for clicked tiles, but there's nothing to play.
    if handle >= 0:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())


def main():
    base_url, handle = sys.argv[0], int(sys.argv[1])
    params = parse_qs(sys.argv[2].lstrip('?')) if len(sys.argv) > 2 else {}
    index_mode = params.get('index', [None])[0]
    if index_mode in ('auto', 'manual'):
        finish_action(handle)
        if os.name == 'nt':
            index_library(base_url, force=index_mode == 'manual')
        return
    selected = params.get('game', [None])[0]
    tool = params.get('tool', [None])[0]
    if selected is not None or tool is not None:
        order_changed = False
        try:
            if os.name == 'nt':
                order_changed = run_action(selected, tool)
            else:
                notify('This add-on currently supports Windows only.')
        finally:
            finish_action(handle)
        if order_changed:
            refresh_library(base_url)
        return
    if os.name != 'nt':
        notify('This add-on currently supports Windows only.')
        xbmcplugin.endOfDirectory(handle, cacheToDisc=False)
        return
    snapshot = catalog.load(profile_directory())
    show_games(handle, base_url, snapshot)
    if snapshot is None:
        xbmc.executebuiltin(f'RunPlugin({base_url}?index=auto)')


if __name__ == '__main__':
    main()
