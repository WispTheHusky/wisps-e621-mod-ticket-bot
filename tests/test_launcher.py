from support import app as bot
import argparse
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch,MagicMock


class LauncherTests(unittest.TestCase):
    def test_manual_errors_have_dialog(self):
        with patch('ticket_bot.tk.Tk') as root,patch('ticket_bot.messagebox.showerror') as dialog:
            bot.report_error('Safe startup error',argparse.Namespace(gui_errors=True,background=False))
            root.return_value.withdraw.assert_called_once()
            dialog.assert_called_once()
            root.return_value.destroy.assert_called_once()

    def test_signin_errors_remain_silent_without_dialog(self):
        with patch('ticket_bot.messagebox.showerror') as dialog,patch('builtins.print') as output:
            bot.report_error('Safe startup error',argparse.Namespace(gui_errors=True,background=True))
            dialog.assert_not_called()
            output.assert_called_once_with('Safe startup error')

    def test_startup_uses_current_pythonw_and_single_source(self):
        command=bot.startup_command()
        self.assertEqual(command,subprocess.list2cmdline([str(Path(bot.sys.executable).with_name('pythonw.exe')),str(Path(bot.__file__).resolve()),'--background']))
        self.assertNotIn('powershell',command.lower())
        self.assertNotIn('run.ps1',command)

    def test_startup_management_only_updates_own_entry(self):
        key=MagicMock()
        with patch('ticket_bot.winreg.CreateKey') as create,patch('ticket_bot.winreg.SetValueEx') as save,patch('ticket_bot.winreg.DeleteValue') as delete,patch('ticket_bot.winreg.QueryValueEx',return_value=('fixture',1)),patch('ticket_bot.startup_command',return_value='fixture-command'):
            create.return_value.__enter__.return_value=key
            self.assertTrue(bot.configure_startup('enable'))
            save.assert_called_once_with(key,'E621TicketBot',0,bot.winreg.REG_SZ,'fixture-command')
            bot.configure_startup('disable')
            delete.assert_called_once_with(key,'E621TicketBot')
