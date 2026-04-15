# Repository Guidelines

## Project Structure & Module Organization
`main.py` boots the FastAPI application, wires CORS middleware, and includes routers from `api.py`. Route handlers use SQLAlchemy sessions from `database.py` and the `Post` ORM in `models.py`. SQLite storage lives in `posts.db`; regenerate it locally if schema changes. Keep reusable helpers close to their domain—HTTP logic in `api.py`, persistence utilities in `database.py`, and model extensions in new modules under `models/` if the file grows. The provided `run.sh` script activates `.venv` and serves the app with Uvicorn.

## Build, Test, and Development Commands
- `python -m venv .venv && source .venv/bin/activate`: recreate the local virtual environment.
- `uv pip sync uv.lock`: install locked FastAPI, SQLAlchemy, and related dependencies (or `pip install fastapi uvicorn sqlalchemy pytz` if `uv` is unavailable).
- `uvicorn main:app --reload`: run the API with auto-reload during development.
- `bash run.sh`: production-style launch that mirrors deployment settings.

## Coding Style & Naming Conventions
Follow PEP 8 with 4-space indentation and snake_case for variables, functions, and filenames (`create_post` table, `get_posts` handler). Keep Pydantic models suffixed with `Create`/`Response` to clarify intent, and annotate function signatures with types. When adding modules, include succinct docstrings describing the endpoint or data flow. Prefer timezone-aware datetime helpers as shown in `models.py:utc_now` and `models.py:format_as_utc`.

## Testing Guidelines
Adopt `pytest` under a top-level `tests/` directory; mirror module names (`tests/test_api_posts.py`) for discovery. Use FastAPI’s `TestClient` against in-memory SQLite (`sqlite:///:memory:`) to isolate state. Aim for coverage on happy path, validation errors, and rollback scenarios; avoid touching `posts.db` during tests.

## Commit & Pull Request Guidelines
Recent commits are concise but mixed-language (`Initial commit`, `message`); standardize on imperative English summaries under 50 characters, e.g., `Add witness pagination`. Reference issues in the body and note schema or API changes explicitly. PRs should include: scope overview, manual or automated test output, database migration notes, and screenshots or sample responses when endpoints change. Tag reviewers familiar with the affected module.
