import pytest

import scm_client
from scm_client import ApiError, ScmClient, SettingsError, load_settings


def write_settings(tmp_path, client_id="sa@1.iam.panserviceaccount.com", secret="s3cret",
                   tsg="1234567890"):
    path = tmp_path / "settings.py"
    path.write_text(f'CLIENT_ID = "{client_id}"\nCLIENT_SECRET = "{secret}"\nTSG_ID = "{tsg}"\n')
    return path


def test_load_settings_returns_values(tmp_path):
    values = load_settings(write_settings(tmp_path))
    assert values == {"CLIENT_ID": "sa@1.iam.panserviceaccount.com", "CLIENT_SECRET": "s3cret",
                      "TSG_ID": "1234567890"}


def test_missing_settings_file_names_the_file(tmp_path):
    missing = tmp_path / "settings.py"
    with pytest.raises(SettingsError) as err:
        load_settings(missing)
    assert str(missing) in str(err.value)
    assert "edit the file" in str(err.value)


def test_empty_values_are_reported(tmp_path):
    with pytest.raises(SettingsError) as err:
        load_settings(write_settings(tmp_path, client_id="", secret=""))
    assert "CLIENT_ID" in str(err.value) and "CLIENT_SECRET" in str(err.value)


def test_non_numeric_tsg_is_rejected(tmp_path):
    with pytest.raises(SettingsError, match="TSG_ID must be numeric"):
        load_settings(write_settings(tmp_path, tsg="abc"))


def test_syntax_error_in_settings_is_reported(tmp_path):
    path = tmp_path / "settings.py"
    path.write_text("CLIENT_ID = ")
    with pytest.raises(SettingsError, match="could not be read"):
        load_settings(path)


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = str(payload)
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, token_response, responses=()):
        self.token_response = token_response
        self.responses = list(responses)
        self.requests = []

    def post(self, url, **kwargs):
        return self.token_response

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs.get("params")))
        return self.responses.pop(0)


def make_client(responses=(), token_status=200):
    token = FakeResponse(token_status, {"access_token": "t", "expires_in": 900})
    return ScmClient("id", "secret", "123", session=FakeHttp(token, responses))


def test_rejected_credentials_raise_settings_error():
    client = make_client(token_status=401)
    with pytest.raises(SettingsError, match="rejected"):
        client.authenticate()


def test_get_all_follows_paging(monkeypatch):
    monkeypatch.setattr(scm_client, "PAGE_LIMIT", 2)
    client = make_client([
        FakeResponse(200, {"data": [{"n": 1}, {"n": 2}], "total": 3}),
        FakeResponse(200, {"data": [{"n": 3}], "total": 3}),
    ])
    assert [i["n"] for i in client.get_all("/x", {"folder": "F"})] == [1, 2, 3]
    offsets = [params["offset"] for _, _, params in client.http.requests]
    assert offsets == [0, 2]


def test_get_all_accepts_bare_list_response():
    client = make_client([FakeResponse(200, [{"n": 1}])])
    assert client.get_all("/x") == [{"n": 1}]


def test_request_retries_rate_limit(monkeypatch):
    monkeypatch.setattr(scm_client.time, "sleep", lambda s: None)
    client = make_client([FakeResponse(429, {}, {"Retry-After": "1"}), FakeResponse(200, {"ok": 1})])
    assert client.request("GET", "/x") == {"ok": 1}


def test_request_raises_api_error_with_message():
    client = make_client([FakeResponse(400, {"_errors": [{"message": "Invalid Object",
                                                          "details": {"message": "bad ref"}}]})])
    with pytest.raises(ApiError) as err:
        client.request("POST", "/x", body={})
    assert err.value.status == 400
    assert err.value.message == "Invalid Object bad ref"


def test_post_is_not_retried_after_server_error(monkeypatch):
    monkeypatch.setattr(scm_client.time, "sleep", lambda s: None)
    client = make_client([FakeResponse(502, {"_errors": [{"message": "bad gateway"}]}),
                          FakeResponse(201, {"id": "1"})])
    with pytest.raises(ApiError) as err:
        client.request("POST", "/x", body={"name": "a"})
    assert err.value.status == 502
    assert len(client.http.requests) == 1


def test_token_refresh_network_error_is_retried_even_for_post(monkeypatch):
    # Nothing was sent to SCM yet, so retrying a POST is safe here.
    monkeypatch.setattr(scm_client.time, "sleep", lambda s: None)
    client = make_client([FakeResponse(201, {"id": "1"})])
    token = client.http.token_response
    outcomes = [scm_client.requests.ConnectionError("dns failed"), token]

    def flaky_post(url, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client.http.post = flaky_post
    assert client.request("POST", "/x", body={"name": "a"}) == {"id": "1"}
    assert len(client.http.requests) == 1


def test_error_message_handles_unexpected_shapes():
    assert ApiError("GET", "/x", 400, {"_errors": ["plain"]}).message == "plain"
    assert ApiError("GET", "/x", 400, "text body").message == "text body"
