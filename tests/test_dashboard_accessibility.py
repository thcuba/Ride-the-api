from pathlib import Path


def test_auto_switch_toggle_accessibility():
    dashboard_path = Path("webui/dashboard.html")
    assert dashboard_path.exists(), "webui/dashboard.html must exist"
    content = dashboard_path.read_text(encoding="utf-8")

    # Verify Auto-Switch toggle uses switch role and accessible slider styling
    assert 'role="switch"' in content
    assert 'aria-checked="${!!s.auto_switch_enabled}"' in content
    assert '<span class="tc-slider"></span>' in content
    assert 'class="tc-toggle"' in content

    # Verify toggleAutoSwitch updates aria-checked dynamically on change and reverts on failure
    assert "el.setAttribute('aria-checked', enabled ? 'true' : 'false')" in content
    assert "el.setAttribute('aria-checked', (!enabled) ? 'true' : 'false')" in content
