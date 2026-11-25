import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from native_recovery import archived_history, reconcile_previous_submissions
from videofactory_native import load, write


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.job = 'job_' + 'a' * 32
        self.prompt_id = 'afe0a3aa-0e97-4951-a777-f5653fbd9c55'
        self.case = root / 'jobs' / self.job
        self.case.mkdir(parents=True)
        self.output = root / 'output'
        self.output.mkdir()
        self.prefix = 'projects/project/shot/' + self.job
        self.graph = {'4': {'class_type': 'VideoFactoryLocalH3IRCompile'},
                      '13': {'class_type': 'SaveVideo', 'inputs': {'filename_prefix': self.prefix}}}
        write(self.case / 'submitted_prompt.json', {'client_id': self.job, 'prompt': self.graph})
        write(self.case / 'submission_response.json', {'prompt_id': self.prompt_id})
        write(self.case / 'submission-intent.json', {'at': '2026-01-01T00:00:00+00:00'})
        self.worker_started = (self.case / 'submission-intent.json').stat().st_mtime + 60
        self.queue = {'queue_running': [], 'queue_pending': []}
        self.history = {}
        self.runtime = Mock(config={'runtime_root': str(root), 'output_root': str(self.output)})
        self.runtime.comfy.side_effect = lambda route: self.queue if route == '/queue' else self.history

    def test_restart_classifies_old_job_only_after_verified_replacement(self):
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            reconcile_previous_submissions(self.runtime, comfy_started_at=self.worker_started - 120)
        archive, interruptions = reconcile_previous_submissions(self.runtime, comfy_started_at=self.worker_started)
        self.assertEqual(archive, {})
        receipt = interruptions[self.prompt_id]
        self.assertEqual(receipt['outcome'], 'interrupted_no_output')
        self.assertEqual(receipt['job_id'], self.job)
        self.assertEqual(receipt['graph_sha256'], hashlib.sha256(json.dumps(self.graph, sort_keys=True).encode()).hexdigest())
        self.assertEqual(load(self.case / 'restart-recovery.json'), receipt)
        self.assertFalse((self.case / 'terminal-history.json').exists())

    def test_existing_output_prevents_retry(self):
        output = self.output / (self.prefix + '_00001_.mp4')
        output.parent.mkdir(parents=True)
        output.write_bytes(b'existing original')
        _, interruptions = reconcile_previous_submissions(self.runtime, comfy_started_at=self.worker_started)
        self.assertEqual(interruptions[self.prompt_id]['outcome'], 'output_preserved')
        self.assertEqual(output.read_bytes(), b'existing original')

    def test_terminal_history_wins_and_can_be_served_after_worker_restart(self):
        self.history[self.prompt_id] = {'prompt': [1, self.prompt_id, self.graph, {'client_id': self.job}],
                                       'status': {'status_str': 'success', 'completed': True}}
        archive, interruptions = reconcile_previous_submissions(self.runtime)
        self.assertEqual(interruptions, {})
        self.assertEqual(archived_history(archive[self.prompt_id], self.prompt_id), self.history[self.prompt_id])
        self.assertIs(load(self.case / 'supervisor-result.json')['terminal_verified'], True)

    def test_unknown_or_busy_queue_does_not_requeue(self):
        self.queue['queue_running'] = [[1, self.prompt_id]]
        with self.assertRaisesRegex(ValueError, 'busy'):
            reconcile_previous_submissions(self.runtime, comfy_started_at=self.worker_started)
        self.queue['queue_running'].clear()
        (self.case / 'submission_response.json').unlink()
        _, interruptions = reconcile_previous_submissions(self.runtime, comfy_started_at=self.worker_started)
        self.assertEqual(interruptions, {})
        self.assertEqual(load(self.case / 'restart-recovery.json')['outcome'], 'submission_unverified')


if __name__ == '__main__':
    unittest.main()
