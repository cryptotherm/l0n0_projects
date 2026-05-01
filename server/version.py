"""
CT TestBench Version Management

Branches:
  main    - Production (stable, tagged releases)
  sandbox - Active development (preview builds)

Version format: MAJOR.MINOR.PATCH
  MAJOR - Breaking changes or major rewrites
  MINOR - New features, non-breaking
  PATCH - Bug fixes, small improvements

Tag history:
  v1.0.0 - Production baseline: PDU slot mapping, rack A/B/C/D,
            diagnostic flow, label printer, inventory import
"""

PRODUCTION_VERSION = "1.0.0"
SANDBOX_VERSION    = "1.1.0-dev"

import subprocess as _sp
import os as _os


def get_branch() -> str:
    branch = _os.environ.get('GIT_BRANCH', '')
    if not branch:
        try:
            branch = _sp.check_output(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=_os.path.dirname(_os.path.dirname(__file__)),
                stderr=_sp.DEVNULL
            ).decode().strip()
        except Exception:
            branch = 'unknown'
    return branch


def get_commit() -> str:
    try:
        return _sp.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=_os.path.dirname(_os.path.dirname(__file__)),
            stderr=_sp.DEVNULL
        ).decode().strip()
    except Exception:
        return 'unknown'


def get_current_version() -> str:
    branch = get_branch()
    if branch == 'main':
        return PRODUCTION_VERSION
    elif branch == 'sandbox':
        return SANDBOX_VERSION
    else:
        return f'dev/{branch}'


def get_build_info() -> dict:
    branch = get_branch()
    channel = 'production' if branch == 'main' else 'sandbox' if branch == 'sandbox' else 'dev'
    return {
        'version': get_current_version(),
        'branch': branch,
        'commit': get_commit(),
        'channel': channel,
        'is_sandbox': branch != 'main',
        'production_version': PRODUCTION_VERSION,
        'sandbox_version': SANDBOX_VERSION,
    }
