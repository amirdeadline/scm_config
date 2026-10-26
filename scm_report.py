"""Save the config_import.py dry-run report as text, Markdown, HTML, JSON or CSV."""

import csv
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from scm_import_plan import (CREATE, SKIP, UNCHANGED, UPDATE, Palette, diff_lines, pretty,
                             render_report, without_markers)

REPORT_FORMATS = {".txt": "text", ".log": "text", ".md": "markdown", ".html": "html", ".htm": "html",
                  ".json": "json", ".csv": "csv"}
ACTION_NAMES = {CREATE: "create", UPDATE: "overwrite", UNCHANGED: "unchanged", SKIP: "skip"}


def report_format(path) -> str:
    return REPORT_FORMATS.get(Path(path).suffix.lower(), "text")


def field_changes(change) -> list:
    """[(field, current value, new value)] for every top-level field that differs.

    A field missing on one side shows as None. On import the whole object is
    replaced, so a field that is only in SCM is removed.
    """
    before = without_markers(change.current or {})
    after = without_markers(change.desired or {})
    missing = object()  # an explicit null in SCM differs from a missing field
    return [(key, before.get(key), after.get(key)) for key in sorted(set(before) | set(after))
            if before.get(key, missing) != after.get(key, missing)]


def summary(changes: list) -> dict:
    counts = {name: 0 for name in ("create", "overwrite", "unchanged", "skip")}
    for change in changes:
        counts[ACTION_NAMES[change.action]] += 1
    return counts


def write_report(path, changes: list, desired, target: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = {"text": write_text, "markdown": write_markdown, "html": write_html,
              "json": write_json, "csv": write_csv}[report_format(path)]
    writer(path, changes, desired, target)


# ------------------------------------------------------------------ formats
def write_text(path: Path, changes, desired, target) -> None:
    path.write_text(render_report(changes, desired, target, Palette(False)), encoding="utf-8")


def as_record(change) -> dict:
    record = {"action": ACTION_NAMES[change.action], "config_type": change.endpoint_key,
              "rulebase": change.position, "name": change.name, "source": change.source}
    if change.action == UPDATE:
        record["fields"] = [{"field": f, "current": old, "new": new} for f, old, new in field_changes(change)]
        record["current"] = without_markers(change.current)
        record["new"] = without_markers(change.desired)
    elif change.action == CREATE:
        record["new"] = without_markers(change.desired)
    elif change.action == SKIP:
        record["reason"] = change.reason
    return record


def write_json(path: Path, changes, desired, target) -> None:
    document = {
        "target": target,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": summary(changes),
        "changes": [as_record(c) for c in changes if c.action != UNCHANGED],
        "unchanged": [c.label for c in changes if c.action == UNCHANGED],
        "collisions": desired.collisions,
        "ignored_types": desired.unknown_types,
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")


def excel_safe(text: str) -> str:
    """Stop Excel from running a cell that starts like a formula."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def cell(value) -> str:
    if value is None:
        return ""
    return excel_safe(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True))


def write_csv(path: Path, changes, desired, target) -> None:
    # utf-8-sig so Excel opens non-ASCII names correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["action", "config_type", "rulebase", "name", "source", "field", "current", "new", "note"])
        for change in changes:
            base = [ACTION_NAMES[change.action], change.endpoint_key, change.position or "", change.name,
                    change.source]
            if change.action == UPDATE:
                for field, old, new in field_changes(change):
                    note = "removed by the import" if new is None else ("added" if old is None else "")
                    writer.writerow(base + [field, cell(old), cell(new), note])
            elif change.action == CREATE:
                writer.writerow(base + ["(new object)", "", cell(without_markers(change.desired)), ""])
            elif change.action == SKIP:
                writer.writerow(base + ["", "", "", change.reason])


def fenced(language: str, lines: list) -> list:
    """A Markdown code block whose fence is longer than any backtick run inside it."""
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    fence = "`" * max(3, longest + 1)
    return [fence + language, *lines, fence]


def write_markdown(path: Path, changes, desired, target) -> None:
    counts = summary(changes)
    out = [f"# Import dry run: {target}", "", f"Generated {datetime.now():%Y-%m-%d %H:%M}", "",
           "| action | count |", "|---|---|"]
    out += [f"| {name} | {count} |" for name, count in counts.items()]
    plain = Palette(False)
    for title, action in (("Overwrite", UPDATE), ("Create", CREATE)):
        selected = [c for c in changes if c.action == action]
        if not selected:
            continue
        out += ["", f"## {title} ({len(selected)})"]
        for change in selected:
            out += ["", f"### {change.label}", "", f"From {change.source}", ""]
            if action == UPDATE:
                out += fenced("diff", diff_lines(change, plain))
            else:
                out += fenced("json", pretty(change.desired, without_markers))
    skipped = [c for c in changes if c.action == SKIP]
    if skipped:
        out += ["", f"## Skipped ({len(skipped)})", ""] + [f"- {c.label}: {c.reason}" for c in skipped]
    if desired.collisions:
        out += ["", "## Name collisions between sources", ""] + [f"- {line}" for line in desired.collisions]
    if desired.unknown_types:
        out += ["", "## Config types not imported (ignored)", ""] + [f"- {line}" for line in desired.unknown_types]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


HTML_STYLE = """
:root { --bg:#fff; --fg:#1d1d1f; --muted:#666; --line:#ddd; --add:#e6ffec; --del:#ffebe9; --hunk:#eef4ff; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181d; --fg:#e6e6e6; --muted:#9a9a9a; --line:#333; --add:#12361f; --del:#4a1c1c; --hunk:#1c2a44; }
}
body { background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; margin:24px; }
h1 { font-size:20px; } h2 { font-size:17px; margin-top:28px; }
table { border-collapse:collapse; } td, th { border:1px solid var(--line); padding:4px 10px; text-align:left; }
details { border:1px solid var(--line); border-radius:6px; margin:6px 0; padding:4px 10px; }
summary { cursor:pointer; font-weight:600; } .muted { color:var(--muted); font-weight:400; }
pre { margin:6px 0; overflow-x:auto; font:12px/1.4 ui-monospace, Consolas, monospace; }
pre span { display:block; white-space:pre; } .add { background:var(--add); } .del { background:var(--del); }
.hunk { background:var(--hunk); }
"""


def html_diff(lines: list) -> str:
    def css(line):
        if line.startswith("+") and not line.startswith("+++"):
            return "add"
        if line.startswith("-") and not line.startswith("---"):
            return "del"
        return "hunk" if line.startswith("@@") else ""
    return "<pre>" + "".join(f'<span class="{css(l)}">{html.escape(l)}</span>' for l in lines) + "</pre>"


def write_html(path: Path, changes, desired, target) -> None:
    counts = summary(changes)
    plain = Palette(False)
    esc = html.escape
    out = ["<!doctype html><html><head><meta charset='utf-8'>",
           "<meta name='viewport' content='width=device-width, initial-scale=1'>",
           f"<title>Import dry run</title><style>{HTML_STYLE}</style></head><body>",
           f"<h1>Import dry run: {esc(target)}</h1>",
           f"<p class='muted'>Generated {datetime.now():%Y-%m-%d %H:%M}</p><table>",
           "<tr><th>action</th><th>count</th></tr>"]
    out += [f"<tr><td>{name}</td><td>{count}</td></tr>" for name, count in counts.items()]
    out.append("</table>")
    for title, action in (("Overwrite", UPDATE), ("Create", CREATE)):
        selected = [c for c in changes if c.action == action]
        if not selected:
            continue
        out.append(f"<h2>{title} ({len(selected)})</h2>")
        for change in selected:
            fields = ", ".join(f for f, _, _ in field_changes(change)) if action == UPDATE else "new object"
            body = (html_diff(diff_lines(change, plain)) if action == UPDATE else
                    html_diff(["+ " + line for line in pretty(change.desired, without_markers)]))
            out.append(f"<details><summary>{esc(change.label)} <span class='muted'>{esc(fields)} "
                       f"&middot; from {esc(change.source)}</span></summary>{body}</details>")
    skipped = [c for c in changes if c.action == SKIP]
    if skipped:
        out.append(f"<h2>Skipped ({len(skipped)})</h2><ul>")
        out += [f"<li>{esc(c.label)}: {esc(c.reason)}</li>" for c in skipped]
        out.append("</ul>")
    for title, lines in (("Name collisions between sources", desired.collisions),
                         ("Config types not imported (ignored)", desired.unknown_types)):
        if lines:
            out.append(f"<h2>{title}</h2><ul>")
            out += [f"<li>{esc(line)}</li>" for line in lines]
            out.append("</ul>")
    out.append("</body></html>")
    path.write_text("\n".join(out), encoding="utf-8")
