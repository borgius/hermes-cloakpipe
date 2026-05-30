# Project Guidelines

## Architecture

- This repo ships a Hermes model-provider plugin; the real plugin root is `plugins/model-providers/cloakpipe/`, not the repository root.
- Treat `plugins/model-providers/cloakpipe/__init__.py` as the main implementation file and keep import-time behavior side-effect free.
- The provider exposes one stable model, `cloakpipe/latest`; do not reintroduce legacy virtual IDs or `CLOAKPIPE_MODELS`.

## Build and Test

- Use `python3 -m unittest discover -s tests -v` for normal verification.
- Use `HERMES_INTEGRATION=1 python3 -m unittest discover -s tests -p 'test_hermes_integration.py' -v` only when changing end-to-end Hermes/CloakPipe behavior.
- Tests use `unittest` with local stubs; do not assume a pytest-only workflow.

## Conventions

- Keep secrets and personal identifiers out of source; use `.env` values or placeholders.
- Route Hermes model traffic through the local wrapper (`CLOAKPIPE_HERMES_BASE_URL`) and use `CLOAKPIPE_BASE_URL` only for CloakPipe privacy endpoints.
- See `README.md` for setup, environment variables, and request-flow details before changing runtime behavior.
- IMPORTANT: Never change hermes sources, you only can read it.
