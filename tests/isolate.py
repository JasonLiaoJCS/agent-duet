"""Imported first by every test module: keeps per-user state (~/.agent-duet) out of the tests."""
import atexit
import os
import shutil
import tempfile

_home = tempfile.mkdtemp(prefix='agent-duet-test-home-')
os.environ['AGENT_DUET_HOME'] = _home
os.environ.pop('AGENT_DUET_DEFAULT_MODE', None)
os.environ.pop('AGENT_DUET_DEPTH', None)  # the suite may itself be run by a bridge worker
atexit.register(shutil.rmtree, _home, ignore_errors=True)
