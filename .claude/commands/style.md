# Style Check Agent

You are reviewing the WayCore codebase for style consistency. Read CLAUDE.md first for project conventions. Be specific — file and line number for every issue.

## Rules

**Python style**
- Type hints on all function signatures (parameters + return type). `async def foo(x: str) -> None` not `async def foo(x)`
- Pydantic models for all data crossing layer boundaries (adapter → worker, worker → DB)
- `structlog` for all logging — never `print()`, never `logging.info()`; always bind `job_id` when inside a workflow step
- `asyncio.sleep` not `time.sleep` in async code
- f-strings not `.format()` or `%`

**Naming**
- Snake case for functions and variables
- `SCREAMING_SNAKE` for module-level constants only
- Pydantic models: PascalCase (e.g. `TransactionData`, `SyncRequest`)
- SQLAlchemy models: PascalCase (e.g. `Transaction`, `SyncJob`)
- Test files: `test_<module>.py`

**Imports**
- Standard library first, third-party second, local third — one blank line between groups
- No wildcard imports (`from x import *`)
- No unused imports

**Functions**
- One responsibility per function
- If a function is longer than ~40 lines, it's probably doing two things
- No mutable default arguments

**Error handling**
- Specific exceptions, not bare `except:`
- Never `except Exception: pass`
- Log before re-raising if context would be lost

**What NOT to flag**
- Line length (not enforced in this project)
- Docstrings on internal functions (not required)
- Minor formatting differences that don't affect readability

## Output Format

List issues as:
`src/path/file.py:42` — [rule] description

Group by file. If a file is clean, one line: `src/path/file.py — clean`.

End with a one-line summary: "N issues across M files" or "All clean."
