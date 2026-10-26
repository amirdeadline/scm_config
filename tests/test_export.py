import json

import pytest

import config_export
from config_export import build_export, export_container, select_containers
from scm_catalog import ENDPOINTS_BY_KEY, Endpoint, is_owned_custom

ADDR = "/config/objects/v1/addresses"
RULES = "/config/security/v1/security-rules"
APPS = "/config/objects/v1/applications"

FOLDERS = [
    {"id": "1", "name": "All", "parent": ""},
    {"id": "2", "name": "Shared", "parent": "All", "snippets": ["corp"]},
    {"id": "3", "name": "Mobile Users", "parent": "Shared", "snippets": ["gp", "default"]},
    {"id": "4", "name": "predefined", "parent": ""},
]
SNIPPETS = [
    {"id": "a", "name": "corp", "type": "custom"},
    {"id": "b", "name": "gp", "type": "custom"},
    {"id": "c", "name": "default", "type": "predefined"},
    {"id": "d", "name": "lonely", "type": "custom"},
]


def test_endpoint_key_is_category_and_resource():
    assert Endpoint("/config/objects/v1/address-groups").key == "objects/address-groups"


def test_owned_custom_filters_inherited_and_builtin():
    assert is_owned_custom({"folder": "Mobile Users"}, "folder", "Mobile Users")
    assert not is_owned_custom({"folder": "Shared"}, "folder", "Mobile Users")
    assert not is_owned_custom({"folder": "predefined"}, "folder", "Mobile Users")
    assert not is_owned_custom({"snippet": "predefined-snippet"}, "snippet", "predefined-snippet")
    assert not is_owned_custom({"snippet": "corp"}, "folder", "Shared")
    assert is_owned_custom({"general": {}}, "folder", "Shared")  # unscoped settings object


def test_no_selection_exports_all_folders_and_attached_custom_snippets():
    folders, snippets = select_containers(FOLDERS, SNIPPETS, [], [])
    assert folders == ["All", "Shared", "Mobile Users"]
    assert snippets == ["corp", "gp"]  # 'default' is built-in, 'lonely' is not attached


def test_folder_selection_includes_parent_folders_and_their_custom_snippets():
    # Mobile Users inherits from Shared and All, and from the 'corp' snippet attached to Shared.
    assert select_containers(FOLDERS, SNIPPETS, ["Mobile Users"], []) == (
        ["All", "Shared", "Mobile Users"], ["corp", "gp"])


def test_folder_selection_without_inherited_is_the_folder_and_its_own_snippets():
    assert select_containers(FOLDERS, SNIPPETS, ["Mobile Users"], [], inherited=False) == (
        ["Mobile Users"], ["gp"])


def test_two_folders_share_their_parents_once():
    folders = FOLDERS + [{"id": "5", "name": "Remote Networks", "parent": "Shared"}]
    assert select_containers(folders, SNIPPETS, ["Remote Networks", "Mobile Users"], []) == (
        ["All", "Shared", "Remote Networks", "Mobile Users"], ["corp", "gp"])


def test_main_records_inherited_folders_in_metadata(fake_scm, monkeypatch, tmp_path):
    client = fake_scm(folders=FOLDERS, snippets=SNIPPETS)
    monkeypatch.setattr(config_export.ScmClient, "from_settings", classmethod(lambda cls: client))
    monkeypatch.setattr(config_export, "setup_logging", lambda *a, **k: tmp_path / "log")
    out = tmp_path / "mu.json"
    assert config_export.main(["--folder", "Mobile Users", "--output", str(out)]) == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert list(document["folders"]) == ["All", "Shared", "Mobile Users"]
    assert document["metadata"]["selection"]["inherited_from"] == ["All", "Shared", "corp"]


def test_snippet_selection_only():
    assert select_containers(FOLDERS, SNIPPETS, [], ["lonely"]) == ([], ["lonely"])


def test_unknown_folder_lists_available_names():
    with pytest.raises(ValueError, match="Available folders: All, Mobile Users, Shared"):
        select_containers(FOLDERS, SNIPPETS, ["Nope"], [])


def test_builtin_snippet_is_refused():
    with pytest.raises(ValueError, match="built-in"):
        select_containers(FOLDERS, SNIPPETS, [], ["default"])


def test_export_container_keeps_only_own_custom_items(fake_scm):
    client = fake_scm(items={
        ADDR: [
            {"id": "1", "name": "web", "folder": "Mobile Users", "ip_netmask": "10.0.0.1/32"},
            {"id": "2", "name": "inherited", "folder": "Shared", "_visible_in": ["Shared", "Mobile Users"]},
        ],
        APPS: [{"id": "3", "name": "facebook", "folder": "predefined", "_visible_in": ["Mobile Users"]}],
        RULES: [
            {"id": "r1", "name": "allow-gp", "folder": "Mobile Users", "_position": "pre", "action": "allow"},
            {"id": "r2", "name": "deny-all", "folder": "Mobile Users", "_position": "post", "action": "deny"},
        ],
    })
    endpoints = [ENDPOINTS_BY_KEY[k] for k in ("objects/addresses", "objects/applications",
                                               "security/security-rules")]
    config, failed = export_container(client, "folder", "Mobile Users", endpoints)
    assert failed == []
    assert config == {
        "objects/addresses": [{"name": "web", "ip_netmask": "10.0.0.1/32"}],
        "security/security-rules": {"pre": [{"name": "allow-gp", "action": "allow"}],
                                    "post": [{"name": "deny-all", "action": "deny"}]},
    }


def test_export_container_skips_not_applicable_and_reports_other_errors(fake_scm):
    client = fake_scm(unsupported={ADDR: 400, RULES: 500})
    endpoints = [ENDPOINTS_BY_KEY["objects/addresses"], ENDPOINTS_BY_KEY["security/security-rules"]]
    config, failed = export_container(client, "snippet", "gp", endpoints)
    assert config == {}
    assert failed == ["security/security-rules"]


def test_same_object_in_two_containers_is_repeated(fake_scm):
    client = fake_scm(items={ADDR: [
        {"id": "1", "name": "web", "folder": "Shared"},
        {"id": "2", "name": "web", "snippet": "gp"},
    ]})
    document, failures = build_export(client, FOLDERS, SNIPPETS, ["Shared"], ["gp"], {})
    assert document["folders"]["Shared"] == {
        "parent_folder": "All",
        "attached_snippets": {"1": "corp"},
        "config": {"objects/addresses": [{"name": "web"}]},
    }
    assert document["snippets"]["gp"] == {"objects/addresses": [{"name": "web"}]}
    assert failures == {}


def test_main_writes_json_file(fake_scm, monkeypatch, tmp_path):
    client = fake_scm(folders=FOLDERS, snippets=SNIPPETS,
                      items={ADDR: [{"id": "1", "name": "web", "folder": "Mobile Users"}]})
    monkeypatch.setattr(config_export.ScmClient, "from_settings", classmethod(lambda cls: client))
    monkeypatch.setattr(config_export, "setup_logging", lambda *a, **k: tmp_path / "log")
    out = tmp_path / "out.json"
    assert config_export.main(["--folder", "Mobile Users", "--no-inherited", "--output", str(out)]) == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert list(document["folders"]) == ["Mobile Users"]
    assert list(document["snippets"]) == ["gp"]
    assert "inherited_from" not in document["metadata"]["selection"]
    assert document["folders"]["Mobile Users"]["config"] == {"objects/addresses": [{"name": "web"}]}
    assert document["metadata"]["selection"]["mode"] == "filtered"


def test_main_reports_settings_problem(monkeypatch, tmp_path, capsys):
    def broken(cls):
        raise config_export.SettingsError("You need to edit the file X")
    monkeypatch.setattr(config_export.ScmClient, "from_settings", classmethod(broken))
    monkeypatch.setattr(config_export, "setup_logging", lambda *a, **k: tmp_path / "log")
    assert config_export.main([]) == 2


def test_list_prints_folder_tree_and_custom_snippets(capsys):
    config_export.print_containers(FOLDERS, SNIPPETS)
    out = capsys.readouterr().out
    assert "  All\n      Shared   [snippets: corp]\n          Mobile Users" in out
    assert "predefined" not in out.split("Custom snippets")[0]
    assert "lonely" in out and "default" not in out.split("Custom snippets")[1]


def test_hierarchy_order_puts_parents_before_children():
    shuffled = [FOLDERS[2], FOLDERS[3], FOLDERS[1], FOLDERS[0]]
    assert config_export.hierarchy_order(shuffled) == ["predefined", "All", "Shared", "Mobile Users"]


def test_full_export_lists_folders_in_hierarchy_with_parent_and_snippets(fake_scm):
    client = fake_scm()
    folders, snippets = select_containers(FOLDERS, SNIPPETS, [], [])
    document, _ = build_export(client, [FOLDERS[2], FOLDERS[1], FOLDERS[0]], SNIPPETS,
                               folders, snippets, {})
    assert list(document["folders"]) == ["All", "Shared", "Mobile Users"]
    assert document["folders"]["All"]["parent_folder"] is None
    assert document["folders"]["Mobile Users"]["parent_folder"] == "Shared"
    assert document["folders"]["Mobile Users"]["attached_snippets"] == {"1": "gp", "2": "default"}
    assert list(document["snippets"]) == ["corp", "gp"]


# --- behaviour seen on a live tenant -------------------------------------
def test_prisma_access_folder_items_carry_folder_shared():
    assert is_owned_custom({"name": "a", "folder": "Shared"}, "folder", "Prisma Access")
    assert not is_owned_custom({"name": "a", "folder": "Shared"}, "folder", "Mobile Users")


def test_snippet_content_applied_to_folder_is_not_folder_config():
    item = {"name": "CloudGenix-IKE-Crypto-Default", "folder": "Mobile Users", "snippet": "default"}
    assert not is_owned_custom(item, "folder", "Mobile Users")


def test_count_only_item_is_ignored():
    assert not is_owned_custom({"count": 0}, "folder", "Mobile Users")


def test_rules_use_own_position_skip_placeholders_and_duplicates(fake_scm):
    client = fake_scm(items={RULES: [
        {"id": "p1", "name": "corp", "folder": "Mobile Users", "position": "pre", "policy_type": "Security"},
        {"id": "r1", "name": "allow-gp", "folder": "Mobile Users", "position": "pre", "action": "allow",
         "_visible_in": ["Mobile Users"], "_position": "pre"},
        {"id": "r2", "name": "late", "folder": "Mobile Users", "position": "post", "action": "deny",
         "_position": "pre"},
    ]})
    config, _ = export_container(client, "folder", "Mobile Users", [ENDPOINTS_BY_KEY["security/security-rules"]])
    assert config == {"security/security-rules": {
        "pre": [{"name": "allow-gp", "action": "allow"}],
        "post": [{"name": "late", "action": "deny"}]}}


def test_snippet_rule_returned_for_both_positions_is_exported_once(fake_scm):
    client = fake_scm(items={RULES: [{"id": "s1", "name": "Allow-ICMP", "snippet": "lab", "action": "allow"}]})
    client.get_all_original = client.get_all
    client.get_all = lambda path, params=None: client.get_all_original(
        path, {k: v for k, v in (params or {}).items() if k != "position"})
    config, _ = export_container(client, "snippet", "lab", [ENDPOINTS_BY_KEY["security/security-rules"]])
    assert config == {"security/security-rules": {"pre": [{"name": "Allow-ICMP", "action": "allow"}]}}


def test_folder_rules_without_reported_rulebase_go_to_pre_with_a_note(fake_scm, caplog):
    # Prisma Access folders such as Mobile Users ignore the position filter and give own rules no position.
    client = fake_scm(items={RULES: [
        {"id": "r1", "name": "allow-web", "folder": "Mobile Users", "action": "allow", "_position": "pre"},
        {"id": "r2", "name": "printers", "folder": "Mobile Users", "action": "allow", "_position": "post"},
    ]})
    client.get_all_original = client.get_all
    client.get_all = lambda path, params=None: client.get_all_original(
        path, {k: v for k, v in (params or {}).items() if k != "position"})
    with caplog.at_level("INFO"):
        config, _ = export_container(client, "folder", "Mobile Users", [ENDPOINTS_BY_KEY["security/security-rules"]])
    assert [r["name"] for r in config["security/security-rules"]["pre"]] == ["allow-web", "printers"]
    assert "does not report pre or post for 2 rule(s)" in caplog.text


def test_shared_in_readonly_snippet_is_custom():
    snippets = SNIPPETS + [{"name": "lab", "type": "readonly"}, {"name": "hip-default", "type": "readonly"}]
    _, names = select_containers(FOLDERS, snippets, [], ["lab"], frozenset({"lab"}))
    assert names == ["lab"]
    with pytest.raises(ValueError, match="built-in"):
        select_containers(FOLDERS, snippets, [], ["hip-default"], frozenset({"lab"}))


def test_placeholder_rows_dropped_from_rule_types_without_positions(fake_scm):
    path = "/config/network/v1/pbf-rules"
    client = fake_scm(items={path: [
        {"id": "1", "name": "corp", "folder": "Shared", "_visible_in": ["Prisma Access"]},
        {"id": "2", "name": "to-isp2", "folder": "Shared", "action": {"forward": {}},
         "_visible_in": ["Prisma Access"]},
    ]})
    config, _ = export_container(client, "folder", "Prisma Access", [ENDPOINTS_BY_KEY["network/pbf-rules"]])
    assert config == {"network/pbf-rules": [{"name": "to-isp2", "action": {"forward": {}}}]}
