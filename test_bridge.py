import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
import bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = json.loads((bridge.ROOT / 'config.json').read_text())
        self.app = bridge.Bridge(Path(self.tmp.name), self.config)
        self.event = {'type': 'im.message.receive_v1', 'sender_id': self.config['owner_id'],
            'chat_id': self.config['chat_id'], 'sender_type': 'user', 'chat_type': 'p2p',
            'message_id': 'om_test1', 'create_time': str(int(time.time()*1000)),
            'message_type': 'text', 'content': '你好'}

    def tearDown(self):
        self.tmp.cleanup()

    def counts(self, table):
        with self.app.db() as db:
            return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]

    def test_only_bound_owner_private_chat(self):
        for key, value in [('sender_id', 'ou_stranger'), ('chat_id', 'oc_other'),
                           ('sender_type', 'bot'), ('chat_type', 'group'), ('type', 'other')]:
            self.assertFalse(self.app.accept(dict(self.event, **{key: value})))
        self.assertEqual(self.counts('jobs'), 0)
        self.assertEqual(self.counts('outbox'), 0)

    def test_duplicate_message_is_not_reexecuted(self):
        self.assertTrue(self.app.accept(self.event))
        self.assertFalse(self.app.accept(self.event))
        self.assertEqual(self.counts('jobs'), 1)
        self.assertEqual(self.counts('outbox'), 1)

    def test_old_or_invalid_event_rejected(self):
        for stamp in ['bad', None, str(int((time.time()-700)*1000)), str(int((time.time()+90)*1000))]:
            self.assertFalse(self.app.accept(dict(self.event, create_time=stamp)))
        self.assertEqual(self.counts('jobs'), 0)

    def test_nontext_and_oversized_do_not_invoke_model(self):
        self.app.accept(dict(self.event, message_type='image'))
        self.app.accept(dict(self.event, message_id='om_test2', content='a'*4001))
        self.assertEqual(self.counts('jobs'), 0)
        self.assertEqual(self.counts('outbox'), 2)

    def test_shell_input_is_only_data(self):
        text = '$(touch /tmp/should-not-exist); rm -rf /'
        self.app.accept(dict(self.event, content=text))
        with self.app.db() as db:
            self.assertEqual(db.execute('SELECT prompt FROM jobs').fetchone()[0], text)

    def test_cancel_stops_queued_jobs(self):
        self.app.accept(self.event)
        self.app.accept(dict(self.event, message_id='om_cancel', content='/cancel'))
        with self.app.db() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0], 'cancelled')
        self.assertTrue(self.app.cancel.is_set())

    def test_new_clears_conversation_but_preserves_quota(self):
        self.app.accept(self.event)
        self.app.accept(dict(self.event, message_id='om_new', content='/new'))
        self.assertEqual(self.app.meta('generation'), '2')
        with self.app.db() as db:
            self.assertEqual(db.execute('SELECT prompt FROM jobs').fetchone()[0], '')
        self.assertEqual(self.counts('jobs'), 1)

    def test_daily_limit(self):
        self.app.config['daily_limit'] = 1
        self.app.accept(self.event)
        self.app.accept(dict(self.event, message_id='om_second'))
        self.assertEqual(self.counts('jobs'), 1)

    def test_recover_does_not_repeat_running_task(self):
        self.app.accept(self.event)
        with self.app.db() as db:
            db.execute("UPDATE jobs SET status='running'")
        self.app.recover()
        self.app.recover()
        with self.app.db() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0], 'interrupted')
        self.assertEqual(self.counts('outbox'), 2)

    def test_reply_chunks_idempotent(self):
        self.app.enqueue_reply('long', '你'*6000)
        self.app.enqueue_reply('long', '你'*6000)
        self.assertEqual(self.counts('outbox'), 3)

    def test_no_agent_tools_or_user_config(self):
        args = bridge.codex_command(self.config, '/tmp/test')
        self.assertIn('--ignore-user-config', args)
        self.assertEqual(args[args.index('--sandbox')+1], 'read-only')
        for name in ['shell_tool', 'unified_exec', 'apps', 'plugins', 'code_mode_host', 'hooks', 'computer_use']:
            self.assertEqual(args[args.index(name)-1], '--disable')
        self.assertIn('mcp_servers={}', args)
        self.assertIn('web_search="disabled"', args)
        self.assertIn('tools.view_image=false', args)
        self.assertEqual(args[-1], '-')

    def test_send_uses_argv_not_shell(self):
        with patch('bridge.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"ok":true}'
            self.assertTrue(self.app.send({'text':'$(echo harmless)', 'id':'abc'}))
            self.assertNotIn('shell', run.call_args.kwargs)
            self.assertIn('$(echo harmless)', run.call_args.args[0])
            self.assertIn(self.config['chat_id'], run.call_args.args[0])

    def test_completed_answer_repaired_on_restart(self):
        self.app.accept(self.event)
        with self.app.db() as db:
            db.execute("UPDATE jobs SET status='completed',answer='你好！'")
        self.app.recover()
        self.app.recover()
        self.assertEqual(self.counts('outbox'), 2)

    def test_end_to_end_worker_sender_with_fake_model(self):
        self.app.accept(self.event)
        sent = []
        def fake_send(row):
            sent.append(row['text'])
            return True
        with patch.object(self.app, 'run_model', return_value=('测试回答', {'input_tokens': 1})), \
             patch.object(self.app, 'send', side_effect=fake_send):
            threads = [threading.Thread(target=f) for f in (self.app.worker, self.app.sender)]
            for thread in threads:
                thread.start()
            deadline = time.time()+5
            while '测试回答' not in sent and time.time()<deadline:
                time.sleep(0.05)
            self.app.stop.set()
            for thread in threads:
                thread.join(timeout=2)
        self.assertIn('测试回答', sent)
        with self.app.db() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0], 'completed')

    def test_failed_json_envelope_not_success(self):
        with patch('bridge.subprocess.run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = '{"ok":false}'
            self.assertFalse(self.app.send({'text':'test','id':'abc'}))


if __name__ == '__main__':
    unittest.main()
