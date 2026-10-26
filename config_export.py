#!/usr/bin/env python3
"""Export custom Strata Cloud Manager configuration of folders and snippets to one JSON file."""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from scm_catalog import (ENDPOINTS, BUILTIN_FOLDERS, Endpoint, is_builtin_snippet, read_owned,
                         strip_server_fields)
from scm_client import SETTINGS_FILE, ApiError, ScmClient, SettingsError, setup_logging

EXPORT_FORMAT_VERSION = 2
# Status codes meaning "this config type does not exist in this folder/snippet".
NOT_APPLICABLE_STATUSES = {400, 403, 404}
ENDPOINT_WORKERS = 8  # config types read at the same time (HTTP calls are capped in ScmClient)

log = logging.getLogger("config_export")

DESCRIPTION = "Export custom Strata Cloud Manager (SCM) configuration to a JSON file."

EPILOG = f"""
What is exported
  Every configuration type that SCM snippets support: objects (addresses, groups,
  services, applications, tags, EDLs, HIP, ...), security profiles and profile
  groups, security / decryption / NAT / QoS / app-override / authentication rules
  (pre and post), identity (auth profiles, server profiles, local users,
  certificate profiles), network (zones, interfaces, IKE/IPsec, routing profiles,
  logical routers), device settings and variables.

  Only custom configuration is exported. Built-in content (App-IDs, predefined
  URL categories, predefined profiles, default snippets) is left out. A built-in
  object that you edited or overrode is saved by SCM inside your folder or snippet,
  so it is exported. Items a folder inherits from its parent folders are not
  repeated in the child; each folder and snippet holds only what is defined in it.
  Prisma Access-only settings (remote networks, service connections,
  mobile-agent settings) are out of scope.

What gets selected
  no --folder / --snippet   all folders, plus every custom snippet attached to a folder
  --folder NAME             that folder, plus every custom snippet attached to it
  --snippet NAME            that snippet only
  Both options can be repeated and combined.

Output file layout
  {{
    "metadata": {{ export time, TSG ID, selection }},
    "folders": {{
      "All":          {{ "parent_folder": null,   "attached_snippets": {{"1": "corp"}}, "config": {{...}} }},
      "Shared":       {{ "parent_folder": "All",  "attached_snippets": {{}},            "config": {{...}} }},
      "Mobile Users": {{ "parent_folder": "Shared", ... }}
    }},
    "snippets": {{
      "corp": {{ "<config type>": [ items ], ... }}
    }}
  }}
  Folders are listed in SCM hierarchy order (parents before children) and point
  to their parent with "parent_folder". "attached_snippets" lists the snippets
  attached to the folder in SCM order; the content of each custom snippet is under
  "snippets". An object with the same name in several folders/snippets appears
  under each of them. Config types are named "<category>/<type>", for example
  "objects/addresses" or "security/security-rules"; rule types hold
  {{"pre": [...], "post": [...]}} in rulebase order.

How to find folder and snippet names
  1. Run:  python config_export.py --list
     It prints the folder tree with the snippets attached to each folder.
  2. Or in the SCM web UI: Manage > Configuration > NGFW and Prisma Access, then
     open the folder selector at the top (folder names as shown there, e.g.
     "Mobile Users", "Remote Networks", "Shared"), and
     Manage > Configuration > NGFW and Prisma Access > Snippets for snippet names.
  Names are case-sensitive. Quote names that contain spaces: --folder "Mobile Users"

Credentials
  The SCM service account (CLIENT_ID, CLIENT_SECRET) and TSG_ID are read from
  {SETTINGS_FILE}

Examples
  python config_export.py
  python config_export.py --list
  python config_export.py --folder "Mobile Users" --output gp.json
  python config_export.py --snippet "Corp-Objects" --output corp.json
  python config_export.py --folder "Remote Networks" --snippet "Branch-Std"
"""


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=DESCRIPTION, epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", action="append", default=[], metavar="FOLDER_NAME",
                        help="export this folder and its attached snippets (repeatable)")
    parser.add_argument("--snippet", action="append", default=[], metavar="SNIPPET_NAME",
                        help="export this snippet (repeatable)")
    parser.add_argument("--output", metavar="JSON_FILE",
                        help="output JSON file path (default: scm_export_<TSG>_<timestamp>.json)")
    parser.add_argument("--list", action="store_true",
                        help="list folder and snippet names, then exit")
    parser.add_argument("--verbose", action="store_true", help="show debug output on the console")
    return parser.parse_args(argv)


# ------------------------------------------------------------------ selection
def custom_folders(folders: list) -> list:
    return [f for f in folders if f.get("name") not in BUILTIN_FOLDERS]


def custom_snippets(snippets: list, shared_in: frozenset = frozenset()) -> dict:
    return {s["name"]: s for s in snippets if not is_builtin_snippet(s, shared_in)}


def select_containers(folders: list, snippets: list, want_folders: list,
                      want_snippets: list, shared_in: frozenset = frozenset()) -> tuple:
    """Resolve the CLI selection into (folder names, snippet names), in a stable order."""
    folder_map = {f["name"]: f for f in custom_folders(folders)}
    snippet_map = custom_snippets(snippets, shared_in)
    all_snippet_names = {s["name"] for s in snippets}

    unknown = [n for n in want_folders if n not in folder_map]
    if unknown:
        raise ValueError(f"Folder(s) not found: {', '.join(unknown)}. "
                         f"Available folders: {', '.join(sorted(folder_map))}. "
                         f"Run with --list to see the folder tree.")
    builtin = [n for n in want_snippets if n in all_snippet_names and n not in snippet_map]
    if builtin:
        raise ValueError(f"Snippet(s) {', '.join(builtin)} hold built-in content only and are "
                         f"not exported. Edits to built-in objects are saved in your folders.")
    unknown = [n for n in want_snippets if n not in snippet_map]
    if unknown:
        raise ValueError(f"Snippet(s) not found: {', '.join(unknown)}. "
                         f"Available snippets: {', '.join(sorted(snippet_map))}. "
                         f"Run with --list to see them.")

    if not want_folders and not want_snippets:
        folder_names = list(folder_map)
    else:
        folder_names = list(dict.fromkeys(want_folders))

    snippet_names = []
    for name in folder_names:
        snippet_names.extend(s for s in folder_map[name].get("snippets") or [] if s in snippet_map)
    snippet_names.extend(want_snippets)
    return folder_names, list(dict.fromkeys(snippet_names))


def print_containers(folders: list, snippets: list, shared_in: frozenset = frozenset()) -> None:
    """Print the folder tree with attached snippets, then all custom snippets."""
    folder_list = custom_folders(folders)
    by_parent = {}
    for folder in folder_list:
        by_parent.setdefault(folder.get("parent") or "", []).append(folder)
    names = {f["name"] for f in folder_list}
    roots = [f for f in folder_list if (f.get("parent") or "") not in names]

    print("Folders (use with --folder):")

    def walk(folder: dict, depth: int) -> None:
        attached = ", ".join(folder.get("snippets") or [])
        suffix = f"   [snippets: {attached}]" if attached else ""
        print(f"  {'    ' * depth}{folder['name']}{suffix}")
        for child in by_parent.get(folder["name"], []):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)

    print("\nCustom snippets (use with --snippet):")
    snippet_map = custom_snippets(snippets, shared_in)
    if not snippet_map:
        print("  (none)")
    for name in sorted(snippet_map):
        description = snippet_map[name].get("description") or ""
        shared = "  (shared from another tenant, read-only here)" if name in shared_in else ""
        print(f"  {name}" + (f"  - {description}" if description else "") + shared)


# --------------------------------------------------------------------- export
def export_container(client: ScmClient, kind: str, name: str,
                     endpoints: Iterable[Endpoint] = ENDPOINTS) -> tuple:
    """Return (config dict, list of endpoint keys that failed unexpectedly).

    Config types are read in parallel; the result keeps the catalog order.
    """
    endpoints = list(endpoints)
    with ThreadPoolExecutor(max_workers=ENDPOINT_WORKERS) as pool:
        results = list(pool.map(lambda ep: read_endpoint(client, ep, kind, name), endpoints))
    config = {}
    failed = []
    unavailable = []
    for endpoint, (value, error) in zip(endpoints, results):
        if error is not None:
            if error.status in NOT_APPLICABLE_STATUSES:
                log.debug("%s '%s': %s not available (%s)", kind, name, endpoint.key, error.message)
            elif error.is_empty_server_error:
                log.debug("%s '%s': %s answered an empty HTTP 500", kind, name, endpoint.key)
                unavailable.append(endpoint.key)
            else:
                log.warning("  ! %s: %s", endpoint.key, error.message)
                failed.append(endpoint.key)
        elif value:
            config[endpoint.key] = value
    if unavailable:
        log.info("  not available in this %s (SCM returned an empty HTTP 500): %s",
                 kind, ", ".join(unavailable))
    return config, failed


def read_endpoint(client: ScmClient, endpoint: Endpoint, kind: str, name: str) -> tuple:
    """(items or {pos: items}, None) or (None, ApiError) for one config type."""
    try:
        owned = read_owned(client, endpoint, kind, name)
    except ApiError as exc:
        return None, exc
    if endpoint.rules:
        return {pos: [strip_server_fields(i) for i in items]
                for pos, items in owned.items() if items}, None
    return [strip_server_fields(i) for i in owned], None


def count_items(config: dict) -> int:
    total = 0
    for value in config.values():
        total += sum(len(v) for v in value.values()) if isinstance(value, dict) else len(value)
    return total


def hierarchy_order(folders: list) -> list:
    """Folder names depth-first from the root, parents before children, as in SCM."""
    names = [f["name"] for f in folders]
    children = {}
    for folder in folders:
        children.setdefault(folder.get("parent") or "", []).append(folder["name"])
    ordered = []

    def walk(name: str) -> None:
        ordered.append(name)
        for child in children.get(name, []):
            walk(child)

    for folder in folders:
        if (folder.get("parent") or "") not in names:
            walk(folder["name"])
    return ordered + [n for n in names if n not in ordered]  # parent loops, just in case


def folder_entry(folder: dict, config: dict) -> dict:
    attached = folder.get("snippets") or []
    return {
        "parent_folder": folder.get("parent") or None,
        "attached_snippets": {str(i): name for i, name in enumerate(attached, start=1)},
        "config": config,
    }


def build_export(client: ScmClient, folders: list, snippets: list, folder_names: list,
                 snippet_names: list, selection: dict) -> tuple:
    folder_map = {f["name"]: f for f in folders}
    document = {
        "metadata": {
            "format_version": EXPORT_FORMAT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tsg_id": client.tsg_id,
            "selection": selection,
        },
        "folders": {},
        "snippets": {},
    }
    failures = {}
    wanted = set(folder_names)
    for name in (n for n in hierarchy_order(folders) if n in wanted):
        config = export_one(client, "folder", name, failures)
        document["folders"][name] = folder_entry(folder_map[name], config)
    for name in snippet_names:
        document["snippets"][name] = export_one(client, "snippet", name, failures)
    return document, failures


def export_one(client: ScmClient, kind: str, name: str, failures: dict) -> dict:
    log.info("Exporting %s '%s' ...", kind, name)
    config, failed = export_container(client, kind, name)
    log.info("  %d item(s) in %d config type(s)", count_items(config), len(config))
    if failed:
        failures[f"{kind} {name}"] = failed
    return config


def default_output(tsg_id: str) -> Path:
    return Path(f"scm_export_{tsg_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)


def main(argv=None) -> int:
    args = parse_args(argv)
    log_file = setup_logging("config_export", args.verbose)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # non-ASCII names on a cp1252 console
    try:
        client = ScmClient.from_settings()
        folders = client.list_folders()
        snippets = client.list_snippets()
        shared_in = client.shared_in_snippet_names()
    except SettingsError as exc:
        log.error("ERROR: %s", exc)
        return 2
    except (ApiError, ConnectionError) as exc:
        log.error("ERROR: %s", exc)
        return 1

    if args.list:
        print_containers(folders, snippets, shared_in)
        return 0

    try:
        folder_names, snippet_names = select_containers(folders, snippets, args.folder, args.snippet,
                                                        shared_in)
    except ValueError as exc:
        log.error("ERROR: %s", exc)
        return 2

    selection = {"folders": args.folder, "snippets": args.snippet,
                 "mode": "filtered" if args.folder or args.snippet else "full"}
    try:
        document, failures = build_export(client, folders, snippets, folder_names, snippet_names,
                                          selection)
    except (ApiError, ConnectionError) as exc:
        log.error("ERROR: export stopped: %s", exc)
        return 1

    output = Path(args.output) if args.output else default_output(client.tsg_id)
    try:
        write_json(output, document)
    except OSError as exc:
        log.error("ERROR: could not write %s: %s", output, exc)
        return 1

    total = (sum(count_items(f["config"]) for f in document["folders"].values())
             + sum(count_items(c) for c in document["snippets"].values()))
    log.info("\nExported %d item(s) from %d folder(s) and %d snippet(s) to %s",
             total, len(folder_names), len(snippet_names), output.resolve())
    if failures:
        log.warning("Some config types could not be read (see %s):", log_file)
        for container, keys in failures.items():
            log.warning("  %s: %s", container, ", ".join(keys))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
