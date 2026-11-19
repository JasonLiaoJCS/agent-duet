"""Imported first by every test module: keeps per-user state (~/.agent-duet) out of the tests."""
import atexit
import os
import shutil
import tempfile

_home = tempfile.mkdtemp(prefix='agent-duet-test-home-')
os.environ['AGENT_DUET_HOME'] = _home
os.environ.pop('AGENT_DUET_DEFAULT_MODE', None)
os.environ.pop('AGENT_DUET_DEPTH', None)  # the suite may itself be run by a bridge worker
# No test may depend on a codex or claude CLI being installed; every external call is mocked.
# These stand-ins are never executed; they only let the bridge resolve a CLI path.
for _name in ('codex', 'claude'):
    _double = os.path.join(_home, _name + '-test-double')
    open(_double, 'w').close()
    os.environ['AGENT_DUET_%s_BIN' % _name.upper()] = _double
atexit.register(shutil.rmtree, _home, ignore_errors=True)
