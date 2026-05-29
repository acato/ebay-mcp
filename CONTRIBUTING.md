# Contributing to ebay-mcp

Thanks for your interest! This project is alpha — under active development.

## Ground rules

- **Read [DESIGN.md](DESIGN.md) first.** The MVP scope, tool signatures, eBay API split (Browse vs Trading), and safety patterns are documented there.
- **No vendor-specific defaults.** This codebase must work for any eBay user. No hardcoded item IDs, sellers, or account info from any specific environment.
- **No secrets in code, tests, or fixtures.** App ID, Cert ID, user tokens come from config files or environment variables. PRs that hardcode credentials will be rejected.
- **Sandbox-first for any new write tool.** New money-commit features must land with sandbox integration tests BEFORE any production smoke.

## Development setup

```bash
git clone https://github.com/acato/ebay-mcp
cd ebay-mcp
uv sync --all-extras
uv run pytest
uv run ruff check
uv run ruff format --check
```

Python 3.11+ required. Dependencies are managed by [uv](https://docs.astral.sh/uv/). See the [Windows: avoid Microsoft Store Python](README.md#windows-avoid-microsoft-store-python) note if you're on Windows.

## Testing

- **Unit tests** (`tests/test_*.py`) — fast, no network. Use `respx` to mock httpx for both Browse REST and Trading XML responses.
- **Sandbox integration tests** (`tests/test_live_sandbox.py`) — gated on `EBAY_MCP_LIVE=1`. Hits `api.sandbox.ebay.com`. Safe to run repeatedly — sandbox has fake money.
- **Production smoke tests** (`tests/test_live_production.py`) — gated on `EBAY_MCP_LIVE=1` AND `EBAY_MCP_LIVE_PRODUCTION=1`. Read-only by default. Never runs in CI.

For sandbox tests:

```bash
export EBAY_MCP_LIVE=1
uv run pytest tests/test_live_sandbox.py
```

Write tests against sandbox use auctions you create as that sandbox test user. Never commit a real config file containing credentials.

## Safety gate compliance (mandatory for money-commit tools)

Every new tool that commits real money MUST:
1. Take a `confirm_amount` parameter that must equal the value-at-risk exactly.
2. Refuse with structured payload if `confirm_amount` mismatches or if amount > $500 cap.
3. Surface the active host (`sandbox` vs `production`) in the response.
4. Have a docstring that loudly warns about production-host behavior.

Tests for the safety gates must come FIRST in the PR, before the happy path.

## Code style

- `ruff check` and `ruff format` are CI-enforced.
- Type hints required on public functions (the MCP tool surface).
- Docstrings on every public function. Google docstring style.

## License compatibility

This project is Apache-2.0. **All runtime dependencies must be compatible** — that means MIT, BSD, ISC, Apache-2.0, or other permissive licenses. **No GPL, LGPL, AGPL, or MPL** runtime deps without explicit project-owner approval. PRs that add copyleft runtime deps will be rejected.

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`. Scope optional.

## License

By contributing, you agree that your contributions will be licensed under Apache-2.0.
