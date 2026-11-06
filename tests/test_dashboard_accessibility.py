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

        # Every page must link to all 5 routes, with LLM Settings between Configuration and Options
        assert 'href="/"' in html
        assert 'href="/patterns"' in html
        assert 'href="/config"' in html
        assert 'href="/#llm-settings"' in html
        assert 'href="/options"' in html

        config_idx = html.find('href="/config"')
        llm_idx = html.find('href="/#llm-settings"')
        options_idx = html.find('href="/options"')
        assert config_idx < llm_idx < options_idx, (
            f"LLM Settings must be positioned between Configuration and Options in {rel_path}"
        )

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


def test_llm_settings_modal_accessibility_and_mobile_ergonomics():
    dash_path = Path("webui/dashboard.html")
    assert dash_path.exists(), "webui/dashboard.html must exist"
    html = dash_path.read_text(encoding="utf-8")

    # Verify modal dialog semantics
    assert 'id="llm-settings-modal"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-labelledby="llm-settings-title"' in html

    # Verify master switch toggle has switch role, aria-checked and touch wrap
    assert 'id="llm-enabled-toggle"' in html
    assert 'class="switch"' in html
    assert 'class="switch-wrap"' in html
    assert 'role="switch"' in html

    # Verify profile purpose selectors have explicit aria-labels and associated labels
    assert 'id="llm-task-correlation"' in html
    assert 'aria-label="LLM profile for correlation and validation"' in html
    assert 'id="llm-task-flush"' in html
    assert 'aria-label="LLM profile for the buffer flush"' in html
    assert 'id="llm-task-learning_context"' in html
    assert 'aria-label="LLM profile for the grounded update flush"' in html

    # Verify profile card controls and micro-interactions
    assert "lpc-status-pill" in html
    assert "lpc-unsaved-badge" in html
    assert "btn-glow-pill" in html
    assert "pw-toggle-btn" in html
    assert "updateProfileStatusPill" in html

    # Verify mobile-specific ergonomics (iOS auto-zoom guard >=16px and sticky save bar)
    assert "#llm-settings-modal input," in html
    assert "font-size: 16px !important;" in html
    assert "position: sticky;" in html
    assert "btn-save-llm" in html


def test_llm_settings_deeplink_has_no_temporal_dead_zone():
    """/#llm-settings calls openLLMSettings() while the document is still parsing.

    Every variable that function assigns must be declared ABOVE that call. With the
    `let` declared further down, the assignment throws
    "ReferenceError: Cannot access 'lastModalActiveElement' before initialization"
    *before* the modal is shown, so the deep link silently does nothing -- while the
    toolbar button and the nav pill keep working, because they run on click, after
    parsing. That asymmetry is what makes this regression hard to spot by hand.
    """
    content = Path("webui/dashboard.html").read_text(encoding="utf-8")

    declaration = content.find("let lastModalActiveElement = null;")
    deeplink_snippet = "if (window.location.hash === '#llm-settings') { openLLMSettings(); }"
    deeplink_call = content.find(deeplink_snippet)
    first_assignment = content.find("lastModalActiveElement = document.activeElement;")

    assert declaration != -1, "lastModalActiveElement must be declared"
    assert deeplink_call != -1, "the /#llm-settings deep link must exist"
    assert first_assignment != -1, "openLLMSettings() must record the element to restore"
    assert declaration < deeplink_call, (
        "lastModalActiveElement must be declared before the deep-link call, otherwise "
        "openLLMSettings() hits its temporal dead zone and the modal never opens"
    )
    assert deeplink_call < first_assignment
