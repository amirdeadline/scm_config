# Test plan

This build is for testing. Run it against a lab tenant first. [README.md](README.md)
explains setup and every option.

## Status

| Area | Status |
|---|---|
| Export (all folders, `--folder`, `--snippet`, `--list`) | Tested on a live lab tenant |
| Dry-run import into a folder or snippet | Tested on a live lab tenant |
| Import that creates new objects and rules in a new snippet | Tested on a live lab tenant (10 items) |
| Import that **overwrites** existing objects or rules | Unit tests only, not yet run against SCM |
| Rule re-ordering after import (move API) | Unit tests only, not yet run against SCM |
| Import into a **folder** | Unit tests only, not yet run against SCM |
| Large imports (hundreds of rules, rate limits) | Not tested |

Every import writes to the **candidate config only**. Nothing reaches devices or
Prisma Access until someone pushes in SCM.

## Setup

1. Python 3.9 or later, then `pip install -r requirements.txt`.
2. Fill in `settings.py` (the file says where to find each value).
3. Optional: `pip install pytest` then `python -m pytest -q`. 63 tests should pass.

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
6. **Bulk find and replace in a lab folder.** Follow "Bulk change" in the README.
   Expected:
   - only the edited items are listed as overwrites;
   - rule positions are unchanged.
7. **Re-run.** Repeat the dry run of step 5 or 6. Expected: `0 to create, 0 to overwrite`.

After each test, review the candidate config in SCM. Revert it or push it there.

## Report a problem

Send these:
- the command you ran;
- the console output;
- `logs/config_export.log` or `logs/config_import.log`;
- the `logs/config_import_report_*.txt` file.

The logs contain config details from your tenant but never the client secret.
Remove anything sensitive before you share them.
