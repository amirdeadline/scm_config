"""Dry-run planning for config_import.py: what will be created, overwritten or skipped."""

import difflib
import json
from dataclasses import dataclass, field
from typing import Optional

from scm_catalog import ENDPOINTS, ENDPOINTS_BY_KEY, RULE_POSITIONS, match_key, strip_server_fields

CREATE, UPDATE, UNCHANGED, SKIP = "create", "update", "unchanged", "skip"


@dataclass(frozen=True)
class Change:
    action: str
    endpoint_key: str
    name: str
    source: str
    position: Optional[str] = None
    desired: Optional[dict] = None
    current: Optional[dict] = None
    reason: str = ""

    @property
    def label(self) -> str:
        where = f"{self.endpoint_key} [{self.position}]" if self.position else self.endpoint_key
        return f"{where} '{self.name}'"


@dataclass
class Desired:
    """Merged import content: (endpoint key, position) -> {match key: (item, source)}."""
    items: dict = field(default_factory=dict)
    collisions: list = field(default_factory=list)
    unknown_types: list = field(default_factory=list)


# --------------------------------------------------------------- input file
def load_export(path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or not ({"folders", "snippets"} & set(document)):
        raise ValueError(f"{path} is not a config_export.py file (no 'folders' or 'snippets' key).")
    for section in ("folders", "snippets"):
        if not isinstance(document.get(section) or {}, dict):
            raise ValueError(f"'{section}' in {path} must be an object.")
    for name, folder in (document.get("folders") or {}).items():
        if not isinstance(folder, dict) or not isinstance(folder.get("config", {}), dict):
            raise ValueError(f"folder '{name}' in {path} has no valid 'config' object.")
    for name, config in (document.get("snippets") or {}).items():
        if not isinstance(config, dict):
            raise ValueError(f"snippet '{name}' in {path} must be an object of config types.")
    return document


def list_sources(document: dict) -> list:
    """[(kind, name, config)] for every folder and snippet in the export file."""
    sources = [("folder", name, folder.get("config") or {})
               for name, folder in (document.get("folders") or {}).items()]
    sources += [("snippet", name, config or {})
                for name, config in (document.get("snippets") or {}).items()]
    return sources


def select_sources(sources: list, wanted: list) -> list:
    if not wanted:
        return sources
    names = {name for _, name, _ in sources}
    missing = [w for w in wanted if w not in names]
    if missing:
        raise ValueError(f"--source {', '.join(missing)} not in the file. "
                         f"It contains: {', '.join(sorted(names))}.")
    return [s for s in sources if s[1] in wanted]


def merge_sources(sources: list) -> Desired:
    """Merge all source containers into one set; on a name clash the later source wins."""
    desired = Desired()
    for kind, name, config in sources:
        source = f"{kind} '{name}'"
        for key, value in config.items():
            endpoint = ENDPOINTS_BY_KEY.get(key)
            if endpoint is None:
                desired.unknown_types.append(f"{key} (from {source})")
                continue
            value = value or ([] if not endpoint.rules else {})
            if endpoint.rules != isinstance(value, dict):
                expected = "an object with 'pre'/'post' lists" if endpoint.rules else "a list"
                desired.unknown_types.append(f"{key} (from {source}): malformed, expected {expected}")
                continue
            if endpoint.rules:
                groups = [(pos, value.get(pos) or []) for pos in RULE_POSITIONS]
            else:
                groups = [(None, value)]
            for position, items in groups:
                bucket = desired.items.setdefault((key, position), {})
                for item in items:
                    if not isinstance(item, dict):
                        desired.unknown_types.append(f"{key} (from {source}): non-object item ignored")
                        continue
                    item_key = match_key(item, endpoint.key_field)
                    if item_key in bucket:
                        desired.collisions.append(
                            f"{key}{f' [{position}]' if position else ''} '{item_key}': "
                            f"{bucket[item_key][1]} replaced by {source}")
                    bucket[item_key] = (item, source)
    return desired


# ------------------------------------------------------------------ planning
# SCM marks an edited built-in object with these and sets them per container
# ("predefined-snippet" in a folder, "predefined" in a snippet), so they are not compared.
OVERRIDE_MARKERS = frozenset({"override_loc", "override_type"})


def without_markers(item: dict) -> dict:
    return {k: v for k, v in strip_server_fields(item).items() if k not in OVERRIDE_MARKERS}


def comparable(item: dict) -> str:
    return json.dumps(without_markers(item), sort_keys=True, ensure_ascii=False)


def plan_changes(desired: Desired, current: dict, any_rulebase: bool = False) -> list:
    """Compare desired items with the target's current items, in import order.

    current: (endpoint key, position) -> {match key: item as read from SCM}
    any_rulebase: match rules by name in pre or post. Used for snippet targets,
    whose rulebase SCM does not report.
    """
    changes = []
    for endpoint in ENDPOINTS:
        positions = RULE_POSITIONS if endpoint.rules else (None,)
        for position in positions:
            bucket = desired.items.get((endpoint.key, position))
            if not bucket:
                continue
            existing = current.get((endpoint.key, position), {})
            other = [p for p in positions if p and p != position]
            other_existing = current.get((endpoint.key, other[0]), {}) if other else {}
            if any_rulebase:
                existing, other_existing = {**other_existing, **existing}, {}
            for item_key, (item, source) in bucket.items():
                common = dict(endpoint_key=endpoint.key, name=item_key, source=source,
                              position=position, desired=item)
                if not endpoint.importable:
                    changes.append(Change(SKIP, reason=endpoint.import_note, **common))
                elif item_key not in existing and item_key in other_existing:
                    changes.append(Change(SKIP, reason=f"a rule with this name already exists in the "
                                                       f"{other[0]} rulebase of the target", **common))
                elif item_key not in existing:
                    changes.append(Change(CREATE, **common))
                elif not existing[item_key].get("id"):
                    changes.append(Change(SKIP, reason="existing object has no id in the API response; "
                                                       "update it manually", **common))
                elif comparable(existing[item_key]) == comparable(item):
                    changes.append(Change(UNCHANGED, current=existing[item_key], **common))
                else:
                    changes.append(Change(UPDATE, current=existing[item_key], **common))
    return changes


# ----------------------------------------------------------------- reporting
class Palette:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def green(self, text):
        return self._wrap("32", text)

    def red(self, text):
        return self._wrap("31", text)

    def yellow(self, text):
        return self._wrap("33", text)

    def cyan(self, text):
        return self._wrap("36", text)

    def bold(self, text):
        return self._wrap("1", text)


def pretty(item: dict, clean=strip_server_fields) -> list:
    return json.dumps(clean(item), indent=2, sort_keys=True, ensure_ascii=False).splitlines()


def diff_lines(change: Change, colors: Palette) -> list:
    before = pretty(change.current, without_markers)
    after = pretty(change.desired, without_markers)
    lines = []
    for line in difflib.unified_diff(before, after, "current (SCM)", "import file", lineterm="", n=3):
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(colors.green(line))
        elif line.startswith("-") and not line.startswith("---"):
            lines.append(colors.red(line))
        elif line.startswith("@@"):
            lines.append(colors.cyan(line))
        else:
            lines.append(line)
    return lines


def render_report(changes: list, desired: Desired, target: str, colors: Palette) -> str:
    """Full dry-run report: summary, every new object, a diff for every overwrite."""
    counts = {action: 0 for action in (CREATE, UPDATE, UNCHANGED, SKIP)}
    for change in changes:
        counts[change.action] += 1

    out = [colors.bold(f"=== Dry run: import into {target} ===")]
    out.append(f"  {colors.green(f'{counts[CREATE]} to create')}, "
               f"{colors.yellow(f'{counts[UPDATE]} to overwrite')}, "
               f"{counts[UNCHANGED]} unchanged, {counts[SKIP]} skipped")

    per_type = {}
    for change in changes:
        per_type.setdefault(change.endpoint_key, {a: 0 for a in counts})[change.action] += 1
    out.append("")
    out.append(f"  {'config type':<48}{'create':>8}{'overwrite':>11}{'same':>7}{'skip':>7}")
    for key, c in per_type.items():
        out.append(f"  {key:<48}{c[CREATE]:>8}{c[UPDATE]:>11}{c[UNCHANGED]:>7}{c[SKIP]:>7}")

    if desired.collisions:
        out.append("")
        out.append(colors.yellow(f"--- Name collisions between sources ({len(desired.collisions)}), "
                                 f"last one wins:"))
        out.extend(f"  {line}" for line in desired.collisions)
    if desired.unknown_types:
        out.append("")
        out.append(colors.yellow("--- Config types this tool does not import (ignored):"))
        out.extend(f"  {line}" for line in desired.unknown_types)

    for change in (c for c in changes if c.action == UPDATE):
        out.append("")
        out.append(colors.yellow(f"~ OVERWRITE {change.label}  (from {change.source})"))
        out.extend(f"    {line}" for line in diff_lines(change, colors))
    for change in (c for c in changes if c.action == CREATE):
        out.append("")
        out.append(colors.green(f"+ CREATE {change.label}  (from {change.source})"))
        out.extend(f"    {colors.green('+ ' + line)}" for line in pretty(change.desired))
    new_rules = [c for c in changes if c.action == CREATE and c.position]
    if new_rules:
        out.append("")
        out.append(colors.cyan(f"--- Rule placement: {len(new_rules)} new rule(s) will be placed next to "
                               f"their neighbours from the file. Existing rules are not moved."))
    skipped = [c for c in changes if c.action == SKIP]
    if skipped:
        out.append("")
        out.append(colors.yellow("--- Skipped:"))
        out.extend(f"  {c.label}: {c.reason}" for c in skipped)
    return "\n".join(out)
