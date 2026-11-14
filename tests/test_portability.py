"""Checks that matter when someone installs Agent Duet on a different computer."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'agent-duet/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('duet_portability', SCRIPTS / 'duet.py')
duet = importlib.util.module_from_spec(spec)
spec.loader.exec_module(duet)
import install


class DefaultModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Path(self.temp.name) / 'settings.json'
        patcher = patch.object(duet, 'SETTINGS_FILE', self.settings)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('AGENT_DUET_DEFAULT_MODE', None)

    def test_fresh_install_defaults_to_read_only_review(self):
        self.assertEqual(duet.default_mode(), 'review')

    def test_settings_file_can_choose_work(self):
        self.settings.write_text(json.dumps({'default_mode': 'work'}), encoding='utf-8')
        self.assertEqual(duet.default_mode(), 'work')

    def test_environment_overrides_settings(self):
        self.settings.write_text(json.dumps({'default_mode': 'work'}), encoding='utf-8')
        os.environ['AGENT_DUET_DEFAULT_MODE'] = 'review'
        self.assertEqual(duet.default_mode(), 'review')

    def test_invalid_or_corrupt_settings_fall_back_to_review(self):
        self.settings.write_text('{not json', encoding='utf-8')
        self.assertEqual(duet.default_mode(), 'review')
        self.settings.write_text(json.dumps({'default_mode': 'root'}), encoding='utf-8')
        self.assertEqual(duet.default_mode(), 'review')


class ShimTests(unittest.TestCase):
    def test_npm_cmd_shim_resolves_to_node_entrypoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            entry = root / 'node_modules/@anthropic-ai/claude-code/cli.js'
            entry.parent.mkdir(parents=True)
            entry.write_text('// cli', encoding='utf-8')
            shim = root / 'claude.cmd'
            shim.write_text('@ECHO off\r\n"%_prog%"  "%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n', encoding='utf-8')
            with patch.object(duet.shutil, 'which', return_value='node'):
                self.assertEqual(duet.shim_target(shim, 'claude'), ['node', str(entry)])

    def test_bundled_exe_needs_no_node(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            exe = root / 'node_modules/@anthropic-ai/claude-code/bin/claude.exe'
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b'MZ')
            shim = root / 'claude.cmd'
            shim.write_text('"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe" %*', encoding='utf-8')
            with patch.object(duet.shutil, 'which', return_value=None):
                self.assertEqual(duet.shim_target(shim, 'claude'), [str(exe)])

    def test_missing_cli_error_tells_how_to_fix(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('AGENT_DUET_CODEX_BIN', None)
            with patch.object(duet.shutil, 'which', return_value=None):
                with self.assertRaisesRegex(RuntimeError, 'AGENT_DUET_CODEX_BIN'):
                    duet.cli('codex')


class InstallSettingsTests(unittest.TestCase):
    def test_reinstall_keeps_previous_mode_and_source_settings_are_not_copied(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source'
            source.mkdir()
            (source / 'SKILL.md').write_text('---\nname: agent-duet\n---\n', encoding='utf-8')
            (source / 'settings.json').write_text('{"default_mode":"work"}', encoding='utf-8')
            target = root / 'home/skills/agent-duet'
            install.copy_skill(source, target)
            self.assertFalse((target / 'settings.json').exists())
            install.write_settings(target, {'default_mode': 'work'})
            install.copy_skill(source, target)
            self.assertEqual(install.read_settings(root / 'missing', target), {})
            install.write_settings(target, {'default_mode': 'work'})
            self.assertEqual(install.read_settings(root / 'missing', target)['default_mode'], 'work')


if __name__ == '__main__':
    unittest.main()
