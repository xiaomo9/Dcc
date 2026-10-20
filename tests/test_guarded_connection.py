import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from test_telemetry import proxy


class GuardedConnectionTests(unittest.TestCase):
    def test_late_response_is_closed_after_soft_timeout(self):
        allow_response = threading.Event()
        response_ready = threading.Event()
        closed = threading.Event()
        response = MagicMock()
        response.status_code = 200
        response.close.side_effect = closed.set
        response.iter_lines.return_value = iter([b'data: first'])

        def delayed_post(*args, **kwargs):
            allow_response.wait(1)
            response_ready.set()
            return response

        with patch.object(proxy.requests, 'post', side_effect=delayed_post), \
                patch.object(proxy, '_log'):
            with self.assertRaises(proxy._SoftTimeout):
                proxy._guarded_connect('http://upstream', {}, {}, soft_timeout=0.01,
                                       decode_unicode=False, req_id='late', protocol='openai')
            allow_response.set()
            self.assertTrue(response_ready.wait(1))
            self.assertTrue(closed.wait(1), 'late response remains open')
        self.assertEqual(response.close.call_count, 1)

    def test_response_handoff_race_closes_once(self):
        for delay in (0, 0.005, 0.015):
            with self.subTest(delay=delay):
                response = MagicMock()
                response.status_code = 200
                response.iter_lines.return_value = iter([b'data: first'])

                def post(*args, **kwargs):
                    time.sleep(delay)
                    return response

                with patch.object(proxy.requests, 'post', side_effect=post), \
                        patch.object(proxy, '_log'):
                    try:
                        conn = proxy._guarded_connect('http://upstream', {}, {}, soft_timeout=0.01,
                                                    decode_unicode=False, req_id='race', protocol='openai')
                    except proxy._SoftTimeout:
                        deadline = time.monotonic() + 1
                        while response.close.call_count == 0 and time.monotonic() < deadline:
                            time.sleep(0.001)
                        self.assertEqual(response.close.call_count, 1)
                    else:
                        with conn.response:
                            self.assertEqual(list(conn.iter_all()), [b'data: first'])
