"""Strata Cloud Manager API client shared by config_export.py and config_import.py.

Loads the service account from settings.py, gets and refreshes the OAuth2 token,
and wraps the configuration API with paging and retry on rate limits.
"""

import importlib.util
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

TOKEN_URL = "https://auth.apps.paloaltonetworks.com/oauth2/access_token"
API_BASE = "https://api.strata.paloaltonetworks.com"
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.py"

# SCM answers a large page about as fast as a small one (some collections,
# like applications with ~11k built-in App-IDs, take ~20 s per request), so
# pages are large and the remaining pages are fetched in parallel.
PAGE_LIMIT = 1000
FALLBACK_PAGE_LIMIT = 200    # used when SCM refuses PAGE_LIMIT for a collection
MAX_CONCURRENT_REQUESTS = 8
REQUEST_TIMEOUT = 60
TOKEN_REFRESH_MARGIN = 60
MAX_RETRIES = 5
# Some config types answer every read with an empty HTTP 500 in folders where
# they do not apply; a plain 500 is therefore retried only once.
MAX_500_ATTEMPTS = 2
RETRY_STATUSES = {429, 500, 502, 503, 504}
# A POST that timed out or failed with 5xx may still have been applied, so
# POST is only retried when SCM clearly refused it (rate limit).
POST_RETRY_STATUSES = {429}
REQUIRED_SETTINGS = ("CLIENT_ID", "CLIENT_SECRET", "TSG_ID")

FOLDERS_PATH = "/config/setup/v1/folders"
SNIPPETS_PATH = "/config/setup/v1/snippets"
SHARED_SNIPPETS_PATH = "/config/setup/v1/shared-snippets"

LOG_DIR = Path(__file__).resolve().parent / "logs"

log = logging.getLogger("scm")


def setup_logging(log_name: str, verbose: bool = False) -> Path:
    """Console at INFO (DEBUG with --verbose), full DEBUG log in logs/<log_name>.log."""
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"{log_name}.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(file_handler)
    root.addHandler(console)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file


class SettingsError(Exception):
    """settings.py is missing, incomplete, or holds credentials SCM rejects."""


class ApiError(Exception):
    """An SCM API call returned an error status."""

    def __init__(self, method, path, status, body):
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} failed with HTTP {status}: {body}")

    @property
    def message(self):
        """Best-effort human readable error text from the SCM error envelope."""
        errors = self.body.get("_errors") if isinstance(self.body, dict) else None
        if isinstance(errors, list) and errors:
            texts = []
            for err in errors:
                if not isinstance(err, dict):
                    texts.append(str(err))
                    continue
                details = err.get("details")
                if isinstance(details, dict):
                    details = details.get("message") or details.get("errors") or ""
                texts.append(f"{err.get('message', '')} {details or ''}".strip())
            return "; ".join(texts)
        return str(self.body) if self.body else f"HTTP {self.status} with an empty response"

    @property
    def is_empty_server_error(self):
        return self.status == 500 and not self.body


def settings_help(reason, settings_file=SETTINGS_FILE):
    return (
        f"{reason}\n"
        f"You need to edit the file {settings_file} and set the SCM service account "
        f"info (CLIENT_ID, CLIENT_SECRET) and the TSG_ID of your tenant."
    )


def load_settings(settings_file=SETTINGS_FILE):
    """Read CLIENT_ID, CLIENT_SECRET and TSG_ID from settings.py and validate them."""
    settings_file = Path(settings_file)
    if not settings_file.is_file():
        raise SettingsError(settings_help("Settings file not found.", settings_file))
    spec = importlib.util.spec_from_file_location("scm_settings", settings_file)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # syntax errors etc. in a user-edited file
        raise SettingsError(settings_help(f"Settings file could not be read: {exc}", settings_file))

    values = {}
    missing = []
    for key in REQUIRED_SETTINGS:
        value = str(getattr(module, key, "") or "").strip()
        if not value or value.startswith("<"):
            missing.append(key)
        values[key] = value
    if missing:
        raise SettingsError(settings_help(f"Missing value(s) in settings: {', '.join(missing)}.",
                                          settings_file))
    if not values["TSG_ID"].isdigit():
        raise SettingsError(settings_help(f"TSG_ID must be numeric, got '{values['TSG_ID']}'.",
                                          settings_file))
    return values


class ScmClient:
    """Minimal SCM configuration API client with token refresh, paging and retries."""

    def __init__(self, client_id, client_secret, tsg_id, settings_file=SETTINGS_FILE,
                 session=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.tsg_id = tsg_id
        self.settings_file = settings_file
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_maxsize=MAX_CONCURRENT_REQUESTS * 2)
            session.mount("https://", adapter)
        self.http = session
        self._token = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

    @classmethod
    def from_settings(cls, settings_file=SETTINGS_FILE):
        values = load_settings(settings_file)
        client = cls(values["CLIENT_ID"], values["CLIENT_SECRET"], values["TSG_ID"],
                     settings_file=settings_file)
        client.authenticate()
        return client

    # ---------------------------------------------------------------- auth
    def authenticate(self):
        try:
            response = self.http.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials", "scope": f"tsg_id:{self.tsg_id}"},
                auth=(self.client_id, self.client_secret),
                headers={"Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ConnectionError(f"Could not reach the SCM auth service {TOKEN_URL}: {exc}")
        if response.status_code in (400, 401, 403):
            raise SettingsError(settings_help(
                f"SCM rejected the service account credentials or TSG ID "
                f"(HTTP {response.status_code}: {response.text.strip()[:200]}).",
                self.settings_file))
        if response.status_code != 200:
            raise ConnectionError(f"Token request failed: HTTP {response.status_code} {response.text[:200]}")
        token = response.json()
        self._token = token["access_token"]
        self._token_expires_at = time.time() + int(token.get("expires_in", 900))
        log.debug("Access token acquired, expires in %ss", token.get("expires_in"))

    def _auth_header(self):
        with self._token_lock:
            if not self._token or time.time() > self._token_expires_at - TOKEN_REFRESH_MARGIN:
                self.authenticate()
            return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    # ---------------------------------------------------------------- http
    def request(self, method, path, params=None, body=None):
        url = API_BASE + path
        retry_statuses = POST_RETRY_STATUSES if method == "POST" else RETRY_STATUSES
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                headers = self._auth_header()
                with self._slots:
                    response = self.http.request(method, url, params=params, json=body,
                                                 headers=headers, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES or method == "POST":
                    raise ConnectionError(f"{method} {path} failed: {exc}")
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 401 and attempt == 1:
                self._token = None  # token revoked or expired early: refresh once
                continue
            last_try = attempt >= (MAX_500_ATTEMPTS if response.status_code == 500 else MAX_RETRIES)
            if response.status_code in retry_statuses and not last_try:
                wait = response.headers.get("Retry-After")
                delay = float(wait) if wait and wait.isdigit() else 2 ** attempt
                log.debug("%s %s -> HTTP %s, retrying in %ss", method, path, response.status_code, delay)
                time.sleep(delay)
                continue
            log.debug("%s %s %s -> HTTP %s", method, path, params or "", response.status_code)
            if response.status_code >= 400:
                try:
                    payload = response.json()
                except ValueError:
                    payload = response.text
                raise ApiError(method, path, response.status_code, payload)
            if not response.content:
                return None
            return response.json()
        raise ConnectionError(f"{method} {path} failed after {MAX_RETRIES} attempts")

    def get_all(self, path, params=None):
        """GET every item of a collection.

        The first page gives the total; the remaining pages are then fetched in
        parallel. Without a total, pages are read one after another.
        """
        params = dict(params or {})
        first, limit = self._first_page(path, params)
        if isinstance(first, list):  # some endpoints return a bare list without paging
            return first
        items = list((first or {}).get("data") or [])
        total = (first or {}).get("total")
        step = len(items)  # SCM may cap the page size below the requested limit
        if not step or (total is not None and step >= total):
            return items
        if total is None:
            return items + self._sequential_pages(path, params, step, limit)
        offsets = list(range(step, total, step))
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_REQUESTS) as pool:
            pages = pool.map(lambda off: self._page(path, params, step, off), offsets)
            for data in pages:
                items.extend(data)
        return items

    def _first_page(self, path, params):
        try:
            return self.request("GET", path, params={**params, "limit": PAGE_LIMIT, "offset": 0}), PAGE_LIMIT
        except ApiError as exc:
            if exc.status not in (400, 413, 502) or PAGE_LIMIT <= FALLBACK_PAGE_LIMIT:
                raise
            log.debug("%s refused limit %s (%s), using %s", path, PAGE_LIMIT, exc.status,
                      FALLBACK_PAGE_LIMIT)
            page = self.request("GET", path, params={**params, "limit": FALLBACK_PAGE_LIMIT, "offset": 0})
            return page, FALLBACK_PAGE_LIMIT

    def _page(self, path, params, limit, offset):
        page = self.request("GET", path, params={**params, "limit": limit, "offset": offset})
        return (page or {}).get("data") or [] if isinstance(page, dict) else (page or [])

    def _sequential_pages(self, path, params, step, limit):
        items = []
        offset = step
        while True:
            data = self._page(path, params, limit, offset)
            items.extend(data)
            offset += len(data)
            if len(data) < step:
                return items

    def create(self, path, body, params=None):
        return self.request("POST", path, params=params, body=body)

    def update(self, path, object_id, body):
        return self.request("PUT", f"{path}/{object_id}", body=body)

    def move_rule(self, path, rule_id, rulebase, destination, destination_rule=None):
        body = {"destination": destination, "rulebase": rulebase}
        if destination_rule:
            body["destination_rule"] = destination_rule
        return self.request("POST", f"{path}/{rule_id}:move", body=body)

    # ------------------------------------------------------------ containers
    def list_folders(self):
        return self.get_all(FOLDERS_PATH)

    def list_snippets(self):
        return self.get_all(SNIPPETS_PATH)

    def shared_in_snippet_names(self):
        """Names of snippets shared to this tenant by another tenant (read-only here)."""
        try:
            shares = self.request("GET", SHARED_SNIPPETS_PATH)
        except ApiError as exc:
            log.debug("Cannot read shared snippets: %s", exc.message)
            return frozenset()
        if isinstance(shares, dict):
            shares = shares.get("data") or []
        return frozenset(s.get("snippet_name") for s in shares or []
                         if isinstance(s, dict) and str(s.get("recipient_tsg")) == str(self.tsg_id))

    def create_snippet(self, name, description=""):
        return self.create(SNIPPETS_PATH, {"name": name, "description": description})
