## 2026-03-31 - Path Traversal in Device Database Assignment API
**Vulnerability:** The `POST /api/devices/{device_id}/database` endpoint accepted `database_name` directly from the JSON body and appended `.db` to construct `db_path = db_manager.device_db_dir / f"{database_name}.db"`. Unsanitized user inputs containing path traversal sequences (e.g. `../../evil_db`) allowed SQLite databases to be assigned and created outside `device_db_dir`.
**Learning:** Even when appending a fixed extension like `.db`, relative path components (`..`, `/`) in input strings allow navigating outside destination directories when joined with `Path` objects.
**Prevention:** Always validate user-provided filename components using `sanitize_filename_component` from `core.atomic_io` before constructing filesystem paths.

## 2026-04-12 - AST Whitelisting for Formula Evaluation
**Vulnerability:** AST tree evaluation in `PatternEngine._eval_formula` could potentially allow unintended AST node execution (such as attribute access, method calls, or container indexing) if `simpleeval` configuration or internal node handling were bypassed or improperly restricted.
**Learning:** Hardening formula evaluators requires defense-in-depth: beyond configuring evaluator options (like `allowed_attrs={}`), validating the parsed AST tree against a strict whitelist of allowed AST node types (`_ALLOWED_FORMULA_AST_NODES`) ensures unsafe syntax constructs (e.g. `Attribute`, `Subscript`, `Lambda`, comps) are rejected before evaluation.
**Prevention:** Always perform strict AST node whitelisting on user-supplied expression inputs prior to evaluating them.
