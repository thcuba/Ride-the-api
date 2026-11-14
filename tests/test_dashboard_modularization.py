"""Tests for webui/static/js/dashboard modularization.

Verifies that the monolithic dashboard.js is decomposed into domain-scoped scripts,
that all required scripts are loaded by webui/dashboard.html in correct order,
that exported global functions match inline event handlers, and that each script
retains valid syntax and structure.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML = REPO_ROOT / "webui" / "dashboard.html"
JS_DIR = REPO_ROOT / "webui" / "static" / "js"
DASHBOARD_JS = JS_DIR / "dashboard.js"
MODULES_DIR = JS_DIR / "dashboard"

EXPECTED_MODULES = [
    "devices.js",
    "tls.js",
    "database.js",
    "llm_settings.js",
    "buffer.js",
]

MAX_ORCHESTRATOR_LINES = 350


def test_modular_dashboard_files_exist():
    """All domain-specific modular scripts must exist."""
    assert DASHBOARD_JS.exists(), "webui/static/js/dashboard.js must exist"
    assert MODULES_DIR.is_dir(), "webui/static/js/dashboard/ directory must exist"

    for module_name in EXPECTED_MODULES:
        module_path = MODULES_DIR / module_name
        assert module_path.is_file(), f"Missing modular script: {module_path}"
        assert module_path.stat().st_size > 0, f"Empty modular script: {module_path}"


def test_dashboard_js_is_decomposed_and_lightweight():
    """dashboard.js must now act as orchestrator (< 350 lines), not a 2000+ line monolith."""
    lines = DASHBOARD_JS.read_text(encoding="utf-8").splitlines()
    assert len(lines) < MAX_ORCHESTRATOR_LINES, (
        f"dashboard.js should be < {MAX_ORCHESTRATOR_LINES} lines, but has {len(lines)}"
    )


def test_dashboard_html_includes_all_modules_in_correct_order():
    """webui/dashboard.html must include modular scripts before dashboard.js."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")

    # Find script tags referencing /static/js/
    script_srcs = re.findall(r'<script\s+src="([^"]+)"></script>', html)
    dashboard_related = [
        s.split("?")[0]
        for s in script_srcs
        if "/static/js/dashboard" in s or s.endswith("/dashboard.js")
    ]

    expected_order = [
        "/static/js/dashboard/devices.js",
        "/static/js/dashboard/tls.js",
        "/static/js/dashboard/database.js",
        "/static/js/dashboard/llm_settings.js",
        "/static/js/dashboard/buffer.js",
        "/static/js/dashboard.js",
    ]

    err_msg = (
        f"Incorrect script order in dashboard.html.\n"
        f"Got: {dashboard_related}\n"
        f"Expected: {expected_order}"
    )
    assert dashboard_related == expected_order, err_msg


def test_modular_scripts_pass_node_syntax_check():
    """If node is available, all modular scripts must pass node --check without syntax errors."""
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not installed")

    all_scripts = [DASHBOARD_JS] + [MODULES_DIR / m for m in EXPECTED_MODULES]
    for script in all_scripts:
        res = subprocess.run(
            [node_exe, "--check", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, f"Syntax error in {script}:\n{res.stderr}"


def test_all_html_event_handlers_are_defined():
    """All inline onclick/onchange/oninput handlers in dashboard.html must be defined in scripts."""
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    handler_calls = re.findall(r'on[a-z]+="([a-zA-Z0-9_]+)\(', html)

    # Collect all JS text from all scripts loaded in dashboard.html
    js_text = ""
    for path in [
        JS_DIR / "common.js",
        JS_DIR / "api.js",
        DASHBOARD_JS,
    ] + [MODULES_DIR / m for m in EXPECTED_MODULES]:
        if path.exists():
            js_text += "\n" + path.read_text(encoding="utf-8")

    # Native JS keywords/statements that might match regex
    ignored = {"if"}

    for handler in set(handler_calls):
        if handler in ignored:
            continue
        # Function must be defined or assigned to window
        defined = (
            f"function {handler}" in js_text
            or f"async function {handler}" in js_text
            or f"window.{handler}" in js_text
        )
        assert defined, (
            f"Handler '{handler}' used in dashboard.html is not defined in any loaded JS file"
        )
