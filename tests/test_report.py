import csv
import json

import pytest

from scm_import_plan import merge_sources, plan_changes
from scm_report import REPORT_FORMATS, field_changes, report_format, write_report

BS = "\\"


@pytest.fixture
def planned():
    desired = merge_sources([("folder", "Mobile Users", {
        "objects/addresses": [{"name": "web", "ip_netmask": "10.0.0.2/32"}, {"name": "new", "fqdn": "a.example"}],
        "identity/certificates": [{"name": "cert"}],
        "security/security-rules": {"pre": [{"name": "r1", "action": "allow", "source_user": [f"newcorp{BS}a"]},
                                            {"name": "r2", "action": "allow"}]},
    })])
    current = {
        ("objects/addresses", None): {"web": {"id": "1", "name": "web", "folder": "Mobile Users",
                                              "ip_netmask": "10.0.0.1/32", "description": "old"}},
        ("security/security-rules", "pre"): {
            "r1": {"id": "2", "name": "r1", "action": "allow", "source_user": [f"labcorp{BS}a"]},
            "r2": {"id": "3", "name": "r2", "action": "allow"}},
    }
    return desired, plan_changes(desired, current)


def test_format_follows_the_file_extension():
    assert report_format("diff.HTML") == "html"
    assert report_format("x.csv") == "csv"
    assert report_format("x.unknown") == "text"
    assert set(REPORT_FORMATS.values()) == {"text", "markdown", "html", "json", "csv"}


def test_field_changes_lists_changed_added_and_removed_fields(planned):
    _, changes = planned
    web = next(c for c in changes if c.name == "web")
    assert field_changes(web) == [("description", "old", None), ("ip_netmask", "10.0.0.1/32", "10.0.0.2/32")]


def test_field_set_to_null_in_scm_is_listed_as_a_change():
    desired = merge_sources([("folder", "F", {"objects/addresses": [{"name": "a", "fqdn": "x"}]})])
    current = {("objects/addresses", None): {"a": {"id": "1", "name": "a", "fqdn": "x", "description": None}}}
    change = plan_changes(desired, current)[0]
    assert change.action == "update"
    assert field_changes(change) == [("description", None, None)]


def test_csv_cells_that_look_like_formulas_are_neutralised(tmp_path):
    desired = merge_sources([("folder", "F", {"objects/addresses": [{"name": "a", "description": "=HYPERLINK(1)"}]})])
    current = {("objects/addresses", None): {"a": {"id": "1", "name": "a", "description": "-old"}}}
    path = tmp_path / "d.csv"
    write_report(path, plan_changes(desired, current), desired, "x")
    row = next(r for r in csv.DictReader(path.open(encoding="utf-8-sig")) if r["field"] == "description")
    assert (row["current"], row["new"]) == ("'-old", "'=HYPERLINK(1)")


def test_json_report_has_summary_fields_and_full_objects(tmp_path, planned):
    desired, changes = planned
    path = tmp_path / "diff.json"
    write_report(path, changes, desired, "folder 'Mobile Users'")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["target"] == "folder 'Mobile Users'"
    assert data["summary"] == {"create": 1, "overwrite": 2, "unchanged": 1, "skip": 1}
    rule = next(c for c in data["changes"] if c["name"] == "r1")
    assert rule["action"] == "overwrite" and rule["rulebase"] == "pre"
    assert rule["fields"] == [{"field": "source_user", "current": [f"labcorp{BS}a"], "new": [f"newcorp{BS}a"]}]
    assert rule["current"]["source_user"] == [f"labcorp{BS}a"] and "id" not in rule["current"]
    assert data["unchanged"] == ["security/security-rules [pre] 'r2'"]


def test_csv_report_has_one_row_per_changed_field(tmp_path, planned):
    desired, changes = planned
    path = tmp_path / "diff.csv"
    write_report(path, changes, desired, "folder 'Mobile Users'")
    rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
    overwrite_rows = [(r["name"], r["field"], r["current"], r["new"]) for r in rows if r["action"] == "overwrite"]
    assert ("r1", "source_user", f'["labcorp{BS*2}a"]', f'["newcorp{BS*2}a"]') in overwrite_rows
    assert ("web", "ip_netmask", "10.0.0.1/32", "10.0.0.2/32") in overwrite_rows
    assert any(r["action"] == "skip" and r["name"] == "cert" and r["note"] for r in rows)
    assert any(r["action"] == "create" and r["name"] == "new" for r in rows)


def test_html_report_is_escaped_and_shows_diff(tmp_path, planned):
    desired, changes = planned
    path = tmp_path / "diff.html"
    write_report(path, changes, desired, "folder '<Mobile Users>'")
    html = path.read_text(encoding="utf-8")
    assert "&lt;Mobile Users&gt;" in html and "<Mobile Users>" not in html
    assert 'class="del"' in html and 'class="add"' in html
    assert "labcorp" in html and "newcorp" in html


def test_markdown_fence_survives_backticks_in_values(tmp_path):
    desired = merge_sources([("folder", "F", {"objects/addresses": [{"name": "a", "description": "x ``` y"}]})])
    write_report(tmp_path / "d.md", plan_changes(desired, {}), desired, "x")
    assert "````json" in (tmp_path / "d.md").read_text(encoding="utf-8")


def test_markdown_and_text_reports(tmp_path, planned):
    desired, changes = planned
    write_report(tmp_path / "diff.md", changes, desired, "folder 'Mobile Users'")
    write_report(tmp_path / "diff.txt", changes, desired, "folder 'Mobile Users'")
    markdown = (tmp_path / "diff.md").read_text(encoding="utf-8")
    assert "| overwrite | 2 |" in markdown and "```diff" in markdown
    assert "~ OVERWRITE security/security-rules [pre] 'r1'" in (tmp_path / "diff.txt").read_text(encoding="utf-8")
