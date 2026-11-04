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


def test_primary_navigation_accessibility_across_pages():
    pages = [
        ("webui/dashboard.html", '<a href="/" aria-current="page">Dashboard</a>'),
        ("webui/patterns.html", '<a href="/patterns" aria-current="page">Patterns</a>'),
        ("webui/config.html", '<a href="/config" aria-current="page">Configuration</a>'),
        ("webui/options.html", '<a href="/options" aria-current="page">Options</a>'),
    ]

    for rel_path, current_link in pages:
        p = Path(rel_path)
        assert p.exists(), f"{rel_path} must exist"
        html = p.read_text(encoding="utf-8")

        # Every page must contain the semantic navigation landmark
        assert '<nav class="primary-nav" aria-label="Primary navigation">' in html, (
            f"Missing primary navigation landmark in {rel_path}"
        )

        # Every page must link to all 4 core routes
        assert 'href="/"' in html
        assert 'href="/patterns"' in html
        assert 'href="/config"' in html
        assert 'href="/options"' in html

        # And identify its own active route with aria-current="page"
        assert current_link in html, (
            f"Missing or incorrect aria-current on active link in {rel_path}"
        )
