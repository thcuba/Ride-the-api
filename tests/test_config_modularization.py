"""Tests for webui/static/js/config modularization.

Verifies that the monolithic config.js is decomposed into domain-scoped scripts,
that all required scripts are loaded by webui/config.html in the correct order,
that exported global functions match inline event handlers, and that each script
retains valid syntax and structure.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_HTML = REPO_ROOT / "webui" / "config.html"
JS_DIR = REPO_ROOT / "webui" / "static" / "js"
CONFIG_JS = JS_DIR / "config.js"
MODULES_DIR = JS_DIR / "config"

EXPECTED_MODULES = [
    "schema.js",
    "state.js",
    "renderer.js",
    "sidebar.js",
]

MAX_ORCHESTRATOR_LINES = 250


class ConfigHtmlParser(HTMLParser):
    """Parses config.html to extract script sources and inline event handlers."""

    def __init__(self) -> None:
        super().__init__()
        self.script_srcs: list[str] = []
        self.inline_handlers: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, val in attrs:
            if tag == "script" and name == "src" and val:
                self.script_srcs.append(val)
            elif name.startswith("on") and val:
                match = re.match(r"([a-zA-Z0-9_]+)\(", val.strip())
                if match:
                    self.inline_handlers.add(match.group(1))


def test_modular_config_files_exist():
    """All domain-specific modular scripts must exist."""
    assert CONFIG_JS.exists(), "webui/static/js/config.js must exist"
    assert MODULES_DIR.is_dir(), "webui/static/js/config/ directory must exist"

    for module_name in EXPECTED_MODULES:
        module_path = MODULES_DIR / module_name
        assert module_path.is_file(), f"Missing modular script: {module_path}"
        assert module_path.stat().st_size > 0, f"Empty modular script: {module_path}"


def test_config_js_is_decomposed_and_lightweight():
    """config.js must now act as orchestrator (< 250 lines), not a 1600+ line monolith."""
    lines = CONFIG_JS.read_text(encoding="utf-8").splitlines()
    assert len(lines) < MAX_ORCHESTRATOR_LINES, (
        f"config.js should be < {MAX_ORCHESTRATOR_LINES} lines, but has {len(lines)}"
    )


def test_config_html_includes_all_modules_in_correct_order():
    """webui/config.html must include modular scripts before config.js."""
    parser = ConfigHtmlParser()
    parser.feed(CONFIG_HTML.read_text(encoding="utf-8"))

    config_related = [
        s.split("?")[0]
        for s in parser.script_srcs
        if "/static/js/config" in s or s.endswith("/config.js")
    ]

    expected_order = [
        "/static/js/config/schema.js",
        "/static/js/config/state.js",
        "/static/js/config/renderer.js",
        "/static/js/config/sidebar.js",
        "/static/js/config.js",
    ]

    err_msg = (
        f"Incorrect script order in config.html.\nGot: {config_related}\nExpected: {expected_order}"
    )
    assert config_related == expected_order, err_msg


def test_modular_scripts_pass_node_syntax_check():
    """If node is available, all modular scripts must pass node --check without syntax errors."""
    node_exe = shutil.which("node")
    if not node_exe:
        pytest.skip("node is not installed")

    all_scripts = [CONFIG_JS] + [MODULES_DIR / m for m in EXPECTED_MODULES]
    for script in all_scripts:
        res = subprocess.run(
            [node_exe, "--check", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, f"Syntax error in {script}:\n{res.stderr}"


def test_all_html_event_handlers_are_defined():
    """All inline onclick/onchange/oninput handlers in config.html must be defined in scripts."""
    parser = ConfigHtmlParser()
    parser.feed(CONFIG_HTML.read_text(encoding="utf-8"))

    js_text = ""
    for path in [
        JS_DIR / "common.js",
        JS_DIR / "api.js",
        CONFIG_JS,
    ] + [MODULES_DIR / m for m in EXPECTED_MODULES]:
        if path.exists():
            js_text += "\n" + path.read_text(encoding="utf-8")

    ignored = {"if"}

    for handler in parser.inline_handlers:
        if handler in ignored:
            continue
        defined = (
            f"function {handler}" in js_text
            or f"async function {handler}" in js_text
            or f"window.{handler}" in js_text
        )
        assert defined, (
            f"Handler '{handler}' used in config.html is not defined in any loaded JS file"
        )


def test_schema_module_exports():
    """schema.js must define key dictionaries and lookup functions."""
    schema_js = (MODULES_DIR / "schema.js").read_text(encoding="utf-8")
    expected_symbols = [
        "HINTS",
        "FIELD_HINTS",
        "SECTION_ORDER",
        "SECTION_LABELS",
        "SECTION_ICONS",
        "lookupPathPattern",
        "hintFor",
        "suggestionsFor",
        "defaultForKey",
        "enumOptionsFor",
    ]
    for sym in expected_symbols:
        assert sym in schema_js, f"Symbol '{sym}' missing from schema.js"


def test_state_module_exports():
    """state.js must define core state variables, helpers, and API handlers."""
    state_js = (MODULES_DIR / "state.js").read_text(encoding="utf-8")
    expected_symbols = [
        "cfg",
        "dirty",
        "getAt",
        "setAt",
        "collectSection",
        "setDirty",
        "markFieldDirty",
        "updateSaveBtn",
        "loadConfig",
        "saveConfig",
    ]
    for sym in expected_symbols:
        assert sym in state_js, f"Symbol '{sym}' missing from state.js"


def test_renderer_module_exports():
    """renderer.js must define form, array, field, and quick-action rendering functions."""
    renderer_js = (MODULES_DIR / "renderer.js").read_text(encoding="utf-8")
    expected_symbols = [
        "ensureTooltip",
        "placeTip",
        "attachTip",
        "hideTooltip",
        "buildDatalist",
        "copyFieldPath",
        "copyConfigPath",
        "renderObject",
        "renderField",
        "renderArray",
        "rerenderSection",
        "renderLLMSection",
        "renderQuickActions",
    ]
    for sym in expected_symbols:
        assert sym in renderer_js, f"Symbol '{sym}' missing from renderer.js"


def test_sidebar_module_exports():
    """sidebar.js must define sidebar building, filtering, and section selection."""
    sidebar_js = (MODULES_DIR / "sidebar.js").read_text(encoding="utf-8")
    expected_symbols = [
        "clearSidebarSearch",
        "filterSidebar",
        "buildSidebar",
        "selectSection",
    ]
    for sym in expected_symbols:
        assert sym in sidebar_js, f"Symbol '{sym}' missing from sidebar.js"
