"""SameThread (`hop`) - one chat history across Claude Code, OpenCode and Antigravity CLI (agy).

Every chat is mirrored into the other tools' own session stores, so each tool's
native resume picker lists it, titled with the tools that wrote it:
"Fix login (Agy)", "Fix login (Agy+Claude)". Originals are never modified; hop
only rewrites mirrors it created. agy cannot be written to, so chats reach agy
through `hop agy`, which seeds a new agy conversation from the transcript.

State lives in ~/.hop: config.json, index.json, threads/<id>.json, hop.log.
"""
import argparse
import datetime as dt
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import subprocess
import sys
import time
import urllib.parse
import uuid
from collections import Counter

from . import __version__, integrations

HOME = os.path.expanduser('~')
HOP_DIR = os.environ.get('HOP_HOME') or os.path.join(HOME, '.hop')
THREAD_DIR = os.path.join(HOP_DIR, 'threads')
HANDOFF_DIR = os.path.join(HOP_DIR, 'handoff')
INDEX_PATH = os.path.join(HOP_DIR, 'index.json')
CONFIG_PATH = os.path.join(HOP_DIR, 'config.json')
LOCK_PATH = os.path.join(HOP_DIR, 'sync.lock')
PENDING_PATH = os.path.join(HOP_DIR, 'sync.pending')
LOG_PATH = os.path.join(HOP_DIR, 'hop.log')

CC_PROJECTS = os.path.join(HOME, '.claude', 'projects')
OC_DB = os.environ.get('OPENCODE_DB_PATH') or os.path.join(HOME, '.local', 'share', 'opencode', 'opencode.db')
AGY_DIR = os.path.join(HOME, '.gemini', 'antigravity-cli')

NAMES = {'cc': 'Claude', 'oc': 'OpenCode', 'agy': 'Agy'}
WRITABLE = ('cc', 'oc')
HOP_MODEL = 'hop-import'
HOP_SLUG = 'hop-'
TAG_RE = re.compile(r'\s*\((?:Claude|OpenCode|Agy)(?:\+(?:Claude|OpenCode|Agy))*\)\s*$')
HANDOFF_RE = re.compile(r'\[hop-handoff:([0-9a-f]+)\]')

DEFAULT_CONFIG = {
    'targets': ['cc', 'oc'],
    'max_age_days': 30,
    'min_user_messages': 1,
    'cc_entrypoints': ['cli'],
    'path_remap': {'C:\\Windows\\System32': '~'},
    'max_chars': 400000,
    'tool_output_chars': 1500,
    'tool_input_chars': 300,
    'oc_model': None,
}

QUIET = False
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


class HopError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def log(msg):
    line = f'{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}'
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass
    if not QUIET:
        print(msg, flush=True)


def read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def load_config():
    user = read_json(CONFIG_PATH, None)
    if user is None:
        write_json(CONFIG_PATH, DEFAULT_CONFIG, indent=2)
        user = {}
    return {**DEFAULT_CONFIG, **user}


def now_ms():
    return int(time.time() * 1000)


def iso_ms(s):
    if not s:
        return 0
    s = str(s).strip().replace(' ', 'T').replace('Z', '+00:00')
    m = re.match(r'(.*?\.)(\d+)(.*)$', s)
    if m:
        s = m.group(1) + (m.group(2) + '000000')[:6] + m.group(3)
    try:
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return 0


def ms_iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def ago(ms):
    s = max(0, (now_ms() - ms) // 1000)
    for unit, n in (('d', 86400), ('h', 3600), ('m', 60)):
        if s >= n:
            return f'{s // n}{unit} ago'
    return 'just now'


def clip(text, n):
    text = text or ''
    if len(text) <= n:
        return text
    head, tail = n * 2 // 3, n // 3
    return f'{text[:head]}\n... [{len(text) - n} chars cut] ...\n{text[-tail:]}'


def same_path(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def remap(cfg, path):
    for src, dst in (cfg.get('path_remap') or {}).items():
        if same_path(path, os.path.expanduser(src)):
            return os.path.expanduser(dst)
    return path


def el(id_, role, text, ts, kind='msg'):
    return {'id': str(id_), 'role': role, 'text': text, 'ts': ts or 0, 'kind': kind}


def dedup(els):
    pos, out = {}, []
    for e in els:
        if e['id'] in pos:
            out[pos[e['id']]] = e
        else:
            pos[e['id']] = len(out)
            out.append(e)
    return out


CALL_KEYS = ('command', 'CommandLine', 'file_path', 'filePath', 'AbsolutePath', 'TargetFile', 'path',
             'SearchPath', 'pattern', 'Query', 'query', 'Url', 'url', 'description', 'prompt')


def fmt_call(name, args, cfg):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    val = None
    if isinstance(args, dict):
        for k in CALL_KEYS:
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                val = v
                break
    if val is None:
        val = json.dumps(args, ensure_ascii=False) if args else ''
    if len(val) > 1 and val[0] == val[-1] == '"':
        try:
            val = json.loads(val)
        except ValueError:
            pass
    return f'▸ {name}: {clip(str(val).strip(), cfg["tool_input_chars"])}'


def fmt_result(out, cfg):
    out = str(out or '').strip()
    return '  ⎿ ' + clip(out, cfg['tool_output_chars']) if out else '  ⎿ (no output)'


def run_quiet(args, cwd=None, timeout=180):
    env = dict(os.environ, HOP_SYNCING='1')
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8',
                          errors='replace', timeout=timeout, creationflags=NO_WINDOW)


# ---------------------------------------------------------------- Claude Code

def cc_slug(path):
    return re.sub(r'[^A-Za-z0-9]', '-', path)


CC_NOISE_RE = re.compile(r'<(system-reminder|local-command-caveat|local-command-stdout|local-command-stderr)>.*?</\1>', re.S)


def cc_user_text(s):
    m = re.search(r'<command-name>(.*?)</command-name>', s, re.S)
    if m:
        a = re.search(r'<command-args>(.*?)</command-args>', s, re.S)
        return f'{m.group(1).strip()} {a.group(1).strip() if a else ""}'.strip()
    return CC_NOISE_RE.sub('', s).strip()


def cc_blocks_text(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get('type') == 'text':
                out.append(b.get('text') or '')
            elif b.get('type') == 'image':
                out.append('[image]')
    return '\n'.join(out)


def cc_list(cfg):
    out = {}
    if not os.path.isdir(CC_PROJECTS):
        return out
    for d in os.scandir(CC_PROJECTS):
        if not d.is_dir():
            continue
        for f in os.scandir(d.path):
            if f.name.endswith('.jsonl') and f.is_file():
                st = f.stat()
                sid = f.name[:-6]
                out['cc:' + sid] = {'tool': 'cc', 'id': sid, 'path': f.path, 'sig': f'{st.st_mtime_ns}:{st.st_size}',
                                    'updated': int(st.st_mtime * 1000)}
    return out


def cc_read(s, cfg):
    recs, leaf = {}, None
    meta = {'cwd': None, 'entrypoint': None, 'version': None, 'agent': None, 'custom': None, 'ai': None, 'is_hop': False}
    with open(s['path'], encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            t = r.get('type')
            if t == 'custom-title':
                meta['custom'] = r.get('customTitle')
            elif t == 'agent-name':
                meta['agent'] = r.get('agentName')
            elif t == 'ai-title':
                meta['ai'] = r.get('aiTitle')
            u = r.get('uuid')
            if u:
                recs[u] = r
            if t in ('user', 'assistant') and u and not r.get('isSidechain'):
                leaf = u
                meta['cwd'] = meta['cwd'] or r.get('cwd')
                meta['entrypoint'] = meta['entrypoint'] or r.get('entrypoint')
                meta['version'] = r.get('version') or meta['version']
                if t == 'assistant' and (r.get('message') or {}).get('model') == HOP_MODEL:
                    meta['is_hop'] = True

    chain, u, guard = [], leaf, set()
    while u and u in recs and u not in guard:
        guard.add(u)
        chain.append(recs[u])
        u = recs[u].get('parentUuid') or recs[u].get('logicalParentUuid')
    chain.reverse()

    els = []
    for r in chain:
        t = r.get('type')
        if t not in ('user', 'assistant') or r.get('isMeta') or r.get('isApiErrorMessage'):
            continue
        u, ts = r['uuid'], iso_ms(r.get('timestamp'))
        content = (r.get('message') or {}).get('content')
        if t == 'user':
            if r.get('isCompactSummary'):
                txt = cc_blocks_text(content).strip()
                if txt:
                    els.append(el(f'{u}#0', 'user', txt, ts, 'summary'))
                continue
            blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else (content or [])
            for i, b in enumerate(blocks):
                if not isinstance(b, dict):
                    continue
                bt = b.get('type')
                if bt == 'text':
                    txt = cc_user_text(b.get('text') or '')
                    if txt.startswith('<task-notification>'):
                        els.append(el(f'{u}#{i}', 'tool', fmt_result(txt, cfg), ts))
                    elif txt:
                        els.append(el(f'{u}#{i}', 'user', txt, ts))
                elif bt == 'tool_result':
                    els.append(el(f'{u}#{i}', 'tool', fmt_result(cc_blocks_text(b.get('content')), cfg), ts))
                elif bt == 'image':
                    els.append(el(f'{u}#{i}', 'user', '[image]', ts))
        else:
            if isinstance(content, str):
                content = [{'type': 'text', 'text': content}]
            for i, b in enumerate(content or []):
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text' and (b.get('text') or '').strip():
                    els.append(el(f'{u}#{i}', 'assistant', b['text'].strip(), ts))
                elif b.get('type') == 'tool_use':
                    els.append(el(f'{u}#{i}', 'tool', fmt_call(b.get('name'), b.get('input'), cfg), ts))

    s.update(cwd=meta['cwd'] or HOME, entrypoint=meta['entrypoint'], version=meta['version'], is_hop=meta['is_hop'],
             title=meta['agent'] or meta['custom'] or meta['ai'])
    return els


def cc_write(cp, cwd, msgs, title, version):
    sid = cp['id'] if cp else str(uuid.uuid4())
    path = cp['path'] if cp else os.path.join(CC_PROJECTS, cc_slug(cwd), sid + '.jsonl')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    base = {'isSidechain': False, 'userType': 'external', 'entrypoint': 'cli', 'cwd': cwd, 'sessionId': sid,
            'version': version or '2.1.0'}
    lines, seen, parent = [], [], None
    for m in msgs:
        u = str(uuid.uuid4())
        r = dict(base, parentUuid=parent, type=m['role'], uuid=u, timestamp=ms_iso(m['ts']))
        if m['role'] == 'user':
            r['message'] = {'role': 'user', 'content': m['text']}
        else:
            r['message'] = {'id': 'msg_hop_' + uuid.uuid4().hex[:24], 'type': 'message', 'role': 'assistant',
                            'model': HOP_MODEL, 'content': [{'type': 'text', 'text': m['text']}],
                            'stop_reason': 'end_turn', 'stop_sequence': None,
                            'usage': {'input_tokens': 0, 'output_tokens': 0}}
        lines.append(json.dumps(r, ensure_ascii=False))
        seen.append(u + '#0')
        parent = u
    lines.append(json.dumps({'type': 'custom-title', 'customTitle': title, 'sessionId': sid}, ensure_ascii=False))
    tmp = path + '.hoptmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(tmp, path)
    last = max(m['ts'] for m in msgs) / 1000
    os.utime(path, (last, last))
    st = os.stat(path)
    return {'id': sid, 'path': path, 'seen': seen, 'sig': f'{st.st_mtime_ns}:{st.st_size}'}


def cc_retitle(cp, title):
    if time.time() - os.path.getmtime(cp['path']) < 30:
        return False
    with open(cp['path'], 'a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps({'type': 'custom-title', 'customTitle': title, 'sessionId': cp['id']}, ensure_ascii=False) + '\n')
    return True


# ---------------------------------------------------------------- OpenCode

B62 = string.digits + string.ascii_letters
_oid_last = [0, 0]


def oc_id(prefix, ts, desc=False):
    """Same layout as OpenCode's Identifier.create: 6 time bytes (hex) + 14 random base62 chars."""
    if ts != _oid_last[0]:
        _oid_last[0], _oid_last[1] = ts, 0
    _oid_last[1] += 1
    n = ts * 0x1000 + _oid_last[1]
    if desc:
        n = ~n
    hx = ''.join('%02x' % ((n >> (40 - 8 * i)) & 0xff) for i in range(6))
    return f'{prefix}_{hx}' + ''.join(secrets.choice(B62) for _ in range(14))


def oc_connect(rw=False):
    if not os.path.exists(OC_DB):
        return None
    return sqlite3.connect(f'file:{OC_DB}?mode={"rw" if rw else "ro"}', uri=True, timeout=15)


def oc_bin():
    p = shutil.which('opencode')
    if not p:
        raise HopError('opencode not found on PATH')
    exe = os.path.join(os.path.dirname(p), 'node_modules', 'opencode-ai', 'bin', 'opencode.exe')
    return exe if os.path.exists(exe) else p


def oc_list(cfg):
    c = oc_connect()
    if not c:
        return {}
    try:
        stats = {sid: (n, mx) for sid, n, mx in
                 c.execute('select session_id, count(*), max(time_updated) from part group by session_id')}
        out = {}
        for sid, directory, title, slug, upd in c.execute(
                'select id, directory, title, slug, time_updated from session '
                'where parent_id is null and time_archived is null'):
            n, mx = stats.get(sid, (0, 0))
            out['oc:' + sid] = {'tool': 'oc', 'id': sid, 'cwd': os.path.normpath(directory), 'title': title,
                                'is_hop': (slug or '').startswith(HOP_SLUG), 'sig': f'{upd}:{n}:{mx}',
                                'updated': upd}
        return out
    finally:
        c.close()


def oc_sig(sid):
    c = oc_connect()
    try:
        upd = c.execute('select time_updated from session where id=?', (sid,)).fetchone()
        n, mx = c.execute('select count(*), max(time_updated) from part where session_id=?', (sid,)).fetchone()
        return f'{upd[0] if upd else 0}:{n}:{mx}'
    finally:
        c.close()


def oc_read(s, cfg):
    c = oc_connect()
    try:
        order, msgs, parts = [], {}, {}
        for mid, data in c.execute('select id, data from message where session_id=? order by time_created, id', (s['id'],)):
            msgs[mid] = json.loads(data)
            order.append(mid)
        for pid, mid, data in c.execute('select id, message_id, data from part where session_id=? order by id', (s['id'],)):
            parts.setdefault(mid, []).append((pid, json.loads(data)))
    finally:
        c.close()
    els = []
    for mid in order:
        m = msgs[mid]
        role = m.get('role')
        kind = 'summary' if role == 'assistant' and m.get('summary') else 'msg'
        mts = (m.get('time') or {}).get('created') or 0
        for pid, p in parts.get(mid, []):
            pt = p.get('type')
            ts = (p.get('time') or {}).get('start') or mts
            if pt == 'text':
                txt = (p.get('text') or '').strip()
                if txt and not p.get('synthetic') and not p.get('ignored'):
                    els.append(el(pid, 'user' if role == 'user' else 'assistant', txt, ts, kind))
            elif pt == 'tool':
                st = p.get('state') or {}
                if st.get('status') in ('pending', 'running'):
                    continue
                out = st.get('output') if st.get('status') == 'completed' else st.get('error')
                els.append(el(pid, 'tool', fmt_call(p.get('tool'), st.get('input'), cfg) + '\n' + fmt_result(out, cfg), ts))
            elif pt == 'file':
                els.append(el(pid, 'user' if role == 'user' else 'tool',
                              f'[file: {p.get("filename") or p.get("url") or "?"}]', ts))
    return els


def oc_model(cfg):
    if cfg.get('oc_model'):
        provider, _, model = cfg['oc_model'].partition('/')
        return {'providerID': provider, 'modelID': model}
    c = oc_connect()
    try:
        row = c.execute("select json_extract(data, '$.model') from message where json_extract(data, '$.role')='user' "
                        "order by time_created desc limit 1").fetchone()
        ver = c.execute('select version from session order by time_updated desc limit 1').fetchone()
    finally:
        c.close()
    if not row or not row[0]:
        raise HopError('no OpenCode model found; set "oc_model" in ~/.hop/config.json (provider/model)')
    m = json.loads(row[0])
    return {'providerID': m['providerID'], 'modelID': m['modelID'], '_version': ver[0] if ver else '1.0.0'}


def oc_write(cp, cwd, msgs, title, model):
    if not os.path.isdir(cwd):
        raise HopError(f'folder no longer exists: {cwd}')
    first = msgs[0]['ts'] or now_ms()
    sid = cp['id'] if cp else oc_id('ses', first, desc=True)
    mdl = {'providerID': model['providerID'], 'modelID': model['modelID']}
    doc_msgs, seen, last, parent = [], [], 0, None
    for m in msgs:
        ts = max(m['ts'] or 0, last + 1)
        last = ts
        mid, pid = oc_id('msg', ts), oc_id('prt', ts)
        if m['role'] == 'user':
            info = {'id': mid, 'sessionID': sid, 'role': 'user', 'time': {'created': ts}, 'agent': 'build', 'model': mdl}
            parent = mid
        else:
            info = {'id': mid, 'sessionID': sid, 'role': 'assistant', 'parentID': parent, 'mode': 'build',
                    'agent': 'build', 'path': {'cwd': cwd, 'root': '/'}, 'cost': 0,
                    'tokens': {'input': 0, 'output': 0, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}},
                    'modelID': mdl['modelID'], 'providerID': mdl['providerID'],
                    'time': {'created': ts, 'completed': ts}, 'finish': 'stop'}
        doc_msgs.append({'info': info, 'parts': [{'id': pid, 'sessionID': sid, 'messageID': mid, 'type': 'text',
                                                  'text': m['text']}]})
        seen.append(pid)
    doc = {'info': {'id': sid, 'slug': HOP_SLUG + sid[-8:].lower(), 'projectID': 'global', 'directory': cwd,
                    'title': title, 'version': model.get('_version', '1.0.0'),
                    'time': {'created': first, 'updated': last}},
           'messages': doc_msgs}
    tmp = os.path.join(HOP_DIR, 'tmp', sid + '.json')
    write_json(tmp, doc)
    exe = oc_bin()
    try:
        if cp:
            run_quiet([exe, 'session', 'delete', sid, '--pure'])
        r = run_quiet([exe, 'import', tmp, '--pure'], cwd=cwd)
        if 'Imported session' not in (r.stdout + r.stderr):
            raise HopError(f'opencode import failed: {(r.stderr or r.stdout).strip()[-300:]}')
    finally:
        os.remove(tmp)
    return {'id': sid, 'seen': seen, 'sig': oc_sig(sid)}


def oc_retitle(cp, title):
    c = oc_connect(rw=True)
    try:
        c.execute('update session set title=? where id=?', (title, cp['id']))
        c.commit()
    finally:
        c.close()
    return True


# ---------------------------------------------------------------- Antigravity CLI (read-only)

AGY_HDR_RE = re.compile(r'^(Created At|Completed At):.*\n?', re.M)


def agy_transcript(cid):
    base = os.path.join(AGY_DIR, 'brain', cid, '.system_generated', 'logs')
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


def agy_list(cfg):
    db = os.path.join(AGY_DIR, 'conversation_summaries.db')
    if not os.path.exists(db):
        return {}
    c = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=15)
    try:
        rows = c.execute('select conversation_id, title, last_modified_time, workspace_uris, '
                         'parent_conversation_id, nesting_depth from conversation_summaries').fetchall()
    finally:
        c.close()
    out = {}
    for cid, title, modified, ws, parent, depth in rows:
        path = agy_transcript(cid)
        if parent or depth or not path:
            continue
        try:
            uris = json.loads(ws or '[]')
        except ValueError:
            uris = []
        st = os.stat(path)
        out['agy:' + cid] = {'tool': 'agy', 'id': cid, 'path': path, 'title': title,
                             'cwd': remap(cfg, uri_to_path(uris[0]) if uris else HOME),
                             'sig': f'{st.st_mtime_ns}:{st.st_size}', 'is_hop': False,
                             'updated': iso_ms(modified) or int(st.st_mtime * 1000)}
    return out


def agy_read(s, cfg):
    els = []
    with open(s['path'], encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
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
                els.append(el(idx, 'tool', fmt_result(AGY_HDR_RE.sub('', content).strip() or d.get('error'), cfg), ts))
    return els


READERS = {'cc': cc_read, 'oc': oc_read, 'agy': agy_read}
LISTERS = {'cc': cc_list, 'oc': oc_list, 'agy': agy_list}


def load(s, cfg):
    if 'els' not in s:
        s['els'] = dedup(READERS[s['tool']](s, cfg))
    return s['els']


# ---------------------------------------------------------------- threads

def new_thread(origin, base_title, cwd):
    return {'tid': uuid.uuid4().hex[:12], 'origin': origin, 'base_title': base_title, 'cwd': cwd,
            'created': now_ms(), 'updated': 0, 'head': origin, 'elements': [], 'copies': {}, 'suppressed': []}


def authors(th):
    seen = []
    for e in th['elements']:
        if e.get('author') and e['author'] not in seen:
            seen.append(e['author'])
    return seen


def thread_title(th):
    return f"{th['base_title']} ({'+'.join(NAMES[a] for a in authors(th))})"


def clean_title(title, els):
    title = TAG_RE.sub('', (title or '').strip())
    if not title or title.startswith('New session - '):
        first = next((e['text'] for e in els if e['role'] == 'user' and e['kind'] == 'msg'), 'Untitled')
        title = first.strip().splitlines()[0][:60] if first.strip() else 'Untitled'
    return title


def render(th, cfg):
    els = th['elements']
    start = max((i for i, e in enumerate(els) if e['kind'] == 'summary'), default=0)
    msgs = []
    for e in els[start:]:
        if e['kind'] == 'summary':
            role, text = 'user', '[Summary of earlier conversation]\n' + e['text']
        else:
            role, text = ('user' if e['role'] == 'user' else 'assistant'), e['text']
        if msgs and msgs[-1]['role'] == role:
            msgs[-1]['text'] += '\n\n' + text
        else:
            msgs.append({'role': role, 'text': text, 'ts': e['ts']})
    dropped, total = 0, sum(len(m['text']) for m in msgs)
    while total > cfg['max_chars'] and len(msgs) > 2:
        total -= len(msgs.pop(0)['text'])
        dropped += 1
    for m in msgs:
        m['text'] = clip(m['text'], cfg['max_chars'])
    src = '+'.join(NAMES[a] for a in authors(th))
    note = (f'[hop] Conversation carried over from {src} ("{th["base_title"]}"). '
            'Tool calls from other agents appear as text: "▸ tool: input" then "⎿ result".')
    if dropped:
        note += f' {dropped} earlier messages were omitted to fit the context window.'
    if msgs and msgs[0]['role'] == 'user':
        msgs[0]['text'] = note + '\n\n' + msgs[0]['text']
    else:
        msgs.insert(0, {'role': 'user', 'text': note, 'ts': msgs[0]['ts'] if msgs else now_ms()})
    return msgs


class Sync:
    def __init__(self, cfg, days=None, dry=False):
        self.cfg, self.dry = cfg, dry
        self.days = days or cfg['max_age_days']
        self.idx = read_json(INDEX_PATH, {})
        self.idx.setdefault('copies', {})
        self.idx.setdefault('skip', {})
        self.threads, self.dirty, self.touched = {}, set(), set()
        self.stats = Counter()
        self.model = None

    def thread(self, tid):
        if tid not in self.threads:
            self.threads[tid] = read_json(os.path.join(THREAD_DIR, tid + '.json'), None)
        return self.threads[tid]

    def save(self, tid=None):
        if self.dry:
            return
        for t in ([tid] if tid else list(self.touched | self.dirty)):
            if self.threads.get(t):
                write_json(os.path.join(THREAD_DIR, t + '.json'), self.threads[t])
        write_json(INDEX_PATH, self.idx)
        try:
            os.utime(LOCK_PATH)
        except OSError:
            pass

    def run(self):
        self.sessions, self.listed = {}, set()
        for tool, lister in LISTERS.items():
            try:
                self.sessions.update(lister(self.cfg))
                self.listed.add(tool)
            except Exception as e:  # one broken store must not stop the others
                log(f'! could not list {NAMES[tool]} sessions: {e}')
        self.claim_handoffs()
        self.scan_copies()
        self.adopt_new()
        self.materialize()
        self.save()
        return self.stats

    def claim_handoffs(self):
        """Attach agy conversations started by `hop agy` to the thread they continue."""
        path = os.path.join(HANDOFF_DIR, 'pending.json')
        pending = {t: ts for t, ts in read_json(path, {}).items() if now_ms() - ts < 86400000}
        if not pending:
            return
        for key, s in self.sessions.items():
            if s['tool'] != 'agy' or key in self.idx['copies'] or s['updated'] < min(pending.values(), default=now_ms()):
                continue
            els = load(s, self.cfg)
            first = next((e for e in els if e['role'] == 'user'), None)
            m = first and HANDOFF_RE.search(first['text'])
            th = m and self.thread(m.group(1))
            if not th:
                continue
            users = [i for i, e in enumerate(els) if e['role'] == 'user']
            cut = users[1] if len(users) > 1 else len(els)
            th['copies'][key] = {'owned': False, 'seen': [e['id'] for e in els[:cut]], 'sig': None,
                                 'upto': len(th['elements'])}
            self.idx['copies'][key] = th['tid']
            pending.pop(th['tid'], None)
            self.touched.add(th['tid'])
            log(f'= linked agy conversation to "{th["base_title"]}"')
        if not self.dry:
            write_json(path, pending)

    def scan_copies(self):
        grown = {}
        for key, tid in list(self.idx['copies'].items()):
            th = self.thread(tid)
            if not th or key not in th['copies']:
                self.idx['copies'].pop(key, None)
                continue
            cp, s = th['copies'][key], self.sessions.get(key)
            if s is None:
                if key.split(':')[0] not in self.listed:
                    continue
                if cp.get('broken'):
                    del th['copies'][key]
                    self.idx['copies'].pop(key, None)
                    self.touched.add(tid)
                elif cp['owned']:
                    tool = key.split(':')[0]
                    log(f'- {NAMES[tool]} mirror was deleted, not recreating: "{th["base_title"]}"')
                    th['suppressed'].append(tool)
                    del th['copies'][key]
                    self.idx['copies'].pop(key, None)
                    self.touched.add(tid)
                continue
            if s['sig'] == cp.get('sig'):
                continue
            try:
                els = load(s, self.cfg)
            except Exception as e:
                log(f'! could not read {key}: {e}')
                continue
            known = set(cp['seen'])
            new = [e for e in els if e['id'] not in known]
            cp['sig'] = s['sig']
            self.touched.add(tid)
            if new:
                grown.setdefault(tid, []).append((key, new, s['updated']))

        for tid, items in grown.items():
            th = self.thread(tid)
            n = len(th['elements'])
            items.sort(key=lambda x: x[2])
            for key, new, upd in items:
                cp = th['copies'][key]
                tool = key.split(':')[0]
                if key != items[-1][0] or cp['upto'] < n:
                    self.fork(th, key, new, upd)
                    continue
                th['elements'] += [dict(e, author=tool) for e in new]
                cp['seen'] += [e['id'] for e in new]
                cp['upto'] = len(th['elements'])
                th['head'], th['updated'] = key, max(th['updated'], upd)
                self.dirty.add(tid)
                self.stats['continued'] += 1

    def fork(self, th, key, new, upd):
        cp = th['copies'].pop(key)
        tool = key.split(':')[0]
        nt = new_thread(key, th['base_title'] + ' [fork]', th['cwd'])
        nt['elements'] = [dict(e) for e in th['elements'][:cp['upto']]] + [dict(e, author=tool) for e in new]
        cp['seen'] += [e['id'] for e in new]
        cp['upto'] = len(nt['elements'])
        nt['copies'][key], nt['updated'] = cp, upd
        self.threads[nt['tid']] = nt
        self.idx['copies'][key] = nt['tid']
        self.touched.add(th['tid'])
        self.dirty.add(nt['tid'])
        self.stats['forked'] += 1
        log(f'~ {NAMES[tool]} copy diverged, split into its own chat: "{nt["base_title"]}"')

    def ineligible(self, s, els):
        if s.get('is_hop'):
            return 'hop mirror'
        if s['tool'] == 'cc' and s.get('entrypoint') not in self.cfg['cc_entrypoints']:
            return f'entrypoint {s.get("entrypoint")}'
        if sum(1 for e in els if e['role'] == 'user' and e['kind'] == 'msg') < self.cfg['min_user_messages']:
            return 'too short'
        if not any(e['role'] != 'user' for e in els):
            return 'no reply yet'
        return None

    def adopt_new(self):
        cutoff = now_ms() - self.days * 86400000
        for key, s in sorted(self.sessions.items(), key=lambda kv: kv[1]['updated']):
            if key in self.idx['copies'] or s['updated'] < cutoff:
                continue
            if self.idx['skip'].get(key) == s['sig']:
                continue
            try:
                els = load(s, self.cfg)
            except Exception as e:
                log(f'! could not read {key}: {e}')
                continue
            if s['tool'] == 'cc' and s.get('version'):
                self.idx['cc_version'] = s['version']
            why = self.ineligible(s, els)
            if why:
                self.idx['skip'][key] = s['sig']
                continue
            self.idx['skip'].pop(key, None)
            th = new_thread(key, clean_title(s.get('title'), els), s.get('cwd') or HOME)
            th['elements'] = [dict(e, author=s['tool']) for e in els]
            th['copies'][key] = {'owned': False, 'seen': [e['id'] for e in els], 'sig': s['sig'],
                                 'upto': len(els)}
            th['updated'] = s['updated']
            self.threads[th['tid']] = th
            self.idx['copies'][key] = th['tid']
            self.dirty.add(th['tid'])
            self.stats['new'] += 1

    def materialize(self):
        for tid in self.idx.pop('retry', []):
            if self.thread(tid):
                self.dirty.add(tid)
        for tid in sorted(self.dirty, key=lambda t: self.threads[t]['updated']):
            th = self.threads[tid]
            n, title = len(th['elements']), thread_title(th)
            for tool in self.cfg['targets']:
                if tool not in WRITABLE or tool in th['suppressed']:
                    continue
                mine = {k: cp for k, cp in th['copies'].items() if k.startswith(tool + ':')}
                current = [(k, cp) for k, cp in mine.items() if cp['upto'] == n]
                if current:
                    for k, cp in current:
                        if cp['owned'] and cp.get('title') != title and not self.dry:
                            ok = (cc_retitle if tool == 'cc' else oc_retitle)(cp, title)
                            if ok:
                                cp['title'] = title
                    continue
                owned = next(((k, cp) for k, cp in mine.items() if cp['owned']), (None, None))
                verb = 'update' if owned[0] else 'create'
                if self.dry:
                    log(f'[dry-run] {verb} {NAMES[tool]}: {title}')
                    self.stats[verb] += 1
                    continue
                try:
                    msgs = render(th, self.cfg)
                    if tool == 'cc':
                        res = cc_write(owned[1], th['cwd'], msgs, title, self.idx.get('cc_version'))
                    else:
                        self.model = self.model or oc_model(self.cfg)
                        res = oc_write(owned[1], th['cwd'], msgs, title, self.model)
                except Exception as e:
                    log(f'! {NAMES[tool]} {verb} failed for "{title}": {e}')
                    self.stats['failed'] += 1
                    if owned[1]:
                        owned[1]['broken'] = True  # an update deletes before importing; don't mistake it for user deletion
                    if not isinstance(e, HopError) or 'no longer exists' not in str(e):
                        self.idx.setdefault('retry', []).append(tid)
                    continue
                key = f'{tool}:{res["id"]}'
                cp = th['copies'].setdefault(key, {'owned': True})
                cp.pop('broken', None)
                cp.update(seen=res['seen'], sig=res['sig'], upto=n, title=title)
                if res.get('path'):
                    cp['path'], cp['id'] = res['path'], res['id']
                else:
                    cp['id'] = res['id']
                self.idx['copies'][key] = tid
                self.stats[verb] += 1
                log(f'{"+" if verb == "create" else "~"} {NAMES[tool]}: {title}')
                self.save(tid)


# ---------------------------------------------------------------- locking / detaching

def locked_sync(cfg, days=None, dry=False):
    os.makedirs(HOP_DIR, exist_ok=True)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            stale = time.time() - os.path.getmtime(LOCK_PATH) > 600
        except OSError:
            stale = True
        if not stale:
            open(PENDING_PATH, 'w').close()
            log('sync already running; queued another pass')
            return None
        os.remove(LOCK_PATH)
        return locked_sync(cfg, days, dry)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    total = Counter()
    try:
        while True:
            if os.path.exists(PENDING_PATH):
                os.remove(PENDING_PATH)
            total += Sync(cfg, days, dry).run()
            if dry or not os.path.exists(PENDING_PATH):
                break
    finally:
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
    return total


def detach(argv):
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    if os.name == 'nt' and os.path.exists(pyw):
        exe = pyw
    flags = (0x00000008 | 0x00000200) if os.name == 'nt' else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe, '-m', 'samethread'] + argv, cwd=HOME, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=os.name != 'nt')


# ---------------------------------------------------------------- commands

def all_threads():
    idx = read_json(INDEX_PATH, {})
    out = {}
    for tid in set(idx.get('copies', {}).values()):
        th = read_json(os.path.join(THREAD_DIR, tid + '.json'), None)
        if th:
            out[tid] = th
    return sorted(out.values(), key=lambda t: t['updated'], reverse=True)


def resume_cmd(key, th):
    tool, sid = key.split(':', 1)
    return {'cc': f'claude --resume {sid}', 'oc': f'opencode -s {sid}', 'agy': f'agy --conversation {sid}'}[tool]


def pick(threads, query):
    if query is None:
        return threads[0] if threads else None
    if query.isdigit() and 1 <= int(query) <= len(threads):
        return threads[int(query) - 1]
    hits = [t for t in threads if query.lower() in thread_title(t).lower() or t['tid'].startswith(query)]
    return hits[0] if hits else None


def scoped(args):
    threads = all_threads()
    if not args.all:
        threads = [t for t in threads if same_path(t['cwd'], os.getcwd())]
    return threads


def cmd_list(args):
    threads = scoped(args)
    if not threads:
        print('No chats for this folder yet.' + ('' if args.all else ' Try: hop list --all'))
        return
    for i, th in enumerate(threads[:args.limit], 1):
        muted = '  ·  not mirrored' if set(WRITABLE) <= set(th.get('suppressed', [])) else ''
        print(f'{i:>3}. {thread_title(th)}  ·  {ago(th["updated"])}{muted}' + (f'  ·  {th["cwd"]}' if args.all else ''))
        for key, cp in sorted(th['copies'].items()):
            state = 'mirror' if cp['owned'] else 'original'
            stale = '' if cp['upto'] == len(th['elements']) else ' (behind)'
            print(f'       {NAMES[key.split(":")[0]]:<8} {state:<8}{stale}  {resume_cmd(key, th)}')


def cmd_resume(args):
    th = pick(scoped(args), args.query)
    if not th:
        sys.exit('No matching chat. See: hop list')
    tool = args.tool
    keys = [k for k in th['copies'] if k.startswith(tool + ':')]
    if not keys:
        sys.exit(f'No {NAMES[tool]} copy of "{thread_title(th)}" yet. Run: hop sync')
    key = max(keys, key=lambda k: (th['copies'][k]['upto'], th['copies'][k]['owned']))
    cmd = resume_cmd(key, th).split()
    cwd = th['cwd'] if os.path.isdir(th['cwd']) else None
    sys.exit(subprocess.call([shutil.which(cmd[0]) or cmd[0]] + cmd[1:], cwd=cwd))


def cmd_agy(args):
    cfg = load_config()
    th = pick(scoped(args), args.query)
    if not th:
        sys.exit('No matching chat. See: hop list')
    msgs = render(th, cfg)
    path = os.path.join(HANDOFF_DIR, th['tid'] + '.md')
    body = [f'# {thread_title(th)}\n']
    for m in msgs:
        body.append(f'## {"User" if m["role"] == "user" else "Assistant"}\n\n{m["text"]}\n')
    os.makedirs(HANDOFF_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(body))
    pending = read_json(os.path.join(HANDOFF_DIR, 'pending.json'), {})
    pending[th['tid']] = now_ms() - 60000
    write_json(os.path.join(HANDOFF_DIR, 'pending.json'), pending)
    prompt = (f'[hop-handoff:{th["tid"]}] We are continuing the conversation "{th["base_title"]}" that started in '
              f'{"+".join(NAMES[a] for a in authors(th))}. Read the whole transcript at {path} with your file tool '
              f'(all of it, page through if long). Then reply with one short line saying you are ready, and wait for me.')
    agy = shutil.which('agy') or 'agy'
    cwd = th['cwd'] if os.path.isdir(th['cwd']) else None
    sys.exit(subprocess.call([agy, '-i', prompt], cwd=cwd))


def cmd_sync(args):
    if args.detach:
        detach(['sync', '--quiet'] + (['--days', str(args.days)] if args.days else []))
        return
    stats = locked_sync(load_config(), args.days, args.dry_run)
    if stats is not None and not QUIET:
        print('done: ' + (', '.join(f'{k} {v}' for k, v in sorted(stats.items())) or 'nothing changed'))


def cmd_hook(args):
    """Entry point for tool hooks: never blocks the tool, always prints valid JSON for agy."""
    if args.json:
        print('{}', flush=True)
    if not os.environ.get('HOP_SYNCING'):
        detach(['sync', '--quiet'])


def wait_lock(seconds=60):
    deadline = time.time() + seconds
    while True:
        try:
            os.close(os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            if time.time() > deadline:
                return False
            time.sleep(0.5)


def delete_copy(key, cp):
    tool = key.split(':')[0]
    if tool == 'cc' and os.path.exists(cp['path']):
        os.remove(cp['path'])
    elif tool == 'oc':
        run_quiet([oc_bin(), 'session', 'delete', cp['id'], '--pure'])


def cmd_forget(args):
    th = pick(scoped(args), args.query)
    if not th:
        sys.exit('No matching chat. See: hop list')
    if not wait_lock():
        sys.exit('A sync is still running; try again in a moment.')
    try:
        th = read_json(os.path.join(THREAD_DIR, th['tid'] + '.json'), th)
        for key, cp in list(th['copies'].items()):
            if cp['owned']:
                delete_copy(key, cp)
                del th['copies'][key]
        th['suppressed'] = list(WRITABLE)
        write_json(os.path.join(THREAD_DIR, th['tid'] + '.json'), th)
    finally:
        os.remove(LOCK_PATH)
    print(f'Deleted the mirrors of "{thread_title(th)}" and stopped mirroring it. The original is untouched.')


def cmd_clean(args):
    threads = all_threads()
    owned = [(k, cp, th) for th in threads for k, cp in th['copies'].items() if cp['owned']]
    if not args.yes:
        for k, cp, th in owned:
            print(f'would delete {NAMES[k.split(":")[0]]} mirror: {cp.get("title") or thread_title(th)}')
        print(f'{len(owned)} mirrors. Originals are untouched. Re-run with --yes to delete.')
        return
    for k, cp, th in owned:
        try:
            delete_copy(k, cp)
        except Exception as e:
            print(f'! {k}: {e}')
    shutil.rmtree(THREAD_DIR, ignore_errors=True)
    for p in (INDEX_PATH, os.path.join(HANDOFF_DIR, 'pending.json')):
        if os.path.exists(p):
            os.remove(p)
    print(f'deleted {len(owned)} mirrors and reset hop state')


def main():
    global QUIET
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(prog='hop', description='SameThread: one chat history across Claude Code, OpenCode and agy.')
    ap.add_argument('--version', action='version', version=f'samethread {__version__}')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('install', help='wire up auto-sync hooks for Claude Code, OpenCode and agy')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(fn=lambda a: integrations.install(a.dry_run))

    p = sub.add_parser('uninstall', help='remove the hooks `hop install` added (chats and mirrors stay)')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(fn=lambda a: integrations.uninstall(a.dry_run))

    p = sub.add_parser('sync', help='mirror new/changed chats into every tool')
    p.add_argument('--days', type=int, help='only adopt chats active in the last N days (default: config)')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--detach', action='store_true', help='run in the background and return immediately')
    p.add_argument('-q', '--quiet', action='store_true')
    p.set_defaults(fn=cmd_sync)

    for name, fn, hlp in (('list', cmd_list, 'show chats for this folder and how to resume them'),
                          ('resume', cmd_resume, 'open a chat in cc (Claude) or oc (OpenCode)'),
                          ('agy', cmd_agy, 'continue a chat in agy by seeding a new conversation'),
                          ('forget', cmd_forget, "delete a chat's mirrors and stop mirroring it")):
        p = sub.add_parser(name, help=hlp)
        if name == 'resume':
            p.add_argument('tool', choices=['cc', 'oc', 'agy'])
        if name != 'list':
            p.add_argument('query', nargs='?', help='number from `hop list`, or part of the title')
        p.add_argument('-a', '--all', action='store_true', help='all folders, not just the current one')
        p.add_argument('-n', '--limit', type=int, default=30)
        p.set_defaults(fn=fn)

    p = sub.add_parser('hook', help='called by tool hooks; starts a background sync')
    p.add_argument('--json', action='store_true', help='print {} for hook protocols that need JSON (agy)')
    p.set_defaults(fn=cmd_hook)

    p = sub.add_parser('clean', help='delete every mirror hop created and reset its state')
    p.add_argument('--yes', action='store_true')
    p.set_defaults(fn=cmd_clean)

    args = ap.parse_args()
    QUIET = getattr(args, 'quiet', False) or args.cmd == 'hook'
    os.makedirs(HOP_DIR, exist_ok=True)
    args.fn(args)
