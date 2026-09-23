"""Helpers the agent adapters use to add and remove their auto-sync hooks.

Every hook runs `python -m samethread hook`, which starts a background sync and
returns at once. `hop uninstall` removes exactly the entries these helpers add.
"""
import json
import os
import shutil
import sys

MARK = '-m samethread hook'


def bare_python():
    """Interpreter path without spaces, so it needs no quoting in bash, PowerShell or `cmd /c`."""
    exe = sys.executable
    if os.name == 'nt' and ' ' in exe:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(exe, buf, 1024):
            exe = buf.value
    return exe


def command(slash='/', extra=''):
    exe = bare_python()
    return f'{exe.replace(chr(92), "/") if slash == "/" else exe} {MARK}{extra}'


def json_hook():
    """A Claude-style command hook. Falls back to exec form when the path can't avoid spaces."""
    cmd = command()
    if ' ' not in cmd.split(' -m ')[0]:
        return {'type': 'command', 'command': cmd, 'timeout': 30}
    return {'type': 'command', 'command': sys.executable, 'args': ['-m', 'samethread', 'hook'], 'timeout': 30}


def is_ours(hook):
    return isinstance(hook, dict) and (MARK in (hook.get('command') or '')
                                       or hook.get('args') == ['-m', 'samethread', 'hook'])


def load(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(path, data, dry):
    if dry:
        print(f'  [dry-run] would write {path}')
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and not os.path.exists(path + '.samethread.bak'):
        shutil.copy2(path, path + '.samethread.bak')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, path)


def add_json_hooks(path, events, dry, root=None):
    """Merge a command hook into a Claude-style {"hooks": {Event: [{"hooks": [...]}]}} file."""
    data = load(path)
    container = data.setdefault(root, {}) if root else data
    hooks = container.setdefault('hooks', {})
    added = []
    for event in events:
        groups = hooks.setdefault(event, [])
        if any(is_ours(h) for g in groups for h in g.get('hooks', [])):
            continue
        groups.append({'hooks': [json_hook()]})
        added.append(event)
    if added:
        save(path, data, dry)
    return f'{" + ".join(added)} hook added ({path})' if added else 'hook already installed'


def remove_json_hooks(path, dry, root=None):
    data = load(path)
    container = (data.get(root) or {}) if root else data
    hooks = container.get('hooks') or {}
    removed = 0
    for event in list(hooks):
        kept = []
        for g in hooks[event]:
            mine = [h for h in g.get('hooks', []) if is_ours(h)]
            removed += len(mine)
            if not mine:
                kept.append(g)
            elif len(mine) < len(g['hooks']):
                kept.append(dict(g, hooks=[h for h in g['hooks'] if not is_ours(h)]))
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if removed:
        save(path, data, dry)
    return f'removed {removed} hooks' if removed else 'nothing to remove'


def write_file(path, content, dry):
    try:
        with open(path, encoding='utf-8') as f:
            if f.read() == content:
                return 'already installed'
    except FileNotFoundError:
        pass
    if dry:
        print(f'  [dry-run] would write {path}')
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)
    return f'added ({path})'


def remove_file(path, dry):
    if not os.path.exists(path):
        return 'nothing to remove'
    if not dry:
        os.remove(path)
    return 'removed'


def js_kicker(events_js):
    """Body of a JS plugin that spawns a detached `python -m samethread hook` on the given events."""
    return f'''import {{ spawn }} from "node:child_process"

const PYTHON = {json.dumps(sys.executable)}

function kick() {{
  if (process.env.HOP_SYNCING) return
  try {{
    const child = spawn(PYTHON, ["-m", "samethread", "hook"], {{ detached: true, stdio: "ignore", windowsHide: true }})
    child.on("error", () => {{}})
    child.unref()
  }} catch {{}}
}}
{events_js}'''
