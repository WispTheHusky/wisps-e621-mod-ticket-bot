from support import app as bot, SOURCE
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data=Path(folder.name)/'data'
        setting=patch.object(bot,'DATA',self.data)
        setting.start();self.addCleanup(setting.stop)

    def test_dpapi_round_trip_and_logout_delete_only_this_account(self):
        bot.save_key('TestUser','synthetic-local-api-key')
        bot.save_key('AnotherUser','different-synthetic-key')
        path=bot.credential_path('TestUser')
        encrypted=path.read_bytes()
        self.assertNotIn(b'synthetic-local-api-key',encrypted)
        self.assertEqual(bot.read_key('TestUser'),'synthetic-local-api-key')
        bot.delete_key('TestUser')
        self.assertIsNone(bot.read_key('TestUser'))
        self.assertEqual(bot.read_key('AnotherUser'),'different-synthetic-key')

    def test_corrupt_credential_fails_instead_of_returning_garbage(self):
        bot.atomic_write(bot.credential_path('TestUser'),b'not-encrypted-data')
        with self.assertRaises(OSError):bot.read_key('TestUser')

    def test_failed_atomic_save_preserves_previous_credential(self):
        bot.save_key('TestUser','old-synthetic-key')
        with patch.object(bot.os,'replace',side_effect=OSError('fixture')):
            with self.assertRaises(OSError):bot.save_key('TestUser','new-synthetic-key')
        self.assertEqual(bot.read_key('TestUser'),'old-synthetic-key')

    def test_credential_name_cannot_escape_local_folder(self):
        self.assertEqual(bot.credential_path('../outside').parent,self.data/'credentials')

    def test_runtime_has_no_profile_or_credential_manager_fallback(self):
        text=SOURCE.read_text(encoding='utf-8')
        for forbidden in ('LOCALAPPDATA','APPDATA','Path.home(', 'CredReadW','CredWriteW','CredDeleteW','SND_FILENAME'):
            self.assertNotIn(forbidden,text)

    def test_frozen_startup_points_at_executable(self):
        with patch.object(bot.sys,'frozen',True,create=True),patch.object(bot.sys,'executable',r'C:\Portable Folder\E621TicketBot.exe'):
            self.assertEqual(bot.startup_command(),'"C:\\Portable Folder\\E621TicketBot.exe" --background')

    def test_readonly_storage_error_does_not_fall_back_elsewhere(self):
        with patch.object(bot,'atomic_write',side_effect=PermissionError('fixture')):
            with self.assertRaises(PermissionError):bot.load_config()
        self.assertFalse(self.data.exists())
