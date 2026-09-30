from support import fixture_config
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from ticket_bot import State, MonitorError
from ticket_bot import format_last_ticket_found


class LastTicketTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)/'state.json'
        self.state = State(self.path, 'TestUser')

    def test_baseline_has_no_invented_time(self):
        self.state.accept({1, 2})
        self.assertIsNone(self.state.last_ticket_found)
        self.assertIsNone(State(self.path, 'TestUser').last_ticket_found)

    def test_new_batch_persists_aware_detection_time(self):
        self.state.accept({1})
        instant = datetime(2026, 9, 29, 15, 30, 12, tzinfo=timezone.utc)
        with patch('ticket_bot.datetime') as clock:
            clock.now.return_value = instant
            self.state.accept({1, 2, 3})
            clock.now.assert_called_once_with(timezone.utc)
        self.assertEqual(self.state.last_ticket_found, instant)
        self.assertEqual(State(self.path, 'TestUser').last_ticket_found, instant)
        self.assertEqual(json.loads(self.path.read_text())['last_ticket_found'], instant.isoformat())

    def test_empty_existing_and_visual_ack_preserve_time(self):
        self.state.accept(set())
        self.state.accept({1})
        previous = self.state.last_ticket_found
        self.state.accept(set())
        self.state.accept({1})
        self.state.acknowledge_visuals(1)
        self.assertEqual(State(self.path, 'TestUser').last_ticket_found, previous)

    def test_legacy_state_loads_without_making_up_timestamp(self):
        self.path.write_text(json.dumps({'version':1, 'username':'TestUser', 'initialized':True,
                                         'seen_ids':[1], 'pending_visuals':2}))
        state = State(self.path, 'TestUser')
        self.assertIsNone(state.last_ticket_found)
        state.accept({1})
        self.assertIsNone(state.last_ticket_found)
        self.assertEqual(state.pending, 2)

    def test_naive_saved_time_rejected(self):
        self.state.accept(set())
        data = json.loads(self.path.read_text())
        data['last_ticket_found'] = '2026-09-29T15:30:12'
        self.path.write_text(json.dumps(data))
        with self.assertRaises(MonitorError):
            State(self.path, 'TestUser')

    def test_failed_write_does_not_advance_timestamp(self):
        self.state.accept(set())
        with patch('ticket_bot.os.replace', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                self.state.accept({1})
        self.assertIsNone(self.state.last_ticket_found)

    def test_friendly_empty_and_local_display(self):
        self.assertEqual(format_last_ticket_found(None), 'No tickets found since launch.')
        value = Mock()
        value.astimezone.return_value = datetime(2026,9,29,16,30,12,tzinfo=timezone(timedelta(hours=1)))
        self.assertEqual(format_last_ticket_found(value), 'Last Ticket Found: 29/09/26, 16:30:12')
        value.astimezone.assert_called_once_with()
