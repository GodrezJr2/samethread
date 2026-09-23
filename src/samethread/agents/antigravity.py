"""Antigravity CLI (agy): foreign chats land in the resume picker's "Other" tab.

agy owns a protobuf execution trajectory (conversations/<cid>.db) that hop does not write; the
readable history is a JSONL transcript under brain/<cid>/, and the picker index is the
conversation_summaries table. Writing those two (with app_data_dir != 'antigravity-cli') puts a
mirror under the "Other" tab. # ponytail: no protobuf trajectory, so a mirror is readable and
continues as text but has no agy-native tool state; upgrade via agy's importConversationCmd if
bit-identical resume ever matters.
"""
import datetime
import json
import os
import re
import shutil
import sqlite3
import urllib.parse
import uuid

from .. import hookkit
from ..core import (HOME, HopError, el, file_sig, fmt_call, fmt_result, iso_ms, ms_iso,
                    read_jsonl, remap, write_lines)
from .base import Agent

ROOT = os.path.join(HOME, '.gemini', 'antigravity-cli')
HOOKS = os.path.join(HOME, '.gemini', 'config', 'hooks.json')
HDR_RE = re.compile(r'^(Created At|Completed At):.*\n?', re.M)
MIRROR = 'samethread'  # app_data_dir / source marker: a hop-written mirror, lands in "Other"


def db():
    return os.path.join(ROOT, 'conversation_summaries.db')


def stamp(ms):
    """agy's summary timestamps: '2026-03-13 17:12:11.670405+00:00'."""
    return datetime.datetime.fromtimestamp((ms or 0) / 1000, datetime.timezone.utc).isoformat(' ')


def transcript(cid):
    base = os.path.join(ROOT, 'brain', cid, '.system_generated', 'logs')
    for name in ('transcript_full.jsonl', 'transcript.jsonl'):
        p = os.path.join(base, name)
        if os.path.exists(p):
            return p
    return None


def uri_to_path(u):
    p = urllib.parse.unquote(urllib.parse.urlparse(u).path)
    if re.match(r'^/[A-Za-z]:', p):
        p = p[1:]
    return os.path.normpath(p)


class Antigravity(Agent):
    key, name, aliases, binary, writable = 'agy', 'Agy', ('antigravity',), 'agy', True

    def detect(self):
        return os.path.isdir(ROOT) or super().detect()

    def list(self, cfg):
        if not os.path.exists(db()):
            return {}
        c = sqlite3.connect(f'file:{db()}?mode=ro', uri=True, timeout=15)
        try:
            rows = c.execute('select conversation_id, title, last_modified_time, workspace_uris, '
                             'parent_conversation_id, nesting_depth, app_data_dir from '
                             'conversation_summaries').fetchall()
        finally:
            c.close()
        out = {}
        for cid, title, modified, ws, parent, depth, app_dir in rows:
            path = transcript(cid)
            if parent or depth or not path:
                continue
            try:
                uris = json.loads(ws or '[]')
            except ValueError:
                uris = []
            out['agy:' + cid] = self.session(cid, path=path, title=title, sig=file_sig(path),
                                             cwd=remap(cfg, uri_to_path(uris[0]) if uris else HOME),
                                             updated=iso_ms(modified) or int(os.path.getmtime(path) * 1000),
                                             is_hop=app_dir == MIRROR)
        return out

    def read(self, s, cfg):
        els = []
        for d in read_jsonl(s['path']):
            st, idx, ts = d.get('type'), d.get('step_index'), iso_ms(d.get('created_at'))
            content = d.get('content') or ''
            if st == 'USER_INPUT':
                m = re.search(r'<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>', content, re.S)
                txt = m.group(1) if m else re.sub(r'<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>', '', content, flags=re.S)
                if d.get('media'):
                    txt += '\n[image]'
                if txt.strip():
                    els.append(el(idx, 'user', txt.strip(), ts))
            elif st == 'PLANNER_RESPONSE':
                if content.strip():
                    els.append(el(f'{idx}#0', 'assistant', content.strip(), ts))
                for i, tc in enumerate(d.get('tool_calls') or []):
                    els.append(el(f'{idx}#t{i}', 'tool', fmt_call(tc.get('name'), tc.get('args'), cfg), ts))
            elif st == 'CHECKPOINT':
                txt = re.sub(r'^\{\{ CHECKPOINT \d+ \}\}\s*', '', content).strip()
                if txt:
                    els.append(el(idx, 'user', txt, ts, 'summary'))
            elif d.get('source') == 'MODEL':
                els.append(el(idx, 'tool', fmt_result(HDR_RE.sub('', content).strip() or d.get('error'), cfg), ts))
        return els

    def write(self, cp, cwd, msgs, title, ctx):
        if not os.path.isdir(ROOT):
            raise HopError('run agy once so its store exists')
        raw = (cp.get('id') if cp else '') or ''
        cid = raw.split(':', 1)[-1] or str(uuid.uuid4())
        path = os.path.join(ROOT, 'brain', cid, '.system_generated', 'logs', 'transcript_full.jsonl')
        lines = []
        for i, m in enumerate(msgs):
            if m['role'] == 'user':
                rec = {'step_index': i, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT',
                       'status': 'DONE', 'created_at': ms_iso(m['ts']),
                       'content': '<USER_REQUEST>\n' + (m['text'] or '') + '\n</USER_REQUEST>'}
            else:
                rec = {'step_index': i, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
                       'status': 'DONE', 'created_at': ms_iso(m['ts']), 'content': m['text'] or ''}
            lines.append(rec)
        write_lines(path, lines, mtime_ms=msgs[-1]['ts'] if msgs else None)
        self._summary(cid, cwd, msgs, title)
        return self.session(cid, path=path, title=title, sig=file_sig(path), cwd=cwd,
                            updated=msgs[-1]['ts'] if msgs else 0, is_hop=True)

    def _summary(self, cid, cwd, msgs, title):
        users = [i for i, m in enumerate(msgs) if m['role'] == 'user']
        last_user = users[-1] if users else -1
        # agy shows title == preview as the picker label (verified against real rows).
        label = (title or (msgs[users[0]]['text'] if users else ''))[:200]
        c = sqlite3.connect(db(), timeout=15)
        try:
            with c:
                c.execute('insert or replace into conversation_summaries (conversation_id, title, '
                          'preview, step_count, last_modified_time, workspace_uris, source, project_id, '
                          'agent_name, parent_conversation_id, nesting_depth, last_user_input_time, '
                          'last_user_input_step_index, app_data_dir, group_id) '
                          'values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (cid, label, label, len(msgs),
                           stamp(msgs[-1]['ts'] if msgs else 0),
                           json.dumps(['file://' + urllib.parse.quote(cwd or '', safe='/')]),
                           MIRROR, '', MIRROR, '', 0,
                           stamp(msgs[last_user]['ts'] if last_user >= 0 else 0),
                           last_user, MIRROR, MIRROR))
        finally:
            c.close()

    def retitle(self, cp, title):
        if not os.path.exists(db()):
            return False
        c = sqlite3.connect(db(), timeout=15)
        try:
            with c:
                c.execute('update conversation_summaries set title=? where conversation_id=?',
                          (title or '', (cp.get('id') or '').split(':', 1)[-1]))
        finally:
            c.close()
        return True

    def delete(self, cp):
        if not os.path.isdir(ROOT):
            return
        cid = (cp.get('id') or '').split(':', 1)[-1]
        shutil.rmtree(os.path.join(ROOT, 'brain', cid), ignore_errors=True)
        c = sqlite3.connect(db(), timeout=15)
        try:
            with c:
                c.execute('delete from conversation_summaries where conversation_id=?', (cid,))
        finally:
            c.close()

    def resume_cmd(self, sid):
        return ['agy', '--conversation', sid]

    def seed_cmd(self, prompt):
        return ['agy', '-i', prompt]

    def install(self, dry):
        hooks = hookkit.load(HOOKS)
        # agy runs hooks through `cmd /c`, which mangles quotes, so the command stays unquoted.
        entry = {'Stop': [{'type': 'command', 'command': hookkit.command('\\', ' --json'), 'timeout': 15}]}
        if hooks.get('samethread') == entry:
            return 'hook already installed'
        hooks['samethread'] = entry
        hookkit.save(HOOKS, hooks, dry)
        return f'Stop hook added ({HOOKS})'

    def uninstall(self, dry):
        hooks = hookkit.load(HOOKS)
        if hooks.pop('samethread', None) is None:
            return 'nothing to remove'
        hookkit.save(HOOKS, hooks, dry)
        return 'Stop hook removed'
