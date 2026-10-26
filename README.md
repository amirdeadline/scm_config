# SCM Config Export / Import

Command-line tools for Palo Alto Networks Strata Cloud Manager (SCM):

| Script | What it does |
|---|---|
| `config_export.py` | Exports the custom configuration of all folders and snippets, or only the ones you name, to one JSON file. |
| `replace_domain.py` | Replaces an old AD domain with a new one in the user and group IDs of an export file. Works offline. |
| `config_import.py` | Imports that JSON file into a snippet (default) or a folder. It shows a full dry-run diff first, which you can save as text, Markdown, HTML, JSON or CSV. It changes nothing until you type `YES`. |

The export and import scripts read the SCM service account from [settings.py](settings.py).
Testers: start with [TEST_PLAN.md](TEST_PLAN.md).

## Setup

1. Python 3.9 or later, then install the one dependency:

   ```bash
   pip install -r requirements.txt
   ```

2. Edit [settings.py](settings.py) and set `CLIENT_ID`, `CLIENT_SECRET` and `TSG_ID`.
   The file explains where to find each value. If a value is missing or SCM rejects
   it, both scripts stop and tell you to edit this file.

   Service account role: View Only Administrator is enough for export. Import
   needs Superuser or Network Administrator.

## Export

```bash
python config_export.py                                   # all folders + snippets attached to them
python config_export.py --list                            # show folder tree and snippet names
python config_export.py --folder "Mobile Users" --output gp.json
python config_export.py --snippet "Corp-Objects" --output corp.json
python config_export.py --folder "Remote Networks" --snippet "Branch-Std"
```

| Option | Meaning |
|---|---|
| `--folder NAME` | Export this folder and everything it inherits: its parent folders up to `All`, and the custom snippets attached to any of them. Repeatable. |
| `--no-inherited` | With `--folder`: export only that folder and its own custom snippets. |
| `--snippet NAME` | Export this snippet. Repeatable. |
| `--output FILE` | Output JSON file. Default `scm_export_<TSG>_<timestamp>.json`. |
| `--list` | Print folder and snippet names, then exit. |

With no `--folder` or `--snippet`, every folder is exported, plus every custom
snippet attached to a folder.

### Finding folder and snippet names

- Run `python config_export.py --list`.
- Or in SCM: **Manage > Configuration > NGFW and Prisma Access**. The folder
  selector at the top shows folder names. The **Snippets** page shows snippet names.

Names are case-sensitive. Quote names that contain spaces.

### What is exported

Every config type that snippets support, about 120 API endpoints:
- objects
- security profiles
- security, decryption, NAT, QoS, PBF, SD-WAN, app-override and authentication rules (pre/post)
- identity
- network
- device settings
- variables

The full list, in import order, is in [scm_catalog.py](scm_catalog.py).

- Only custom config is exported. App-IDs, predefined URL categories, predefined
  profiles and built-in snippets are left out.
- A built-in object you edited or overrode is stored by SCM in your folder or
  snippet, so it is exported. SCM marks it with `override_loc` and `override_type`
  (for example the colour of the built-in tag `Sanctioned`).
- **A folder export includes what the folder inherits.** For example
  `--folder "Mobile Users"` also exports `Mobile Users Container`, `Prisma Access`
  and `All`, and the custom snippets attached to them. Each one keeps its own key,
  so every item stays under the folder or snippet it is defined in, and nothing is
  copied into the child. `metadata.selection.inherited_from` lists what was added.
  Use `--no-inherited` to export the folder alone.
- Built-in snippets (type `predefined`, such as `DNS-Best-Practice`, `office365`,
  `GlobalProtect-Default`) are named in `attached_snippets`, but their content is
  Palo Alto Networks content and is not exported.
- Prisma Access-only settings (remote networks, service connections, mobile-agent
  settings) are out of scope.
- **Snippet content is not copied into folders.** An object SCM returns for a
  folder that carries a snippet name belongs to that snippet. An example is the
  vendor IKE/IPsec crypto profiles from the built-in `default` snippet.
- **Auto-created match lists are kept.** Prisma Access creates log-forwarding match
  lists such as `gp-gpcs-default` and `userid-gpcs-default` in its folders. They
  are editable folder config, so they are exported.
- **Placeholder rows are skipped.** A folder's rule lists contain one row per
  attached snippet (name only), marking where the snippet's rules go.
- **Shared-in snippets count as custom.** A snippet shared into this tenant from
  another tenant is read-only here, but it is your content, so it is exported.
  `--list` marks these snippets. They cannot be an import target.
- **Some rules are exported under `pre` because SCM hides their rulebase.** For
  snippets and for Prisma Access folders such as `Mobile Users`, the API ignores
  the pre/post filter and gives the rules no position. The list order doesn't
  follow the rulebase either. Those rules go under `pre`, and the export prints a
  note. Folders such as `All` report pre and post correctly. Check the rulebase in
  the SCM UI if it matters.
- **The `Prisma Access` folder is `Shared` in the API.** Its objects carry
  `folder: "Shared"`. The tool handles this.
- **Some config types aren't available in every folder.** NGFW-only types such as
  device settings and route lists answer with an empty HTTP 500 in Prisma Access
  folders. They are listed as "not available in this folder" and skipped.
- **Speed.** A folder export takes about 1.5 to 2 minutes. Most of that is the
  applications list, which SCM returns with all ~11,000 built-in App-IDs. The
  tool fetches the pages in parallel and drops the built-in ones.

### JSON layout

```json
{
  "metadata": {"format_version": 2, "exported_at": "...", "tsg_id": "...", "selection": {}},
  "folders": {
    "All": {
      "parent_folder": null,
      "attached_snippets": {"1": "corp-objects", "2": "best-practice"},
      "config": {"objects/tags": []}
    },
    "Shared": {
      "parent_folder": "All",
      "attached_snippets": {},
      "config": {}
    },
    "Mobile Users": {
      "parent_folder": "Shared",
      "attached_snippets": {"1": "gp"},
      "config": {
        "objects/addresses": [{"name": "web", "ip_netmask": "10.0.0.1/32"}],
        "security/security-rules": {"pre": [], "post": []}
      }
    }
  },
  "snippets": {
    "corp-objects": {"objects/addresses": []},
    "gp": {"security/security-rules": {"pre": [], "post": []}}
  }
}
```

**Folders**
- Folders are listed in SCM hierarchy order, parents before children.
- Each folder names its parent in `parent_folder`. The root folder has `null`.
- `attached_snippets` numbers the folder's snippets in SCM order.
- `config` holds the folder's own objects and rules.
- Folder names are the API names (`All`, `Shared`, ...), which are also what `--folder` takes.
  `--list` shows them.

**Snippets**
- Every exported custom snippet is a key under `snippets`, and its value is its config.
- An attached built-in snippet is named in `attached_snippets`, but its content is not exported.

**Config and fields**
- Config types are named `<category>/<type>`, for example `objects/addresses` or `security/security-rules`.
- Rule types hold `pre` and `post` lists, in rulebase order.
- An object with the same name in several folders or snippets appears under each one.
- SCM-managed fields (`id`, `folder`, `snippet`) are removed.

## Bulk change: new AD domain for users and groups

This is the main use case. The rules and objects of a folder such as
`Mobile Users` name users and groups from an old Active Directory domain. Only the
domain has changed.

**1. Export the folder.** The file also holds the parent folders and snippets the
folder inherits from, so it doubles as a backup.

```bash
python config_export.py --folder "Mobile Users" --output mu.json
```

**2. Replace the domain.** `replace_domain.py` reads the export and writes a new
file. It changes only user and group IDs, in every field of every object and rule:

| Form | Before | After |
|---|---|---|
| NetBIOS | `OLDCORP\jsmith` | `NEWCORP\jsmith` |
| UPN / e-mail | `jsmith@oldcorp.local` | `jsmith@newcorp.local` |
| LDAP DN | `cn=vpn,dc=oldcorp,dc=local` | `cn=vpn,dc=newcorp,dc=local` |
| Domain field | `"user_domain": "OLDCORP"` | `"user_domain": "NEWCORP"` |

A domain field is a field whose name contains `domain` and whose whole value is
the old domain.

```bash
python replace_domain.py --input mu.json --output mu_new.json --source "Mobile Users" --old-domain OLDCORP --new-domain NEWCORP --old-dns oldcorp.local --new-dns newcorp.local --changes replaced.csv
```

- **What changes and what doesn't:** the domain is matched without regard to
  case. Left alone:
  - object and rule names;
  - FQDNs, URLs and UNC paths (`\\oldcorp\share`);
  - other text that only contains the domain name;
  - a DN of a longer domain (`dc=oldcorp,dc=local,dc=com`).
- **Server profiles are left alone by default.** LDAP, RADIUS, TACACS+, Kerberos,
  SAML and MFA server profiles hold the directory servers' base DN and bind
  account, which change only when the servers change. Add
  `--include-server-profiles` to change them too.
- **Check the count per form.** The tool prints how many values it changed in
  each form, for example `NetBIOS: 47, UPN: 2, LDAP DN: 45`. A 0 where you expected
  matches usually means a mistyped `--old-dns`.
- **No escaping needed:** the file is read as JSON, so give the domain plainly.
- **Every replaced value is listed:** on the console, and with `--changes` in a
  CSV file.
- **Your export stays as a backup:** the input file is never overwritten.
- **Editing by hand also works.** Inside JSON a backslash is written twice, so
  `OLDCORP\jsmith` appears in the file as `OLDCORP\\jsmith`.

**3. Review the full diff.** Save it in any format you need. `--report` can be
repeated:

```bash
python config_import.py --input mu_new.json --folder "Mobile Users" --dry-run --report diff.html --report diff.csv --report diff.json
```

**4. Apply it.** Type `YES` when asked.

```bash
python config_import.py --input mu_new.json --folder "Mobile Users"
```

**5. Check.** Run step 3 again. It should report `0 to create, 0 to overwrite`.

What to expect:

- **Only that folder is imported.** The target has the same name as a folder in
  the file, so the parent folders and snippets in the file are left alone. To
  change a snippet too, import it into itself:
  `--input mu_new.json --snippet "<snippet>"`.
- **Changed objects are overwritten.** Each one is shown as a diff. Untouched
  objects are listed as unchanged and are not sent to SCM.
- **New objects and rules in the file are created.** Each new rule is placed next
  to its neighbour from the file.
- **Nothing is deleted, and existing rules are never moved.** An updated rule
  stays in its rulebase. If a new rule cannot be moved, that rule is reported
  and the others are still placed.
- **A create that hits a timeout is not repeated blindly.** The tool first checks
  whether SCM saved it, so no object is created twice.
- **A config type that cannot be read from the target is skipped.** Its items are
  listed under skipped, not shown as new.
- **The whole object is replaced.** A field that is in SCM but not in the file is
  removed. The diff shows it as a `-` line, and the CSV report marks it
  `removed by the import`.
- **The result lands in the candidate config.** Review it in SCM, then push it yourself.
- **Run time:** about 1 second per changed object, so 1000 changed rules take
  about 15 minutes.
- **Do not change object or rule names.** Objects are matched by name, so a
  renamed object is created as a new one and the old one stays.

### Report formats

| Extension | Content |
|---|---|
| `.txt` | The report as printed on the console. |
| `.md` | Summary table, then a `diff` block per overwrite and a `json` block per new object. |
| `.html` | One page: summary, then one expandable diff per object, with the changed field names. |
| `.json` | Every change with its field-level current and new values, both full objects, and the unchanged names. |
| `.csv` | One row per changed field (action, config type, rulebase, name, field, current, new, note). Opens in Excel. |

## Import

```bash
python config_import.py --input gp.json                      # into new snippet NEW_<YYYYMMDD_HHMMSS>
python config_import.py --input gp.json --snippet "GP-Copy"  # existing or new snippet
python config_import.py --input full.json --folder "Lab" --source "Mobile Users"
python config_import.py --input gp.json --snippet "GP-Copy" --dry-run --report diff.txt
```

| Option | Meaning |
|---|---|
| `--input FILE` | JSON file made by `config_export.py`. Required. |
| `--snippet NAME` | Target snippet. Created if it does not exist. |
| `--folder NAME` | Target folder. It must already exist. |
| `--source NAME` | Import only this folder or snippet from the file. Repeatable. |
| `--dry-run` | Show the report and exit without asking. |
| `--report FILE` | Save the dry-run report. The extension picks the format: `.txt`, `.md`, `.html`, `.json`, `.csv`. Repeatable. Default `logs/config_import_report_<timestamp>.txt`. |
| `--no-color` | Plain text output. |

Without `--snippet` or `--folder`, a new snippet `NEW_<YYYYMMDD_HHMMSS>` is created.

### How an import runs

1. **Dry run.** The script reads the target's current config and compares it with
   the file. Objects are matched by config type and name. The report shows:
   - a summary per config type
   - every new object, in full
   - a unified diff (current SCM vs file) for every object that will be **overwritten**
   - skipped items, and name collisions between sources
2. **Confirm.** Type `YES`. Any other answer cancels, and nothing changes.
3. **Apply.** Objects are created or updated in dependency order: variables,
   tags and addresses first, rules last. Items that fail because they refer to
   something not created yet are retried. Each new rule is then moved next to
   its neighbour from the file.

Changes go to the **candidate config only**. Review them in SCM and push from there.
Objects in the target that are not in the file are never deleted.

### Limits

- **Which parts of the file are imported:**
  - With `--source NAME`, only those folders or snippets.
  - Otherwise, if the target has the same name as a folder (for `--folder`) or a
    snippet (for `--snippet`) in the file, only that one.
  - Otherwise, all of them are merged into the target. When a name exists in two
    sources, the later one wins and the dry run lists the clash.
- Certificates are not imported, because they need their private key. Import them by hand.
- A few settings objects with no update API are listed as skipped:
  `ssl-decryption-settings`, `url-admin-override`, `saas-tenant-restrictions`, `autoscale`.
- Secrets such as pre-shared keys and passwords are exported in SCM's encrypted
  form and sent back as they are. SCM may reject them in another tenant. Failures
  are listed at the end, and the exit code is 1.
- New rules are placed next to their neighbours from the file. Rules that already
  existed in the target are never moved.
- NAT rules have no move API, so new NAT rules go to the bottom of the rulebase.
  Check their order in SCM.
- Rules are matched by name in either rulebase, because SCM does not always say
  which one a rule is in. An existing rule is updated where it is and never moved
  between pre and post. A new rule goes to the rulebase it has in the file.
- `override_loc` and `override_type` are not compared. SCM sets them itself and
  stores different values in folders and snippets.
- Custom threat signatures are matched by `threat_id`. Settings objects without a
  name (device settings, authentication portal) are matched as the one settings
  object of the target.

## Logs and exit codes

Each run writes its own full debug log, `logs/config_export_<timestamp>.log` or
`logs/config_import_<timestamp>.log`. Earlier logs are kept, so the log of an
import that changed SCM survives later runs.
Use `--verbose` to see debug output on the console.

| Exit code | Meaning |
|---|---|
| 0 | Success, or the import was cancelled |
| 1 | API error, or some items failed |
| 2 | Bad settings, arguments or input file |

## Tests

```bash
pip install pytest
python -m pytest -q
```

The tests use an in-memory fake of the SCM API. No tenant is needed.
