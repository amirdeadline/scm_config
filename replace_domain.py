#!/usr/bin/env python3
"""Replace an Active Directory domain in the user and group IDs of a config_export.py file."""

import argparse
import copy
import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from scm_report import excel_safe

DESCRIPTION = "Replace an AD domain in user and group IDs inside a config_export.py JSON file."

# Server profiles point at the directory servers themselves (base DN, bind account).
# They change only when the servers change, so they are left alone unless asked.
SERVER_PROFILE_TYPES = frozenset({
    "identity/ldap-server-profiles", "identity/radius-server-profiles", "identity/tacacs-server-profiles",
    "identity/kerberos-server-profiles", "identity/saml-server-profiles", "identity/mfa-servers",
})
NETBIOS, UPN, DN, DOMAIN_FIELD = "NetBIOS", "UPN", "LDAP DN", "domain field"

EPILOG = r"""
What is replaced
  User and group IDs in every field of every object and rule (source_user,
  authentication profile allow lists, user-ID match lists, ...):
    NetBIOS form      OLDCORP\jsmith                 -> NEWCORP\jsmith
    UPN / e-mail      jsmith@oldcorp.local            -> jsmith@newcorp.local        (needs --old-dns/--new-dns)
    LDAP DN           cn=vpn,dc=oldcorp,dc=local      -> cn=vpn,dc=newcorp,dc=local  (needs --old-dns/--new-dns)
    domain field      "user_domain": "OLDCORP"        -> "NEWCORP"  (a field whose name contains "domain"
                                                                     and whose whole value is the domain)
  The domain is matched without regard to case. Left alone:
    - object and rule names (objects are matched by name on import)
    - FQDNs, URLs, UNC paths (\\oldcorp\share) and other text that only contains the name
    - a DN of a longer domain (dc=oldcorp,dc=local,dc=com)
    - server profiles (LDAP, RADIUS, TACACS+, Kerberos, SAML, MFA): their base DN
      and bind account point at the directory servers. Add --include-server-profiles
      to change them too.

  The file is read as JSON, so backslashes need no escaping: --old-domain OLDCORP.

Typical bulk change
  python config_export.py --folder "Mobile Users" --output mu.json
  python replace_domain.py --input mu.json --output mu_new.json --source "Mobile Users" ^
         --old-domain OLDCORP --new-domain NEWCORP --old-dns oldcorp.local --new-dns newcorp.local
  python config_import.py --input mu_new.json --folder "Mobile Users" --dry-run --report diff.html
  python config_import.py --input mu_new.json --folder "Mobile Users"

Every replaced value is listed on the console and, with --changes FILE, in a CSV
file, followed by a count per form. Check that each count is what you expect.
"""


@dataclass(frozen=True)
class DomainRule:
    old_domain: str
    new_domain: str
    old_dns: Optional[str] = None
    new_dns: Optional[str] = None

    def patterns(self) -> list:
        """[(form, compiled pattern, replacement)] for the in-text forms."""
        # Not after a word character, dot, dash or backslash: "xoldcorp\bob" and
        # the UNC path "\\oldcorp\share" are left alone.
        rules = [(NETBIOS, re.compile(rf"(?<![\w.\\-]){re.escape(self.old_domain)}\\", re.IGNORECASE),
                  self.new_domain + "\\")]
        if self.old_dns and self.new_dns:
            rules.append((UPN, re.compile(rf"@{re.escape(self.old_dns)}(?![\w.-])", re.IGNORECASE),
                          "@" + self.new_dns))
            old_dn = r"\s*,\s*".join(rf"dc\s*=\s*{re.escape(part)}" for part in self.old_dns.split("."))
            new_dn = ",".join(f"dc={part}" for part in self.new_dns.split("."))
            # Not followed by another dc= component: that would be a different, longer domain.
            rules.append((DN, re.compile(rf"(?<![\w-]){old_dn}(?![\w-])(?!\s*,\s*dc\s*=)", re.IGNORECASE),
                          new_dn))
        return rules


@dataclass(frozen=True)
class Replacement:
    container: str
    where: str
    old: str
    new: str
    kind: str


def child_path(path: str, key: str) -> str:
    # An object label ends with its quoted name: "... 'rule-1' / source_user[0]".
    return f"{path} / {key}" if path.endswith("'") else f"{path}.{key}"


class Replacer:
    def __init__(self, rule: DomainRule):
        self.rule = rule
        self.patterns = rule.patterns()
        self.found = []

    def string(self, value: str, path: str, key: str, container: str) -> str:
        if key and "domain" in key.lower() and value.strip().lower() == self.rule.old_domain.lower():
            self.found.append(Replacement(container, path, value, self.rule.new_domain, DOMAIN_FIELD))
            return self.rule.new_domain
        new, kinds = value, []
        for kind, pattern, replacement in self.patterns:
            new, count = pattern.subn(lambda _m, r=replacement: r, new)
            if count:
                kinds.append(kind)
        if kinds:
            self.found.append(Replacement(container, path, value, new, ", ".join(kinds)))
        return new

    def value(self, value, path: str, container: str, key: str = ""):
        """Return a copy of value with the replacements made."""
        if isinstance(value, dict):
            return {k: self.value(v, child_path(path, k), container, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, f"{path}[{i}]", container, key) for i, v in enumerate(value)]
        if isinstance(value, str):
            return self.string(value, path, key, container)
        return value

    def item(self, item, label: str, container: str):
        """An object or rule: everything except its own name, which is the match key on import."""
        if not isinstance(item, dict):
            return item
        return {k: v if k == "name" else self.value(v, child_path(label, k), container, k)
                for k, v in item.items()}

    def config(self, config: dict, container: str, include_server_profiles: bool) -> dict:
        result = {}
        for key, section in config.items():
            if key in SERVER_PROFILE_TYPES and not include_server_profiles:
                result[key] = section
                continue
            if isinstance(section, dict):  # rule types: {"pre": [...], "post": [...]}
                result[key] = {pos: [self.item(i, f"{container} / {key} [{pos}] '{name_of(i)}'", container)
                                     for i in items] for pos, items in section.items()}
            else:
                result[key] = [self.item(i, f"{container} / {key} '{name_of(i)}'", container) for i in section]
        return result


def name_of(item) -> str:
    return item.get("name", "") if isinstance(item, dict) else ""


def replace_in_document(document: dict, rule: DomainRule, sources: Optional[list] = None,
                        include_server_profiles: bool = False) -> tuple:
    """Return (new document, [Replacement]). The input document is not changed."""
    replacer = Replacer(rule)
    new_doc = copy.deepcopy(document)
    for name, folder in (new_doc.get("folders") or {}).items():
        if not sources or name in sources:
            folder["config"] = replacer.config(folder.get("config") or {}, f"folder '{name}'",
                                               include_server_profiles)
    for name, config in list((new_doc.get("snippets") or {}).items()):
        if not sources or name in sources:
            new_doc["snippets"][name] = replacer.config(config or {}, f"snippet '{name}'", include_server_profiles)
    return new_doc, replacer.found


# ----------------------------------------------------------------------- CLI
NETBIOS_FORBIDDEN = re.compile(r"[\\/@.,=\s]")
DNS_LABEL = re.compile(r"^[A-Za-z0-9-]+$")


def validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    for option, value in (("--old-domain", args.old_domain), ("--new-domain", args.new_domain)):
        if not value or NETBIOS_FORBIDDEN.search(value):
            parser.error(f"{option} must be a NetBIOS domain name such as OLDCORP "
                         f"(no spaces, backslash, @, dot, comma or =), got {value!r}")
    if args.old_domain.lower() == args.new_domain.lower():
        parser.error("--old-domain and --new-domain must differ")
    if bool(args.old_dns) != bool(args.new_dns):
        parser.error("--old-dns and --new-dns must be given together")
    for option, value in (("--old-dns", args.old_dns), ("--new-dns", args.new_dns)):
        if value is not None and not all(DNS_LABEL.match(label) for label in value.split(".")):
            parser.error(f"{option} must be a DNS domain such as oldcorp.local "
                         f"(no @, no leading or trailing dot), got {value!r}")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=DESCRIPTION, epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, metavar="JSON_FILE", help="file made by config_export.py")
    parser.add_argument("--output", metavar="JSON_FILE",
                        help="file to write (default: <input>_replaced.json); never the input file")
    parser.add_argument("--old-domain", required=True, help="old NetBIOS domain, e.g. OLDCORP")
    parser.add_argument("--new-domain", required=True, help="new NetBIOS domain, e.g. NEWCORP")
    parser.add_argument("--old-dns", help="old DNS domain for UPNs and DNs, e.g. oldcorp.local")
    parser.add_argument("--new-dns", help="new DNS domain for UPNs and DNs, e.g. newcorp.local")
    parser.add_argument("--source", action="append", default=[], metavar="NAME",
                        help="only change this folder or snippet of the file (repeatable; default: all)")
    parser.add_argument("--include-server-profiles", action="store_true",
                        help="also change LDAP/RADIUS/TACACS+/Kerberos/SAML/MFA server profiles")
    parser.add_argument("--changes", metavar="CSV_FILE", help="also save the list of replaced values as CSV")
    args = parser.parse_args(argv)
    validate(parser, args)
    return args


def write_changes_csv(path: Path, found: list) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["container", "location", "form", "old value", "new value"])
        writer.writerows([excel_safe(v) for v in (r.container, r.where, r.kind, r.old, r.new)] for r in found)


def form_counts(found: list, rule: DomainRule) -> str:
    counts = Counter(kind for r in found for kind in r.kind.split(", "))
    forms = [NETBIOS, UPN, DN, DOMAIN_FIELD] if rule.old_dns else [NETBIOS, DOMAIN_FIELD]
    return ", ".join(f"{form}: {counts.get(form, 0)}" for form in forms)


def main(argv=None) -> int:
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    source = Path(args.input)
    output = Path(args.output) if args.output else source.with_name(source.stem + "_replaced.json")
    if output.resolve() == source.resolve():
        print("ERROR: --output must be a different file from --input, so the export stays as a backup.")
        return 2
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
        names = set(document.get("folders") or {}) | set(document.get("snippets") or {})
    except (OSError, ValueError, AttributeError) as exc:
        print(f"ERROR: cannot read {source}: {exc}")
        return 2
    missing = [s for s in args.source if s not in names]
    if missing:
        print(f"ERROR: --source {', '.join(missing)} not in the file. It contains: {', '.join(sorted(names))}.")
        return 2

    rule = DomainRule(args.old_domain, args.new_domain, args.old_dns, args.new_dns)
    new_doc, found = replace_in_document(document, rule, args.source or None, args.include_server_profiles)
    for item in found:
        print(f"  {item.where}\n      {item.old}  ->  {item.new}")
    output.write_text(json.dumps(new_doc, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.changes:
        write_changes_csv(Path(args.changes), found)
    objects = len({r.where.rpartition("' / ")[0] for r in found})
    print(f"\n{len(found)} value(s) changed in {objects} object(s). Per form: {form_counts(found, rule)}")
    print(f"Written to {output.resolve()}")
    if not found:
        print("Nothing matched. Check the domain names; the file was written unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
