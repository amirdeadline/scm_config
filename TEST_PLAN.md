# Test plan

This build is for testing. Run it against a lab tenant first. [README.md](README.md)
explains setup and every option.

## Status

| Area | Status |
|---|---|
| Export (all folders, `--folder` with inherited config, `--snippet`, `--list`) | Tested on a live lab tenant |
| Dry-run import into a folder or snippet | Tested on a live lab tenant |
| Import that creates objects and rules in a new snippet | Tested on a live lab tenant (55 items, folder plus inherited config) |
| Import that **overwrites** existing objects or rules in a snippet | Tested on a live lab tenant |
| Rule placement after import (move API) | Tested on a live lab tenant (snippet) |
| **Bulk AD domain change in `Mobile Users`** (export, `replace_domain.py`, import into the same folder) | Tested on a live lab tenant in 3 rounds. Users and groups in NetBIOS, UPN and DN form (lower and upper case), 45+ security rules, a decryption rule and an authentication profile. Each round: up to 50 changes, 0 failures, 0 deletions, existing rule order unchanged, new rules placed after their neighbour, re-run clean, checked by an independent before/after comparison |
| Updating a post rule keeps it in post | Tested on a live lab tenant |
| Dry-run report as `.txt` `.md` `.html` `.json` `.csv` | Tested on a live lab tenant |
| Large imports (1000+ rules, rate limits) | Not tested; measured about 0.85 s per change |

Every import writes to the **candidate config only**. Nothing reaches devices or
Prisma Access until someone pushes in SCM.

## Setup

1. Python 3.9 or later, then `pip install -r requirements.txt`.
2. Fill in `settings.py` (the file says where to find each value).
3. Optional: `pip install pytest` then `python -m pytest -q`. 103 tests should pass.

## Tests to run

Run them in this order and keep each report from `logs/`.

1. **List.** `python config_export.py --list`. The folder tree and snippet names match the SCM UI.
2. **Export a folder.** `python config_export.py --folder "<folder>" --output f.json`.
   Spot-check a few objects and rules in `f.json` against the SCM UI.
3. **Round trip, no edits.** `python config_import.py --input f.json --folder "<folder>" --dry-run`.
   Expected: `0 to create, 0 to overwrite`.
4. **New snippet.** `python config_import.py --input f.json --source "<folder>"`, then type `YES`.
   A new snippet `NEW_<timestamp>` is created and not attached to any folder, so it
   has no effect on traffic. Check its content in SCM.
5. **Overwrite in a snippet.** Export that snippet (`--snippet NEW_<timestamp> --output s.json`).
   Then edit one rule description and one object value, and add two rules next to
   an existing one. Import it back with `--snippet NEW_<timestamp>`. Expected:
   - the dry run shows exactly those changes;
   - after `YES`, SCM shows the edits;
   - the new rules sit next to their neighbours from the file.
6. **Bulk AD domain change in a lab folder.** Follow "Bulk change" in the README:
   `config_export.py`, then `replace_domain.py`, then `config_import.py --folder`.
   Save the dry run with `--report diff.html --report diff.csv`. Expected:
   - only objects that hold old-domain users or groups are listed as overwrites;
   - in the CSV, the changed fields are the user fields only (`source_user`, `allow_list`, ...);
   - nothing is deleted, rule positions are unchanged;
   - the old domain no longer appears in the folder (export it again and search).
7. **Re-run.** Repeat the dry run of step 5 or 6. Expected: `0 to create, 0 to overwrite`.

After each test, review the candidate config in SCM. Revert it or push it there.

## Report a problem

Send these:
- the command you ran;
- the console output;
- the matching `logs/config_export_<timestamp>.log` or `logs/config_import_<timestamp>.log`;
- the report files (`logs/config_import_report_*.txt`, or the ones you saved with `--report`).

The logs contain config details from your tenant but never the client secret.
Remove anything sensitive before you share them.
