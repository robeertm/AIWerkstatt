"""Review gate for restricted tasks (tasks created with a service token).

A restricted task never goes live by itself. Before it runs, the project workspace is
copied to a snapshot; while any restricted task of the project is running or awaiting
review, the project is "held" (no deploy, publish or release). A signed-in admin then
approves (deploy) or rejects (restore the snapshot). This module owns the snapshot files;
the review state itself lives on the thread (see engine.get_review / set_review).

Workspaces are plain folders (not git repositories), so a file snapshot is the undo.
Heavy, re-creatable folders (``node_modules``, ``.venv``) are neither copied nor replaced.
Symlinks are copied as links and never followed.
"""
import os
import shutil
import tempfile

import config

SNAPSHOTS = os.path.join(config.DATA, "review-snapshots")
WORKSPACES = os.environ.get("AIWERKSTATT_WORKSPACES", "/workspaces")
_KEEP = ("node_modules", ".venv")


def snapshot_path(pid):
    return os.path.join(SNAPSHOTS, pid)


def has_snapshot(pid):
    return os.path.isdir(snapshot_path(pid))


def _workspace(pid):
    ws = os.path.join(WORKSPACES, pid)
    if os.path.islink(ws):
        # The workspace folder itself must be a real folder: following a link here could
        # copy (or, on restore, delete) files outside the workspace volume.
        raise OSError("The project workspace is a symbolic link; refusing to touch it.")
    return ws


def _copy_entries(src, dst):
    """Copy the entries of ``src`` into the existing folder ``dst``."""
    for name in os.listdir(src):
        if name in _KEEP:
            continue
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if os.path.islink(s):
            os.symlink(os.readlink(s), d)
        elif os.path.isdir(s):
            shutil.copytree(s, d, symlinks=True, ignore=shutil.ignore_patterns(*_KEEP))
        else:
            shutil.copy2(s, d)


def ensure_snapshot(pid):
    """Snapshot the workspace unless a snapshot already exists (the oldest unreviewed state
    is the one to return to). A missing workspace gives an empty snapshot. Returns True when
    a new snapshot was taken; raises OSError on failure so the caller can fail closed."""
    if has_snapshot(pid):
        return False
    os.makedirs(SNAPSHOTS, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".%s-" % pid, dir=SNAPSHOTS)
    try:
        ws = _workspace(pid)
        if os.path.isdir(ws):
            _copy_entries(ws, tmp)
        os.rename(tmp, snapshot_path(pid))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return True


def restore_snapshot(pid):
    """Replace the workspace contents (except ``node_modules``/``.venv``) with the snapshot.
    Raises OSError (FileNotFoundError when there is no snapshot)."""
    snap = snapshot_path(pid)
    if not os.path.isdir(snap):
        raise FileNotFoundError("There is no snapshot for this project.")
    ws = _workspace(pid)
    os.makedirs(ws, exist_ok=True)
    for name in os.listdir(ws):
        if name in _KEEP:
            continue
        p = os.path.join(ws, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.remove(p)
    _copy_entries(snap, ws)


def drop_snapshot(pid):
    shutil.rmtree(snapshot_path(pid), ignore_errors=True)
