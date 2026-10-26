import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scm_client import ApiError  # noqa: E402


class FakeScm:
    """In-memory stand-in for ScmClient with folder/snippet scoping and rule positions."""

    def __init__(self, folders=None, snippets=None, items=None, unsupported=None):
        self.tsg_id = "1234567890"
        self.folders = folders or []
        self.snippets = snippets or []
        self.items = {path: list(values) for path, values in (items or {}).items()}
        self.unsupported = unsupported or {}   # path -> HTTP status to raise
        self.fail_once = set()                 # names whose first create fails
        self.shared_in = set()                 # snippet names shared from another tenant
        self.snippet_rules_in_both = False     # live SCM: snippet rules ignore the position filter
        self.calls = []

    def list_folders(self):
        return self.folders

    def list_snippets(self):
        return self.snippets

    def shared_in_snippet_names(self):
        return frozenset(self.shared_in)

    def create_snippet(self, name, description=""):
        self.calls.append(("create_snippet", name))
        self.snippets.append({"name": name, "id": str(uuid.uuid4()), "type": "custom"})

    def get_all(self, path, params=None):
        if path in self.unsupported:
            raise ApiError("GET", path, self.unsupported[path], {"_errors": [{"message": "nope"}]})
        params = params or {}
        result = []
        for item in self.items.get(path, []):
            any_position = self.snippet_rules_in_both and "snippet" in params
            if "position" in params and not any_position and item.get("_position", "pre") != params["position"]:
                continue
            scope = params.get("folder") or params.get("snippet")
            visible = item.get("_visible_in", [item.get("folder") or item.get("snippet")])
            if scope in visible:
                result.append({k: v for k, v in item.items() if not k.startswith("_")})
        return result

    def create(self, path, body, params=None):
        self.calls.append(("create", path, body.get("name"), (params or {}).get("position")))
        if body.get("name") in self.fail_once:
            self.fail_once.discard(body["name"])
            raise ApiError("POST", path, 400, {"_errors": [{"message": "reference not found"}]})
        stored = {**body, "id": str(uuid.uuid4())}
        if params and params.get("position"):
            stored["_position"] = params["position"]
        self.items.setdefault(path, []).append(stored)
        return stored

    def update(self, path, object_id, body, params=None):
        self.calls.append(("update", path, body.get("name"), params))
        for index, item in enumerate(self.items.get(path, [])):
            if item["id"] == object_id:
                extra = {k: v for k, v in item.items() if k.startswith("_")}
                self.items[path][index] = {**body, "id": object_id, **extra}
                return self.items[path][index]
        raise ApiError("PUT", path, 404, {"_errors": [{"message": "not found"}]})

    def move_rule(self, path, rule_id, rulebase, destination, destination_rule=None):
        self.calls.append(("move", path, rule_id, destination, destination_rule))
        rules = self.items[path]
        rule = next(r for r in rules if r["id"] == rule_id)
        rules.remove(rule)
        anchor = next(i for i, r in enumerate(rules) if r["id"] == destination_rule)
        rules.insert(anchor + 1 if destination == "after" else anchor, rule)


@pytest.fixture
def fake_scm():
    return FakeScm
