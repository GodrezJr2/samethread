import json
import os
import tempfile
import unittest

os.environ['HOP_HOME'] = tempfile.mkdtemp(prefix='hop-test-')

from samethread import cli  # noqa: E402

CFG = dict(cli.DEFAULT_CONFIG)


def thread(elements):
    th = cli.new_thread('agy:x', 'Auth refactor', os.getcwd())
    th['elements'] = elements
    return th


def e(id_, role, text, author='agy', kind='msg', ts=1000):
    return dict(cli.el(id_, role, text, ts, kind), author=author)


class Helpers(unittest.TestCase):
    def test_iso_ms_handles_every_timestamp_shape(self):
        self.assertEqual(cli.iso_ms('2026-09-22T04:18:45Z'), 1790050725000)
        self.assertEqual(cli.iso_ms('2026-09-23 05:42:48.2564+00:00'), cli.iso_ms('2026-09-23T05:42:48.256400Z'))
        self.assertEqual(cli.iso_ms(''), 0)
        self.assertEqual(cli.iso_ms('garbage'), 0)

    def test_opencode_ids_match_opencode_layout(self):
        sid = cli.oc_id('ses', 1790000000000, desc=True)
        self.assertRegex(sid, r'^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$')
        older, newer = cli.oc_id('msg', 1000), cli.oc_id('msg', 2000)
        self.assertLess(older, newer)
        self.assertGreater(cli.oc_id('ses', 1000, True), cli.oc_id('ses', 2000, True))

    def test_claude_project_slug(self):
        self.assertEqual(cli.cc_slug('D:\\Project\\Svalte test'), 'D--Project-Svalte-test')

    def test_titles_drop_old_tags_and_fall_back_to_first_prompt(self):
        self.assertEqual(cli.clean_title('Fix login (Agy+Claude)', []), 'Fix login')
        self.assertEqual(cli.clean_title('New session - 2026-09-23', [e('1', 'user', 'why is CI red?\nmore')]), 'why is CI red?')

    def test_fmt_call_prefers_the_meaningful_argument(self):
        self.assertEqual(cli.fmt_call('Bash', {'command': 'ls -la', 'timeout': 5}, CFG), '▸ Bash: ls -la')
        self.assertEqual(cli.fmt_call('run_command', {'CommandLine': '"dir"'}, CFG), '▸ run_command: dir')


class Render(unittest.TestCase):
    def test_merges_turns_and_tags_the_source(self):
        th = thread([e('1', 'user', 'hi'), e('2', 'assistant', 'looking'), e('3', 'tool', '▸ ls'),
                     e('4', 'assistant', 'done'), e('5', 'user', 'thanks', author='cc')])
        msgs = cli.render(th, CFG)
        self.assertEqual([m['role'] for m in msgs], ['user', 'assistant', 'user'])
        self.assertIn('Agy+Claude', msgs[0]['text'])
        self.assertEqual(msgs[1]['text'], 'looking\n\n▸ ls\n\ndone')
        self.assertEqual(cli.thread_title(th), 'Auth refactor (Agy+Claude)')

    def test_starts_from_the_latest_summary(self):
        th = thread([e('1', 'user', 'old'), e('2', 'assistant', 'old reply'),
                     e('3', 'user', 'what happened so far', kind='summary'), e('4', 'user', 'next')])
        msgs = cli.render(th, CFG)
        self.assertNotIn('old reply', ''.join(m['text'] for m in msgs))
        self.assertIn('[Summary of earlier conversation]', msgs[0]['text'])

    def test_drops_oldest_messages_past_the_budget(self):
        els = []
        for i in range(10):
            els += [e(f'u{i}', 'user', 'q' * 100), e(f'a{i}', 'assistant', 'a' * 100)]
        msgs = cli.render(thread(els), dict(CFG, max_chars=500))
        self.assertLessEqual(len(msgs), 7)
        self.assertIn('earlier messages were omitted', msgs[0]['text'])


class ClaudeRoundTrip(unittest.TestCase):
    def test_written_mirror_reads_back_and_detects_continuation(self):
        th = thread([e('1', 'user', 'remember MANGO'), e('2', 'assistant', 'stored')])
        path = os.path.join(os.environ['HOP_HOME'], 'mirror.jsonl')
        res = cli.cc_write({'id': 'sid-1', 'path': path}, os.getcwd(), cli.render(th, CFG), 'Auth refactor (Agy)', '2.1.0')

        s = {'tool': 'cc', 'path': path}
        els = cli.cc_read(s, CFG)
        self.assertEqual({x['id'] for x in els}, set(res['seen']))
        self.assertEqual(s['title'], 'Auth refactor (Agy)')
        self.assertTrue(s['is_hop'])

        with open(path, encoding='utf-8') as f:
            last = [json.loads(line) for line in f if '"uuid"' in line][-1]
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'user', 'uuid': 'u-new', 'parentUuid': last['uuid'], 'sessionId': 'sid-1',
                                'timestamp': '2026-09-23T06:00:00Z', 'message': {'role': 'user', 'content': 'codeword?'}}) + '\n')
            f.write(json.dumps({'type': 'assistant', 'uuid': 'a-new', 'parentUuid': 'u-new', 'sessionId': 'sid-1',
                                'timestamp': '2026-09-23T06:00:01Z',
                                'message': {'role': 'assistant', 'content': [
                                    {'type': 'thinking', 'thinking': '', 'signature': 'x'},
                                    {'type': 'text', 'text': 'MANGO'}]}}) + '\n')
        new = [x for x in cli.cc_read({'tool': 'cc', 'path': path}, CFG) if x['id'] not in set(res['seen'])]
        self.assertEqual([(x['role'], x['text']) for x in new], [('user', 'codeword?'), ('assistant', 'MANGO')])


class Antigravity(unittest.TestCase):
    def test_reads_transcript_steps(self):
        path = os.path.join(os.environ['HOP_HOME'], 'transcript_full.jsonl')
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
        els = cli.agy_read({'path': path}, CFG)
        self.assertEqual([(x['role'], x['text']) for x in els], [
            ('user', 'fix the build'), ('assistant', 'Checking.'),
            ('tool', '▸ run_command: npm test'), ('tool', '  ⎿ 1 failing')])


if __name__ == '__main__':
    unittest.main()
