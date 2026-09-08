"""Deterministic git commit/dirty-state capture for a mod source repo.

Read-only: shells out to `git` only to inspect the repository (rev-parse,
status --porcelain), never to mutate it. This captures the checked-out
state at the moment of capture -- it is not a build-verified guarantee
that a given DLL was actually compiled from this exact commit, only the
best deterministic signal available without deeper build-system
integration (see README's Phase 4B docs). A path that isn't a valid git
working tree, or a missing git binary, is a clear error here rather than
a silently stored NULL, matching the rest of this codebase's fail-loud
conventions for explicitly-supplied input.
"""

import subprocess


def capture_revision(repo_path):
    """"<short-sha>", or "<short-sha>-dirty" if the working tree has any
    uncommitted changes (tracked or untracked -- the conservative signal).
    """
    try:
        sha = subprocess.run(['git', '-C', str(repo_path), 'rev-parse', '--short', 'HEAD'],
                             capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        status = subprocess.run(['git', '-C', str(repo_path), 'status', '--porcelain'],
                                capture_output=True, text=True, timeout=10, check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise ValueError(f'{repo_path} is not a usable git repository: {exc.stderr.strip()}') from exc
    return f'{sha}-dirty' if status.strip() else sha
