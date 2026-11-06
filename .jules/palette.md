## 2026-11-06 - [LLM Settings Modal Aesthetic, Glowing Indicator Pills & Mobile Ergonomics]
**Learning:** Dense modal settings interfaces designed primarily for desktop suffer from severe usability and visual shortcomings on mobile: tiny hit targets (<25px) on profile test/delete actions lead to mis-taps, sub-16px input font sizes trigger disruptive iOS auto-zoom viewport jumps, and unstyled raw checkboxes feel antiquated. Additionally, users expect micro-interactive feedback on buttons via trailing glowing indicator pills and radiant status badges for active and unsaved states.
**Action:** Restructure the LLM Settings modal into sleek elevated cards with 48x26px touch toggles (`role="switch"`), illuminated trailing button indicator pills (`.btn-glow-pill`), vibrant glowing status/unsaved badges (`.lpc-status-pill`, `.lpc-unsaved-badge`), 16px minimum font size guard for mobile inputs, and a sticky mobile footer with blur backdrop.

## 2026-11-06 - [Glowing Status Indicator Pills in Sidebar Navigation]
**Learning:** Text-only or static pills at the trailing end of navigation tabs can feel disconnected from the interactive feedback loop. Users expect clear micro-interactions indicating hover proximity and active view selection.
**Action:** Enhance trailing badge indicators (`.count`) with cubic-bezier transitions, subtle scaling, and vibrant glow halos (`box-shadow: 0 0 10px ...` on hover; brilliant radiant white/accent glow on active state), while preserving amber alerts for unsaved dirty states.

## 2026-11-05 - [Touch-Friendly Configuration Form Controls & Mobile Ergonomics]
**Learning:** Dense form editors designed primarily for desktop present severe usability flaws on smartphones: sub-16px input font sizes trigger disruptive iOS auto-zoom viewport jumps, boolean toggles stack awkwardly under descriptions instead of anchoring to the right, and tiny touch targets (<40px) or hidden save buttons require excessive vertical scrolling.
**Action:** Enforce a minimum 16px font-size for mobile inputs, preserve side-by-side row alignment for boolean toggles (`.field.field-switch`) across all viewports with 48x26px touch-wrapped toggles, implement smooth SVG chevrons for nested card groups, and make the mobile save bar sticky above the bottom edge for effortless one-tap persistence.

## 2026-11-05 - [Configuration Lateral Navigation Aesthetics, Icons & Accessible Filtering]
**Learning:** Complex configuration panels with dozens of sections easily overwhelm users when presented as flat, un-iconified text lists without quick-search capability. Furthermore, sidebar navigation items acting as tabs must synchronize vertical tablist semantics (`role="tablist"`, `aria-orientation="vertical"`, `role="tab"`), `aria-selected` and `aria-current` states to ensure screen reader orientation.
**Action:** Redesign configuration sidebars with semantic SVG iconography per domain, accessible quick-search filtering (`aria-label="Filter configuration sections"`), unified active/hover gradients aligned with `app.css`, and non-disruptive unsaved change indicator pills.

## 2026-10-08 - [Clipboard Copy Fallbacks for Non-HTTPS Local Deployments]
**Learning:** In local network HTTP deployments (e.g., `http://192.168.1.x:8911`), modern browsers disable `navigator.clipboard.writeText` due to Secure Context restrictions. Relying solely on `navigator.clipboard` causes one-click copy buttons (for device IDs or JSON templates) to fail with "Clipboard API not available" toast warnings.
**Action:** Provide a transparent fallback function using a temporary off-screen `<textarea>` and `document.execCommand('copy')` whenever `navigator.clipboard` is missing or fails, ensuring copy actions succeed across all HTTP/HTTPS local network contexts.

## 2026-10-03 - [JSON Textarea Validation & Data Loss Prevention]
**Learning:** In pattern or config editors with freeform JSON `<textarea>` inputs, silently catching JSON `SyntaxError`s during form payload assembly wipes user input by defaulting to empty objects `{}`. Throwing descriptive validation errors with field labels before network requests prevents data loss and informs users of the exact JSON syntax error.
**Action:** Wrap JSON template parsing in named helper functions that throw labelled errors (`Invalid JSON in ${label}: ${err.message}`), catch them in submit handlers, and display actionable error toasts while keeping form controls enabled.

## 2026-10-01 - [Password Input Visibility Toggles & Focus States]
**Learning:** Masked password fields without a visibility toggle increase entry errors and user frustration. Adding an inline overlay toggle button (`type="button"`) requires updating both visual icon states (eye vs eye-off) and accessible attributes (`aria-label` and `title` changing between "Show password" and "Hide password") along with explicit `:focus-visible` styling to maintain keyboard navigation clarity without obscuring input text.
**Action:** Wrap password inputs requiring user visual verification in relative containers with right-padded inputs (`padding-right: 28px`), and dynamically update `type`, `aria-label`, `title`, and inner SVG paths in the toggle click handler.

## 2026-08-30 - [Dynamic Form Field Label Association & SPA Section Focus]
**Learning:** In dynamically constructed JavaScript UI forms, rendering field names in `<div>` tags instead of `<label for="${id}">` breaks explicit label association in assistive technologies and disables native click-to-focus mouse behavior. In SPA section switching, setting `tabindex="-1"` and programmatically calling `.focus()` on the section title or heading re-orients focus and alerts screen reader users to newly loaded views.
**Action:** Always generate `<label for="${id}" class="...">` linked directly to control element IDs in dynamic form builders, and programmatically shift focus to the view heading when switching SPA tabs or sections.

## 2026-08-29 - [HTML Attribute Quotes in Template Strings & Screen Reader Controls]
**Learning:** In dynamically generated HTML template literals inside JavaScript, single/double quote mismatches in event handler inline strings (e.g. `onchange="...('${id}")"`) cause hidden JS syntax errors that break UI interactions like count updates. Additionally, dynamically inserted icon buttons (`&times;`) and unlabeled checkboxes in buffer lists are invisible to screen readers without explicit `aria-label` attributes.
**Action:** Always validate inline JS event handler quotes in template strings, and add descriptive `aria-label` attributes incorporating action and item context (e.g., `aria-label="Delete entry ${method} ${path}"`) when generating interactive elements dynamically.
