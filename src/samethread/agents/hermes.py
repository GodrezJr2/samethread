"""Hermes Agent: $HERMES_HOME/state.db — SQLite `sessions`/`messages` tables."""
import datetime as dt
import json
import os
import sqlite3
import uuid

from ..core import HOME, HOP_ORIGIN, el, fmt_call, fmt_result
from .base import Agent

ROOT = os.environ.get('HERMES_HOME') or os.path.join(HOME, '.hermes')
DB = os.path.join(ROOT, 'state.db')
ORIGIN = json.dumps({'imported_from': {'tool': HOP_ORIGIN}})
FIELDS = ('id, source, model, system_prompt, parent_session_id, title, cwd, profile_name, '
          'started_at, last_activity_at, message_count, origin_json')
KEYS = tuple(f.strip() for f in FIELDS.split(','))


def connect(rw=False):
    if rw:
        return sqlite3.connect(f'file:{DB}?mode=rw', uri=True, timeout=15)
    if not os.path.exists(DB):
        return None
    try:
        c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=15)
        c.execute('pragma schema_version')  # sqlite3.connect is lazy: force the open so a failure is catchable
        return c
    except sqlite3.OperationalError:
        # A WAL-mode db whose -wal/-shm sidecars are gone (Hermes exited cleanly) cannot be opened
        # mode=ro; a read-write handle reads the same committed data.
        return sqlite3.connect(f'file:{DB}?mode=rw', uri=True, timeout=15)


def profile_name():
    """<root>/state.db -> 'default'; <root>/profiles/<name>/state.db -> '<name>'."""
    parent = os.path.dirname(DB)
    return os.path.basename(parent) if os.path.basename(os.path.dirname(parent)) == 'profiles' else 'default'


def new_session_id():
    return dt.datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:6]


def stat(c, sid):
    n, mx = c.execute('select count(*), coalesce(max(id), 0) from messages where session_id=?', (sid,)).fetchone()
    row = c.execute('select last_activity_at, started_at from sessions where id=?', (sid,)).fetchone()
    upd = int(max(row[0] or 0, row[1] or 0) * 1000) if row else 0
    return {'sig': f'{upd}:{n}:{mx}', 'updated': upd}


class Hermes(Agent):
    key, name, aliases, binary, writable = 'hermes', 'Hermes', ('hermes-agent',), 'hermes', True

    def detect(self):
        return os.path.exists(DB) or super().detect()

    def list(self, cfg):
        c = connect()
        if not c:
            return {}
        try:
            out = {}
            for row in c.execute(f'select {FIELDS}, model_config from sessions where source=? '
                                 'and parent_session_id is null and archived=0 and hidden=0', ('cli',)):
                d = dict(zip(KEYS, row))
                if '_delegate_from' in (row[-1] or ''):
                    continue
                out['hermes:' + d['id']] = self.session(
                    d['id'], cwd=os.path.normpath(d['cwd'] or HOME), title=d['title'],
                    is_hop=self._is_hop(d['origin_json']), **stat(c, d['id']))
            return out
        finally:
            c.close()

    def read(self, s, cfg):
        c = connect()
        try:
            s['cwd'] = os.path.normpath(s['cwd']) if s.get('cwd') else None
            row = c.execute('select cwd, title, origin_json from sessions where id=?', (s['id'],)).fetchone()
            if row:
                s['cwd'] = os.path.normpath(row[0] or HOME)
                s['title'] = row[1]
                s['is_hop'] = self._is_hop(row[2])
            rows = c.execute("select id, role, content, tool_calls, tool_name, timestamp from messages "
                             "where session_id=? and active=1 and role in ('user','assistant','tool') "
                             "order by timestamp, id", (s['id'],)).fetchall()
        finally:
            c.close()
        els = []
        for mid, role, content, calls, tname, ts in rows:
            ts = int((ts or 0) * 1000)
            if role == 'tool':
                els.append(el(mid, 'tool', fmt_result(content, cfg), ts))
                continue
            if content:
                els.append(el(mid, role, content, ts))
            for i, tc in enumerate(self._calls(calls)):
                fn = tc.get('function') or {}
                els.append(el(f'{mid}#t{i}', 'tool', fmt_call(fn.get('name') or tname, fn.get('arguments'), cfg), ts))
        return els

    def write(self, cp, cwd, msgs, title, ctx):
        sid = cp['id'] if cp else new_session_id()
        os.makedirs(ROOT, exist_ok=True)
        c = connect(rw=True)
        try:
            c.execute('PRAGMA foreign_keys=ON')
            last = (msgs[-1]['ts'] / 1000) if msgs else 0
            if cp:
                c.execute('delete from messages where session_id=?', (sid,))
            with c:  # one transaction; on a title collision retry without it (title is unique)
                try:
                    self._session_row(c, cp, sid, cwd, title, last, len(msgs))
                except sqlite3.IntegrityError:
                    self._session_row(c, cp, sid, cwd, None, last, len(msgs))
                c.executemany('insert into messages (session_id, role, content, timestamp) values (?,?,?,?)',
                              [(sid, m['role'], m['text'], m['ts'] / 1000) for m in msgs])
            seen = [str(i) for (i,) in c.execute('select id from messages where session_id=? order by id', (sid,))]
            return {'id': sid, 'seen': seen, **stat(c, sid)}
        finally:
            c.close()

    def _session_row(self, c, cp, sid, cwd, title, last, n):
        if cp:
            c.execute('update sessions set title=?, last_activity_at=?, message_count=? where id=?', (title, last, n, sid))
        else:
            # model is left NULL on purpose: `hermes --resume` reuses the session's stored model, and a
            # placeholder like 'hop-import' is not a real provider model — it makes the mirror unresumable.
            c.execute('insert into sessions (id, source, title, cwd, profile_name, started_at, '
                      'last_activity_at, message_count, origin_json) values (?,?,?,?,?,?,?,?,?)',
                      (sid, 'cli', title, cwd, profile_name(), last, last, n, ORIGIN))

    def retitle(self, cp, title):
        c = connect(rw=True)
        try:
            with c:
                c.execute('update sessions set title=? where id=?', (title, cp['id']))
        except sqlite3.IntegrityError:
            return False
        finally:
            c.close()
        return True

    def delete(self, cp):
        c = connect(rw=True)
        if not c:
            return
        try:
            with c:
                c.execute('delete from messages where session_id=?', (cp['id'],))  # FK: children first
                c.execute('delete from sessions where id=?', (cp['id'],))
        finally:
            c.close()

    def resume_cmd(self, sid):
        return ['hermes', '--resume', sid]

    def install(self, dry):
        # Hermes' shell hooks need an allowlist plus a config.yaml edit a third-party installer should not make,
        # so there is nothing safe to add here. Mirrors land on any other agent's sync, or `hop sync`.
        return 'no hook support yet; synced when another agent (or `hop sync`) runs'

    @staticmethod
    def _is_hop(origin_json):
        try:
            return json.loads(origin_json or '')['imported_from']['tool'] == HOP_ORIGIN
        except (ValueError, KeyError, TypeError):
            return False

    @staticmethod
    def _calls(raw):
        try:
            calls = json.loads(raw or '[]')
        except ValueError:
            return []
        return [tc for tc in calls if isinstance(tc, dict)]
