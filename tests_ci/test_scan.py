"""Audit failures, empty inventories and skipped dependencies cannot approve a release."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, sys
if sys.argv[1:3] == ['-m', 'pip_audit']:
    dependency = {'name': 'example', 'version': '1', 'vulns': []}
    if os.getenv('SKIPPED'): dependency['skip_reason'] = 'unsupported package'
    pathlib.Path('artifacts/vulnerabilities.json').write_text(json.dumps({
        'dependencies': [] if os.getenv('EMPTY') else [dependency]}))
    sys.exit(int(os.getenv('AUDIT_EXIT', '0')))
os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
'''


class ScanTest(unittest.TestCase):
    def scan(self, **settings):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = root / 'audit-python'
            fake.write_text(FAKE)
            fake.chmod(0o755)
            environment = dict(os.environ, PYTHON=str(fake), **settings)
            return subprocess.run(['bash', str(REPO / 'scripts/scan.sh')], cwd=root,
                                  env=environment, capture_output=True, text=True)

    def test_complete_inventory_is_accepted(self):
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_or_skipped_inventory_is_rejected(self):
        self.assertNotEqual(self.scan(EMPTY='1').returncode, 0)
        self.assertNotEqual(self.scan(SKIPPED='1').returncode, 0)

    def test_vulnerability_or_tool_failure_blocks_release(self):
        for status in ('1', '2'):
            with self.subTest(status=status):
                self.assertEqual(self.scan(AUDIT_EXIT=status).returncode, int(status))
