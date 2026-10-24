"""Remove completed harness-owned checkouts; retain durable replay receipts."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from harness.git_ops import GitAdapter, GitError

# Exact producer-owned files only. Unknown ignored files (including secrets)
# deliberately block removal; never turn this into a .task-* wildcard.
RUNTIME_FILES = frozenset({
    '.task-meta.json', '.task-summary.json', '.task-prompt.md',
    '.task-reap-prepared.json', '.task-reap-complete.json',
    '.task-cmux-surface', '.task-origin-session', '.task-review-skill',
    '.task-close-armed.json', '.task-review.json', '.task-review-resolution.json',
    '.task-review-authorized-continuation.json', '.task-verification.json',
    '.task-verification-contract.json', '.task-verification-response.json',
    '.wiki-agent-runtime', '.wiki-cmux-surface', '.wiki-reap-command',
})


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('cleanup receipt must be an object')
    return value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt_path(vault: Path, worktree: Path) -> Path:
    key = hashlib.sha256(os.fsencode(worktree)).hexdigest()
    return vault / '.vault-meta/harness/worktree-cleanup' / f'{key}.json'


def _write(path: Path, value: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _retained(result: dict[str, Any], reason: str) -> dict[str, Any]:
    return {**result, 'worktree_cleanup': {'status': 'retained', 'reason': reason}}


def _owned_git(worktree: Path, meta: dict[str, Any]) -> tuple[GitAdapter, str]:
    source = Path(str(meta.get('target_repo') or '')).resolve()
    cwd = Path.cwd().resolve()
    if (worktree == source or source in worktree.parents or worktree in source.parents
            or cwd == worktree or worktree in cwd.parents or worktree.is_symlink()
            or not (worktree / '.git').is_file()):
        raise ValueError('checkout is not an external inactive linked worktree')
    child = GitAdapter(worktree)
    isolation = child.isolation()
    gitdir = Path(child._run(['rev-parse', '--absolute-git-dir']))
    pending = any((gitdir / name).exists() for name in (
        'sequencer', 'REVERT_HEAD', 'BISECT_START'))
    owner = _read(gitdir / 'llm-obsidian-harness-owner.json')
    expected = {'schema_version': 1, 'source_root': str(source),
                'target': str(worktree), 'branch': meta.get('branch')}
    if (owner != expected or isolation.branch != meta.get('branch')
            or isolation.detached or isolation.branch in {'main', 'master'}
            or isolation.common_dir != GitAdapter(source).isolation().common_dir
            or (gitdir / 'locked').exists() or pending or child._operation()):
        raise ValueError('checkout ownership, branch, lock or operation changed')
    return child, child.revision('HEAD')


def _disposable(path: str) -> bool:
    item = Path(path)
    return path in RUNTIME_FILES or ('__pycache__' in item.parts and item.suffix == '.pyc')


def _check_files(child: GitAdapter) -> list[str]:
    flags = child._run(['ls-files', '-v', '-z'], strip=False).split('\0')
    if any(row and (row[0].islower() or row[0] == 'S') for row in flags):
        raise ValueError('index flags hide tracked worktree changes')
    if child.diff_text(name_only=True) or child.diff_text(cached=True, name_only=True):
        raise ValueError('tracked changes remain')
    # No exclude flags: enumerate BOTH ignored and ordinary untracked files.
    others = child._run(['ls-files', '--others', '-z'], strip=False).split('\0')
    paths = [path for path in others if path]
    if any(not _disposable(path) or any(c in path for c in '\n\r\\*?[]')
           or (child.root / path).is_symlink() for path in paths):
        raise ValueError('unknown untracked or ignored files remain')
    return paths


def _validate_completion(vault: Path, worktree: Path, current: str,
                         meta: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    close_status = result.get('plan_close_status')
    closed = close_status == 'closed' or (
        close_status == 'retained' and meta.get('reap_policy', {}).get('mode') == 'shared')
    if (meta.get('version') not in {3, 4} or meta.get('interaction_policy') != 'unattended'
            or meta.get('origin_session') != current or meta.get('worktree') != str(worktree)
            or meta.get('vault_root') != str(vault) or not meta.get('task_id')
            or result.get('status') != 'complete' or not closed):
        raise ValueError('task identity or completed plan is not eligible')
    complete = _read(worktree / '.task-reap-complete.json')
    result_path = Path(str(result.get('result_path') or '')).resolve()
    expected = {'validated': True, 'task_session_status': 'archived',
                'current_session': current, 'vault_root': str(vault),
                'task_name': meta.get('task_name'), 'result_path': str(result_path),
                'plan_close_status': close_status,
                'meta_sha256': _hash(worktree / '.task-meta.json'),
                'summary_sha256': _hash(worktree / '.task-summary.json'),
                'result_sha256': _hash(result_path)}
    if vault / 'wiki' not in result_path.parents or any(complete.get(k) != v for k, v in expected.items()):
        raise ValueError('durable completion evidence does not match')
    if (worktree / '.task-session-binding.json').exists():
        raise ValueError('checkout still has a live task session binding')
    return complete


def _remove(vault: Path, worktree: Path, receipt: dict[str, Any], path: Path) -> dict[str, Any]:
    result, meta = receipt['result'], receipt['meta']
    try:
        complete = _validate_completion(vault, worktree, receipt['current_session'], meta, result)
        child, head = _owned_git(worktree, meta)
        if head != receipt['head'] or complete != receipt['completion']:
            raise ValueError('checkout changed after cleanup preparation')
        disposable = _check_files(child)
        # Git's own final dirty/lock/submodule checks stay enabled. Temporary
        # exclusions permit only the previously inspected runtime files; no force.
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent) as excludes:
            excludes.write('\n'.join('/' + p for p in disposable) + '\n')
            excludes.flush()
            GitAdapter(meta['target_repo'])._run([
                '-c', f'core.excludesFile={excludes.name}', 'worktree', 'remove', str(worktree)])
    except (OSError, ValueError, GitError) as exc:
        return _retained(result, str(exc))
    receipt['status'] = 'removed'
    _write(path, receipt)
    return {**result, 'worktree_cleanup': {'status': 'removed'}}


def replay_cleanup(vault: Path, worktree: Path, current: str) -> dict[str, Any] | None:
    """Replay without callback/provider effects, including a crash after removal."""
    path = _receipt_path(vault, worktree)
    if not path.exists():
        return None
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = _read(path)
        if (receipt.get('version') != 1 or receipt.get('worktree') != str(worktree)
                or receipt.get('vault') != str(vault) or receipt.get('current_session') != current
                or receipt.get('status') not in {'prepared', 'removed'}):
            raise ValueError('cleanup receipt identity does not match coordinator')
        if worktree.exists() or worktree.is_symlink():
            if receipt['status'] == 'removed':
                raise ValueError('completed worktree path has been reused')
            result = _remove(vault, worktree, receipt, path)
        else:
            receipt['status'] = 'removed'
            _write(path, receipt)
            result = {**receipt['result'], 'worktree_cleanup': {'status': 'removed'}}
        return {**result, 'idempotent': True}


def cleanup_completed_worktree(vault: Path, worktree: Path, current: str,
                               meta: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Called only after the reap result is filed and provider resources are empty."""
    try:
        complete = _validate_completion(vault, worktree, current, meta, result)
        child, head = _owned_git(worktree, meta)
        _check_files(child)
    except (OSError, ValueError, GitError) as exc:
        return _retained(result, str(exc))
    path = _receipt_path(vault, worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = {'version': 1, 'status': 'prepared', 'vault': str(vault),
                   'worktree': str(worktree), 'current_session': current,
                   'meta': meta, 'completion': complete, 'result': result, 'head': head}
        _write(path, receipt)
        return _remove(vault, worktree, receipt, path)
