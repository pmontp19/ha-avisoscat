# Agent instructions

Operating rules for this repository. Project description and user-facing behaviour →
`README.md`. Design detail → `docs/` (`01-data-sources.md` … `05-implementation-plan.md`);
those documents are the contract, so read the relevant section before changing behaviour.

## Commits

- Strict Conventional Commits (`feat:`, `fix:`, `fix!:`, `docs:`, `chore:`, `refactor:`,
  `test:`, `ci:`). `release-please` depends on them for the version and changelog; a badly
  formatted subject drops out of the release. Detail in `CONTRIBUTING.md`.
- Do not reference implementation-plan task numbers (`T14`, `Task 5`) in commits or code
  comments: the plan evolves and the reference goes stale.
- Never hand-edit `version` in `pyproject.toml` or
  `custom_components/avisoscat/manifest.json`: only `release-please` touches them.

## Code

- Comments and identifiers in English. User-facing strings via `_attr_translation_key` plus
  `translations/{ca,es,en}.json`, Catalan as the reference language. Any new entity or
  config-flow field needs a key in **all three** files or hassfest fails.
- Integration state on `entry.runtime_data`, never `hass.data[DOMAIN]`.
- Multi-entry by design: one config entry per comarca, so no `single_config_entry` in the
  manifest (unlike the sibling `ha-incendiscat`).
- The keyless `meteo.cat` inline payload is **not** an official API and can change without
  notice: read fields with `.get()` plus a default, never direct indexing, and keep the last
  good state on failure instead of clearing it.
- The payload is not stable byte for byte between requests even when the warnings have not
  changed: the `afectacions` list comes back rotated. Any payload hash or snapshot comparison
  must be order-insensitive or it reports a change every cycle
  (`docs/captures/smp-page-choice-2026-08-06.md`).
- `comentari`, `llindar` and `meteor_nom` are untrusted external text: never `allow_html`,
  never direct HTML interpolation. Diagnostics must keep redacting `latitude`, `longitude`
  and `api_key`.
- Warning validity depends on the wall clock, not only on fetched data: the 6-hour UTC bands
  change without the source changing. Keep the one-minute local recompute
  (`docs/04-architecture.md` §5) network-free.
- Config-flow-only integration: do not reintroduce YAML (`configuration.yaml`) support.

## Tests

- Zero real network in tests.
- Clock-dependent logic → the `clock` fixture (`FakeClock` in `tests/conftest.py`), never a
  real `sleep()` and never `freezegun`.
- Test stack, coverage gate and fixture rules → `CONTRIBUTING.md`. Do not lower the coverage
  gate to make a change pass.

## Before opening or updating a PR

Run the local gates listed in `CONTRIBUTING.md`: they are exactly what
`.github/workflows/ci.yml` runs. `validate.yml` additionally runs hassfest and HACS
validation.

Two validation facts worth knowing before you debug them again:

- hassfest rejects `config_flow: true` without a `config_flow.py` defining the flow handler.
- HACS validation checks repository metadata too (topics, description, license, issues
  enabled), not just the files in this repo.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
