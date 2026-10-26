import json

import pytest

import config_import
from config_import import apply_changes, read_current, resolve_target, restore_rule_order
from scm_import_plan import (CREATE, SKIP, UNCHANGED, UPDATE, Palette, list_sources, load_export,
                             merge_sources, plan_changes, render_report, select_sources)

ADDR = "/config/objects/v1/addresses"
GROUPS = "/config/objects/v1/address-groups"
RULES = "/config/security/v1/security-rules"

EXPORT = {
    "metadata": {},
    "folders": {
        "Mobile Users": {"parent_folder": "Shared", "attached_snippets": {"1": "gp"}, "config": {
            "objects/addresses": [{"name": "web", "ip_netmask": "10.0.0.1/32"},
                                  {"name": "db", "ip_netmask": "10.0.0.2/32"}],
            "objects/address-groups": [{"name": "servers", "static": ["web", "db"]}],
            "identity/certificates": [{"name": "gp-cert"}],
            "security/security-rules": {"pre": [{"name": "r1", "action": "allow"}, {"name": "r2", "action": "allow"}, {"name": "r3", "action": "allow"}]},
        }},
    },
    "snippets": {
        "gp": {
            "objects/addresses": [{"name": "web", "ip_netmask": "192.168.1.1/32"}],
            "unknown/thing": [{"name": "x"}],
        },
    },
}


def write_export(tmp_path, document=EXPORT):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_load_export_rejects_other_json(tmp_path):
    with pytest.raises(ValueError, match="not a config_export.py file"):
        load_export(write_export(tmp_path, {"foo": 1}))


def test_merge_reports_collision_and_last_source_wins():
    desired = merge_sources(list_sources(EXPORT))
    item, source = desired.items[("objects/addresses", None)]["web"]
    assert item["ip_netmask"] == "192.168.1.1/32" and source == "snippet 'gp'"
    assert len(desired.collisions) == 1 and "'web'" in desired.collisions[0]
    assert desired.unknown_types == ["unknown/thing (from snippet 'gp')"]


def test_select_sources_limits_and_validates():
    sources = list_sources(EXPORT)
    assert [s[1] for s in select_sources(sources, ["gp"])] == ["gp"]
    with pytest.raises(ValueError, match="It contains: Mobile Users, gp"):
        select_sources(sources, ["nope"])


def test_plan_create_update_unchanged_skip_in_dependency_order():
    desired = merge_sources(select_sources(list_sources(EXPORT), ["Mobile Users"]))
    current = {
        ("objects/addresses", None): {
            "web": {"id": "1", "name": "web", "folder": "T", "ip_netmask": "10.0.0.1/32"},
            "db": {"id": "2", "name": "db", "folder": "T", "ip_netmask": "10.9.9.9/32"},
        },
    }
    changes = plan_changes(desired, current)
    actions = [(c.endpoint_key, c.name, c.action) for c in changes]
    assert actions == [
        ("objects/addresses", "web", UNCHANGED),
        ("objects/addresses", "db", UPDATE),
        ("objects/address-groups", "servers", CREATE),
        ("identity/certificates", "gp-cert", SKIP),
        ("security/security-rules", "r1", CREATE),
        ("security/security-rules", "r2", CREATE),
        ("security/security-rules", "r3", CREATE),
    ]


def test_report_shows_diff_for_overwrite_and_full_new_objects():
    desired = merge_sources(select_sources(list_sources(EXPORT), ["Mobile Users"]))
    current = {("objects/addresses", None): {"db": {"id": "2", "name": "db", "ip_netmask": "10.9.9.9/32"}}}
    report = render_report(plan_changes(desired, current), desired, "snippet 'T'", Palette(False))
    assert "~ OVERWRITE objects/addresses 'db'" in report
    assert '-  "ip_netmask": "10.9.9.9/32"' in report
    assert '+  "ip_netmask": "10.0.0.2/32"' in report
    assert "+ CREATE objects/address-groups 'servers'" in report
    assert "identity/certificates 'gp-cert': certificates need their private key" in report
    assert "\033[" not in report


def test_resolve_target_rules(fake_scm):
    client = fake_scm(folders=[{"name": "Lab"}], snippets=[{"name": "s1", "type": "custom"},
                                                           {"name": "default", "type": "predefined"}])
    assert resolve_target(client, "Lab", None) == ("folder", "Lab", True)
    assert resolve_target(client, None, "s1") == ("snippet", "s1", True)
    assert resolve_target(client, None, "new-one") == ("snippet", "new-one", False)
    kind, name, exists = resolve_target(client, None, None)
    assert kind == "snippet" and name.startswith("NEW_") and not exists
    with pytest.raises(ValueError, match="not found"):
        resolve_target(client, "Nope", None)
    with pytest.raises(ValueError, match="built-in"):
        resolve_target(client, None, "default")


def test_read_current_ignores_inherited_items(fake_scm):
    client = fake_scm(items={ADDR: [
        {"id": "1", "name": "web", "snippet": "T"},
        {"id": "2", "name": "parent", "folder": "Shared", "_visible_in": ["T"]},
    ]})
    current = read_current(client, "snippet", "T", {("objects/addresses", None)})
    assert list(current[("objects/addresses", None)]) == ["web"]


def test_apply_retries_items_that_depend_on_later_objects(fake_scm):
    client = fake_scm()
    client.fail_once = {"servers"}
    desired = merge_sources(select_sources(list_sources(EXPORT), ["Mobile Users"]))
    changes = plan_changes(desired, {})
    done, failed = apply_changes(client, changes, "snippet", "T")
    assert failed == []
    assert len(done) == 6  # 2 addresses, 1 group, 3 rules (certificate skipped)
    created = [c for c in client.calls if c[0] == "create"]
    assert created[0] == ("create", ADDR, "web", None)
    assert ("create", RULES, "r1", "pre") in created
    assert all(item["snippet"] == "T" for item in client.items[ADDR])


def test_apply_update_uses_existing_id(fake_scm):
    client = fake_scm(items={ADDR: [{"id": "abc", "name": "db", "snippet": "T", "ip_netmask": "1.1.1.1/32"}]})
    desired = merge_sources([("folder", "F", {"objects/addresses": [{"name": "db", "ip_netmask": "2.2.2.2/32"}]})])
    changes = plan_changes(desired, read_current(client, "snippet", "T", set(desired.items)))
    done, failed = apply_changes(client, changes, "snippet", "T")
    assert [c.action for c in done] == [UPDATE] and failed == []
    assert client.items[ADDR][0] == {"id": "abc", "name": "db", "ip_netmask": "2.2.2.2/32", "snippet": "T"}


def test_permanent_failures_are_returned(fake_scm):
    client = fake_scm()
    original = client.create

    def always_fail(path, body, params=None):
        if body["name"] == "db":
            raise config_import.ApiError("POST", path, 400, {"_errors": [{"message": "bad"}]})
        return original(path, body, params)

    client.create = always_fail
    desired = merge_sources([("folder", "F", {"objects/addresses": [{"name": "web"}, {"name": "db"}]})])
    done, failed = apply_changes(client, plan_changes(desired, {}), "snippet", "T")
    assert [c.name for c in done] == ["web"]
    assert [(c.name, msg) for c, msg in failed] == [("db", "bad")]


def test_rule_order_restored_for_created_rules(fake_scm):
    client = fake_scm(items={RULES: [
        {"id": "x2", "name": "r2", "snippet": "T", "_position": "pre", "action": "allow"},
    ]})
    desired = merge_sources([("folder", "F", {"security/security-rules": {
        "pre": [{"name": "r1", "action": "allow"}, {"name": "r2", "action": "allow"}, {"name": "r3", "action": "allow"}]}})])
    changes = plan_changes(desired, read_current(client, "snippet", "T", set(desired.items)))
    done, _ = apply_changes(client, changes, "snippet", "T")
    warnings = restore_rule_order(client, desired, done, "snippet", "T")
    assert warnings == []
    assert [r["name"] for r in client.items[RULES]] == ["r1", "r2", "r3"]


def run_main(monkeypatch, tmp_path, client, args, answer="YES"):
    monkeypatch.setattr(config_import.ScmClient, "from_settings", classmethod(lambda cls: client))
    monkeypatch.setattr(config_import, "setup_logging", lambda *a, **k: tmp_path / "log")
    monkeypatch.setattr(config_import, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    return config_import.main(["--input", str(write_export(tmp_path)), "--no-color", *args])


def test_main_creates_new_snippet_after_confirmation(fake_scm, monkeypatch, tmp_path, capsys):
    client = fake_scm(snippets=[])
    assert run_main(monkeypatch, tmp_path, client, ["--source", "Mobile Users"]) == 0
    created_snippet = [c for c in client.calls if c[0] == "create_snippet"]
    assert len(created_snippet) == 1 and created_snippet[0][1].startswith("NEW_")
    assert len(client.items[ADDR]) == 2
    assert "Dry run: import into snippet 'NEW_" in capsys.readouterr().out


def test_main_accepts_yes_piped_from_windows_powershell(fake_scm, monkeypatch, tmp_path):
    # Windows PowerShell 5.1 puts a byte-order mark in front of text piped to a program.
    client = fake_scm(snippets=[])
    assert run_main(monkeypatch, tmp_path, client, ["--source", "Mobile Users"], answer="﻿YES\r") == 0
    assert len(client.items[ADDR]) == 2


def test_main_changes_nothing_when_not_confirmed(fake_scm, monkeypatch, tmp_path, capsys):
    client = fake_scm(snippets=[{"name": "T", "type": "custom"}],
                      items={ADDR: [{"id": "1", "name": "web", "snippet": "T", "ip_netmask": "9.9.9.9/32"}]})
    assert run_main(monkeypatch, tmp_path, client, ["--snippet", "T"], answer="no") == 0
    assert [c for c in client.calls if c[0] in ("create", "update")] == []
    assert "will be OVERWRITTEN" in capsys.readouterr().out


def test_main_dry_run_writes_report_and_does_not_prompt(fake_scm, monkeypatch, tmp_path):
    client = fake_scm(folders=[{"name": "Lab"}])
    report = tmp_path / "report.txt"

    def no_prompt(prompt=""):
        raise AssertionError("must not prompt in dry run")

    monkeypatch.setattr(config_import.ScmClient, "from_settings", classmethod(lambda cls: client))
    monkeypatch.setattr(config_import, "setup_logging", lambda *a, **k: tmp_path / "log")
    monkeypatch.setattr("builtins.input", no_prompt)
    code = config_import.main(["--input", str(write_export(tmp_path)), "--folder", "Lab",
                               "--dry-run", "--report", str(report)])
    assert code == 0
    assert "+ CREATE objects/addresses 'web'" in report.read_text(encoding="utf-8")
    assert client.calls == []


def test_folder_and_snippet_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        config_import.parse_args(["--input", "x.json", "--folder", "a", "--snippet", "b"])


# --- regressions from code review ----------------------------------------
def test_existing_rules_are_never_moved(fake_scm):
    client = fake_scm(items={RULES: [
        {"id": "x2", "name": "r2", "snippet": "T", "_position": "pre", "action": "allow"},
        {"id": "xd", "name": "DENY", "snippet": "T", "_position": "pre", "action": "allow"},
    ]})
    desired = merge_sources([("folder", "F", {"security/security-rules": {
        "pre": [{"name": "r1", "action": "allow"}, {"name": "r2", "action": "allow"}, {"name": "r3", "action": "allow"}]}})])
    changes = plan_changes(desired, read_current(client, "snippet", "T", set(desired.items)))
    done, _ = apply_changes(client, changes, "snippet", "T")
    restore_rule_order(client, desired, done, "snippet", "T")
    assert [r["name"] for r in client.items[RULES]] == ["r1", "r2", "r3", "DENY"]
    moved = {c[2] for c in client.calls if c[0] == "move"}
    assert "x2" not in moved and "xd" not in moved


def test_signatures_are_keyed_by_threat_id():
    desired = merge_sources([("snippet", "S", {"security/anti-spyware-signatures": [
        {"threat_id": 6900001, "threatname": "a"}, {"threat_id": 6900002, "threatname": "b"}]})])
    assert list(desired.items[("security/anti-spyware-signatures", None)]) == ["6900001", "6900002"]
    assert desired.collisions == []


def test_duplicate_name_inside_one_source_is_reported():
    desired = merge_sources([("snippet", "S", {"objects/addresses": [{"name": "a"}, {"name": "a"}]})])
    assert len(desired.collisions) == 1


def test_malformed_rule_section_is_reported_not_crashing():
    desired = merge_sources([("snippet", "S", {"security/security-rules": [{"name": "r1", "action": "allow"}]})])
    assert desired.items == {}
    assert "malformed" in desired.unknown_types[0]


def test_rule_name_in_other_rulebase_is_skipped():
    desired = merge_sources([("snippet", "S", {"security/security-rules": {"pre": [{"name": "r1", "action": "allow"}]}})])
    current = {("security/security-rules", "post"): {"r1": {"id": "1", "name": "r1"}}}
    change = plan_changes(desired, current)[0]
    assert change.action == SKIP and "post rulebase" in change.reason


def test_override_markers_rewritten_by_scm_are_not_a_change():
    # SCM stores an edited built-in tag as "predefined-snippet" in a folder but "predefined" in a snippet.
    tag = {"name": "Tolerated", "color": "Orange", "override_loc": "predefined-snippet", "override_type": "snippet"}
    desired = merge_sources([("folder", "All", {"objects/tags": [tag]})])
    current = {"id": "1", "name": "Tolerated", "color": "Orange", "override_loc": "predefined", "override_type": "snippet"}
    assert plan_changes(desired, {("objects/tags", None): {"Tolerated": current}})[0].action == UNCHANGED

    recoloured = {**current, "color": "Maroon"}
    changes = plan_changes(desired, {("objects/tags", None): {"Tolerated": recoloured}})
    assert changes[0].action == UPDATE
    report = render_report(changes, desired, "snippet 'S'", Palette(False))
    assert '-  "color": "Maroon"' in report and "override_loc" not in report.split("~ OVERWRITE")[1]


def test_snippet_rule_is_matched_by_name_in_either_rulebase(fake_scm):
    client = fake_scm(items={RULES: [
        {"id": "x1", "name": "web", "snippet": "S", "_position": "post", "action": "allow"},
    ]})
    client.snippet_rules_in_both = True
    unchanged = merge_sources([("folder", "All", {"security/security-rules": {"post": [{"name": "web", "action": "allow"}]}})])
    current = read_current(client, "snippet", "S", set(unchanged.items))
    assert plan_changes(unchanged, current, any_rulebase=True)[0].action == UNCHANGED

    edited = merge_sources([("folder", "All", {"security/security-rules": {"post": [{"name": "web", "action": "deny"}]}})])
    change = plan_changes(edited, current, any_rulebase=True)[0]
    assert change.action == UPDATE and change.current["id"] == "x1"


def test_main_reimport_into_snippet_reports_nothing_to_change(fake_scm, monkeypatch, tmp_path, capsys):
    client = fake_scm(snippets=[{"name": "S", "type": "custom"}], items={RULES: [
        {"id": "x1", "name": "r1", "snippet": "S", "_position": "pre", "action": "allow"},
    ]})
    client.snippet_rules_in_both = True
    document = {"metadata": {}, "folders": {"All": {"config": {
        "security/security-rules": {"post": [{"name": "r1", "action": "allow"}]}}}}, "snippets": {}}
    monkeypatch.setattr(config_import.ScmClient, "from_settings", classmethod(lambda cls: client))
    monkeypatch.setattr(config_import, "setup_logging", lambda *a, **k: tmp_path / "log")
    monkeypatch.setattr(config_import, "LOG_DIR", tmp_path / "logs")
    path = write_export(tmp_path, document)
    assert config_import.main(["--input", str(path), "--snippet", "S", "--dry-run", "--no-color"]) == 0
    assert "0 to create, 0 to overwrite, 1 unchanged, 0 skipped" in capsys.readouterr().out


def test_new_snippet_rules_are_ordered_although_scm_lists_them_under_pre(fake_scm):
    client = fake_scm(items={RULES: [
        {"id": "x2", "name": "r2", "snippet": "S", "_position": "post", "action": "allow"},
    ]})
    client.snippet_rules_in_both = True
    desired = merge_sources([("folder", "All", {"security/security-rules": {
        "post": [{"name": "r1", "action": "allow"}, {"name": "r2", "action": "allow"}, {"name": "r3", "action": "allow"}]}})])
    changes = plan_changes(desired, read_current(client, "snippet", "S", set(desired.items)), any_rulebase=True)
    done, _ = apply_changes(client, changes, "snippet", "S")
    assert restore_rule_order(client, desired, done, "snippet", "S") == []
    assert [r["name"] for r in client.items[RULES]] == ["r1", "r2", "r3"]


def test_existing_item_without_id_is_skipped():
    desired = merge_sources([("snippet", "S", {"objects/addresses": [{"name": "a", "fqdn": "x"}]})])
    change = plan_changes(desired, {("objects/addresses", None): {"a": {"name": "a"}}})[0]
    assert change.action == SKIP and "no id" in change.reason


def test_network_error_on_one_item_does_not_stop_the_run(fake_scm):
    client = fake_scm()
    original = client.create

    def flaky(path, body, params=None):
        if body["name"] == "db":
            raise ConnectionError("timed out")
        return original(path, body, params)

    client.create = flaky
    desired = merge_sources([("folder", "F", {"objects/addresses": [{"name": "db"}, {"name": "web"}]})])
    done, failed = apply_changes(client, plan_changes(desired, {}), "snippet", "T")
    assert [c.name for c in done] == ["web"]
    assert [(c.name, m) for c, m in failed] == [("db", "timed out")]


def test_round_trip_into_same_folder_imports_only_that_folder(fake_scm, monkeypatch, tmp_path, capsys):
    """Export folder (with its snippet), edit, import back: snippet content must stay out."""
    client = fake_scm(folders=[{"name": "Mobile Users"}], items={ADDR: [
        {"id": "1", "name": "web", "folder": "Mobile Users", "ip_netmask": "10.0.0.1/32"},
        {"id": "2", "name": "db", "folder": "Mobile Users", "ip_netmask": "10.0.0.9/32"},
    ]})
    assert run_main(monkeypatch, tmp_path, client, ["--folder", "Mobile Users"]) == 0
    out = capsys.readouterr().out
    assert "~ OVERWRITE objects/addresses 'db'" in out
    assert "OVERWRITE objects/addresses 'web'" not in out   # unchanged, gp's version ignored
    updates = [c for c in client.calls if c[0] == "update"]
    assert updates == [("update", ADDR, "db")]
    web = next(i for i in client.items[ADDR] if i["name"] == "web")
    assert web["ip_netmask"] == "10.0.0.1/32"
    reports = list((tmp_path / "logs").glob("config_import_report_*.txt"))
    assert len(reports) == 1 and "OVERWRITE objects/addresses 'db'" in reports[0].read_text(encoding="utf-8")


def test_bulk_find_and_replace_overwrites_only_changed_rules(fake_scm, monkeypatch, tmp_path):
    rules = [{"id": f"r{i}", "name": f"rule{i}", "folder": "GP", "_position": "pre",
              "source_user": ["old\\alice" if i % 2 else "local\\bob"]} for i in range(6)]
    client = fake_scm(folders=[{"name": "GP"}], items={RULES: [dict(r) for r in rules]})
    exported = {"folders": {"GP": {"parent_folder": None, "attached_snippets": {}, "config": {
        "security/security-rules": {"pre": [
            {k: v for k, v in r.items() if k not in ("id", "folder", "_position")} for r in rules]}}}}}
    edited = json.loads(json.dumps(exported).replace("old\\\\", "new\\\\"))
    path = tmp_path / "gp.json"
    path.write_text(json.dumps(edited), encoding="utf-8")
    monkeypatch.setattr(config_import.ScmClient, "from_settings", classmethod(lambda cls: client))
    monkeypatch.setattr(config_import, "setup_logging", lambda *a, **k: tmp_path / "log")
    monkeypatch.setattr(config_import, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr("builtins.input", lambda prompt="": "YES")
    assert config_import.main(["--input", str(path), "--folder", "GP", "--no-color"]) == 0
    assert sorted(c[2] for c in client.calls if c[0] == "update") == ["rule1", "rule3", "rule5"]
    assert not [c for c in client.calls if c[0] in ("create", "move")]
    users = [r["source_user"][0] for r in client.items[RULES]]
    assert users == ["local\\bob", "new\\alice"] * 3
    assert [r["name"] for r in client.items[RULES]] == [f"rule{i}" for i in range(6)]
