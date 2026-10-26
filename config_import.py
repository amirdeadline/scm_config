#!/usr/bin/env python3
"""Import a config_export.py JSON file into a Strata Cloud Manager snippet or folder."""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from scm_catalog import (BUILTIN_FOLDERS, ENDPOINTS_BY_KEY, RULE_POSITIONS, is_builtin_snippet,
                         match_key, read_owned, strip_server_fields)
from scm_client import LOG_DIR, SETTINGS_FILE, ApiError, ScmClient, SettingsError, setup_logging
from scm_import_plan import (CREATE, UNREADABLE, UPDATE, Palette, list_sources, load_export,
                             merge_sources, plan_changes, render_report, select_sources)
from scm_report import report_format, write_report

NOT_APPLICABLE_STATUSES = {400, 403, 404}
MAX_PASSES = 3  # objects that refer to objects created later are retried

log = logging.getLogger("config_import")

DESCRIPTION = "Import an SCM configuration JSON file (made by config_export.py) into a snippet or folder."

EPILOG = f"""
Target
  --snippet NAME   import into this snippet; it is created if it does not exist
  --folder NAME    import into this existing folder
  neither          a new snippet named NEW_<YYYYMMDD_HHMMSS> is created
  Use only one of --snippet / --folder.

How it works
  1. Dry run: the file is compared with the current config of the target and a
     full report is printed: every object to create, and a diff (current SCM vs
     import file) for every existing object that will be OVERWRITTEN. Objects
     are matched by config type and name; identical objects are left alone.
  2. You confirm by typing YES. Nothing is changed before that.
  3. Objects are created/updated in dependency order (tags and addresses first,
     rules last). Rules keep their pre/post rulebase; each new rule is moved
     next to its neighbour from the file. Existing rules are never moved.
  Objects in the target that are not in the file are never deleted.
  Changes go to the SCM candidate configuration only. Review them in SCM and
  push the config from there.

Which part of the file is imported
  --source NAME    only these folders/snippets from the file (repeatable)
  target name matches a folder (for --folder) or snippet (for --snippet) in
                   the file: only that one is imported. This is the round trip:
                   export a folder, edit the JSON, import it back into the same
                   folder. The folder's attached snippets in the file are not
                   copied into it.
  otherwise        all folders and snippets in the file are merged into the
                   target. If the same name exists in two of them, the dry run
                   lists the collision and the later one wins.

Bulk change: new AD domain for users and groups
  python config_export.py --folder "Mobile Users" --output mu.json
  python replace_domain.py --input mu.json --output mu_new.json --source "Mobile Users" ^
         --old-domain OLDCORP --new-domain NEWCORP --old-dns oldcorp.local --new-dns newcorp.local
  python config_import.py --input mu_new.json --folder "Mobile Users" --dry-run --report diff.html
  python config_import.py --input mu_new.json --folder "Mobile Users"
  Only objects that really changed are listed and overwritten; new objects and
  rules in the file are created; nothing is deleted and existing rules keep their
  position. You can also edit the JSON by hand. Do not change object or rule
  names: a renamed object is created as a new one and the old one stays.

Report
  The full dry-run report is printed and saved with --report FILE. The file
  extension picks the format, and --report can be repeated:
    .txt   plain text, as printed            .md    Markdown with diff blocks
    .html  page with a diff per object       .json  every change with field-level
    .csv   one row per changed field                current/new values
  Default: logs/config_import_report_<timestamp>.txt. Overwrites show a full
  diff (current SCM vs import file). An object is replaced as a whole, so a field
  that is in SCM but not in the file is removed; the diff shows it.

Not imported
  Certificates (need their private key) and a few settings objects with no
  update API are listed as skipped. Secrets (pre-shared keys, passwords) are
  sent as exported; SCM may reject them, which is reported at the end.

Credentials
  The SCM service account (CLIENT_ID, CLIENT_SECRET) and TSG_ID are read from
  {SETTINGS_FILE}

Examples
  python config_import.py --input gp.json
  python config_import.py --input gp.json --snippet "GP-Copy"
  python config_import.py --input full.json --folder "Lab" --source "Mobile Users"
  python config_import.py --input gp.json --snippet "GP-Copy" --dry-run --report diff.txt
  python config_import.py --input mu.json --folder "Mobile Users" --dry-run --report d.html --report d.csv
"""


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=DESCRIPTION, epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, metavar="JSON_FILE",
                        help="JSON file created by config_export.py")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--folder", metavar="FOLDER_NAME", help="import into this existing folder")
    target.add_argument("--snippet", metavar="SNIPPET_NAME",
                        help="import into this snippet (created if missing)")
    parser.add_argument("--source", action="append", default=[], metavar="NAME",
                        help="only import this folder/snippet from the file (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the report and exit without asking or changing anything")
    parser.add_argument("--report", action="append", default=[], metavar="FILE",
                        help="save the dry-run report to FILE; the extension picks the format: "
                             ".txt .md .html .json .csv (repeatable; "
                             "default: logs/config_import_report_<timestamp>.txt)")
    parser.add_argument("--no-color", action="store_true", help="plain text report")
    parser.add_argument("--verbose", action="store_true", help="show debug output on the console")
    return parser.parse_args(argv)


# --------------------------------------------------------------------- target
def resolve_target(client: ScmClient, folder: str, snippet: str) -> tuple:
    """Return (kind, name, exists)."""
    if folder:
        folders = {f["name"]: f for f in client.list_folders()}
        if folder in BUILTIN_FOLDERS or folder not in folders:
            names = ", ".join(sorted(n for n in folders if n not in BUILTIN_FOLDERS))
            raise ValueError(f"Folder '{folder}' not found. Available folders: {names}")
        return "folder", folder, True
    name = snippet or f"NEW_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    existing = {s["name"]: s for s in client.list_snippets()}
    if name in existing and is_builtin_snippet(existing[name]):
        raise ValueError(f"Snippet '{name}' is built-in or read-only (shared from another tenant) "
                         f"and cannot be changed. Choose another --snippet.")
    return "snippet", name, name in existing


def read_current(client: ScmClient, kind: str, name: str, keys: set) -> dict:
    """Current custom items of the target for every config type in the file.

    Returns (endpoint key, position) -> {match key: item}. For rule types both
    rulebases are read, so a name clash with the other rulebase can be shown.
    """
    current = {UNREADABLE: {}}
    for key in sorted({k for k, _ in keys}):
        endpoint = ENDPOINTS_BY_KEY[key]
        if not endpoint.importable:
            continue
        try:
            owned = read_owned(client, endpoint, kind, name)
        except ApiError as exc:
            if exc.status not in NOT_APPLICABLE_STATUSES and not exc.is_empty_server_error:
                raise
            log.warning("  ! %s cannot be read from %s '%s' (HTTP %s); its items are skipped",
                        key, kind, name, exc.status)
            current[UNREADABLE][key] = f"HTTP {exc.status}"
            continue
        groups = owned.items() if endpoint.rules else [(None, owned)]
        for position, items in groups:
            current[(key, position)] = {match_key(i, endpoint.key_field): i for i in items}
    return current


# ---------------------------------------------------------------------- apply
def request_body(change, kind: str, name: str) -> dict:
    return {**strip_server_fields(change.desired), kind: name}


def apply_change(client: ScmClient, change, kind: str, name: str) -> None:
    endpoint = ENDPOINTS_BY_KEY[change.endpoint_key]
    body = request_body(change, kind, name)
    if change.action == CREATE:
        params = {"position": change.position} if change.position else None
        client.create(endpoint.path, body, params=params)
    else:
        # Send the rulebase only when SCM reported it for this rule; otherwise SCM keeps it as is.
        known = (change.current or {}).get("position") in RULE_POSITIONS
        client.update(endpoint.path, change.current["id"], body,
                      params={"position": change.current["position"]} if known else None)


def may_have_been_applied(change, exc) -> bool:
    """A create that timed out or got a 5xx may still have been saved by SCM."""
    if change.action != CREATE:
        return False
    return not isinstance(exc, ApiError) or exc.status >= 500


def exists_in_target(client: ScmClient, change, kind: str, name: str) -> bool:
    endpoint = ENDPOINTS_BY_KEY[change.endpoint_key]
    owned = read_owned(client, endpoint, kind, name)
    items = [i for pos in RULE_POSITIONS for i in owned[pos]] if endpoint.rules else owned
    return any(match_key(i, endpoint.key_field) == change.name for i in items)


def apply_changes(client: ScmClient, changes: list, kind: str, name: str) -> tuple:
    """Apply creates/updates; retry failures so references to later objects resolve.

    A create that may have been saved despite an error is looked up before it is
    sent again, so it is never created twice.
    """
    pending = [c for c in changes if c.action in (CREATE, UPDATE)]
    total = len(pending)
    done = []
    errors = {}
    for attempt in range(1, MAX_PASSES + 1):
        failed = []
        for change in pending:
            try:
                apply_change(client, change, kind, name)
            except (ApiError, ConnectionError) as exc:
                errors[change.label] = exc.message if isinstance(exc, ApiError) else str(exc)
                log.debug("  pass %d failed %s: %s", attempt, change.label, errors[change.label])
                if not (may_have_been_applied(change, exc) and saved_anyway(client, change, kind, name)):
                    failed.append(change)
                    continue
            done.append(change)
            errors.pop(change.label, None)
            log.info("  [%d/%d] %s %s", len(done), total,
                     "created" if change.action == CREATE else "updated", change.label)
        if not failed or len(failed) == len(pending):
            pending = failed
            break
        log.info("  retrying %d item(s) that may depend on objects created later ...", len(failed))
        pending = failed
    return done, [(c, errors[c.label]) for c in pending]


def saved_anyway(client: ScmClient, change, kind: str, name: str) -> bool:
    try:
        found = exists_in_target(client, change, kind, name)
    except (ApiError, ConnectionError) as exc:
        log.debug("  could not check whether %s was saved: %s", change.label, exc)
        return False
    if found:
        log.info("  note: SCM reported an error for %s, but the object was saved", change.label)
    return found


def place_rule(client: ScmClient, path: str, ids: dict, order: list, rule: str, position: str,
               destination: str, anchor: str) -> None:
    """Move one rule before/after an anchor rule, unless it is already there."""
    offset = 1 if destination == "after" else -1
    if order.index(rule) == order.index(anchor) + offset:
        return
    client.move_rule(path, ids[rule], position, destination, ids[anchor])
    order.remove(rule)
    order.insert(order.index(anchor) + (1 if destination == "after" else 0), rule)


def restore_rule_order(client: ScmClient, desired, done: list, kind: str, name: str) -> list:
    """Place each rule created in this run next to its neighbours from the file.

    SCM adds new rules at the bottom of the rulebase. Each new rule is moved
    after the nearest earlier rule from the file, or, if it has none, before the
    nearest later rule that already existed. Rules that existed before the import
    are never moved. A rule that cannot be moved is reported and the others are
    still placed. Returns warnings.
    """
    warnings = []
    created = {}
    for change in done:
        if change.action == CREATE and change.position:
            created.setdefault((change.endpoint_key, change.position), set()).add(change.name)
    for (key, position), new_names in created.items():
        endpoint = ENDPOINTS_BY_KEY[key]
        bucket = desired.items[(key, position)]
        if len(bucket) < 2:
            continue
        if not endpoint.movable:
            warnings.append(f"{key} [{position}]: SCM has no move API; new rules were added at the "
                            f"bottom, check their order in SCM.")
            continue
        try:
            # Both rulebases: where SCM does not report pre/post, every rule is listed under pre.
            owned = read_owned(client, endpoint, kind, name)
        except (ApiError, ConnectionError) as exc:
            warnings.append(f"{key} [{position}]: rule order not restored: {exc}")
            continue
        rules = [r for pos in RULE_POSITIONS for r in owned[pos] if r.get("id") and r.get("name")]
        warnings += place_new_rules(client, endpoint, position, list(bucket), new_names, rules)
    return warnings


def place_new_rules(client: ScmClient, endpoint, position: str, file_order: list, new_names: set,
                    rules: list) -> list:
    ids = {r["name"]: r["id"] for r in rules}
    order = [r["name"] for r in rules]
    wanted = [n for n in file_order if n in ids]
    warnings = []
    for index, rule in enumerate(wanted):
        if rule not in new_names:
            continue
        if index > 0:
            destination, anchor = "after", wanted[index - 1]
        else:
            destination = "before"
            anchor = next((w for w in wanted[1:] if w not in new_names), None)
            if anchor is None:
                continue
        try:
            place_rule(client, endpoint.path, ids, order, rule, position, destination, anchor)
        except (ApiError, ConnectionError) as exc:
            warnings.append(f"{endpoint.key} [{position}] '{rule}': could not be moved {destination} "
                            f"'{anchor}', check its position in SCM: {exc}")
    return warnings


def pick_sources(sources: list, wanted: list, kind: str, name: str) -> list:
    """Choose which folders/snippets of the file to import into the target."""
    if wanted:
        return select_sources(sources, wanted)
    same = [s for s in sources if s[0] == kind and s[1] == name]
    if same and len(sources) > 1:
        log.info("The file has %s '%s' (same as the target): importing only that one. "
                 "Use --source to choose other parts of the file.", kind, name)
        return same
    return sources


def default_report_file() -> Path:
    return LOG_DIR / f"config_import_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"


def confirm(changes: list, target_label: str) -> bool:
    creates = sum(1 for c in changes if c.action == CREATE)
    updates = sum(1 for c in changes if c.action == UPDATE)
    print()
    if updates:
        print(f"WARNING: {updates} existing object(s) in {target_label} will be OVERWRITTEN "
              f"with the content shown above.")
    print(f"About to create {creates} and overwrite {updates} object(s) in {target_label} "
          f"(candidate config only).")
    try:
        answer = input("Type YES to apply these changes: ")
    except EOFError:
        return False
    # Windows PowerShell 5.1 prefixes piped input with a byte-order mark.
    return answer.lstrip(chr(0xFEFF)).strip() == "YES"


def main(argv=None) -> int:
    args = parse_args(argv)
    log_file = setup_logging("config_import", args.verbose)
    if os.name == "nt":
        os.system("")  # enable ANSI colors in the Windows console
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # non-ASCII names on a cp1252 console
    colors = Palette(enabled=not args.no_color and sys.stdout.isatty())

    try:
        document = load_export(args.input)
        all_sources = list_sources(document)
        select_sources(all_sources, args.source)  # fail fast on a wrong --source
    except (OSError, ValueError) as exc:
        log.error("ERROR: cannot use input file: %s", exc)
        return 2

    try:
        client = ScmClient.from_settings()
        kind, name, exists = resolve_target(client, args.folder, args.snippet)
        sources = pick_sources(all_sources, args.source, kind, name)
        desired = merge_sources(sources)
        if not desired.items:
            log.error("ERROR: the selected part of the input file has no importable configuration.")
            return 2
        target_label = f"{kind} '{name}'" + ("" if exists else " (new, will be created)")
        log.info("Reading current configuration of %s ...", target_label)
        current = read_current(client, kind, name, set(desired.items)) if exists else {}
    except SettingsError as exc:
        log.error("ERROR: %s", exc)
        return 2
    except ValueError as exc:
        log.error("ERROR: %s", exc)
        return 2
    except (ApiError, ConnectionError) as exc:
        log.error("ERROR: %s", exc)
        return 1

    changes = plan_changes(desired, current)
    print(render_report(changes, desired, target_label, colors))
    sys.stdout.flush()  # keep the report above the log lines when output is piped
    for report_file in [Path(p) for p in args.report] or [default_report_file()]:
        try:
            write_report(report_file, changes, desired, target_label)
            log.info("\nFull report (%s) saved to %s", report_format(report_file), report_file.resolve())
        except OSError as exc:
            log.warning("Could not save the report to %s: %s", report_file, exc)

    to_apply = [c for c in changes if c.action in (CREATE, UPDATE)]
    if not to_apply:
        log.info("\nNothing to change: the target already matches the file.")
        return 0
    if args.dry_run:
        log.info("\nDry run only, nothing was changed.")
        return 0
    if not confirm(changes, target_label):
        log.info("Cancelled, nothing was changed.")
        return 0

    try:
        if not exists:
            client.create_snippet(name, f"Imported from {Path(args.input).name}")
            log.info("Created snippet '%s'", name)
        done, failed = apply_changes(client, changes, kind, name)
        warnings = restore_rule_order(client, desired, done, kind, name)
    except (ApiError, ConnectionError, SettingsError) as exc:
        log.error("ERROR: import stopped: %s", exc)
        return 1

    log.info("\nApplied %d of %d change(s) to %s.", len(done), len(to_apply), f"{kind} '{name}'")
    for warning in warnings:
        log.warning("  note: %s", warning)
    if failed:
        log.error("%d item(s) failed (details in %s):", len(failed), log_file)
        for change, message in failed:
            log.error("  %s: %s", change.label, message)
    log.info("Changes are in the SCM candidate config. Review and push them in Strata Cloud Manager.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
