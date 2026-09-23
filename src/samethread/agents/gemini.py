"""Gemini CLI: ~/.gemini/tmp/<project-slug>/chats/session-<time>-<id8>.jsonl.

Project slugs come from Gemini's own registry (~/.gemini/projects.json). hop only writes into folders
Gemini already registered, so it never has to claim a slug on Gemini's behalf.
"""
import datetime as dt
import hashlib
import os
import uuid

from .. import hookkit
from ..core import (HOME, HOP_MODEL, HopError, append_line, el, file_sig, fmt_call, fmt_result, iso_ms, lp, ms_iso,
                    read_json, read_jsonl, text_of, write_lines)
from .base import Agent

ROOT = os.path.join(HOME, '.gemini')
REGISTRY = os.path.join(ROOT, 'projects.json')


def registry():
    return (read_json(REGISTRY, {}) or {}).get('projects') or {}


def reg_key(cwd):
    p = os.path.normpath(cwd)
    return p.lower() if os.name == 'nt' else p


class Gemini(Agent):
    key, name, aliases, binary, writable = 'gemini', 'Gemini', ('gemini-cli',), 'gemini', True

    def detect(self):
        return os.path.exists(REGISTRY) or super().detect()

    def list(self, cfg):
        out, tmp = {}, os.path.join(ROOT, 'tmp')
        if not os.path.isdir(tmp):
            return out
        roots = {slug: path for path, slug in registry().items()}
        for d in os.scandir(tmp):
            chats = os.path.join(d.path, 'chats')
            if not d.is_dir() or not os.path.isdir(chats):
                continue
            for f in os.scandir(chats):
                if f.is_file() and f.name.startswith('session-') and f.name.endswith(('.jsonl', '.json')):
                    st = f.stat()
                    first = next(read_jsonl(f.path), {}) if f.name.endswith('.jsonl') else read_json(f.path, {})
                    sid = first.get('sessionId')
                    if sid:
                        out['gemini:' + sid] = self.session(sid, path=f.path, cwd=roots.get(d.name), is_hop=False,
                                                            sig=f'{st.st_mtime_ns}:{st.st_size}',
                                                            updated=int(st.st_mtime * 1000))
        return out

    def read(self, s, cfg):
        if s['path'].endswith('.json'):
            conv = read_json(s['path'], {})
            records = conv.get('messages') or []
            s['title'] = conv.get('summary')
        else:
            records = []
            for r in read_jsonl(s['path']):
                if '$set' in r:
                    s['title'] = r['$set'].get('summary') or s['title']
                elif '$rewindTo' in r:
                    ids = [m['id'] for m in records]
                    if r['$rewindTo'] in ids:
                        records = records[:ids.index(r['$rewindTo'])]
                elif r.get('type') and r.get('id'):
                    records = [m for m in records if m['id'] != r['id']] + [r]
        els = []
        for m in records:
            t, mid, ts = m.get('type'), m['id'], iso_ms(m.get('timestamp'))
            txt = text_of(m.get('content')).strip()
            if t == 'user' and txt:
                els.append(el(mid, 'user', txt, ts))
            elif t == 'gemini':
                if m.get('model') == HOP_MODEL:
                    s['is_hop'] = True
                if txt:
                    els.append(el(mid, 'assistant', txt, ts))
                for i, tc in enumerate(m.get('toolCalls') or []):
                    els.append(el(f'{mid}#t{i}', 'tool', fmt_call(tc.get('name'), tc.get('args'), cfg) + '\n'
                                  + fmt_result(tc.get('resultDisplay') or tc.get('result'), cfg), ts))
        return els

    def write(self, cp, cwd, msgs, title, ctx):
        first, last = msgs[0]['ts'], max(m['ts'] for m in msgs)
        if cp:
            sid, path = cp['id'], cp['path']
        else:
            slug = registry().get(reg_key(cwd))
            if not slug:
                raise HopError(f'Gemini CLI has not been used in {cwd} yet')
            sid = str(uuid.uuid4())
            stamp = dt.datetime.fromtimestamp(first / 1000, dt.timezone.utc).isoformat()[:16].replace(':', '-')
            path = os.path.join(ROOT, 'tmp', slug, 'chats', f'session-{stamp}-{sid[:8]}.jsonl')
        lines = [{'sessionId': sid, 'projectHash': hashlib.sha256(cwd.encode()).hexdigest(), 'startTime': ms_iso(first),
                  'lastUpdated': ms_iso(last), 'kind': 'main'}]
        seen = []
        for m in msgs:
            mid = str(uuid.uuid4())
            rec = {'id': mid, 'timestamp': ms_iso(m['ts']), 'type': 'user' if m['role'] == 'user' else 'gemini',
                   'content': [{'text': m['text']}]}
            if m['role'] != 'user':
                rec['model'] = HOP_MODEL
            lines.append(rec)
            seen.append(mid)
        lines.append({'$set': {'summary': title, 'lastUpdated': ms_iso(last)}})
        write_lines(path, lines, last)
        return {'id': sid, 'path': path, 'seen': seen, 'sig': file_sig(path)}

    def retitle(self, cp, title):
        if not cp['path'].endswith('.jsonl'):
            return False
        append_line(cp['path'], {'$set': {'summary': title}})
        return True

    def delete(self, cp):
        if os.path.exists(lp(cp['path'])):
            os.remove(lp(cp['path']))

    def resume_cmd(self, sid):
        return ['gemini', '--resume', sid]

    def install(self, dry):
        return hookkit.add_json_hooks(os.path.join(ROOT, 'settings.json'), ['AfterAgent', 'SessionEnd'], dry)

    def uninstall(self, dry):
        return hookkit.remove_json_hooks(os.path.join(ROOT, 'settings.json'), dry)
