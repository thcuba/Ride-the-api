# Contributing to Ride-the-API

Thanks for contributing! This guide covers the development workflow, how to run
tests and linters, and the conventions we follow.

## Development setup

Requirements: Python 3.11+ (CI runs 3.12), GNU Make not required — plain
Python tooling is used.

```bash
git clone https://github.com/thcuba/Ride-the-api.git
cd Ride-the-api
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
```

If the project has no `[dev]` extra yet (or you prefer explicit tooling):

```bash
pip install -e .
pip install pytest ruff mypy
```

Configure your LLM in `config/config.yaml` (see `docs/configuration.md`) and
start the server:

```bash
python -m core.server
```

## Running tests

Run the full suite:

```bash
python -m pytest
```

Run only the tests for the module you changed:

```bash
python -m pytest tests/test_<module>.py -q
```

Always run at least the targeted tests for your change, plus the full suite
before pushing. Windows note: set `PYTHONIOENCODING=utf-8` if you hit encoding
issues in test output.

## Linting and typing

```bash
python -m ruff check .          # linter
python -m ruff format --check . # formatting
python -m mypy core             # static types (if configured)
```

The ruff configuration lives in `pyproject.toml` (line-length 100). When a rule
legitimately does not apply, prefer a targeted `# noqa: <RULE>` on the offending
line over disabling the rule globally.

## Code conventions

- **Type hints** are required on all public functions and methods.
- Use `collections.abc` for generic ABCs (`Sequence`, `Callable`, …) rather than
  `typing` aliases.
- **Timezone-aware datetimes** (`datetime.now(UTC)`) — never `utcnow()`.
- Logging goes through the module-level `logger` (`logging.getLogger(__name__)`),
  never `print`.
- Keep changes focused; do not reformat unrelated code.

## Architecture notes

- `core/database.py` — SQLite engines. Every async engine MUST set WAL,
  `synchronous=NORMAL` (or FULL where durability matters), `busy_timeout`, and
  `foreign_keys`. Schema changes require an Alembic migration, not just
  `create_all`.
- `core/tls_mitm.py` — TLS interception. HTTP/1.1 parsing/serialization must use
  the `h11` state machine (`parse_decrypted_http_request`,
  `serialize_http_response`); do not reintroduce regex parsers.
- `core/pipeline.py` — request handling is built on explicit **fallback
  chains** (`core/fallback_policy.py`): learned pattern → cloud forward → safe
  local default. A new handling path should preserve that ordering.
- State and buffered data must never be lost on restart — persistence is
  database-backed and file writes are atomic (temp file + `os.replace`).

## Commit and PR workflow

1. Create a branch off `main`: `git checkout -b fix/description`.
2. Make focused commits. Prefix subject lines with the subsystem, e.g.
   `feat(fallback):`, `fix(tls):`, `refactor(db):`, `docs(contributing):`,
   `chore(dependabot):`.
3. Run the targeted tests and the full suite; run `ruff check` on changed files.
4. Push and open a pull request. Reference any related issue in the PR body.
5. CI runs lint + tests on the PR; fix any failures before requesting review.

Dependency updates are handled by [Dependabot](.github/dependabot.yml) (pip,
Docker, GitHub Actions). Avoid manually bumping versions outside of a
Dependabot PR unless there is a strong reason.

## Where contributions are welcome

- **Protocol servers** (`core/protocol_servers/`) — real transports for MQTT,
  CoAP, Modbus, WebSocket, HTTP/2, and the Zigbee/Z-Wave/Matter bridges.
- **Adapters** (`adapters/`) — new device vendors and protocol parsing.
- **Cloud forwarding** — wiring `core/upstream_resolver.py` into adapters.
- **Fallback robustness** — improving `core/fallback_policy.py`, circuit
  breakers, and retry behavior.
- **Tests and documentation** for any of the above.

## License

By contributing you agree that your contributions are licensed under the
project's MIT license.