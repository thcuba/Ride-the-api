"""The Windows service installer never runs NSSM from a user-writable place (S-6).

NSSM becomes the service binary and runs as LocalSystem. Static checks: this
environment has no PowerShell to run the script.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "packaging" / "windows" / "service" / "install_service.ps1").read_text()
SETUP = (ROOT / "packaging" / "windows" / "setup.iss").read_text()


def _assignment(name: str) -> str:
    match = re.search(rf"^\${name}\s*=\s*(.+)$", SCRIPT, re.MULTILINE)
    assert match, name
    return match.group(1)


def test_nssm_lives_under_the_install_folder():
    assert "$InstallDir" in _assignment("NssmDir")
    assert "$NssmDir" in _assignment("NssmPath")
    assert "TEMP" not in _assignment("NssmPath")


def test_download_goes_to_a_fresh_folder_and_can_be_pinned():
    assert "NewGuid()" in SCRIPT
    assert "Get-FileHash" in SCRIPT
    assert "RIDE_NSSM_SHA256" in SCRIPT


def test_uninstall_needs_no_download():
    uninstall = SCRIPT[SCRIPT.index("if ($Uninstall) {") :]
    uninstall = uninstall[: uninstall.index("exit 0")]
    assert "Get-Nssm" not in uninstall
    assert "sc.exe delete" in uninstall


def test_setup_removes_the_fetched_nssm():
    assert 'Name: "{app}\\nssm"' in SETUP
