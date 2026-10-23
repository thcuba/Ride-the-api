## 2026-03-31 - Path Traversal in Device Database Assignment API
**Vulnerability:** The `POST /api/devices/{device_id}/database` endpoint accepted `database_name` directly from the JSON body and appended `.db` to construct `db_path = db_manager.device_db_dir / f"{database_name}.db"`. Unsanitized user inputs containing path traversal sequences (e.g. `../../evil_db`) allowed SQLite databases to be assigned and created outside `device_db_dir`.
**Learning:** Even when appending a fixed extension like `.db`, relative path components (`..`, `/`) in input strings allow navigating outside destination directories when joined with `Path` objects.
**Prevention:** Always validate user-provided filename components using `sanitize_filename_component` from `core.atomic_io` before constructing filesystem paths.

## 2026-03-31 - Insecure Wildcard CORS Policy
**Vulnerability:** CORS middleware was configured with wildcard origins (`allow_origins=["*"]`), allowing malicious web pages hosted on arbitrary domains to issue cross-origin HTTP requests against the local replacement proxy server.
**Learning:** Using wildcard origins in CORS middleware exposes endpoints to cross-site request attacks from any browser visiting untrusted web pages.
**Prevention:** Restrict `allow_origins` to explicitly trusted local application origins (e.g., `["http://localhost:8911", "http://127.0.0.1:8911"]`).
