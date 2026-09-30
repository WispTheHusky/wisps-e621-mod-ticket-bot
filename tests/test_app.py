from support import fixture_config
"""Exercise batch dispatch using fake desktop services and real durable state."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import ticket_bot as bot
from ticket_bot import State


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target
    def start(self):
        self.target()


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = fixture_config()
        self.patches = [patch("ticket_bot.InstanceLock"), patch("ticket_bot.Halo"),
                        patch("ticket_bot.tk.Tk"), patch("ticket_bot.tk.StringVar"), 
                          patch("ticket_bot.signal.signal"),
                        patch("ticket_bot.sound"), patch("ticket_bot.toast"),
                        patch("ticket_bot.threading.Thread", ImmediateThread)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        bot.tk.StringVar.side_effect = lambda *args, **kwargs: Mock()
        stop_patch = patch("ticket_bot.StopSignal")
        stop_patch.start().return_value.is_set.return_value = False
        self.addCleanup(stop_patch.stop)
        for target in ('ticket_bot.Dashboard', 'ticket_bot.WindowControls'):
            item = patch(target)
            item.start()
            self.addCleanup(item.stop)
        args = argparse.Namespace(test=True, delay=1, simulate_fullscreen=0)
        self.app = bot.App(self.config, args)
        self.app.state = State(Path(self.temp.name) / "state.json", self.config["username"])
        self.app.state.accept(set())
        self.app.blocked = Mock(return_value=False)

    def scan(self, ids):
        self.app.events.put(("snapshot", ids))
        self.app.next_gate = 0
        self.app.tick()

    def test_multi_ticket_poll_is_one_sound_toast_and_halo(self):
        self.app.gate.safe_checks = 1
        self.scan({10, 11, 12})
        bot.sound.assert_called_once()
        bot.toast.assert_called_once_with(3, test=False)
        self.app.halo.show.assert_called_once()
        self.app.tick()
        self.assertEqual(self.app.state.pending, 0)
        self.scan({10, 11, 12})
        self.assertEqual(bot.sound.call_count, 1)
        self.assertEqual(bot.toast.call_count, 1)

    def test_new_ticket_details_exclude_baseline_existing_and_test(self):
        self.app.blocked.return_value=True
        self.app.state.initialized=False
        self.scan({1:'baseline'})
        self.assertEqual(self.app.recent.rows,[])
        self.scan({1:'baseline changed',2:'new reason',3:None})
        self.assertEqual([(r[0],r[1]) for r in self.app.recent.rows],[(3,None),(2,'new reason')])
        self.scan({1:'old',2:'unchanged',3:None})
        self.app.test_alert()
        self.assertEqual(len(self.app.recent.rows),2)
        self.assertIn('new reason',self.app.state.path.read_text())

    def test_ticket_test_settles_inflight_real_result_then_restores_without_fake_writes(self):
        class Value:
            def __init__(self,value):self.value=value
            def get(self):return self.value
            def set(self,value):self.value=value
        for name in ('status','last','last_found','new_count'):
            setattr(self.app,name,Value('real '+name))
        self.app.schedule.enabled=True
        self.app.schedule.inflight=True
        self.app.blocked.return_value=True
        self.app.request_ticket_test()
        self.app.tick()
        self.assertIsNone(self.app.ticket_test)
        self.app.events.put(('scan_result',{101:'genuine fixture returned during pause'}))
        self.app.tick()
        self.assertIn(101,self.app.state.seen)
        self.app.sound_gate.available_at=0
        self.app.tick()
        self.assertIsNotNone(self.app.ticket_test)
        before=self.app.state.path.read_bytes()
        real_time=self.app.state.last_ticket_found
        real_pending=self.app.state.pending
        self.assertEqual(self.app.recent.rows[0][0],'TEST-621')
        old_test=self.app.ticket_test
        self.app.request_ticket_test()
        self.assertIs(self.app.ticket_test,old_test)
        self.app.ticket_test['until']=0
        self.app.tick()
        self.assertIsNone(self.app.ticket_test)
        self.assertEqual(self.app.recent.rows[0][0],101)
        self.assertEqual(self.app.state.path.read_bytes(),before)
        self.assertEqual(self.app.state.last_ticket_found,real_time)
        self.assertEqual(self.app.state.pending,real_pending)
        self.assertEqual(self.app.test_pending,0)
        self.assertFalse(self.app.schedule.testing)

    def test_ticket_test_preserves_terminal_and_prior_pause(self):
        self.app.schedule.paused=True
        self.app.schedule.terminal=True
        self.app.request_ticket_test()
        self.app.begin_ticket_test(100)
        self.app.finish_ticket_test(110)
        self.assertTrue(self.app.schedule.paused)
        self.assertTrue(self.app.schedule.terminal)
        self.assertFalse(self.app.schedule.ready(200))

    def test_interval_saved_atomically_without_changing_backoff(self):
        root=Path(self.temp.name)
        (root/'config.json').write_text(json.dumps({'poll_seconds':5,'other':'preserve'}))
        self.app.schedule.finish(100,retry_delay=120)
        with patch('ticket_bot.DATA',root):
            self.assertTrue(self.app.change_interval('15'))
        self.assertEqual(json.loads((root/'config.json').read_text()),{'poll_seconds':15,'other':'preserve'})
        self.assertEqual(self.app.schedule.deadline,220)

    def test_alert_options_are_independent_saved_and_take_effect(self):
        root=Path(self.temp.name)
        path=root/'config.json'
        path.write_text(json.dumps({'sound_enabled':True,'halo_enabled':True,'other':'keep'}))
        self.app.config.update(sound_enabled=True,halo_enabled=True)
        with patch('ticket_bot.DATA',root),patch('ticket_bot.stop_sound') as stop_sound:
            self.assertTrue(self.app.set_alert_option('halo_enabled',False))
            self.assertTrue(self.app.config['sound_enabled'])
            self.app.halo.hide.assert_called()
            self.assertTrue(self.app.set_alert_option('sound_enabled',False))
            stop_sound.assert_called_once()
            self.app.play_sound()
            bot.sound.assert_not_called()
            self.assertTrue(self.app.set_alert_option('sound_enabled',True))
            self.app.play_sound()
            bot.sound.assert_called_once()
        self.assertEqual(json.loads(path.read_text()),{'sound_enabled':True,'halo_enabled':False,'other':'keep'})

    def test_failed_alert_option_save_preserves_runtime_choice(self):
        root=Path(self.temp.name)
        (root/'config.json').write_text(json.dumps({'halo_enabled':True}))
        self.app.config['halo_enabled']=True
        with patch('ticket_bot.DATA',root),patch('ticket_bot.os.replace',side_effect=OSError),patch('ticket_bot.messagebox.showerror'):
            self.assertFalse(self.app.set_alert_option('halo_enabled',False))
        self.assertTrue(self.app.config['halo_enabled'])
        self.assertTrue(json.loads((root/'config.json').read_text())['halo_enabled'])

    def test_expired_fake_visual_completion_cannot_ack_real_count(self):
        self.app.state.accept({12},{12:'real'})
        pending=self.app.state.pending
        self.app.test_generation=4
        self.app.test_pending=1
        self.app.blocked.return_value=True
        self.app.events.put(('visual_done',(0,1,3)))
        self.app.tick()
        self.assertEqual(self.app.state.pending,pending)
        self.assertEqual(self.app.test_pending,1)

    def test_scan_worker_cannot_overlap_and_success_sets_identity(self):
        self.app.schedule.enabled=True
        self.app.client=Mock()
        self.app.client.snapshot.return_value={33:'fixture'}
        self.app.start_scan(100)
        self.app.start_scan(101)
        self.app.client.snapshot.assert_called_once()
        self.app.blocked.return_value=True
        self.app.tick()
        self.assertTrue(self.app.authenticated)
        from ticket_bot import MonitorError
        self.app.events.put(('scan_error',MonitorError('Access rejected',retryable=False)))
        self.app.tick()
        self.assertFalse(self.app.authenticated)
        self.assertTrue(self.app.schedule.terminal)

    def test_current_scan_worker_preserves_retry_after_and_normal_interval(self):
        self.app.schedule.enabled=True
        self.app.client=Mock()
        self.app.client.snapshot.side_effect=bot.MonitorError('rate limited',retry_after=180)
        with patch('ticket_bot.time.monotonic',return_value=100),patch('ticket_bot.random.uniform',return_value=1):
            self.app.start_scan(100)
            self.app.tick()
        self.assertEqual(self.app.schedule.deadline,280)
        self.assertTrue(self.app.schedule.backoff)
        self.app.client.snapshot.side_effect=None
        self.app.client.snapshot.return_value={}
        with patch('ticket_bot.time.monotonic',return_value=280):
            self.app.start_scan(280)
            self.app.tick()
        self.assertEqual(self.app.schedule.deadline,280+self.config['poll_seconds'])

    def test_terminal_scan_failure_never_establishes_a_baseline_or_retries(self):
        self.app.state=State(Path(self.temp.name)/'new-account.json','Fixture')
        self.app.schedule.enabled=True
        self.app.client=Mock()
        client=self.app.client
        client.snapshot.side_effect=bot.MonitorError('Authentication rejected.',retryable=False)
        self.app.start_scan(100)
        self.app.tick()
        client.snapshot.assert_called_once()
        self.assertFalse(self.app.state.initialized)
        self.assertFalse(self.app.schedule.ready(10**12))
        self.assertFalse(self.app.authenticated)

    def test_avatar_lookup_is_cached_not_per_poll(self):
        self.app.args.test=False
        self.app.authenticated=True
        with patch('ticket_bot.fetch_avatar',return_value=(None,'fixture unavailable')) as fetch,patch.object(self.app.stop,'wait',return_value=False):
            self.app.maybe_avatar()
            self.app.avatar_busy=False
            self.app.maybe_avatar()
            fetch.assert_called_once_with(self.config['username'],self.app.session_stop)
            self.app.avatar_attempt_at-=901
            self.app.maybe_avatar()
            self.assertEqual(fetch.call_count,2)

    def test_stale_fake_failure_does_not_delay_real_alerts(self):
        self.app.test_generation=7
        self.app.retry_visual_at=0
        self.app.events.put(('visual_failed',(0,1,6)))
        self.app.blocked.return_value=True
        self.app.tick()
        self.assertEqual(self.app.retry_visual_at,0)

    def test_cancelled_close_still_processes_new_tickets(self):
        self.app.blocked.return_value=True
        self.app.request_close()
        self.scan({6:'synthetic while confirmation visible'})
        self.assertTrue(self.app.confirming)
        self.assertFalse(self.app.stop.is_set())
        self.app.finish_close_confirmation(False)
        self.scan({7:'synthetic after cancel'})
        self.assertFalse(self.app.stop.is_set())
        self.assertEqual(self.app.recent.rows[0][0],7)

    def test_documented_stop_signal_does_not_prompt(self):
        self.app.stop_signal.is_set.return_value=True
        self.app.tick()
        self.app.dashboard.show_close_confirmation.assert_not_called()
        self.assertTrue(self.app.closing)
        self.assertTrue(self.app.stop.is_set())

    def test_invalid_intervals_show_inline_range_without_saving(self):
        previous=self.app.config['poll_seconds']
        deadline=self.app.schedule.deadline
        with patch('ticket_bot.os.replace') as save,patch('ticket_bot.messagebox.showerror') as popup:
            for raw in ('4','3601','1.5','','abc'):
                self.assertFalse(self.app.change_interval(raw))
                self.app.dashboard.show_interval_error.assert_called_with('Choose a whole number from 5 to 3600 seconds.')
            save.assert_not_called()
            popup.assert_not_called()
        self.assertEqual(self.app.config['poll_seconds'],previous)
        self.assertEqual(self.app.schedule.deadline,deadline)

    def test_interval_write_failure_is_inline_and_preserves_previous_value(self):
        root=Path(self.temp.name)
        path=root/'config.json'
        path.write_text(json.dumps({'poll_seconds':self.app.config['poll_seconds']}))
        original=path.read_bytes()
        previous=self.app.config['poll_seconds']
        with patch('ticket_bot.DATA',root),patch('ticket_bot.os.replace',side_effect=OSError),patch('ticket_bot.messagebox.showerror') as popup:
            self.assertFalse(self.app.change_interval('30'))
            self.app.dashboard.show_interval_error.assert_called_with('Could not save the refresh interval. Try again.')
            popup.assert_not_called()
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(self.app.config['poll_seconds'],previous)

    def test_fullscreen_batches_sound_now_visuals_combine_on_exit(self):
        self.app.blocked.return_value = True
        self.scan({10, 11})
        self.assertIsNotNone(self.app.state.last_ticket_found)
        self.app.last_found.set.assert_called_with(bot.format_last_ticket_found(self.app.state.last_ticket_found))
        self.app.sound_gate.available_at = 0  # next normal poll after sound finishes
        self.scan({10, 11, 12, 13, 14})
        self.assertEqual(bot.sound.call_count, 2)
        bot.toast.assert_not_called()
        self.app.halo.show.assert_not_called()
        self.assertEqual(self.app.state.pending, 5)
        self.app.blocked.return_value = False
        self.app.next_gate = 0
        self.app.tick()
        bot.toast.assert_not_called()
        self.app.next_gate = 0
        self.app.tick()
        bot.toast.assert_called_once_with(5, test=False)
        self.app.halo.show.assert_called_once()
        self.app.tick()
        self.assertEqual(self.app.state.pending, 0)

    def test_test_alert_and_error_do_not_change_last_ticket_time(self):
        self.app.blocked.return_value = True
        self.scan({1})
        previous = self.app.state.last_ticket_found
        self.app.test_alert()
        self.app.events.put(('status', 'Network failure; state unchanged.'))
        self.app.tick()
        self.assertEqual(self.app.state.last_ticket_found, previous)
        self.assertEqual(State(self.app.state.path, self.config['username']).last_ticket_found, previous)

    def test_default_poll_interval_and_no_endpoint_settings(self):
        self.assertTrue(5 <= self.config["poll_seconds"] <= 3600)
        self.assertEqual(self.config["halo_seconds"], 3)
        self.assertNotIn("endpoint", self.config)
        self.assertNotIn("method", self.config)

    def test_unsafe_config_rejected(self):
        original = dict(bot.DEFAULT_CONFIG)
        for extra in ({"endpoint": "https://evil.test"}, {"method": "POST"}):
            with self.assertRaises(ValueError):
                bot.validate_config(original | extra)

    def test_background_mode_hides_window(self):
        args = argparse.Namespace(test=True, delay=3600, simulate_fullscreen=0, background=True)
        app = bot.App(self.config, args)
        app.root.withdraw.assert_called_once()
        app.root.iconbitmap.assert_any_call(default=str(bot.resource_path('assets/e621-ticket-bot.ico')))
        app.root.iconbitmap.assert_any_call(str(bot.resource_path('assets/e621-ticket-bot.ico')))

    def test_missing_credential_opens_login_capable_window(self):
        with patch('ticket_bot.read_key', return_value=None), \
             patch('ticket_bot.sys.argv', ['ticket_bot.py', '--background']), patch('builtins.print'):
            bot.tk.Tk.reset_mock()
            self.assertEqual(bot.main(), 0)
            bot.tk.Tk.assert_called_once()

    def test_logged_out_startup_never_fetches_profile_or_starts_monitor(self):
        self.app.args.test=False
        with patch.object(self.app,'maybe_avatar') as avatar,patch('ticket_bot.read_key',return_value=None):
            self.app.startup_login()
        avatar.assert_not_called()
        self.assertFalse(self.app.schedule.enabled)
        self.assertFalse(self.app.authenticated)

    def test_login_validates_without_baseline_and_start_is_explicit(self):
        root=Path(self.temp.name)
        (root/'config.json').write_text(json.dumps({'username':'Fixture','poll_seconds':6}))
        self.app.args.test=False
        self.app.state=None
        self.app.config['username']='Fixture'
        with patch('ticket_bot.Client',autospec=True) as client,patch('ticket_bot.DATA',root),patch('ticket_bot.save_key') as save,patch.object(self.app,'maybe_avatar'):
            self.assertTrue(self.app.begin_login('Fixture','synthetic-key'))
            client.return_value.page.assert_called_once_with(None)
            client.return_value.snapshot.assert_not_called()
            self.app.tick()
            save.assert_called_once_with('Fixture','synthetic-key')
            self.assertTrue(self.app.authenticated)
            self.assertFalse(self.app.schedule.enabled)
            self.assertFalse(self.app.state.initialized)
            self.app.login_overlay_active=False  # simulated completion of UI animation
            self.app.toggle_running()
            self.assertTrue(self.app.schedule.enabled)
            self.app.toggle_running()
            self.assertFalse(self.app.schedule.enabled)
        self.assertNotIn('synthetic-key',(root/'config.json').read_text())

    def test_invalid_login_does_not_save_or_enable(self):
        from ticket_bot import MonitorError
        with patch('ticket_bot.Client') as client,patch('ticket_bot.save_key') as save:
            client.return_value.page.side_effect=MonitorError('Rejected',retryable=False)
            self.app.begin_login('Fixture','synthetic-key')
            self.app.tick()
            save.assert_not_called()
        self.assertFalse(self.app.authenticated)
        self.assertFalse(self.app.login_busy)
        self.assertFalse(self.app.schedule.enabled)

    def test_logout_invalidates_pending_results_and_removes_saved_login(self):
        old=self.app.state
        original=old.path.read_bytes()
        self.app.authenticated=True
        self.app.avatar_image=object()
        self.app.schedule.enabled=True
        generation=self.app.session_id
        stop=self.app.session_stop
        with patch('ticket_bot.delete_key') as delete:
            self.app.logout()
            delete.assert_called_once_with(self.config['username'])
        self.app.events.put(('session',(generation,'scan_result',{99:'stale'})))
        self.app.events.put(('session',(generation,'login',(True,'Fixture','secret',Mock()))))
        self.app.tick()
        self.assertTrue(stop.is_set())
        self.assertFalse(self.app.authenticated)
        self.assertFalse(self.app.schedule.enabled)
        self.assertIsNone(self.app.state)
        self.assertIsNone(self.app.avatar_image)
        self.assertIsNone(self.app.avatar_attempt_at)
        self.assertEqual(old.path.read_bytes(),original)

    def test_session_total_accumulates_real_detections_and_excludes_tests(self):
        self.app.blocked.return_value=True
        self.scan({10:'first',11:'second'})
        self.scan({10:'first',11:'second',12:'third'})
        self.scan({10:'first',11:'second',12:'third'})
        self.assertEqual(self.app.session_counts[self.config['username']],3)
        self.app.new_count.set.assert_called_with('New Tickets Found This Session: 3')
        self.app.test_alert()
        self.assertEqual(self.app.session_counts[self.config['username']],3)

    def test_locked_actions_cannot_trigger_tests(self):
        self.app.args.test=False
        self.app.test_alert()
        self.app.request_ticket_test()
        self.assertFalse(self.app.test_requested)
        self.assertEqual(self.app.test_pending,0)

    def test_toast_wording_singular_and_plural(self):
        self.patches[-1].stop()  # subprocess needs real reader threads
        for count, expected in ((1, "1 new E621 moderator ticket found"),
                                (2, "2 new E621 moderator tickets found"),
                                (5, "5 new E621 moderator tickets found")):
            result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                     str(bot.resource_path('toast.ps1')), "-Count", str(count), "-FormatOnly"],
                                    capture_output=True, text=True, creationflags=0x08000000)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
