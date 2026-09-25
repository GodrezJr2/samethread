import json
import os
import sqlite3
import tempfile
import unittest

TMP = tempfile.mkdtemp(prefix='hop-test-')
os.environ.update(HOP_HOME=os.path.join(TMP, 'hop'), HOME=TMP, USERPROFILE=TMP, CODEX_HOME=os.path.join(TMP, '.codex'),
                  HERMES_HOME=os.path.join(TMP, '.hermes'))

from samethread import core, sync  # noqa: E402
from samethread.agents import AGENTS, NAMES, resolve  # noqa: E402

CFG = dict(core.DEFAULT_CONFIG)


def thread(elements):
    th = sync.new_thread('agy:x', 'Auth refactor', os.getcwd())
    th['elements'] = elements
    return th


def e(id_, role, text, author='agy', kind='msg', ts=1790000000000):
    return dict(core.el(id_, role, text, ts, kind), author=author)


class Helpers(unittest.TestCase):
    def test_iso_ms_handles_every_timestamp_shape(self):
        self.assertEqual(core.iso_ms('2026-09-22T04:18:45Z'), 1790050725000)
        self.assertEqual(core.iso_ms('2026-09-23 05:42:48.2564+00:00'), core.iso_ms('2026-09-23T05:42:48.256400Z'))
        self.assertEqual(core.iso_ms(1790050725000), 1790050725000)
        self.assertEqual(core.iso_ms('garbage'), 0)

    def test_compact_json_matches_json_stringify(self):
        self.assertEqual(core.dumps({'subtype': 'custom_title', 'n': 1}), '{"subtype":"custom_title","n":1}')

    def test_titles_drop_old_tags_and_fall_back_to_first_prompt(self):
        self.assertEqual(sync.clean_title('Fix login (Agy+Qwen+Kimi)', []), 'Fix login')
        self.assertEqual(sync.clean_title('New session - 2026-09-23', [e('1', 'user', 'why is CI red?\nmore')]), 'why is CI red?')

    def test_fmt_call_prefers_the_meaningful_argument(self):
        self.assertEqual(core.fmt_call('Bash', {'command': 'ls -la', 'timeout': 5}, CFG), '▸ Bash: ls -la')
        self.assertEqual(core.fmt_call('shell', {'command': ['git', 'status']}, CFG), '▸ shell: git status')

    def test_agent_names_resolve(self):
        self.assertEqual(resolve('claude'), 'cc')
        self.assertEqual(resolve('MiniMax'), 'mmx')
        self.assertEqual(resolve('hermes'), 'hermes')
        self.assertEqual(len(NAMES), 10)


class Render(unittest.TestCase):
    def test_merges_turns_and_tags_the_source(self):
        th = thread([e('1', 'user', 'hi'), e('2', 'assistant', 'looking'), e('3', 'tool', '▸ ls'),
                     e('4', 'assistant', 'done'), e('5', 'user', 'thanks', author='kimi')])
        msgs = sync.render(th, CFG)
        self.assertEqual([m['role'] for m in msgs], ['user', 'assistant', 'user'])
        self.assertIn('Agy+Kimi', msgs[0]['text'])
        self.assertEqual(msgs[1]['text'], 'looking\n\n▸ ls\n\ndone')
        self.assertEqual(sync.thread_title(th), 'Auth refactor (Agy+Kimi)')

    def test_starts_from_the_latest_summary(self):
        th = thread([e('1', 'user', 'old'), e('2', 'assistant', 'old reply'),
                     e('3', 'user', 'what happened so far', kind='summary'), e('4', 'user', 'next')])
        msgs = sync.render(th, CFG)
        self.assertNotIn('old reply', ''.join(m['text'] for m in msgs))
        self.assertIn('[Summary of earlier conversation]', msgs[0]['text'])

    def test_drops_oldest_messages_past_the_budget(self):
        els = []
        for i in range(10):
            els += [e(f'u{i}', 'user', 'q' * 100), e(f'a{i}', 'assistant', 'a' * 100)]
        msgs = sync.render(thread(els), dict(CFG, max_chars=500))
        self.assertLessEqual(len(msgs), 7)
        self.assertIn('earlier messages were omitted', msgs[0]['text'])


class RoundTrip(unittest.TestCase):
    """Every writable agent must read back exactly what it wrote, with the tagged title."""

    def roundtrip(self, key, cwd):
        agent = AGENTS[key]
        th = thread([e('1', 'user', 'remember MANGO'), e('2', 'assistant', 'stored')])
        ctx = {'cfg': CFG}
        if key == 'kimi':
            ctx['kimi_bind'] = {'type': 'profile.bind', 'agentId': 'main', 'modelAlias': 'm', 'profileName': 'agent',
                                'thinkingEffort': 'high', 'systemPrompt': '', 'disallowedTools': []}
        res = agent.write(None, cwd, sync.render(th, CFG), 'Auth refactor (Agy)', ctx)
        sessions = agent.list(CFG)
        s = sessions[f'{key}:{res["id"]}']
        els = sync.load(s, CFG)
        self.assertEqual({x['id'] for x in els}, set(res['seen']), key)
        self.assertEqual(els[-1]['text'], 'stored', key)
        self.assertIn('remember MANGO', els[0]['text'], key)
        self.assertTrue(s['is_hop'], key)
        if key != 'oc':
            self.assertEqual(s.get('title') or '', 'Auth refactor (Agy)', key)
        return agent, res

    def test_claude(self):
        agent, res = self.roundtrip('cc', os.getcwd())
        with open(res['path'], encoding='utf-8') as f:
            self.assertIn('"customTitle":"Auth refactor (Agy)"', f.read())  # Claude's tail scan needs compact JSON

    def test_codex(self):
        self.roundtrip('codex', os.getcwd())

    def test_qwen(self):
        _, res = self.roundtrip('qwen', os.getcwd())
        with open(res['path'], encoding='utf-8') as f:
            self.assertIn('"subtype":"custom_title"', f.read())  # Qwen greps for this exact string

    def test_pi(self):
        self.roundtrip('pi', os.getcwd())

    def test_kimi(self):
        os.makedirs(os.path.join(TMP, '.kimi-code'), exist_ok=True)
        self.roundtrip('kimi', os.getcwd())

    def test_gemini_needs_a_registered_folder(self):
        cwd = os.getcwd()
        with self.assertRaises(core.HopError):
            AGENTS['gemini'].write(None, cwd, sync.render(thread([e('1', 'user', 'x'), e('2', 'assistant', 'y')]), CFG),
                                   't', {'cfg': CFG})
        reg = os.path.join(TMP, '.gemini', 'projects.json')
        os.makedirs(os.path.dirname(reg), exist_ok=True)
        with open(reg, 'w') as f:
            json.dump({'projects': {os.path.normpath(cwd).lower() if os.name == 'nt' else cwd: 'proj'}}, f)
        self.roundtrip('gemini', cwd)

    def test_claude_detects_a_continued_mirror(self):
        agent, res = self.roundtrip('cc', os.getcwd())
        with open(res['path'], encoding='utf-8') as f:
            last = [json.loads(line) for line in f if '"uuid"' in line][-1]
        with open(res['path'], 'a', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'user', 'uuid': 'u-new', 'parentUuid': last['uuid'], 'sessionId': res['id'],
                                'timestamp': '2026-09-23T06:00:00Z', 'message': {'role': 'user', 'content': 'codeword?'}}) + '\n')
            f.write(json.dumps({'type': 'assistant', 'uuid': 'a-new', 'parentUuid': 'u-new', 'sessionId': res['id'],
                                'timestamp': '2026-09-23T06:00:01Z',
                                'message': {'role': 'assistant', 'content': [
                                    {'type': 'thinking', 'thinking': '', 'signature': 'x'},
                                    {'type': 'text', 'text': 'MANGO'}]}}) + '\n')
        s = agent.list(CFG)[f'cc:{res["id"]}']
        new = [x for x in sync.load(s, CFG) if x['id'] not in set(res['seen'])]
        self.assertEqual([(x['role'], x['text']) for x in new], [('user', 'codeword?'), ('assistant', 'MANGO')])


class Antigravity(unittest.TestCase):
    def test_resume_other_uses_ide_store(self):
        import samethread.agents.antigravity as agy_mod
        old = agy_mod.OTHER_ROOT
        root = tempfile.mkdtemp(prefix='agy-other-')
        try:
            agy_mod.OTHER_ROOT = root
            os.makedirs(os.path.join(root, 'conversations'))
            open(os.path.join(root, 'conversations', 'other-id.pb'), 'wb').close()
            self.assertEqual(AGENTS['agy'].resume_cmd('other-id'),
                             ['agy', '--app_data_dir=antigravity', '--conversation', 'other-id'])
        finally:
            agy_mod.OTHER_ROOT = old
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_seed_targets_other_store(self):
        self.assertEqual(AGENTS['agy'].seed_cmd('continue'),
                         ['agy', '--app_data_dir=antigravity', '-i', 'continue'])

    def test_sync_agy_dry_run(self):
        from samethread.cli import cmd_sync_agy
        args = type('Args', (), {'dry_run': True})()
        cmd_sync_agy(args)

    def test_sync_agy_continues_after_a_failed_seed(self):
        from unittest.mock import patch
        from samethread import cli

        first = thread([e('1', 'user', 'first')])
        second = thread([e('2', 'user', 'second')])
        args = type('Args', (), {'dry_run': False})()
        with patch.object(cli, 'all_threads', return_value=[first, second]), \
             patch.object(cli, 'seed', side_effect=[1, 0]) as seed:
            cli.cmd_sync_agy(args)
        self.assertEqual(seed.call_count, 2)

    def test_resume_without_query_asks_for_a_chat(self):
        import io
        from unittest.mock import patch
        from samethread import cli

        first = thread([e('1', 'user', 'first')])
        first['updated'] = 200
        second = thread([e('2', 'user', 'second')])
        second['updated'] = 100
        second['base_title'] = 'Second chat'

        class TTYInput(io.StringIO):
            def isatty(self):
                return True

        args = type('Args', (), {'agent': 'agy', 'query': None, 'all': False})()
        with patch.object(cli, 'scoped', return_value=[first, second]), \
             patch.object(cli, 'seed') as seed, \
             patch('sys.stdin', TTYInput('2\n')), \
             self.assertRaises(SystemExit) as exit:
            cli.cmd_resume(args)

        self.assertIn('Second chat', str(exit.exception))
        seed.assert_called_once()
        self.assertEqual(seed.call_args.args[1]['base_title'], 'Second chat')

    def test_resume_without_query_does_not_guess_when_not_interactive(self):
        import io
        from unittest.mock import patch
        from samethread import cli

        args = type('Args', (), {'agent': 'agy', 'query': None, 'all': False})()
        with patch.object(cli, 'scoped', return_value=[thread([e('1', 'user', 'only')])]), \
             patch.object(cli.sys, 'stdin', io.StringIO()), \
             self.assertRaises(SystemExit) as exit:
            cli.cmd_resume(args)
        self.assertIn('Choose a chat explicitly', str(exit.exception))

    def test_reads_transcript_steps(self):
        path = os.path.join(TMP, 'transcript_full.jsonl')
        steps = [
            {'step_index': 0, 'type': 'USER_INPUT', 'source': 'USER_EXPLICIT', 'created_at': '2026-09-22T04:18:45Z',
             'content': '<USER_REQUEST>\nfix the build\n</USER_REQUEST>\n<ADDITIONAL_METADATA>t</ADDITIONAL_METADATA>'},
            {'step_index': 1, 'type': 'PLANNER_RESPONSE', 'source': 'MODEL', 'content': 'Checking.',
             'thinking': 'hidden', 'tool_calls': [{'name': 'run_command', 'args': {'CommandLine': 'npm test'}}]},
            {'step_index': 2, 'type': 'RUN_COMMAND', 'source': 'MODEL',
             'content': 'Created At: x\nCompleted At: y\n1 failing'},
            {'step_index': 3, 'type': 'SYSTEM_MESSAGE', 'source': 'SYSTEM', 'content': 'ignored'},
        ]
        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(json.dumps(s) for s in steps))
        els = AGENTS['agy'].read({'path': path}, CFG)
        self.assertEqual([(x['role'], x['text']) for x in els], [
            ('user', 'fix the build'), ('assistant', 'Checking.'),
            ('tool', '▸ run_command: npm test'), ('tool', '  ⎿ 1 failing')])


HERMES_HOME_DIR = os.path.join(TMP, '.hermes')

DDL = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, model_config TEXT,
  system_prompt TEXT, parent_session_id TEXT,
  started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
  message_count INTEGER DEFAULT 0, title TEXT, title_source TEXT,
  cwd TEXT, last_activity_at REAL, origin_json TEXT,
  profile_name TEXT, archived INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (parent_session_id) REFERENCES sessions(id));
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
  role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
  timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX idx_sessions_title_unique ON sessions(title) WHERE title IS NOT NULL;
"""


class HermesStore(unittest.TestCase):
    """The store must be shaped exactly like the real $HERMES_HOME/state.db (title index included)."""

    @classmethod
    def setUpClass(cls):
        os.makedirs(HERMES_HOME_DIR, exist_ok=True)
        c = sqlite3.connect(os.path.join(HERMES_HOME_DIR, 'state.db'))
        c.executescript(DDL)
        c.commit()
        c.close()

    def conn(self):
        return sqlite3.connect(os.path.join(HERMES_HOME_DIR, 'state.db'))

    def setUp(self):
        # Every test starts from an empty store: no test may depend on another having run first.
        c = self.conn()
        c.executescript('delete from messages; delete from sessions;')
        c.commit()
        c.close()

    def test_mirror_roundtrips_and_collides_on_title(self):
        agent, th = AGENTS['hermes'], thread([e('1', 'user', 'remember MANGO'), e('2', 'assistant', 'stored')])
        res = agent.write(None, os.getcwd(), sync.render(th, CFG), 'Hermes (Agy)', {'cfg': CFG})
        s = agent.list(CFG)['hermes:' + res['id']]
        els = sync.load(s, CFG)
        self.assertEqual({x['id'] for x in els}, set(res['seen']))
        self.assertEqual(els[-1]['text'], 'stored')
        self.assertIn('remember MANGO', els[0]['text'])
        self.assertTrue(s['is_hop'])
        self.assertEqual(s['sig'], res['sig'])
        again = agent.write(None, os.getcwd(), sync.render(th, CFG), 'Hermes (Agy)', {'cfg': CFG})  # same title
        self.assertNotEqual(again['id'], res['id'])
        c = self.conn()
        count = c.execute('select count(*) from messages where session_id=?', (res['id'],)).fetchone()[0]
        prof = c.execute('select profile_name, model, source from sessions where id=?', (res['id'],)).fetchone()
        c.close()
        self.assertEqual(count, len(res['seen']))
        # model must stay NULL: hermes --resume reuses the stored model, and a placeholder
        # ('hop-import') is not a provider model, which makes the mirror unresumable.
        self.assertEqual(prof, ('default', None, 'cli'))

    def test_update_rewrites_the_same_mirror(self):
        agent = AGENTS['hermes']
        first = thread([e('1', 'user', 'old')])
        res = agent.write(None, os.getcwd(), sync.render(first, CFG), 'Mirror', {'cfg': CFG})
        cp = agent.list(CFG)['hermes:' + res['id']]
        second = thread([e('2', 'user', 'new')])
        updated = agent.write(cp, os.getcwd(), sync.render(second, CFG), 'Mirror', {'cfg': CFG})
        self.assertEqual(updated['id'], res['id'])
        text = [e['text'] for e in sync.load(agent.list(CFG)['hermes:' + res['id']], CFG)]
        self.assertEqual(len(text), 1)
        self.assertIn('new', text[0])
        self.assertNotIn('old', text[0])

    def test_vanished_mirror_raises_hop_error_without_retryable_raw_error(self):
        agent = AGENTS['hermes']
        res = agent.write(None, os.getcwd(), sync.render(thread([e('1', 'user', 'old')]), CFG),
                          'Mirror', {'cfg': CFG})
        c = self.conn()
        c.execute('delete from messages where session_id=?', (res['id'],))
        c.execute('delete from sessions where id=?', (res['id'],))
        c.commit()
        c.close()
        cp = {'id': res['id']}
        with self.assertRaises(core.HopError):
            agent.write(cp, os.getcwd(), sync.render(thread([e('2', 'user', 'new')]), CFG),
                        'Mirror', {'cfg': CFG})

    def test_reads_a_real_user_session(self):
        c = self.conn()
        c.execute("insert into sessions (id, source, started_at, title, cwd) values ('real1','cli',1.0,'My chat','/tmp')")
        c.executemany('insert into messages (session_id, role, content, timestamp, tool_calls, tool_name) '
                      'values (?,?,?,?,?,?)',
                      [('real1', 'user', 'fix it', 2.0, None, None),
                       ('real1', 'assistant', 'on it', 3.0,
                        '[{"id":"t1","type":"function","function":{"name":"Bash","arguments":"{\\"command\\":\\"ls\\"}"}}]', None),
                       ('real1', 'tool', 'a.py', 4.0, None, 'Bash'),
                       ('real1', 'session_meta', 'hidden', 5.0, None, None)])
        c.commit()
        c.close()
        s = AGENTS['hermes'].list(CFG)['hermes:real1']
        self.assertFalse(s['is_hop'])
        els = sync.load(s, CFG)
        self.assertEqual([(x['role'], x['text']) for x in els],
                         [('user', 'fix it'), ('assistant', 'on it'), ('tool', '▸ Bash: ls'), ('tool', '  ⎿ a.py')])

    def test_reads_a_wal_store_without_sidecars(self):
        """Hermes exits cleanly, leaving a WAL-mode db with no -wal/-shm; list() must still open it."""
        c = self.conn()
        c.execute("insert into sessions (id, source, started_at, title, cwd) values ('wal1','cli',1.0,'W','/tmp')")
        c.execute("insert into messages (session_id, role, content, timestamp) values ('wal1','user','hi',2.0)")
        c.commit()
        c.execute('PRAGMA journal_mode=WAL')
        c.close()  # sidecars are removed on close
        self.assertFalse(os.path.exists(os.path.join(HERMES_HOME_DIR, 'state.db-wal')))
        self.assertIn('hermes:wal1', AGENTS['hermes'].list(CFG))

    def test_missing_store_raises_hop_error(self):
        """Hermes installed but never run: a HopError, not a raw OperationalError, so sync stops retrying."""
        db = os.path.join(HERMES_HOME_DIR, 'state.db')
        os.rename(db, db + '.away')  # keep the store for later tests
        try:
            with self.assertRaises(core.HopError):
                AGENTS['hermes'].write(None, os.getcwd(), sync.render(thread([e('1', 'user', 'x')]), CFG),
                                       'T', {'cfg': CFG})
            AGENTS['hermes'].delete({'id': 'whatever'})  # must be a no-op, not a crash
            self.assertEqual(AGENTS['hermes'].list(CFG), {})
        finally:
            os.rename(db + '.away', db)

    def test_title_collision_gets_a_suffix_not_a_blank(self):
        """Hermes requires unique titles; a second mirror must keep a title (suffixed), never go untitled."""
        agent, th = AGENTS['hermes'], thread([e('1', 'user', 'a'), e('2', 'assistant', 'b')])
        msgs, title = sync.render(th, CFG), 'Greeting (OpenCode)'
        res = agent.write(None, os.getcwd(), msgs, title, {'cfg': CFG})
        res2 = agent.write(None, os.getcwd(), msgs, title, {'cfg': CFG})
        c = self.conn()
        t1 = c.execute('select title from sessions where id=?', (res['id'],)).fetchone()[0]
        t2 = c.execute('select title from sessions where id=?', (res2['id'],)).fetchone()[0]
        c.close()
        self.assertEqual(t1, title)
        self.assertTrue(t2 and t2.startswith(title) and t2 != title, t2)


class KimiIndex(unittest.TestCase):
    """A real Kimi state.json stores updatedAt as an ISO string; list() must hand sync an int (ms)."""

    def test_updated_at_is_converted_to_epoch_ms(self):
        sdir = os.path.join(TMP, '.kimi-code', 'sessions', 'wd_x_abc', 'session_iso1')
        os.makedirs(os.path.join(sdir, 'agents', 'main'), exist_ok=True)
        with open(os.path.join(sdir, 'agents', 'main', 'wire.jsonl'), 'w') as f:
            f.write(core.dumps({'type': 'context.append_message', 'time': 1, 'message': {
                'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}],
                'origin': {'kind': 'user'}, 'id': 'm1'}}) + '\n')
        with open(os.path.join(sdir, 'state.json'), 'w') as f:
            f.write(core.dumps({'id': 'session_iso1', 'archived': False, 'cwd': '/tmp',
                                'updatedAt': '2026-07-18T13:10:14.672Z', 'title': 'T'}))
        with open(os.path.join(TMP, '.kimi-code', 'session_index.jsonl'), 'a') as f:
            f.write(core.dumps({'sessionId': 'session_iso1', 'sessionDir': sdir, 'workDir': '/tmp'}) + '\n')
        s = AGENTS['kimi'].list(CFG)['kimi:session_iso1']
        self.assertIsInstance(s['updated'], int)
        self.assertEqual(s['updated'], core.iso_ms('2026-07-18T13:10:14.672Z'))
        sorted([s], key=lambda x: x['updated'])  # sync.adopt_new's sort key must not raise


if __name__ == '__main__':
    unittest.main()
