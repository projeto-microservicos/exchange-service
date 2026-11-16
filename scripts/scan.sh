#!/usr/bin/env bash
# Run with development dependencies installed. Any advisory or scanner failure blocks release.
set -euo pipefail
mkdir -p artifacts
set +e
"${PYTHON:-python3}" -m pip_audit -r requirements.txt --format json --output artifacts/vulnerabilities.json
scan_status=$?
set -e
"${PYTHON:-python3}" - <<'PY'
import json
from pathlib import Path
report = json.loads(Path('artifacts/vulnerabilities.json').read_text())
dependencies = report.get('dependencies', [])
if not dependencies or any('skip_reason' in dependency for dependency in dependencies):
    raise SystemExit('Dependency audit is empty or incomplete')
print(f'Audited {len(dependencies)} runtime dependencies')
PY
exit "$scan_status"
