import isolate  # noqa: F401  (must come first)
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent-duet/scripts'))
import install

class InstallTests(unittest.TestCase):
    def test_reinstall_removes_obsolete_files_and_keeps_backup_outside_skills(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'source'
            source.mkdir()
            (source/'SKILL.md').write_text('---\nname: agent-duet\n---\n',encoding='utf-8')
            (source/'new.txt').write_text('new',encoding='utf-8')
            target=root/'config/skills/agent-duet'
            install.copy_skill(source,target)
            (target/'obsolete.txt').write_text('old',encoding='utf-8')
            install.copy_skill(source,target)
            self.assertFalse((target/'obsolete.txt').exists())
            self.assertTrue((target/'new.txt').exists())
            self.assertEqual([p.name for p in target.parent.iterdir()],['agent-duet'])
            self.assertTrue(list((root/'config/agent-duet-backups').glob('skill-*/obsolete.txt')))
    def test_unrelated_directory_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'source'; source.mkdir()
            target=root/'target'; target.mkdir()
            (target/'mine.txt').write_text('preserve',encoding='utf-8')
            with self.assertRaises(RuntimeError):
                install.copy_skill(source,target)
            self.assertEqual((target/'mine.txt').read_text(),'preserve')
