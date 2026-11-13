# Dromos

A training plan that adapts to you.

Dromos pulls your runs from Garmin, compares them against your marathon plan, and
re-plans the remaining weeks from what you actually ran. You talk to it through
Claude (custom connector) or any other MCP client.

**Status:** Phase 0, checking Garmin access.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## Garmin spike

```sh
# First time: put your credentials in .env (gitignored). The script logs in
# (asks for an MFA code if enabled) and saves tokens to ~/.dromos/garmin-tokens
cp .env.example .env
.venv/bin/python scripts/garmin_spike.py

# After that, the saved tokens are reused; you can remove the password from .env
.venv/bin/python scripts/garmin_spike.py
```

Prints your last 30 runs and saves the raw JSON to `data/` (gitignored).
