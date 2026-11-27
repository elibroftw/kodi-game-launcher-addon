import importlib.util
import json
import os
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

ADDON_ID = 'plugin.game.installedgamelibrary'
ADDON_DIR = Path(__file__).resolve().parents[1] / ADDON_ID
PLUGIN_URL = f'plugin://{ADDON_ID}/'
sys.path.insert(0, str(ADDON_DIR))

from resources.lib import catalog, discovery, history, standalone, windows  # noqa: E402


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def put(self, path, text=''):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding='utf-8')
        return file

    def test_steam_additional_library_and_cached_poster(self):
        library = json.dumps(str(self.root / 'Games Library'))
        self.put(
            'Steam/steamapps/libraryfolders.vdf',
            '"libraryfolders" { "1" { "path" ' + library + ' } }',
        )
        apps = 'Games Library/steamapps/'
        self.put(
            apps + 'appmanifest_123.acf',
            '"AppState" { "appid" "123" "name" "Test Game" "installdir" "Test" "StateFlags" "4" }',
        )
        self.put(
            apps + 'appmanifest_124.acf',
            '"AppState" { "appid" "124" "name" "Downloading" "installdir" "Test" "StateFlags" "2" }',
        )
        self.put(
            apps + 'appmanifest_125.acf',
            '"AppState" { "appid" "125" "name" "Removed" "installdir" "Removed" "StateFlags" "4" }',
        )
        self.put(
            apps + 'appmanifest_228980.acf',
            '"AppState" { "appid" "228980" "name" "Steamworks" "installdir" "Test" "StateFlags" "4" }',
        )
        self.put(
            apps + 'appmanifest_126.acf',
            '"AppState" { "appid" "127" "name" "Bad ID" "installdir" "Test" "StateFlags" "4" }',
        )
        self.put(apps + 'common/Test/test.exe')
        poster = self.put('Steam/appcache/librarycache/123/hash/library_600x900.jpg')
        [game] = discovery.steam_games(self.root / 'Steam')
        self.assertEqual(game.launch, 'steam://rungameid/123')
        self.assertEqual(game.poster, poster)

    def test_epic_launch_uses_launcher_uri_and_safe_executable(self):
        exe = self.put('Installed/Bin/Game.exe')
        manifest = dict(
            CatalogNamespace='namespace',
            CatalogItemId='catalog',
            AppName='app',
            DisplayName='Test',
            InstallLocation=str(self.root / 'Installed'),
            LaunchExecutable='Bin/Game.exe',
        )
        self.put('Manifests/install.item', json.dumps(manifest))
        self.put('Manifests/incomplete.item', json.dumps(dict(manifest, bIsIncompleteInstall=True)))
        self.put('Elsewhere.exe')
        self.put(
            'Manifests/escape.item', json.dumps(dict(manifest, LaunchExecutable='../Elsewhere.exe'))
        )
        self.put('Manifests/broken.item', '\n{')
        [game] = discovery.epic_games(self.root / 'Manifests')
        self.assertEqual(game.executable, exe)
        self.assertEqual(
            game.launch,
            'com.epicgames.launcher://apps/namespace%3Acatalog%3Aapp?action=launch&silent=true',
        )

    def test_shortcut_allowlist(self):
        for uri, launcher in [
            ('steam://rungameid/123', 'Steam'),
            (
                'com.epicgames.launcher://apps/namespace%3Acatalog%3Aapp?action=launch&silent=true',
                'Epic Games',
            ),
            ('goggalaxy://launch/123', 'GOG'),
            ('uplay://launch/123/0', 'Ubisoft Connect'),
            ('origin://launchgame/OFB-EAST:123', 'EA'),
            ('link2ea://launchgame/123', 'EA'),
        ]:
            with self.subTest(uri=uri):
                shortcut = self.put('Game.url', '[InternetShortcut]\nURL=' + uri)
                game = discovery.internet_shortcut(shortcut)
                self.assertEqual((game.launcher, game.launch), (launcher, uri))
        for uri in (
            'https://example.com/',
            'file:///bogus.exe',
            'steam://open/library',
            'steam://rungameid/../123',
            'steam://rungameid/228980',
            'steam://rungameid/123?extra=1',
            'com.epicgames.launcher://apps/app?action=store',
            'com.epicgames.launcher://apps/app?action=launch&action=store',
            'link2ea://launchgame/123?theme="broken"',
        ):
            with self.subTest(rejected=uri):
                self.assertIsNone(discovery.launcher_uri(uri))

    def test_galaxy_shortcut_keeps_link(self):
        exe = self.put('GOG/GalaxyClient.exe')
        link = self.put('Test Game.lnk')
        game = discovery.galaxy_shortcut(link, str(exe), '/command=runGame /gameId="42"', '')
        self.assertEqual((game.key, game.launch), ('GOG:42', str(link)))
        self.assertIsNone(discovery.galaxy_shortcut(link, str(exe), '', ''))

    def test_launcher_game_lookup_uses_shortcut_without_unrelated_scans(self):
        uri = 'com.epicgames.launcher://apps/app?action=launch&silent=true'
        manifest = discovery.Game(
            'Epic Games',
            'app',
            'Official Title',
            'com.epicgames.launcher://apps/ns%3Acatalog%3Aapp?action=launch',
        )
        link = self.put('Desktop/Game.url', '[InternetShortcut]\nURL=' + uri)
        with (
            patch.object(windows, 'steam_root', return_value=None),
            patch.object(windows, 'env_path', return_value=self.root),
            patch.object(windows, 'epic_games', return_value=[manifest]),
            patch.object(windows, 'shortcut_files', return_value=[link]) as shortcuts,
            patch.object(windows, 'standalone_games') as scan,
            patch.object(windows, 'shell_shortcuts') as links,
        ):
            [game] = windows.discover(manifest.key)
        self.assertEqual(game.title, manifest.title)
        self.assertEqual(game.launch, uri)
        shortcuts.assert_called_once_with(include_links=False)
        links.assert_not_called()
        scan.assert_not_called()

    def test_gog_lookup_falls_back_to_offline_installs(self):
        game = discovery.Game('GOG', '42', 'Offline Game', 'Game.exe')
        with (
            patch.object(windows, 'steam_root', return_value=None),
            patch.object(windows, 'env_path', return_value=None),
            patch.object(windows, 'shortcut_files', return_value=[]),
            patch.object(windows, 'standalone_games', return_value=[game]),
        ):
            self.assertEqual(windows.discover(game.key), [game])

    def test_shell_shortcut_reader_ignores_bad_rows(self):
        link = self.put('Desktop/Game.lnk')
        valid = {
            'path': str(link),
            'target': 'GalaxyClient.exe',
            'arguments': '/command=runGame /gameId=42',
        }
        data = [None, {}, dict(valid, path='Other.lnk'), valid]
        with patch.object(windows, 'powershell', return_value=json.dumps(data)):
            [game] = windows.shell_shortcuts([link])
        self.assertEqual(game.key, 'GOG:42')

    def test_games_folder_prefers_matching_exe_and_uses_local_poster(self):
        exe = self.put('Games/My Game/bin/x64/MyGame.exe')
        poster = self.put('Games/My Game/poster.jpg')
        self.put('Games/My Game/Other.exe')
        self.put('Games/My Game/unins000.exe')
        self.put('Games/Ambiguous/First.exe')
        self.put('Games/Ambiguous/Second.exe')
        self.put('Games/Helpers/VC_redist.exe')
        self.put('Games/Helpers/UnityCrashHandler64.exe')
        self.put('Games/Wrapper/Deeper/TooDeep.exe')
        [game] = standalone.standalone_games(self.root / 'Games', [])
        self.assertEqual((game.title, game.executable, game.poster), ('My Game', exe, poster))
        self.assertEqual(game.working_dir, exe.parent)

    def test_program_files_requires_evidence_outside_games_collection(self):
        self.put('Program Files/Office/Office.exe')
        self.put('Program Files/Publisher/Utility/Utility.exe')
        self.put(
            'Program Files/Publisher/Not a Shipping Game/Binaries/Win64/Tool-Win64-Shipping.exe'
        )
        unreal = self.put('Program Files/Publisher/Racer/Binaries/Win64/Racer-Win64-Shipping.exe')
        self.put('Program Files/Publisher/Racer/Content/Paks/main.pak')
        unity = self.put('Program Files (x86)/Platformer/Platformer.exe')
        self.put('Program Files (x86)/Platformer/UnityPlayer.dll')
        self.put('Program Files (x86)/Platformer/Platformer_Data/globalgamemanagers')
        self.put('Program Files (x86)/Platformer/Platformer_Data/Plugins/x86/steam_api.dll')
        retro = self.put('Program Files/Games/Retro/Retro.exe')
        roots = [self.root / 'Program Files', self.root / 'Program Files (x86)']
        games = list(standalone.standalone_games(self.root / 'No Games', roots + roots))
        self.assertEqual(
            {game.title: game.executable for game in games},
            {'Racer': unreal, 'Platformer': unity, 'Retro': retro},
        )
        self.assertEqual(len(games), 3)

    def test_discover_scans_current_users_local_programs_without_listing_ordinary_apps(self):
        exe = self.put('Local/Programs/Racer/Binaries/Win64/Racer-Win64-Shipping.exe')
        self.put('Local/Programs/Racer/Content/Paks/main.pak')
        self.put('Local/Programs/Editor/Editor.exe')
        env = dict(
            LOCALAPPDATA=str(self.root / 'Local'),
            ProgramW6432='',
            ProgramFiles='',
            ProgramData='',
            **{'ProgramFiles(x86)': ''},
        )

        def scan(_games_root, program_roots, managed_dirs):
            return standalone.standalone_games(None, program_roots, managed_dirs)

        with (
            patch.dict(os.environ, env),
            patch.object(windows, 'steam_root', return_value=None),
            patch.object(windows, 'shortcut_files', return_value=[]),
            patch.object(windows, 'standalone_games', side_effect=scan),
        ):
            [game] = windows.discover()
        self.assertEqual((game.title, game.executable), ('Racer', exe))

    def test_manifest_managed_installs_are_not_listed_twice(self):
        self.put('Games/Steam Game/SteamGame.exe')
        self.put('Program Files/Games/Epic Game/EpicGame.exe')
        self.put('Program Files/Games/Keep/Keep.exe')
        managed = [self.root / 'Games/Steam Game', self.root / 'Program Files/Games/Epic Game']
        [game] = standalone.standalone_games(
            self.root / 'Games', [self.root / 'Program Files'], managed
        )
        self.assertEqual(game.title, 'Keep')

    def test_gog_primary_task_retains_working_directory_and_deduplicates_shortcut(self):
        exe = self.put('Program Files/GOG Games/Classic/bin/Start.exe')
        primary = dict(type='FileTask', isPrimary=True, path=r'bin\Start.exe', workingDir='.')
        self.put(
            'Program Files/GOG Games/Classic/goggame-42.info',
            json.dumps(dict(gameId='42', name='Classic', playTasks=[primary])),
        )
        [game] = standalone.standalone_games(None, [self.root / 'Program Files'])
        self.assertEqual(
            (game.key, game.executable, game.working_dir), ('GOG:42', exe, exe.parent.parent)
        )
        shortcut = discovery.Game('GOG', '42', 'Classic', 'goggalaxy://launch/42')
        [merged] = discovery.merge_games([shortcut, game])
        self.assertEqual(
            (merged.launch, merged.working_dir, merged.executable), (shortcut.launch, None, exe)
        )

    def test_gog_unsupported_task_is_not_replaced_by_a_guess(self):
        self.put('Outside.exe')
        self.put('Games/Classic/Classic.exe')
        for task in [
            dict(path=r'..\..\Outside.exe'),
            dict(path='Classic.exe', arguments='--required'),
        ]:
            primary = dict(type='FileTask', isPrimary=True, **task)
            self.put(
                'Games/Classic/goggame-42.info', json.dumps(dict(gameId='42', playTasks=[primary]))
            )
            with self.subTest(task=task):
                self.assertEqual(list(standalone.standalone_games(self.root / 'Games', [])), [])

    def test_unreal_bootstrap_does_not_mask_ambiguous_shipping_executables(self):
        self.put('Games/Collection/Collection.exe')
        for project in ('One', 'Two'):
            self.put(f'Games/Collection/{project}/Binaries/Win64/{project}-Win64-Shipping.exe')
            self.put(f'Games/Collection/{project}/Content/Paks/main.pak')
        self.assertEqual(list(standalone.standalone_games(self.root / 'Games', [])), [])

    def test_unsupported_gog_install_is_not_mistaken_for_publisher_folder(self):
        folder = 'Program Files/Classic'
        self.put(f'{folder}/Project/Binaries/Win64/Game-Win64-Shipping.exe')
        self.put(f'{folder}/Project/Content/Paks/main.pak')
        task = dict(type='FileTask', isPrimary=True, path='Classic.exe', arguments='--required')
        self.put(f'{folder}/goggame-42.info', json.dumps(dict(gameId='42', playTasks=[task])))
        self.assertEqual(list(standalone.standalone_games(None, [self.root / 'Program Files'])), [])

    def test_scan_does_not_follow_linked_game_folder(self):
        exe = self.put('Outside/Outside.exe')
        games = self.root / 'Games'
        games.mkdir()
        try:
            (games / 'Linked Game').symlink_to(exe.parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Directory symlinks are not available')
        self.assertEqual(list(standalone.standalone_games(games, [])), [])

    def test_direct_launch_uses_absolute_exe_and_working_directory(self):
        exe = self.put('Games/My Game/MyGame.exe')
        [game] = standalone.standalone_games(self.root / 'Games', [])
        with patch.object(windows.subprocess, 'Popen') as popen:
            windows.launch(game)
        popen.assert_called_once_with([str(exe)], cwd=str(exe.parent))

    def test_artwork_prefers_poster_then_executable(self):
        poster = self.put('poster.jpg')
        exe = self.put('Game.exe')
        games = [
            discovery.Game('test', 'poster', 'A', '', poster=poster, executable=exe),
            discovery.Game('test', 'exe', 'B', '', executable=exe),
            discovery.Game('test', 'blank', 'C', ''),
        ]

        def extract(_, data, timeout):
            [job] = data['items']
            self.assertEqual(job['source'], str(exe))
            Path(job['output']).write_bytes(b'fake PNG')

        with patch.object(windows, 'powershell', side_effect=extract) as invoke:
            images = windows.artwork(games, self.root / 'cache')
            self.assertEqual(windows.artwork(games, self.root / 'cache'), images)
        self.assertEqual(images[games[0].key], ('poster', str(poster)))
        self.assertEqual(images[games[1].key][0], 'icon')
        self.assertNotIn(games[2].key, images)
        invoke.assert_called_once()

    def test_xbox_artwork_uses_cached_icons_and_retries_missing_later(self):
        cache = self.root / 'icons'

        def copy_icon(_, data, timeout):
            xbox, game_bar = data['items']
            self.assertEqual([xbox['key'], game_bar['key']], ['xbox', 'game-bar'])
            Path(xbox['output']).write_bytes(b'fake PNG')
            return ''

        with patch.object(windows, 'powershell', side_effect=copy_icon) as invoke:
            self.assertEqual(windows.xbox_artwork(cache), {'xbox': str(cache / 'xbox.png')})
            self.assertEqual(windows.xbox_artwork(cache), {'xbox': str(cache / 'xbox.png')})
        invoke.assert_called_once()

    def test_xbox_launch_uses_fixed_windows_target(self):
        with (
            patch.dict(os.environ, {'SystemRoot': str(self.root)}),
            patch.object(windows.subprocess, 'Popen') as popen,
        ):
            windows.open_xbox()
        popen.assert_called_once_with([str(self.root / 'explorer.exe'), windows.XBOX_APP])

    def test_history_keeps_distinct_games_newest_first(self):
        path = self.root / 'profile' / 'last_launched.json'
        alpha, beta, zeta = [
            discovery.Game('Steam', str(i), title, '')
            for i, title in enumerate(('Alpha', 'Beta', 'Zeta'))
        ]
        games = [zeta, beta, alpha]
        self.assertEqual(history.order_games(games, path), [alpha, beta, zeta])
        for key in (beta.key, zeta.key, beta.key):
            self.assertTrue(history.record_launch(path, key))
        self.assertFalse(history.record_launch(path, beta.key))
        self.assertEqual(history.read_history(path), [beta.key, zeta.key])
        self.assertEqual(history.order_games(games, path), [beta, zeta, alpha])
        self.assertEqual(history.order_games([zeta, alpha], path), [zeta, alpha])

    def test_corrupt_history_does_not_prevent_recording_launch(self):
        path = self.put('profile/last_launched.json', '{unfinished')
        self.assertEqual(history.read_history(path), [])
        self.assertTrue(history.record_launch(path, 'Steam:5'))
        self.assertEqual(history.read_history(path), ['Steam:5'])

    def test_catalog_only_stores_display_data_and_reuses_existing_index(self):
        game = discovery.Game('Steam', '10', 'Test', 'steam://rungameid/10')
        with (
            patch.object(catalog, 'index_lock', side_effect=lambda _: nullcontext(True)),
            patch.object(windows, 'discover', return_value=[game]) as discover,
            patch.object(windows, 'artwork', return_value={game.key: ('poster', 'test.jpg')}),
            patch.object(windows, 'xbox_artwork', return_value={'xbox': 'xbox.png'}),
        ):
            self.assertTrue(catalog.rebuild(self.root))
            self.assertFalse(catalog.rebuild(self.root))
        discover.assert_called_once_with()
        expected = catalog.Catalog(
            (catalog.GameTile(game.key, game.title, game.launcher, 'test.jpg', True),),
            {'xbox': 'xbox.png'},
        )
        self.assertEqual(catalog.load(self.root), expected)
        self.assertNotIn(game.launch, (self.root / catalog.FILENAME).read_text(encoding='utf-8'))

    def test_failed_rescan_leaves_previous_catalog_on_disk(self):
        with (
            patch.object(catalog, 'index_lock', side_effect=lambda _: nullcontext(True)),
            patch.object(windows, 'discover', side_effect=[[], OSError('unreadable')]),
            patch.object(windows, 'xbox_artwork', return_value={}),
        ):
            self.assertTrue(catalog.rebuild(self.root))
            with self.assertRaises(OSError):
                catalog.rebuild(self.root, force=True)
        self.assertEqual(catalog.load(self.root), catalog.Catalog((), {}))

    @unittest.skipUnless(os.name == 'nt', 'Windows byte-range lock')
    def test_index_lock_blocks_another_invocation_until_released(self):
        with catalog.index_lock(self.root) as acquired:
            self.assertTrue(acquired)
            with catalog.index_lock(self.root) as duplicate:
                self.assertFalse(duplicate)
        with catalog.index_lock(self.root) as acquired:
            self.assertTrue(acquired)

    def test_game_bar_sends_windows_shortcut(self):
        def send(count, inputs, size):
            self.assertEqual(count, 4)
            self.assertEqual(
                [
                    (event.kind, event.data.keyboard.vk, event.data.keyboard.flags)
                    for event in inputs
                ],
                [(1, 0x5B, 1), (1, 0x47, 0), (1, 0x47, 2), (1, 0x5B, 3)],
            )
            if os.name == 'nt':
                self.assertEqual(size, 40 if sys.maxsize > 2**32 else 28)
            return count

        dll = Mock()
        dll.SendInput.side_effect = send
        with patch.object(windows.ctypes, 'WinDLL', return_value=dll, create=True):
            windows.open_game_bar()
        dll.SendInput.assert_called_once()

    def test_game_bar_releases_pressed_keys_on_partial_input(self):
        dll = Mock()
        dll.SendInput.side_effect = [2, 2]
        with (
            patch.object(windows.ctypes, 'WinDLL', return_value=dll, create=True),
            patch.object(windows.ctypes, 'get_last_error', return_value=0, create=True),
        ):
            with self.assertRaisesRegex(OSError, '2 of 4 events'):
                windows.open_game_bar()
        recovery = dll.SendInput.call_args.args[1]
        self.assertEqual(
            [(event.data.keyboard.vk, event.data.keyboard.flags) for event in recovery],
            [(0x47, 2), (0x5B, 3)],
        )


class KodiTests(unittest.TestCase):
    def setUp(self):
        profile = TemporaryDirectory()
        self.addCleanup(profile.cleanup)
        self.profile = Path(profile.name)
        self.modules = {
            name: Mock() for name in ('xbmc', 'xbmcaddon', 'xbmcgui', 'xbmcplugin', 'xbmcvfs')
        }
        spec = importlib.util.spec_from_file_location(
            'installed_library_kodi_test', ADDON_DIR / 'addon.py'
        )
        self.addon = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, self.modules):
            spec.loader.exec_module(self.addon)
        self.modules['xbmcvfs'].translatePath.return_value = profile.name
        self.history_file = self.profile / self.addon.HISTORY_FILE

    def set_estuary_games(self, parent_folder=False):
        xbmc = self.modules['xbmc']
        xbmc.getSkinDir.return_value = 'skin.estuary'
        xbmc.getCondVisibility.return_value = parent_folder
        xbmc.getInfoLabel.return_value = 'new-listing'
        xbmc.Monitor.return_value.waitForAbort.return_value = False
        self.modules['xbmcgui'].getCurrentWindowId.return_value = self.addon.GAMES_WINDOW
        return xbmc

    def test_estuary_games_view_uses_horizontal_poster_view_and_xbox_art(self):
        xbmc = self.set_estuary_games()
        xbox, game_bar = Mock(), Mock()
        self.modules['xbmcgui'].ListItem.side_effect = [xbox, game_bar]
        snapshot = catalog.Catalog((), {'xbox': 'xbox.png', 'game-bar': 'bar.png'})
        with patch.object(self.addon, 'uuid4', return_value=SimpleNamespace(hex='new-listing')):
            self.addon.show_games(1, PLUGIN_URL, snapshot)
        self.modules['xbmcplugin'].setContent.assert_called_once_with(1, 'games')
        self.assertEqual(
            xbmc.executebuiltin.call_args_list,
            [call('Container.SetViewMode(51)'), call('SetFocus(51,1,absolute)')],
        )
        xbox.setArt.assert_called_once_with(
            {'icon': 'xbox.png', 'thumb': 'xbox.png', 'poster': 'xbox.png'}
        )
        game_bar.setArt.assert_called_once_with(
            {'icon': 'bar.png', 'thumb': 'bar.png', 'poster': 'bar.png'}
        )
        xbox.addContextMenuItems.assert_called_once_with(
            [('Rescan installed games', f'RunPlugin({PLUGIN_URL}?index=manual)')]
        )

    def test_cold_library_shows_indexing_tile_before_scheduling_background_scan(self):
        xbmc = self.modules['xbmc']
        plugin = self.modules['xbmcplugin']

        def schedule(command):
            self.assertEqual(command, f'RunPlugin({PLUGIN_URL}?index=auto)')
            plugin.endOfDirectory.assert_called_once_with(7, cacheToDisc=False)

        xbmc.executebuiltin.side_effect = schedule
        with (
            patch.object(sys, 'argv', [PLUGIN_URL, '7', '']),
            patch.object(self.addon, 'os', SimpleNamespace(name='nt')),
            patch.object(windows, 'discover') as discover,
            patch.object(windows, 'xbox_artwork') as artwork,
        ):
            self.addon.main()
        discover.assert_not_called()
        artwork.assert_not_called()
        self.modules['xbmcgui'].ListItem.assert_any_call(label='Indexing installed games…')
        xbmc.executebuiltin.assert_called_once()

    def test_warm_library_even_when_empty_does_not_trigger_indexing(self):
        with (
            patch.object(catalog, 'index_lock', side_effect=lambda _: nullcontext(True)),
            patch.object(windows, 'discover', return_value=[]),
            patch.object(windows, 'xbox_artwork', return_value={}),
        ):
            catalog.rebuild(self.profile)
        with (
            patch.object(sys, 'argv', [PLUGIN_URL, '7', '']),
            patch.object(self.addon, 'os', SimpleNamespace(name='nt')),
            patch.object(windows, 'discover') as discover,
            patch.object(windows, 'artwork') as artwork,
        ):
            self.addon.main()
        discover.assert_not_called()
        artwork.assert_not_called()
        self.modules['xbmc'].executebuiltin.assert_not_called()
        self.assertEqual(len(self.modules['xbmcplugin'].addDirectoryItems.call_args.args[1]), 2)

    def test_background_rescan_publishes_and_refreshes_open_library(self):
        game = discovery.Game('Steam', '10', 'Test', 'steam://rungameid/10')
        xbmc = self.set_estuary_games()
        xbmc.getInfoLabel.return_value = PLUGIN_URL
        with (
            patch.object(sys, 'argv', [PLUGIN_URL, '-1', '?index=manual']),
            patch.object(self.addon, 'os', SimpleNamespace(name='nt')),
            patch.object(catalog, 'index_lock', side_effect=lambda _: nullcontext(True)),
            patch.object(windows, 'discover', return_value=[game]),
            patch.object(windows, 'artwork', return_value={}),
            patch.object(windows, 'xbox_artwork', return_value={}),
        ):
            self.addon.main()
        self.assertEqual(catalog.load(self.profile).games[0].key, game.key)
        xbmc.executebuiltin.assert_called_once_with('Container.Refresh')
        self.modules['xbmcplugin'].setResolvedUrl.assert_not_called()

    def test_focus_starts_on_first_game_when_available(self):
        xbmc = self.set_estuary_games()
        self.addon.focus_initial_item(5, 'new-listing')
        xbmc.executebuiltin.assert_called_with('SetFocus(51,2,absolute)')

    def test_focus_skips_kodi_parent_and_waits_for_listing(self):
        xbmc = self.set_estuary_games(parent_folder=True)
        xbmc.getInfoLabel.side_effect = ['old-listing', 'new-listing']
        self.addon.focus_initial_item(3, 'new-listing')
        xbmc.getInfoLabel.assert_called_with(
            'Container.ListItemAbsolute(1).Property(GameLibraryListing)'
        )
        xbmc.Monitor.return_value.waitForAbort.assert_called_once()
        xbmc.executebuiltin.assert_called_with('SetFocus(51,3,absolute)')

    def test_games_click_launches_outside_kodi_and_finishes_resolution(self):
        launch = Mock()
        args = [PLUGIN_URL, '7', '?tool=xbox']
        with (
            patch.object(sys, 'argv', args),
            patch.object(self.addon, 'os', SimpleNamespace(name='nt')),
            patch.dict(self.addon.TOOLS, {'xbox': ('Open Xbox app', launch)}),
        ):
            self.addon.main()
        launch.assert_called_once_with()
        item = self.modules['xbmcgui'].ListItem.return_value
        self.modules['xbmcplugin'].setResolvedUrl.assert_called_once_with(7, False, item)
        self.assertFalse(self.history_file.exists())
        self.modules['xbmc'].executebuiltin.assert_not_called()

    def test_game_launch_records_history_before_refreshing_open_library(self):
        game = discovery.Game('Steam', '10', 'Zeta', 'steam://rungameid/10')
        alpha = discovery.Game('Steam', '20', 'Alpha', 'steam://rungameid/20')
        xbmc = self.set_estuary_games()
        xbmc.getInfoLabel.return_value = PLUGIN_URL
        args = [PLUGIN_URL, '7', '?game=Steam%3A10']
        history.record_launch(self.history_file, alpha.key)

        def refresh(command):
            self.assertEqual(command, 'Container.Refresh')
            self.modules['xbmcplugin'].setResolvedUrl.assert_called_once()
            self.assertEqual(history.read_history(self.history_file), [game.key, alpha.key])

        xbmc.executebuiltin.side_effect = refresh
        with (
            patch.object(sys, 'argv', args),
            patch.object(self.addon, 'os', SimpleNamespace(name='nt')),
            patch.object(windows, 'discover', return_value=[game]) as discover,
            patch.object(windows, 'launch') as launch,
        ):
            self.addon.main()
        discover.assert_called_once_with(game.key)
        launch.assert_called_once_with(game)
        xbmc.executebuiltin.assert_called_once_with('Container.Refresh')
        tiles = tuple(
            catalog.GameTile(entry.key, entry.title, entry.launcher) for entry in (alpha, game)
        )
        with patch.object(self.addon, 'focus_initial_item'):
            self.addon.show_games(8, PLUGIN_URL, catalog.Catalog(tiles, {}))
        entries = self.modules['xbmcplugin'].addDirectoryItems.call_args.args[1]
        self.assertEqual(
            [entry[0].split('?')[1] for entry in entries],
            ['tool=xbox', 'tool=game-bar', 'game=Steam%3A10', 'game=Steam%3A20'],
        )

    def test_failed_game_launch_does_not_change_history_or_refresh(self):
        game = discovery.Game('Steam', '10', 'Test', 'steam://rungameid/10')
        args = [PLUGIN_URL, '7', '?game=Steam%3A10']
        with (
            patch.object(sys, 'argv', args),
            patch.object(self.addon, 'os', SimpleNamespace(name='nt')),
            patch.object(windows, 'discover', return_value=[game]),
            patch.object(windows, 'launch', side_effect=OSError('failed')),
        ):
            self.addon.main()
        self.assertFalse(self.history_file.exists())
        self.modules['xbmc'].executebuiltin.assert_not_called()


if __name__ == '__main__':
    unittest.main()
