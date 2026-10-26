import json

import pytest

import replace_domain
from replace_domain import DomainRule, replace_in_document

BS = "\\"


def document(rules, objects=None, other=None):
    folders = {"Mobile Users": {"parent_folder": "Shared", "attached_snippets": {}, "config": {
        "security/security-rules": {"pre": rules},
        **(objects or {}),
    }}}
    if other:
        folders["Shared"] = {"parent_folder": "All", "attached_snippets": {}, "config": other}
    return {"metadata": {}, "folders": folders, "snippets": {}}


RULE = DomainRule("LABCORP", "NEWCORP", "labcorp.local", "newcorp.local")


def test_replaces_netbios_upn_and_dn_forms_case_insensitively():
    doc = document([{"name": "r1", "action": "allow", "source_user": [
        f"labcorp{BS}jsmith", f"LabCorp{BS}VPN Users", "adoe@LABCORP.local",
        "cn=vpn,ou=groups,dc=labcorp, DC=local", "any"]}])
    new_doc, changes = replace_in_document(doc, RULE)
    users = new_doc["folders"]["Mobile Users"]["config"]["security/security-rules"]["pre"][0]["source_user"]
    assert users == [f"NEWCORP{BS}jsmith", f"NEWCORP{BS}VPN Users", "adoe@newcorp.local",
                     "cn=vpn,ou=groups,dc=newcorp,dc=local", "any"]
    assert len(changes) == 4
    assert changes[0].where == "folder 'Mobile Users' / security/security-rules [pre] 'r1' / source_user[0]"


def test_does_not_touch_names_fqdns_urls_or_lookalike_domains():
    rule = {"name": f"labcorp{BS}rule", "action": "allow", "description": "portal.labcorp.local",
            "source_user": [f"xlabcorp{BS}bob", "joe@labcorp.local.evil", "dc=labcorpx,dc=local"]}
    objects = {"objects/addresses": [{"name": "portal", "fqdn": "portal.labcorp.local"}],
               "security/url-categories": [{"name": "u", "list": ["*.labcorp.local"]}]}
    doc = document([rule], objects)
    new_doc, changes = replace_in_document(doc, RULE)
    assert changes == []
    assert new_doc == doc


def test_input_document_is_not_modified():
    doc = document([{"name": "r1", "source_user": [f"labcorp{BS}a"]}])
    before = json.dumps(doc)
    replace_in_document(doc, RULE)
    assert json.dumps(doc) == before


def test_only_selected_sources_are_changed():
    doc = document([{"name": "r1", "source_user": [f"labcorp{BS}a"]}],
                   other={"identity/authentication-profiles": [{"name": "p", "allow_list": [f"labcorp{BS}g"]}]})
    new_doc, changes = replace_in_document(doc, RULE, sources=["Mobile Users"])
    assert [c.container for c in changes] == ["folder 'Mobile Users'"]
    assert new_doc["folders"]["Shared"]["config"]["identity/authentication-profiles"][0]["allow_list"] == [f"labcorp{BS}g"]


def test_nested_fields_are_replaced():
    doc = document([], {"identity/authentication-profiles": [
        {"name": "p", "allow_list": [f"labcorp{BS}vpn"], "method": {"ldap": {"login_attribute": "adoe@labcorp.local"}}}]})
    new_doc, changes = replace_in_document(doc, RULE)
    profile = new_doc["folders"]["Mobile Users"]["config"]["identity/authentication-profiles"][0]
    assert profile["allow_list"] == [f"NEWCORP{BS}vpn"]
    assert profile["method"]["ldap"]["login_attribute"] == "adoe@newcorp.local"
    assert {c.where.rsplit(" / ", 1)[1] for c in changes} == {"allow_list[0]", "method.ldap.login_attribute"}


def test_netbios_only_when_no_dns_domain_given():
    doc = document([{"name": "r1", "source_user": [f"labcorp{BS}a", "a@labcorp.local"]}])
    new_doc, _ = replace_in_document(doc, DomainRule("labcorp", "newcorp"))
    assert new_doc["folders"]["Mobile Users"]["config"]["security/security-rules"]["pre"][0]["source_user"] == [
        f"newcorp{BS}a", "a@labcorp.local"]


def test_main_writes_output_and_lists_changes(tmp_path, capsys):
    src = tmp_path / "in.json"
    src.write_text(json.dumps(document([{"name": "r1", "source_user": [f"labcorp{BS}a"]}])), encoding="utf-8")
    out = tmp_path / "out.json"
    code = replace_domain.main(["--input", str(src), "--output", str(out),
                                "--old-domain", "labcorp", "--new-domain", "newcorp"])
    assert code == 0
    text = capsys.readouterr().out
    assert f"labcorp{BS}a  ->  newcorp{BS}a" in text and "1 value(s) changed" in text
    assert json.loads(out.read_text(encoding="utf-8"))["folders"]["Mobile Users"]["config"][
        "security/security-rules"]["pre"][0]["source_user"] == [f"newcorp{BS}a"]


def test_main_refuses_to_overwrite_the_input(tmp_path):
    src = tmp_path / "in.json"
    src.write_text(json.dumps(document([])), encoding="utf-8")
    assert replace_domain.main(["--input", str(src), "--output", str(src),
                                "--old-domain", "a", "--new-domain", "b"]) == 2


def test_dn_of_a_longer_domain_and_unc_paths_are_left_alone():
    values = ["cn=a,dc=labcorp,dc=local,dc=com", f"{BS}{BS}labcorp{BS}share", "cn=b,DC = labcorp , DC = local"]
    doc = document([{"name": "r1", "source_user": values}])
    new_doc, changes = replace_in_document(doc, RULE)
    users = new_doc["folders"]["Mobile Users"]["config"]["security/security-rules"]["pre"][0]["source_user"]
    assert users == ["cn=a,dc=labcorp,dc=local,dc=com", f"{BS}{BS}labcorp{BS}share", "cn=b,dc=newcorp,dc=local"]


def test_bare_domain_is_replaced_only_in_domain_fields():
    doc = document([], {"identity/authentication-profiles": [
        {"name": "p", "user_domain": "LABCORP", "description": "labcorp"}]})
    new_doc, changes = replace_in_document(doc, RULE)
    profile = new_doc["folders"]["Mobile Users"]["config"]["identity/authentication-profiles"][0]
    assert profile == {"name": "p", "user_domain": "NEWCORP", "description": "labcorp"}
    assert [c.kind for c in changes] == ["domain field"]


def test_server_profiles_are_excluded_by_default_and_can_be_included():
    ldap = {"name": "ldap", "base": "dc=labcorp,dc=local", "bind_dn": f"labcorp{BS}svc"}
    doc = document([], {"identity/ldap-server-profiles": [ldap]})
    unchanged, changes = replace_in_document(doc, RULE)
    assert changes == [] and unchanged == doc
    changed, changes = replace_in_document(doc, RULE, include_server_profiles=True)
    assert changed["folders"]["Mobile Users"]["config"]["identity/ldap-server-profiles"][0]["base"] == "dc=newcorp,dc=local"


def test_nested_name_fields_are_replaced_only_the_object_name_is_kept():
    doc = document([], {"identity/local-user-groups": [{"name": f"labcorp{BS}g", "members": [{"name": f"labcorp{BS}u"}]}]})
    new_doc, _ = replace_in_document(doc, RULE)
    group = new_doc["folders"]["Mobile Users"]["config"]["identity/local-user-groups"][0]
    assert group == {"name": f"labcorp{BS}g", "members": [{"name": f"NEWCORP{BS}u"}]}


@pytest.mark.parametrize("args, message", [
    (["--old-domain", "", "--new-domain", "b"], "old-domain"),
    (["--old-domain", "a", "--new-domain", "a"], "differ"),
    (["--old-domain", f"a{BS}", "--new-domain", "b"], "old-domain"),
    (["--old-domain", "a", "--new-domain", "b", "--old-dns", "@a.local", "--new-dns", "b.local"], "old-dns"),
    (["--old-domain", "a", "--new-domain", "b", "--old-dns", "a.local.", "--new-dns", "b.local"], "old-dns"),
])
def test_invalid_domain_arguments_are_refused(args, message, capsys):
    with pytest.raises(SystemExit):
        replace_domain.parse_args(["--input", "x", *args])
    assert message in capsys.readouterr().err


def test_main_prints_a_count_per_form(tmp_path, capsys):
    src = tmp_path / "in.json"
    src.write_text(json.dumps(document([{"name": "r1", "source_user": [f"labcorp{BS}a", "a@labcorp.local"]}])),
                   encoding="utf-8")
    replace_domain.main(["--input", str(src), "--old-domain", "labcorp", "--new-domain", "newcorp",
                         "--old-dns", "labcorp.local", "--new-dns", "newcorp.local"])
    out = capsys.readouterr().out
    assert "NetBIOS: 1" in out and "UPN: 1" in out and "LDAP DN: 0" in out


def test_dns_domains_must_be_given_together():
    with pytest.raises(SystemExit):
        replace_domain.parse_args(["--input", "x", "--old-domain", "a", "--new-domain", "b", "--old-dns", "a.local"])
