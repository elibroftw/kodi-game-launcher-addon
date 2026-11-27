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

from resources.lib import catalog, discovery, hero_catalog, history, online_art, standalone, windows  # noqa: E402


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        offline = patch.object(online_art, 'fetch', return_value=None)
        offline.start()
        self.addCleanup(offline.stop)

    def put(self, path, text=''):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding='utf-8')
        return file

    def test_steam_additional_library_and_cached_artwork(self):
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
        hero = self.put('Steam/appcache/librarycache/123/library_hero.jpg')
        [game] = discovery.steam_games(self.root / 'Steam')
        self.assertEqual(game.launch, 'steam://rungameid/123')
        self.assertEqual(game.poster, poster)
        self.assertEqual(game.fanart, hero)

        wide_cover = self.put('Steam/appcache/librarycache/123/library_header.jpg')
        [game] = discovery.steam_games(self.root / 'Steam')
        self.assertEqual(game.poster, poster)
        self.assertEqual(game.fanart, hero)
        hero.unlink()
        [game] = discovery.steam_games(self.root / 'Steam')
        self.assertIsNone(game.fanart)
        self.assertEqual(game.fallback_fanart, wide_cover)

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

    def test_catalog_caches_game_background_and_prefers_install_folder_override(self):
        hero = self.put('library_hero.jpg')
        self.put('Installed/Override/fanart.png')
        override = self.put('Installed/Override/landscape.jpg')
        games = [
            discovery.Game('Steam', '1', 'Cached artwork', 'unused', fanart=hero),
            discovery.Game(
                'Steam', '2', 'Custom artwork', 'unused', install_dir=override.parent, fanart=hero
            ),
            discovery.Game('Standalone', '3', 'No artwork', 'unused'),
        ]
        with (
            patch.object(catalog, 'index_lock', side_effect=lambda _: nullcontext(True)),
            patch.object(windows, 'discover', return_value=games),
            patch.object(windows, 'artwork', return_value={}),
            patch.object(windows, 'xbox_artwork', return_value={}),
        ):
            self.assertTrue(catalog.rebuild(self.root))
        tiles = catalog.load(self.root).games
        self.assertEqual([tile.fanart for tile in tiles], [str(hero), str(override), None])

    def test_old_catalog_reindexes_to_pick_preferred_widescreen_art(self):
        self.put(catalog.FILENAME, json.dumps({'version': 6, 'games': [], 'tool_icons': {}}))
        self.assertIsNone(catalog.load(self.root))

    def test_curated_catalog_changes_invalidate_cached_library(self):
        data = {
            'version': catalog.SCHEMA_VERSION,
            'hero_revision': hero_catalog.revision(),
            'games': [],
            'tool_icons': {},
        }
        self.put(catalog.FILENAME, json.dumps(data))
        self.assertEqual(catalog.load(self.root), catalog.Catalog((), {}))
        with patch.object(hero_catalog, 'revision', return_value='changed'):
            self.assertIsNone(catalog.load(self.root))

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


class OnlineArtworkTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cache = self.root / 'profile' / 'artwork' / 'steam'
        self.poster = b'\xff\xd8\xffposter\xff\xd9'
        self.wide = b'\xff\xd8\xfflandscape\xff\xd9'

    def screenshot_metadata(self, app_id, url):
        return json.dumps(
            {app_id: {'success': True, 'data': {'screenshots': [{'path_full': url}]}}}
        ).encode()

    def test_exact_standalone_match_downloads_to_profile_and_reuses_cache(self):
        game_dir = self.root / 'Games' / 'Factorio'
        game_dir.mkdir(parents=True)
        game = discovery.Game('Standalone', '1', 'Factorio', 'factorio.exe', install_dir=game_dir)
        old_cover = self.cache / '427520' / 'landscape.jpg'
        old_cover.parent.mkdir(parents=True)
        old_cover.write_bytes(b'old low-resolution cover')
        results = json.dumps(
            {
                'items': [
                    {'type': 'app', 'id': 427520, 'name': 'Factorio'},
                    {'type': 'app', 'id': 645390, 'name': 'Factorio: Space Age'},
                ]
            }
        ).encode()
        screenshot_url = 'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/427520/ss_ab.1920x1080.jpg'
        details = self.screenshot_metadata('427520', screenshot_url)
        with (
            patch.object(hero_catalog, 'load', return_value=()),
            patch.object(
                online_art, 'fetch', side_effect=[results, self.poster, details, self.wide]
            ) as fetch,
        ):
            [first] = online_art.enrich_games([game], self.cache)
            [second] = online_art.enrich_games([game], self.cache)
        self.assertEqual(first, second)
        self.assertEqual((first.key, first.launch), (game.key, game.launch))
        self.assertEqual(first.poster, self.cache / '427520' / 'poster.jpg')
        self.assertEqual(first.fanart, self.cache / '427520' / 'screenshot.jpg')
        self.assertEqual(first.poster.read_bytes(), self.poster)
        self.assertEqual(first.fanart.read_bytes(), self.wide)
        self.assertEqual(list(game_dir.iterdir()), [])
        self.assertEqual(fetch.call_count, 4)
        urls = [args.args[0] for args in fetch.call_args_list]
        self.assertIn('term=Factorio', urls[0])
        self.assertTrue(urls[1].endswith('/427520/library_600x900.jpg'))
        self.assertIn('appids=427520&filters=screenshots', urls[2])
        self.assertEqual(urls[3], screenshot_url)

    def test_curated_dune_hero_matches_correct_game_and_not_spice_wars(self):
        heroes = hero_catalog.load()
        installed = discovery.Game('Steam', '1172710', 'Dune: Awakening', 'unused')
        alias = discovery.Game('Standalone', 'other', 'Dune Awakening', 'unused')
        spice_wars = discovery.Game('Steam', '1605220', 'Dune: Spice Wars', 'unused')
        wrong_id = discovery.Game('Steam', '41', 'Dune: Awakening', 'unused')
        hero = hero_catalog.match(installed, heroes)
        self.assertEqual(
            hero.url, 'https://duneawakening.com/wp-content/uploads/Paralax-Background-2-1.jpg'
        )
        self.assertEqual(hero_catalog.match(alias, heroes), hero)
        spice_wars_hero = hero_catalog.match(spice_wars, heroes)
        self.assertNotEqual(spice_wars_hero, hero)
        self.assertTrue(spice_wars_hero.url.startswith('https://store-images.s-microsoft.com/'))
        self.assertIsNone(hero_catalog.match(wrong_id, heroes))

    def test_curated_hero_is_downloaded_once_and_overrides_steam_art(self):
        game = discovery.Game(
            'Standalone',
            '1',
            'Dune Awakening',
            'unused',
            poster=self.root / 'portrait.jpg',
            fanart=self.root / 'steam.jpg',
        )
        with patch.object(online_art, 'fetch', return_value=self.wide) as fetch:
            [first] = online_art.enrich_games([game], self.cache)
            [second] = online_art.enrich_games([game], self.cache)
        self.assertEqual(first, second)
        self.assertEqual(first.poster, game.poster)
        self.assertEqual(first.fanart.parent, self.cache.parent / 'curated')
        self.assertEqual(first.fanart.read_bytes(), self.wide)
        hero = hero_catalog.match(game, hero_catalog.load())
        fetch.assert_called_once_with(hero.url, online_art.MAX_IMAGE_BYTES)

    def test_extensionless_factorio_url_works_for_steam_and_standalone(self):
        poster = self.root / 'portrait.jpg'
        steam = discovery.Game('Steam', '427520', 'Factorio', 'unused', poster=poster)
        standalone_game = discovery.Game('Standalone', 'test', 'Factorio', 'unused', poster=poster)
        heroes = hero_catalog.load()
        hero = hero_catalog.match(steam, heroes)
        self.assertTrue(hero.url.startswith('https://assets.nintendo.com/image/upload/'))
        self.assertEqual(hero_catalog.match(standalone_game, heroes), hero)
        with patch.object(online_art, 'fetch', return_value=self.wide) as fetch:
            first = online_art.enrich_game(steam, self.cache, heroes)
            second = online_art.enrich_game(standalone_game, self.cache, heroes)
        self.assertEqual(first.fanart, second.fanart)
        self.assertEqual(second.fanart.read_bytes(), self.wide)
        fetch.assert_called_once_with(hero.url, online_art.MAX_IMAGE_BYTES)

    def test_hades_png_hero_is_cached_as_png(self):
        game = discovery.Game('Steam', '1145360', 'Hades', 'unused', poster=self.root / 'tile.jpg')
        hero = hero_catalog.match(game, hero_catalog.load())
        self.assertTrue(hero.url.endswith('/Hades_Launch.png?w=1920&q=80'))
        png = b'\x89PNG\r\n\x1a\nimage\0\0\0\0IEND\xaeB`\x82'
        with patch.object(online_art, 'fetch', return_value=png) as fetch:
            [first] = online_art.enrich_games([game], self.cache)
            [second] = online_art.enrich_games([game], self.cache)
        self.assertEqual(first, second)
        self.assertEqual(first.poster, game.poster)
        self.assertEqual(first.fanart.suffix, '.png')
        self.assertEqual(first.fanart.read_bytes(), png)
        fetch.assert_called_once_with(hero.url, online_art.MAX_IMAGE_BYTES)

    def test_sekiro_webp_hero_is_downloaded_and_cached(self):
        game = discovery.Game(
            'Standalone', 'test', 'Sekiro', 'unused', poster=self.root / 'tile.jpg'
        )
        hero = hero_catalog.match(game, hero_catalog.load())
        self.assertEqual(hero.steam_app_id, '814380')
        body = b'WEBPVP8L\x04\0\0\0data'
        webp = b'RIFF' + len(body).to_bytes(4, 'little') + body
        self.assertIsNone(online_art.image_suffix(webp[:-1]))
        with patch.object(online_art, 'fetch', return_value=webp) as fetch:
            [first] = online_art.enrich_games([game], self.cache)
            [second] = online_art.enrich_games([game], self.cache)
        self.assertEqual(first, second)
        self.assertEqual(first.poster, game.poster)
        self.assertEqual(first.fanart.suffix, '.webp')
        self.assertEqual(first.fanart.read_bytes(), webp)
        fetch.assert_called_once_with(hero.url, online_art.MAX_IMAGE_BYTES)

    def test_curated_hero_endpoint_is_cached_by_response_format(self):
        game = discovery.Game(
            'Standalone', 'test', 'Example Game', 'unused', poster=self.root / 'tile.jpg'
        )
        url = 'https://example.com/img.php?src=wallpaper.jpg&height=506'
        self.assertTrue(hero_catalog.is_hero_url(url))
        hero = hero_catalog.Hero(('example game',), url)
        with patch.object(online_art, 'fetch', return_value=self.wide) as fetch:
            resolved = online_art.enrich_game(game, self.cache, (hero,))
        fetch.assert_called_once_with(url, online_art.MAX_IMAGE_BYTES)
        self.assertEqual(resolved.fanart.suffix, '.jpg')
        self.assertEqual(resolved.fanart.read_bytes(), self.wide)

    def test_unavailable_curated_hero_keeps_steam_fallback(self):
        game = discovery.Game(
            'Steam',
            '1172710',
            'Dune: Awakening',
            'unused',
            poster=self.root / 'portrait.jpg',
            fanart=self.root / 'steam.jpg',
        )
        with patch.object(online_art, 'fetch', return_value=None):
            [resolved] = online_art.enrich_games([game], self.cache)
        self.assertEqual(resolved, game)

    def test_ambiguous_title_never_downloads_artwork(self):
        game = discovery.Game('Standalone', '1', 'Ambiguous', 'unused')
        data = json.dumps(
            {
                'items': [
                    {'type': 'app', 'id': 1, 'name': 'Ambiguous'},
                    {'type': 'app', 'id': 2, 'name': 'AMBIGUOUS'},
                ]
            }
        ).encode()
        with patch.object(online_art, 'fetch', return_value=data) as fetch:
            self.assertEqual(online_art.enrich_game(game, self.cache), game)
        fetch.assert_called_once()
        self.assertFalse(self.cache.exists())

    def test_steam_app_id_skips_title_search_and_keeps_existing_art(self):
        poster = self.root / 'existing.jpg'
        game = discovery.Game('Steam', '42', 'Test', 'unused', poster=poster)
        with patch.object(online_art, 'fetch', side_effect=[None, None, self.wide]) as fetch:
            enriched = online_art.enrich_game(game, self.cache)
        self.assertEqual(enriched.poster, poster)
        self.assertEqual(enriched.fanart.read_bytes(), self.wide)
        self.assertEqual(
            [args.args[0].rsplit('/', 1)[-1] for args in fetch.call_args_list[1:]],
            ['library_hero_2x.jpg', 'library_hero.jpg'],
        )

    def test_existing_small_cover_still_works_without_full_size_art(self):
        small = self.cache / '42' / 'landscape.jpg'
        small.parent.mkdir(parents=True)
        small.write_bytes(self.wide)
        game = discovery.Game('Steam', '42', 'Test', 'unused', poster=self.root / 'existing.jpg')
        with patch.object(online_art, 'fetch', return_value=None) as fetch:
            enriched = online_art.enrich_game(game, self.cache)
        self.assertEqual(enriched.fanart, small)
        self.assertEqual(fetch.call_count, 3)

    def test_screenshot_replaces_existing_steam_hero_but_not_tile_poster(self):
        hero = self.root / 'hero.jpg'
        poster = self.root / 'portrait.jpg'
        game = discovery.Game('Steam', '42', 'Test', 'unused', poster=poster, fanart=hero)
        url = 'https://cdn.cloudflare.steamstatic.com/steam/apps/42/ss_ab.jpg'
        details = self.screenshot_metadata('42', url)
        with patch.object(online_art, 'fetch', side_effect=[details, self.wide]):
            result = online_art.enrich_game(game, self.cache)
        self.assertEqual(result.poster, poster)
        self.assertEqual(result.fanart, self.cache / '42' / 'screenshot.jpg')

    def test_screenshot_on_unrelated_host_is_not_fetched(self):
        game = discovery.Game(
            'Steam',
            '42',
            'Test',
            'unused',
            poster=self.root / 'portrait.jpg',
            fanart=self.root / 'hero.jpg',
        )
        details = self.screenshot_metadata('42', 'https://example.com/steam/apps/42/ss_ab.jpg')
        with patch.object(online_art, 'fetch', return_value=details) as fetch:
            self.assertEqual(online_art.enrich_game(game, self.cache), game)
        fetch.assert_called_once()

    def test_non_image_response_is_not_cached(self):
        game = discovery.Game('Steam', '42', 'Test', 'unused')
        with patch.object(online_art, 'fetch', return_value=b'<html>not an image</html>'):
            self.assertEqual(online_art.enrich_game(game, self.cache), game)
        self.assertFalse(self.cache.exists())


class KodiTests(unittest.TestCase):
    def setUp(self):
        offline = patch.object(online_art, 'fetch', return_value=None)
        offline.start()
        self.addCleanup(offline.stop)
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
            {
                'icon': 'xbox.png',
                'thumb': 'xbox.png',
                'poster': 'xbox.png',
                'fanart': self.addon.BLANK_FANART,
            }
        )
        game_bar.setArt.assert_called_once_with(
            {
                'icon': 'bar.png',
                'thumb': 'bar.png',
                'poster': 'bar.png',
                'fanart': self.addon.BLANK_FANART,
            }
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
        self.assertEqual(
            self.modules['xbmcgui'].ListItem.return_value.setArt.call_args.args[0]['fanart'],
            self.addon.BLANK_FANART,
        )
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

    def test_listing_supplies_distinct_backdrop_or_blank_and_keeps_poster(self):
        with_fanart, without_fanart, missing_file = Mock(), Mock(), Mock()
        self.modules['xbmcgui'].ListItem.side_effect = [
            Mock(),
            Mock(),
            with_fanart,
            without_fanart,
            missing_file,
        ]
        hero = self.profile / 'hero.jpg'
        hero.touch()
        games = (
            catalog.GameTile('Steam:1', 'First', 'Steam', 'cover.png', True, str(hero)),
            catalog.GameTile('Steam:2', 'Second', 'Steam'),
            catalog.GameTile('Steam:3', 'Third', 'Steam', fanart=str(self.profile / 'missing.jpg')),
        )
        self.addon.show_games(1, PLUGIN_URL, catalog.Catalog(games, {}))
        with_fanart.setArt.assert_called_once_with(
            {'thumb': 'cover.png', 'icon': 'cover.png', 'poster': 'cover.png', 'fanart': str(hero)}
        )
        self.assertTrue(Path(self.addon.BLANK_FANART).is_file())
        for item in (without_fanart, missing_file):
            self.assertEqual(item.setArt.call_args.args[0]['fanart'], self.addon.BLANK_FANART)

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
