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


def test_configuration_sidebar_accessibility():
    config_path = Path("webui/config.html")
    assert config_path.exists(), "webui/config.html must exist"
    html = config_path.read_text(encoding="utf-8")

    # Verify search input has explicit accessibility label and type search
    assert 'aria-label="Filter configuration sections"' in html
    assert 'type="search"' in html
    assert 'id="sidebarSearch"' in html

    # Verify tablist structure and orientation
    assert "itemsContainer.setAttribute('role', 'tablist')" in html
    assert "itemsContainer.setAttribute('aria-orientation', 'vertical')" in html
    assert "btn.setAttribute('role', 'tab')" in html

    # Verify aria-selected and aria-current synchronisation
    assert "b.setAttribute('aria-selected', 'true')" in html
    assert "b.setAttribute('aria-selected', 'false')" in html
    assert "b.setAttribute('aria-current', 'true')" in html

    # Verify section icons are hidden from screen readers
    assert 'aria-hidden="true"' in html
    assert "SECTION_ICONS" in html
    assert "sec-icon" in html


def test_configuration_form_controls_and_mobile_accessibility():
    config_path = Path("webui/config.html")
    assert config_path.exists(), "webui/config.html must exist"
    html = config_path.read_text(encoding="utf-8")

    # Verify toggle switches have switch role, label association, and touch wrap
    assert "switchWrap.className = 'switch-wrap'" in html
    assert "input.role = 'switch'" in html
    assert "row.classList.add('field-switch')" in html

    # Verify SVG chevrons for collapsible field groups
    assert "chev-svg" in html
    assert 'polyline points="6 9 12 15 18 9"' in html

    # Verify mobile-specific ergonomics (iOS auto-zoom prevention and sticky save bar)
    assert "font-size: 16px !important;" in html
    assert "position: sticky; bottom: 10px;" in html
    assert ".field.field-switch" in html
