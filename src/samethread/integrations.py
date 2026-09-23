"""Auto-sync wiring for each agent CLI.

`hop install` adds one hook per tool that starts a background sync after every
turn; `hop uninstall` removes exactly those entries and nothing else.
"""
import json
import os
import shutil
import sys

HOME = os.path.expanduser('~')
MARK = '-m samethread hook'

CLAUDE_DIR = os.path.join(HOME, '.claude')
CLAUDE_SETTINGS = os.path.join(CLAUDE_DIR, 'settings.json')
AGY_DIR = os.path.join(HOME, '.gemini', 'antigravity-cli')
AGY_HOOKS = os.path.join(HOME, '.gemini', 'config', 'hooks.json')
OC_DIR = os.path.join(HOME, '.config', 'opencode')
OC_PLUGIN = os.path.join(OC_DIR, 'plugins', 'samethread.js')

OC_PLUGIN_SRC = '''// SameThread: after each OpenCode turn, mirror chats into the other agent CLIs.
// Installed by `hop install`; removed by `hop uninstall`.
import { spawn } from "node:child_process"

const PYTHON = %s

function kick() {
  if (process.env.HOP_SYNCING) return
  try {
    const child = spawn(PYTHON, ["-m", "samethread", "hook"], { detached: true, stdio: "ignore", windowsHide: true })
    child.on("error", () => {})
    child.unref()
  } catch {}
}

export const SameThread = async () => {
  kick()
  return {
    event: async ({ event }) => {
      if (event.type === "session.idle") kick()
    },
  }
}
'''


def _bare_python():
    """Interpreter path without spaces, so it needs no quoting in bash, PowerShell or `cmd /c`."""
    exe = sys.executable
    if os.name == 'nt' and ' ' in exe:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(exe, buf, 1024):
            exe = buf.value
    return exe


def _claude_hook():
    exe = _bare_python().replace('\\', '/')
    if ' ' not in exe:
        return {'type': 'command', 'command': f'{exe} {MARK}', 'timeout': 10}
    # Exec form: no shell involved, so spaces in the path are fine.
    return {'type': 'command', 'command': sys.executable, 'args': ['-m', 'samethread', 'hook'], 'timeout': 10}


def _load(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(path, data, dry):
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


def _ours(hook):
    return MARK in (hook.get('command') or '') or hook.get('args') == ['-m', 'samethread', 'hook']


def install_claude(dry):
    if not os.path.isdir(CLAUDE_DIR):
        return 'Claude Code: not found, skipped'
    settings = _load(CLAUDE_SETTINGS)
    hooks = settings.setdefault('hooks', {})
    added = []
    for event in ('Stop', 'SessionEnd'):
        groups = hooks.setdefault(event, [])
        if any(_ours(h) for g in groups for h in g.get('hooks', [])):
            continue
        groups.append({'hooks': [_claude_hook()]})
        added.append(event)
    if added:
        _save(CLAUDE_SETTINGS, settings, dry)
    return f'Claude Code: {" + ".join(added)} hooks added' if added else 'Claude Code: hooks already installed'


def uninstall_claude(dry):
    settings = _load(CLAUDE_SETTINGS)
    hooks = settings.get('hooks') or {}
    removed = 0
    for event in list(hooks):
        kept = []
        for g in hooks[event]:
            mine = [h for h in g.get('hooks', []) if _ours(h)]
            removed += len(mine)
            if not mine:
                kept.append(g)
            elif len(mine) < len(g['hooks']):
                kept.append(dict(g, hooks=[h for h in g['hooks'] if not _ours(h)]))
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if removed:
        _save(CLAUDE_SETTINGS, settings, dry)
    return f'Claude Code: removed {removed} hooks'


def install_agy(dry):
    if not os.path.isdir(AGY_DIR) and not shutil.which('agy'):
        return 'Antigravity CLI: not found, skipped'
    hooks = _load(AGY_HOOKS)
    entry = {'Stop': [{'type': 'command', 'command': f'{_bare_python()} {MARK} --json', 'timeout': 15}]}
    if hooks.get('samethread') == entry:
        return 'Antigravity CLI: Stop hook already installed'
    hooks['samethread'] = entry
    _save(AGY_HOOKS, hooks, dry)
    return f'Antigravity CLI: Stop hook added ({AGY_HOOKS})'


def uninstall_agy(dry):
    hooks = _load(AGY_HOOKS)
    if hooks.pop('samethread', None) is None:
        return 'Antigravity CLI: nothing to remove'
    _save(AGY_HOOKS, hooks, dry)
    return 'Antigravity CLI: Stop hook removed'


def install_opencode(dry):
    if not os.path.isdir(OC_DIR) and not shutil.which('opencode'):
        return 'OpenCode: not found, skipped'
    src = OC_PLUGIN_SRC % json.dumps(sys.executable)
    try:
        with open(OC_PLUGIN, encoding='utf-8') as f:
            if f.read() == src:
                return 'OpenCode: plugin already installed'
    except FileNotFoundError:
        pass
    if dry:
        print(f'  [dry-run] would write {OC_PLUGIN}')
    else:
        os.makedirs(os.path.dirname(OC_PLUGIN), exist_ok=True)
        with open(OC_PLUGIN, 'w', encoding='utf-8') as f:
            f.write(src)
    return f'OpenCode: plugin added ({OC_PLUGIN})'


def uninstall_opencode(dry):
    if not os.path.exists(OC_PLUGIN):
        return 'OpenCode: nothing to remove'
    if not dry:
        os.remove(OC_PLUGIN)
    return 'OpenCode: plugin removed'


def install(dry=False):
    for step in (install_claude, install_opencode, install_agy):
        print('  ' + step(dry))
    if not dry:
        print('\nDone. Run `hop sync` once to mirror your existing chats; after that it runs by itself.')


def uninstall(dry=False):
    for step in (uninstall_claude, uninstall_opencode, uninstall_agy):
        print('  ' + step(dry))
    if not dry:
        print('\nHooks removed. Mirrors stay; delete them with `hop clean --yes`.')
