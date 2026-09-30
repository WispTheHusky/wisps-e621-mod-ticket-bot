import support  # Load the canonical windowed source for the test runner.
import base64
import io
import json
import threading
import unittest
import urllib.error
from ticket_bot import Client, MonitorError, http_failure


def error(code, message):
    return urllib.error.HTTPError('https://e621.net/tickets.json', code, 'unused', {},
                                  io.BytesIO(json.dumps({'message': message}).encode()))


class AuthTests(unittest.TestCase):
    def test_basic_auth_encoding_matches_server_contract(self):
        client = Client({'username': 'TestUser'}, 'synthetic-key', threading.Event())
        scheme, encoded = client.headers['Authorization'].split(' ', 1)
        self.assertEqual(scheme, 'Basic')
        self.assertEqual(base64.b64decode(encoded), b'TestUser:synthetic-key')

    def test_explicit_auth_rejection_is_specific_and_not_retryable(self):
        result = http_failure(error(401, 'SessionLoader::AuthenticationFailure'))
        self.assertIn('username/API-key pair', str(result))
        self.assertFalse(result.retryable)

    def test_unknown_body_never_echoed_or_guessed(self):
        for body in ('sensitive server detail', {'private': 'value'}):
            result = http_failure(error(401, body))
            self.assertNotIn('sensitive', str(result))
            self.assertNotIn('private', str(result))
            self.assertNotIn('rejected the configured', str(result))
            self.assertFalse(result.retryable)

    def test_forbidden_is_not_mislabeled_as_invalid_key(self):
        result = http_failure(error(403, 'unknown'))
        self.assertIn('does not establish', str(result))
        self.assertFalse(result.retryable)

    def test_rate_limits_and_server_errors_still_retry(self):
        for code in (429, 500, 503):
            self.assertTrue(http_failure(error(code, 'unused')).retryable)

