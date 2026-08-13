"""Translation parity and shape tests.

Catalan is the reference language (docs/03-feature-spec.md, "User-facing
strings"): every user-facing string lives under a key in
`translations/{ca,es,en}.json`, and `strings.json` is the canonical schema HA
tools (hassfest, the translations builder) read first. A missing key in any of
the three languages shows up in the UI as a raw `component.avisoscat…` path, so
these tests pin the contract before hassfest ever runs in CI:

1. **Schema parity**: `strings.json` and the three translation files share the
   exact same set of deep keys. A key added to one file but not the others is a
   bug regardless of which file missed it.
2. **Placeholder parity**: a `{comarca}` placeholder in one language must appear
   in every language, or HA's string formatter raises at render time.
3. **Coverage**: every translation key the code actually references (entity
   `_attr_translation_key`, config-flow `step_id`, `async_abort(reason=...)`,
   `errors[...]=...`, repair-issue `translation_key=...`) is present in the
   schema. hassfest catches the same thing at release time; this fails faster
   and points at the offending code site.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

import pytest
from custom_components.avisoscat.comarques import ResolutionError

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "avisoscat"
STRINGS_PATH = COMPONENT_DIR / "strings.json"
TRANSLATION_PATHS = {
    "ca": COMPONENT_DIR / "translations" / "ca.json",
    "es": COMPONENT_DIR / "translations" / "es.json",
    "en": COMPONENT_DIR / "translations" / "en.json",
}

# Every Python module that can reference a translation key. Restricting the
# glob keeps the coverage check fast and avoids dragging in test fixtures.
_CODE_MODULES = tuple(COMPONENT_DIR.glob("*.py"))


def _load_json(path: Path) -> dict:
    """Parse a translation file, failing with the file name on a JSON error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        pytest.fail(f"{path.relative_to(Path.cwd())} is not valid JSON: {err}")


def _deep_keys(obj: object, prefix: str = "") -> set[str]:
    """Flatten a nested dict into a set of dotted paths to its leaves.

    Only dict nesting counts: a leaf is any value that is not a dict, so an
    entity's `state` block contributes one key per state value, and a missing
    `state` block in one language but not another is caught as a parity
    violation rather than papered over.
    """
    keys: set[str] = set()
    if isinstance(obj, dict):
        for name, value in obj.items():
            qualified = f"{prefix}.{name}" if prefix else name
            if isinstance(value, dict):
                keys |= _deep_keys(value, qualified)
            else:
                keys.add(qualified)
    return keys


@pytest.fixture(scope="module")
def strings_schema() -> set[str]:
    """The deep keys of `strings.json`, the canonical schema."""
    return _deep_keys(_load_json(STRINGS_PATH))


@pytest.fixture(scope="module")
def translations() -> dict[str, set[str]]:
    """Deep keys of each `translations/*.json` file."""
    return {
        lang: _deep_keys(_load_json(path)) for lang, path in TRANSLATION_PATHS.items()
    }


@pytest.mark.parametrize("lang", sorted(TRANSLATION_PATHS))
def test_translation_file_matches_strings_schema(
    strings_schema: set[str],
    translations: dict[str, set[str]],
    lang: str,
) -> None:
    """Each translation file has exactly the keys `strings.json` declares.

    hassfest checks the same thing implicitly when it builds the translations
    cache, but it does not tell you *which* key is extra or missing. This
    assertion does, and it also keeps `strings.json` honest as the schema source
    of truth: the day someone adds a key to `ca.json` only, the failure names
    the offender.
    """
    extras = translations[lang] - strings_schema
    missing = strings_schema - translations[lang]
    assert not extras, f"{lang}: keys not in strings.json: {sorted(extras)}"
    assert not missing, f"{lang}: keys missing from {lang}.json: {sorted(missing)}"


def test_all_translation_files_share_the_same_key_set(
    translations: dict[str, set[str]],
) -> None:
    """ca, es and en expose the exact same set of keys: not one more, not one less.

    The task's acceptance criterion. Splitting the diff by language would hide
    a single-language regression behind whichever pair happened to match, so the
    set is compared against a frozen reference (Catalan, the reference language)
    for each file independently.
    """
    reference = translations["ca"]
    for lang, keys in translations.items():
        assert keys == reference, (
            f"{lang} diverges from the Catalan reference:\n"
            f"  only in {lang}: {sorted(keys - reference)}\n"
            f"  missing in {lang}: {sorted(reference - keys)}"
        )


_PLACEHOLDER_RE = re.compile(r"\{[^}]+\}")


def _placeholders(value: object) -> frozenset[str]:
    """Collect every `{placeholder}` name from a translation value.

    Walks dicts and lists so a future nested structure (e.g. an entity
    `state_attributes` block) is covered without rewriting the test.
    """
    found: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            found |= _placeholders(child)
    elif isinstance(value, list):
        for child in value:
            found |= _placeholders(child)
    elif isinstance(value, str):
        found.update(_PLACEHOLDER_RE.findall(value))
    return frozenset(found)


@pytest.mark.parametrize("lang", sorted(TRANSLATION_PATHS))
def test_placeholders_match_strings_json(lang: str) -> None:
    """Every `{placeholder}` in `strings.json` is reproduced in each language.

    HA's string formatter raises `KeyError` at render time if a placeholder is
    missing from a translation, and silently drops the value if a translation
    invents one the code does not supply. Both directions are bugs, so the
    placeholder *sets* are compared for every key path the schema declares.
    """
    strings_data = _load_json(STRINGS_PATH)
    translation_data = _load_json(TRANSLATION_PATHS[lang])

    def _dig(obj: object, parts: list[str]) -> object | None:
        cur: object = obj
        for part in parts:
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    mismatches: list[str] = []
    for dotted in _deep_keys(strings_data):
        parts = dotted.split(".")
        ref = _dig(strings_data, parts)
        if not isinstance(ref, str):
            continue  # Structural key; parity is checked by the schema test.
        other = _dig(translation_data, parts)
        if not isinstance(other, str):
            continue  # Shape parity is enforced elsewhere; do not double-report.
        ref_ph = _placeholders(ref)
        other_ph = _placeholders(other)
        if ref_ph != other_ph:
            mismatches.append(
                f"{dotted}: strings.json has {sorted(ref_ph)}, "
                f"{lang} has {sorted(other_ph)}"
            )
    assert not mismatches, "\n".join(mismatches)


# ---------------------------------------------------------------------------
# Coverage: every translation key the code references must exist in the schema.
#
# hassfest validates this at release time by introspecting the integration; this
# test fails earlier and points at the source line. Mirroring hassfest exactly
# is not the goal: the patterns below catch every form this codebase uses to
# point at a translation key, which is what matters in practice.
# ---------------------------------------------------------------------------


def _code_text() -> str:
    """Concatenate every Python module so a single regex pass covers them all."""
    chunks: list[str] = []
    for path in sorted(_CODE_MODULES):
        chunks.append(f"# file: {path.name}\n{path.read_text(encoding='utf-8')}")
    return "\n".join(chunks)


def _regex_keys(pattern: str, text: str, group: int = 1) -> Iterable[str]:
    yield from (m.group(group) for m in re.finditer(pattern, text, re.MULTILINE))


@pytest.fixture(scope="module")
def code_translation_references() -> dict[str, set[str]]:
    """Translation keys the code references, grouped by schema section.

    Each set is compared against the matching branch of `strings.json`. The
    patterns intentionally match the literal forms used in this codebase:
    `_attr_translation_key = "..."` for entities, `step_id="..."` for flow
    steps, `reason="..."` for aborts, `errors[...] = "..."` for form errors and
    `ISSUE_* = "..."` plus `translation_key=...` for repair issues. Adding a new
    form means adding a new pattern here, not loosening the assertions.
    """
    text = _code_text()
    return {
        # Entities carry their translation key on the class. Both binary sensors
        # and sensors resolve to the same `entity.<platform>.<key>` block, so
        # the platform is resolved by checking both below.
        "entity": {
            *set(_regex_keys(r'_attr_translation_key\s*=\s*"([a-z0-9_]+)"', text)),
            # Relative-day sensors look up their key in a dict literal.
            *set(_regex_keys(r'DIA_\w+:\s*"([a-z0-9_]+)"', text)),
        },
        # Config- and options-flow step ids. `init` belongs to the options flow,
        # every other step id belongs to the config flow; the assertion below
        # checks both branches.
        "step": set(_regex_keys(r'step_id="([a-z0-9_]+)"', text)),
        # Abort reasons surfaced via `async_abort(reason="...")`.
        "abort": set(_regex_keys(r'reason="([a-z0-9_]+)"', text)),
        # Per-field errors surfaced via `errors[CONF_API_KEY] = "..."`. The bare
        # `"base"` assignment from `comarques.ResolutionError` is covered by the
        # enum test below, since the values live there and not in config_flow.
        "error": set(_regex_keys(r'errors\[[^\]]+\]\s*=\s*"([a-z0-9_]+)"', text)),
        # Repair-issue keys. The literal lives on the `ISSUE_*` constant and is
        # passed through to `async_create_issue(translation_key=...)`, so
        # scanning the constant definitions is enough.
        "issue": set(_regex_keys(r'ISSUE_\w+\s*=\s*"([a-z0-9_]+)"', text)),
    }


def test_every_code_referenced_translation_key_exists(
    strings_schema: set[str],
    code_translation_references: dict[str, set[str]],
) -> None:
    """Every translation key named in code has a matching entry in the schema.

    The companion `ResolutionError` values (`cannot_connect`,
    `invalid_geometry`, `location_outside_catalonia`) are covered by a separate
    test below, because they are written indirectly through `errors["base"]`.
    """
    strings = _load_json(STRINGS_PATH)
    binary_sensor_keys = set(strings["entity"]["binary_sensor"])
    sensor_keys = set(strings["entity"]["sensor"])
    config_steps = set(strings["config"]["step"])
    options_steps = set(strings["options"]["step"])
    errors = set(strings["config"]["error"])
    aborts = set(strings["config"]["abort"])
    issues = set(strings["issues"])

    # The `init` step belongs to the options flow; every other step id belongs
    # to the config flow. Splitting the check keeps the error message honest.
    config_step_refs = {s for s in code_translation_references["step"] if s != "init"}
    options_step_refs = code_translation_references["step"] & {"init"}

    entity_refs = code_translation_references["entity"]
    entity_refs_in_strings = binary_sensor_keys | sensor_keys

    missing: dict[str, set[str]] = {}
    if config_step_refs - config_steps:
        missing["config.step"] = config_step_refs - config_steps
    if options_step_refs - options_steps:
        missing["options.step"] = options_step_refs - options_steps
    if entity_refs - entity_refs_in_strings:
        missing["entity.{binary_sensor,sensor}"] = entity_refs - entity_refs_in_strings
    if code_translation_references["error"] - errors:
        missing["config.error"] = code_translation_references["error"] - errors
    if code_translation_references["abort"] - aborts:
        missing["config.abort"] = code_translation_references["abort"] - aborts
    if code_translation_references["issue"] - issues:
        missing["issues"] = code_translation_references["issue"] - issues

    assert not missing, (
        "code references translation keys that do not exist in strings.json:\n"
        + "\n".join(f"  {k}: {sorted(v)}" for k, v in missing.items())
    )


def test_resolution_error_values_are_config_errors() -> None:
    """`comarques.ResolutionError` values double as `config.error.*` keys.

    `async_step_comarca` writes whichever value the resolver returned straight
    into `errors["base"]`, so a typo in the enum is a translation bug that the
    regex above cannot see. Importing the enum at module load keeps this test
    honest even if the enum grows.
    """
    strings = _load_json(STRINGS_PATH)
    error_keys = set(strings["config"]["error"])
    enum_values = {member.value for member in ResolutionError}
    missing = enum_values - error_keys
    assert not missing, (
        f"ResolutionError values not present in config.error: {sorted(missing)}"
    )
