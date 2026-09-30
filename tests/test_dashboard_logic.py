from support import fixture_config
import queue
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import ticket_bot as bot
import ticket_bot as windows
from ticket_bot import RecentTickets, parse_page, MonitorError


class DashboardLogicTests(unittest.TestCase):
    def test_metadata_parse_preserves_multiline_and_missing_reason(self):
        self.assertEqual(parse_page([{'id':3,'status':'pending','reason':'First line\nSecond line'},
                                     {'id':2,'status':'pending'}]), {3:'First line\nSecond line',2:None})

    def test_recent_only_adds_new_ids_and_bounds_memory(self):
        recent = RecentTickets(limit=2)
        when = datetime.now(timezone.utc)
        recent.add({2,3}, {1:'old',2:'new\nreason',3:None}, when)
        self.assertEqual([r[0] for r in recent.rows], [3,2])
        self.assertEqual(recent.rows[1][1], 'new\nreason')
        recent.add({4}, {4:'a'*10001},when)
        self.assertEqual([r[0] for r in recent.rows],[4,3])
        self.assertIn('Display truncated',recent.rows[0][1])




    def test_close_cancel_keeps_monitor_alive_and_confirm_exits(self):
        app=bot.App.__new__(bot.App)
        app.closing=False
        app.confirming=False
        app.root=Mock()
        app.dashboard=Mock()
        app.close=Mock()
        app.request_close()
        self.assertTrue(app.confirming)
        app.close.assert_not_called()
        app.dashboard.show_close_confirmation.call_args.args[0](False)
        self.assertFalse(app.confirming)
        self.assertFalse(app.closing)
        app.close.assert_not_called()
        app.request_close()
        app.dashboard.show_close_confirmation.call_args.args[0](True)
        app.close.assert_called_once()
        app.finish_close_confirmation(True)
        app.close.assert_called_once()

    def test_close_confirmation_not_reentrant(self):
        app=bot.App.__new__(bot.App)
        app.closing=False
        app.confirming=False
        app.root=Mock()
        app.dashboard=Mock()
        app.close=Mock()
        app.request_close()
        app.request_close()
        app.dashboard.show_close_confirmation.assert_called_once()
        app.close.assert_not_called()

    def test_native_close_routing_x_blocked_shell_confirms_once(self):
        root, confirm, ending = Mock(), Mock(), Mock()
        controls=windows.WindowControls(root,confirm,ending)
        with patch('ticket_bot.def_subclass',return_value=20):
            self.assertEqual(controls.dispatch(1,0x84,0,0,621,0),0)
        self.assertEqual(controls.dispatch(1,0xA1,20,0,621,0),0)
        root.after_idle.assert_not_called()
        controls.dispatch(1,0x112,0xF060,0,621,0)
        controls.dispatch(1,0x10,0,0,621,0)
        root.after_idle.assert_called_once()
        controls.confirm_once()
        confirm.assert_called_once()
        self.assertEqual(controls.dispatch(1,0x11,0,0,621,0),1)
        controls.dispatch(1,0x16,1,0,621,0)
        root.after_idle.assert_called_with(ending)
