import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import cc


def config(path):
    return SimpleNamespace(instances_file=Path(path) / 'instances.json',
                           cc_binary=sys.executable, port=4000)


def slot(number='1'):
    return SimpleNamespace(slot=number, local_name='mock',
                           context_window=None, protocol='openai', upstream_model_id='mock')


def launch(cfg, number='1'):
    # exec is the OS boundary: leave the test process running instead of replacing it.
    with patch.object(os, 'execvpe'):
        cc.spawn(cfg, slot(number), None, [])


def concurrent_launch(path, ready, release):
    os.environ['HOME'] = path
    launch(config(path))
    ready.put(os.getpid())
    release.wait(10)


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config(self.tmp.name)
        self.home = patch.dict(os.environ, {'HOME': self.tmp.name})
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def test_same_slot_and_other_slot_are_all_visible_and_stopped(self):
        with patch.object(os, 'kill'), patch.object(os, 'getpid', return_value=101):
            launch(self.cfg)
        with patch.object(os, 'kill'), patch.object(os, 'getpid', return_value=102):
            launch(self.cfg)
        with patch.object(os, 'kill'), patch.object(os, 'getpid', return_value=103):
            launch(self.cfg, '2')
        with patch.object(os, 'kill') as kill:
            self.assertEqual(len(cc.list_instances(self.cfg)), 3)
            self.assertTrue(cc.stop(self.cfg, '1'))
            self.assertEqual([row['slot'] for row in cc.list_instances(self.cfg).values()], ['2'])
            self.assertEqual(cc.stop_all(self.cfg), 1)
            self.assertEqual(cc.list_instances(self.cfg), {})
            signaled = [call.args[0] for call in kill.call_args_list if call.args[1] != 0]
            self.assertCountEqual(signaled, [101, 102, 103])

    def test_legacy_dead_records_failed_stop_and_self_protection(self):
        self.cfg.instances_file.write_text(json.dumps({
            '1': {'slot': '1', 'pid': 101}, '2': {'slot': '2', 'pid': 102}}))

        def kill(pid, sig):
            if pid == 102:
                raise ProcessLookupError()
            if sig:
                raise PermissionError()

        with patch.object(os, 'kill', side_effect=kill):
            self.assertEqual(list(cc.list_instances(self.cfg)), ['101'])
            self.assertFalse(cc.stop(self.cfg, '1'))
            self.assertEqual(len(cc.list_instances(self.cfg)), 1)
        with patch.object(os, 'kill') as kill_mock, patch.object(os, 'getpid', return_value=101):
            self.assertEqual(cc.stop_all(self.cfg), 0)
            self.assertTrue(all(call.args[1] == 0 for call in kill_mock.call_args_list))

    def test_concurrent_processes_do_not_lose_registrations(self):
        ctx = multiprocessing.get_context('spawn')
        ready, release = ctx.Queue(), ctx.Event()
        workers = [ctx.Process(target=concurrent_launch, args=(self.tmp.name, ready, release))
                   for _ in range(6)]
        try:
            for worker in workers:
                worker.start()
            pids = [ready.get(timeout=10) for _ in workers]
            self.assertCountEqual([row['pid'] for row in cc.list_instances(self.cfg).values()], pids)
        finally:
            release.set()
            for worker in workers:
                worker.join(timeout=10)
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
            ready.close()

    def test_failed_write_preserves_previous_instances(self):
        with patch.object(os, 'kill'), patch.object(os, 'getpid', return_value=101):
            launch(self.cfg)
        with patch.object(os, 'kill'), patch.object(os, 'getpid', return_value=102), \
                patch.object(os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                launch(self.cfg)
        with patch.object(os, 'kill'):
            self.assertEqual([row['pid'] for row in cc.list_instances(self.cfg).values()], [101])
