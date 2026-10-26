"""The bulk AD domain change, end to end against the in-memory fake: replace_domain.py, then config_import.py."""
import json

import config_import
import replace_domain

BS = "\\"
RULES = "/config/security/v1/security-rules"
PROFILES = "/config/identity/v1/authentication-profiles"

EXPORT = {
    "metadata": {},
    "folders": {
        "All": {"parent_folder": None, "attached_snippets": {}, "config": {
            "security/security-rules": {"post": [{"name": "parent-rule", "action": "allow",
                                                  "source_user": [f"labcorp{BS}x"]}]}}},
        "Mobile Users": {"parent_folder": "All", "attached_snippets": {"1": "lab"}, "config": {
            "identity/authentication-profiles": [{"name": "auth", "allow_list": [f"labcorp{BS}vpn"]}],
            "security/security-rules": {"pre": [
                {"name": "r1", "action": "allow", "source_user": [f"LABCORP{BS}jsmith", "adoe@labcorp.local"]},
                {"name": "r2", "action": "allow", "source_user": ["any"]},
                {"name": "r3", "action": "allow", "source_user": ["cn=vpn,ou=g,dc=labcorp,dc=local"]},
            ]}}},
    },
    "snippets": {"lab": {"security/security-rules": {"pre": [
        {"name": "snip-rule", "action": "allow", "source_user": [f"labcorp{BS}y"]}]}}},
}


def scm_with_export(fake_scm):
    """A fake tenant holding exactly what EXPORT describes, with ids."""
    items = {RULES: [], PROFILES: []}
    for n, rule in enumerate(EXPORT["folders"]["Mobile Users"]["config"]["security/security-rules"]["pre"]):
        items[RULES].append({**rule, "id": f"mu{n}", "folder": "Mobile Users"})
    items[RULES].append({**EXPORT["folders"]["All"]["config"]["security/security-rules"]["post"][0],
                         "id": "all0", "folder": "All", "_position": "post"})
    items[RULES].append({**EXPORT["snippets"]["lab"]["security/security-rules"]["pre"][0], "id": "s0", "snippet": "lab"})
    items[PROFILES].append({**EXPORT["folders"]["Mobile Users"]["config"]["identity/authentication-profiles"][0],
                            "id": "p0", "folder": "Mobile Users"})
    return fake_scm(folders=[{"name": "All"}, {"name": "Mobile Users", "parent": "All"}],
                    snippets=[{"name": "lab", "type": "custom"}], items=items)


def test_domain_change_round_trip_updates_only_the_folder(fake_scm, monkeypatch, tmp_path):
    exported = tmp_path / "mu.json"
    exported.write_text(json.dumps(EXPORT), encoding="utf-8")
    replaced = tmp_path / "mu_new.json"
    assert replace_domain.main(["--input", str(exported), "--output", str(replaced), "--source", "Mobile Users",
                                "--old-domain", "labcorp", "--new-domain", "newcorp",
                                "--old-dns", "labcorp.local", "--new-dns", "newcorp.local"]) == 0
    document = json.loads(replaced.read_text(encoding="utf-8"))
    document["folders"]["Mobile Users"]["config"]["security/security-rules"]["pre"].insert(
        1, {"name": "r-new", "action": "allow", "source_user": [f"newcorp{BS}new"]})
    replaced.write_text(json.dumps(document), encoding="utf-8")

    client = scm_with_export(fake_scm)
    before_ids = {i["id"] for i in client.items[RULES] + client.items[PROFILES]}
    monkeypatch.setattr(config_import.ScmClient, "from_settings", classmethod(lambda cls: client))
    monkeypatch.setattr(config_import, "setup_logging", lambda *a, **k: tmp_path / "log")
    monkeypatch.setattr("builtins.input", lambda prompt="": "YES")
    report = tmp_path / "diff.json"
    assert config_import.main(["--input", str(replaced), "--folder", "Mobile Users", "--no-color",
                               "--report", str(report)]) == 0

    summary = json.loads(report.read_text(encoding="utf-8"))["summary"]
    assert summary == {"create": 1, "overwrite": 3, "unchanged": 1, "skip": 0}
    rules = {r["name"]: r for r in client.items[RULES]}
    assert rules["r1"]["source_user"] == [f"newcorp{BS}jsmith", "adoe@newcorp.local"]
    assert rules["r3"]["source_user"] == ["cn=vpn,ou=g,dc=newcorp,dc=local"]
    assert client.items[PROFILES][0]["allow_list"] == [f"newcorp{BS}vpn"]
    # Parent folder and snippet are untouched, nothing was deleted, the new rule sits after r1.
    assert rules["parent-rule"]["source_user"] == [f"labcorp{BS}x"]
    assert rules["snip-rule"]["source_user"] == [f"labcorp{BS}y"]
    assert before_ids <= {i["id"] for i in client.items[RULES] + client.items[PROFILES]}
    mu_order = [r["name"] for r in client.items[RULES] if r.get("folder") == "Mobile Users"]
    assert mu_order == ["r1", "r-new", "r2", "r3"]
    touched = {c[2] for c in client.calls if c[0] in ("create", "update")}
    assert touched == {"auth", "r1", "r3", "r-new"}
