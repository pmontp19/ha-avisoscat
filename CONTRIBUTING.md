# Contributing

## Development environment

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements_dev.txt

.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m pytest tests/ --cov=custom_components/avisoscat --cov-fail-under=95
```

All three must be green before opening a PR: they are exactly what `ci.yml` runs.
`validate.yml` adds hassfest and HACS validation on top, which need no local setup.

## Commit messages: Conventional Commits (required)

`release-please` reads the commit history to compute the next version and generate the
changelog; a badly formatted subject is not counted and stays out of the release.

```
<type>[!]: <description>

[optional body]

[BREAKING CHANGE: ... ]
```

Usual types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`. A `!` after the type
(`fix!:`) or a `BREAKING CHANGE:` footer marks an incompatible change.

Do not reference implementation-plan task numbers (`T14`, `Task 5`) in commit messages or
code comments: the plan evolves and the reference goes stale. Explain the *why* directly.

## Release cycle

The project is pre-1.0 (`bump-minor-pre-major` is on in `release-please-config.json`): until
we deliberately reach `1.0.0`, a `fix!`/`BREAKING CHANGE` bumps **minor**, not major.

1. Merge ordinary PRs into `main` with Conventional Commits.
2. `release-please` keeps a `chore(main): release vX.Y.Z` PR up to date with the changelog
   and the version bump in `pyproject.toml` and `custom_components/avisoscat/manifest.json`.
3. Never edit those two version fields by hand: `release-please` is the only source of truth.
4. Merging that PR creates the `vX.Y.Z` tag and the GitHub Release automatically.

Dependabot opens weekly PRs for `github-actions` and `pip`. The pinned test stack is
excluded and bumped by hand instead; the exact list and the reason for each entry live in
`.github/dependabot.yml`.

## Tests

- `pytest-homeassistant-custom-component` plus `aioresponses`; no test makes a real network
  request.
- Clock-dependent logic uses the `clock` fixture (`FakeClock` in `tests/conftest.py`), never
  a real `sleep()` and never `freezegun`. Warning validity is entirely a function of the
  6-hour UTC band, so this covers most of the integration.
- Fixtures under `tests/fixtures/` must be real captured `meteo.cat` responses, never
  invented ones. If you change parsing, re-capture them.

## User-facing strings

Catalan is the reference language. Any new entity or config-flow field needs a key in
**all three** of `custom_components/avisoscat/translations/{ca,es,en}.json` or hassfest
fails. Code, comments and commit messages stay in English.
