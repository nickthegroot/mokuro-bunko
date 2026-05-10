# AGENTS.md

## Commands

```bash
uv sync --extra dev          # install all deps including dev
uv run ruff check src tests  # lint
uv run mypy src              # type-check (strict mode)
uv run pytest                # run all tests

# Run specific test suites
pytest tests/unit/ -v
pytest tests/integration/ -v
pytest tests/web/ -v                      # needs Playwright: playwright install chromium
pytest tests/e2e/ -v -m "not slow"        # skip slow Docker tests
pytest tests/unit/test_database.py -v     # single file
pytest tests/unit/test_database.py::TestDatabase::test_create_user -v  # single test
```

Local server: `uv run mokuro-bunko serve`. Dev-only workflow order: `ruff check` → `mypy` → `pytest`.

## Architecture

- **Single package**: `src/mokuro_bunko/`. No monorepo.
- **Entry point**: `src/mokuro_bunko/__main__.py:main` (Click CLI).
- **Server**: WsgiDAV + Cheroot (cheroot wraps the WSGI app).
- **Middleware stack** (outer→inner at `server.py:121-135`): RequestLog → Cors → Static → Setup → Home → Account → Login → Registration → Queue → Catalog → Auth → Admin → PropfindCache → DAV. Order matters for auth — AuthMiddleware sets `mokuro.role` in environ before downstream middleware reads it.
- **Database**: Raw `sqlite3` module, no ORM. `Database` class in `database.py` handles all queries with per-thread connections.
- **Config**: YAML file → Python `@dataclass` objects in `config.py`. XDG-conformant paths: `~/.config/mokuro-bunko/config.yaml` and `~/.local/share/mokuro-bunko/`. Env overrides: `MOKURO_CONFIG`, `MOKURO_HOST`, `MOKURO_PORT`, `MOKURO_STORAGE`.

## OCR

OCR runs in an isolated venv at `.ocr-env/` (gitignored), separate from the project venv. It bundles PyTorch + mokuro. Do not install OCR deps into the project venv. The OCR installer manages the isolated env; `uv sync` only gets the server dependencies.

## Key conventions

- `from __future__ import annotations` in every `.py` file (PEP 563 deferred evaluation).
- Ruff line-length = 100, mypy `strict = true`.
- `uv.lock` is gitignored — no lockfile in repo.
- Version must be bumped in both `pyproject.toml` and `src/mokuro_bunko/__init__.py`.
- Legacy role alias: `"writer"` → `"uploader"` in `database.py`.

## Testing quirks

- `tests/web/` needs `playwright install chromium` and a running server instance.
- `tests/e2e/` has Docker-dependent tests marked `slow` — skip with `-m "not slow"`.
- Test fixtures in `tests/conftest.py` use temporary directories and in-memory-esque SQLite (file in `/tmp`).
- Playwright page fixture auto-skips if Playwright or browser isn't available.
