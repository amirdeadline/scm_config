"""Catalog of the snippet-capable SCM configuration endpoints.

Only endpoints that accept both `folder` and `snippet` scoping are listed, as
derived from the Strata Cloud Manager OpenAPI spec. Prisma Access-only
resources (remote networks, service connections, mobile-agent settings, ...)
are deliberately out of scope.

The list order is the import order: objects that others refer to come first,
rules come last.
"""

from dataclasses import dataclass
from typing import Any

# Containers that hold only Palo Alto Networks built-in content.
BUILTIN_FOLDERS = frozenset({"predefined", "Predefined"})
BUILTIN_SNIPPET_TYPES = frozenset({"predefined", "readonly"})
BUILTIN_SNIPPET_NAMES = frozenset({"predefined-snippet", "predefined"})

# The folder list calls the Prisma Access folder "Prisma Access", but the
# objects inside it carry folder "Shared".
FOLDER_ALIASES = {"Prisma Access": "Shared"}

# Fields SCM sets itself; they are removed on export and ignored when comparing.
# "position" is kept as the "pre"/"post" key of the rule lists instead.
SERVER_FIELDS = frozenset({"id", "folder", "snippet", "device", "position",
                           "created_at", "last_updated_at"})

# A rulebase lists one row per snippet applied to the folder (name = snippet
# name, no match criteria). Those rows only mark where the snippet's rules go.
PLACEHOLDER_RULE_FIELDS = frozenset({"id", "name", "folder", "snippet", "position", "policy_type"})

RULE_POSITIONS = ("pre", "post")


@dataclass(frozen=True)
class Endpoint:
    path: str
    rules: bool = False          # list with ?position=pre|post
    movable: bool = False        # supports /{id}:move to restore rule order
    key_field: str = "name"      # field that identifies an object inside one folder/snippet
    import_note: str = ""        # non-empty: exported but never imported, and why

    @property
    def key(self) -> str:
        """Short name used in the JSON file, e.g. 'objects/addresses'."""
        parts = self.path.strip("/").split("/")  # config, <category>, v1, <resource>
        return f"{parts[1]}/{parts[-1]}"

    @property
    def is_rulebase(self) -> bool:
        """Rule types, with or without pre/post; their lists hold snippet placeholder rows."""
        return self.path.endswith("-rules")

    @property
    def importable(self) -> bool:
        return not self.import_note


def _ep(path: str, **kwargs: Any) -> Endpoint:
    return Endpoint(path=path, **kwargs)


ENDPOINTS = (
    # --- variables and basic objects
    _ep("/config/setup/v1/variables"),
    _ep("/config/objects/v1/tags"),
    _ep("/config/objects/v1/addresses"),
    _ep("/config/objects/v1/address-groups"),
    _ep("/config/objects/v1/regions"),
    _ep("/config/objects/v1/services"),
    _ep("/config/objects/v1/service-groups"),
    _ep("/config/objects/v1/schedules"),
    _ep("/config/objects/v1/applications"),
    _ep("/config/objects/v1/application-filters"),
    _ep("/config/objects/v1/application-groups"),
    _ep("/config/objects/v1/dynamic-user-groups"),
    _ep("/config/objects/v1/external-dynamic-lists"),
    _ep("/config/objects/v1/hip-objects"),
    _ep("/config/objects/v1/hip-profiles"),
    _ep("/config/objects/v1/advanced-device-objects"),
    _ep("/config/security/v1/url-categories"),
    _ep("/config/security/v1/data-objects"),
    _ep("/config/security/v1/anti-spyware-signatures", key_field="threat_id"),
    _ep("/config/security/v1/vulnerability-protection-signatures", key_field="threat_id"),
    # --- log forwarding
    _ep("/config/objects/v1/syslog-server-profiles"),
    _ep("/config/objects/v1/http-server-profiles"),
    _ep("/config/objects/v1/log-forwarding-profiles"),
    # --- security profiles
    _ep("/config/security/v1/anti-spyware-profiles"),
    _ep("/config/security/v1/vulnerability-protection-profiles"),
    _ep("/config/security/v1/wildfire-anti-virus-profiles"),
    _ep("/config/security/v1/dns-security-profiles"),
    _ep("/config/security/v1/file-blocking-profiles"),
    _ep("/config/security/v1/url-access-profiles"),
    _ep("/config/security/v1/data-filtering-profiles"),
    _ep("/config/security/v1/http-header-profiles"),
    _ep("/config/security/v1/dos-protection-profiles"),
    _ep("/config/security/v1/decryption-profiles"),
    _ep("/config/security/v1/decryption-exclusions"),
    _ep("/config/security/v1/profile-groups"),
    _ep("/config/security/v1/ssl-decryption-settings",
        import_note="settings object without an id-based update API; re-apply manually"),
    _ep("/config/security/v1/url-admin-override",
        import_note="settings object without an id-based update API; re-apply manually"),
    _ep("/config/security/v1/saas-tenant-restrictions",
        import_note="no create API; re-apply manually"),
    # --- identity
    _ep("/config/identity/v1/certificates",
        import_note="certificates need their private key; import them manually"),
    _ep("/config/identity/v1/certificate-profiles"),
    _ep("/config/identity/v1/ocsp-responders"),
    _ep("/config/identity/v1/scep-profiles"),
    _ep("/config/identity/v1/tls-service-profiles"),
    _ep("/config/identity/v1/saml-server-profiles"),
    _ep("/config/identity/v1/ldap-server-profiles"),
    _ep("/config/identity/v1/radius-server-profiles"),
    _ep("/config/identity/v1/tacacs-server-profiles"),
    _ep("/config/identity/v1/kerberos-server-profiles"),
    _ep("/config/identity/v1/mfa-servers"),
    _ep("/config/identity/v1/authentication-profiles"),
    _ep("/config/identity/v1/authentication-sequences"),
    _ep("/config/identity/v1/local-users"),
    _ep("/config/identity/v1/local-user-groups"),
    _ep("/config/identity/v1/authentication-portals"),
    # --- network
    _ep("/config/network/v1/zone-protection-profiles"),
    _ep("/config/network/v1/interface-management-profiles"),
    _ep("/config/network/v1/lldp-profiles"),
    _ep("/config/network/v1/link-tags"),
    _ep("/config/network/v1/ethernet-interfaces"),
    _ep("/config/network/v1/aggregate-interfaces"),
    _ep("/config/network/v1/layer2-subinterfaces"),
    _ep("/config/network/v1/layer3-subinterfaces"),
    _ep("/config/network/v1/loopback-interfaces"),
    _ep("/config/network/v1/tunnel-interfaces"),
    _ep("/config/network/v1/vlan-interfaces"),
    _ep("/config/network/v1/zones"),
    _ep("/config/network/v1/dhcp-interfaces"),
    _ep("/config/network/v1/dns-proxies"),
    _ep("/config/network/v1/ike-crypto-profiles"),
    _ep("/config/network/v1/ipsec-crypto-profiles"),
    _ep("/config/network/v1/ike-gateways"),
    _ep("/config/network/v1/ipsec-tunnels"),
    _ep("/config/network/v1/route-access-lists"),
    _ep("/config/network/v1/route-prefix-lists"),
    _ep("/config/network/v1/route-community-lists"),
    _ep("/config/network/v1/route-path-access-lists"),
    _ep("/config/network/v1/ospf-auth-profiles"),
    _ep("/config/network/v1/bgp-auth-profiles"),
    _ep("/config/network/v1/bgp-address-family-profiles"),
    _ep("/config/network/v1/bgp-filtering-profiles"),
    _ep("/config/network/v1/bgp-redistribution-profiles"),
    _ep("/config/network/v1/bgp-route-maps"),
    _ep("/config/network/v1/bgp-route-map-redistributions"),
    _ep("/config/network/v1/logical-routers"),
    _ep("/config/network/v1/qos-profiles"),
    _ep("/config/network/v1/sdwan-path-quality-profiles"),
    _ep("/config/network/v1/sdwan-saas-quality-profiles"),
    _ep("/config/network/v1/sdwan-traffic-distribution-profiles"),
    _ep("/config/network/v1/sdwan-error-correction-profiles"),
    _ep("/config/network/v1/config-match-list"),
    _ep("/config/network/v1/globalprotect-match-list"),
    _ep("/config/network/v1/hipmatch-match-list"),
    _ep("/config/network/v1/iptag-match-list"),
    _ep("/config/network/v1/system-match-list"),
    _ep("/config/network/v1/userid-match-list"),
    # --- device settings (one object per folder/snippet)
    _ep("/config/device/v1/general-settings"),
    _ep("/config/device/v1/management-interface"),
    _ep("/config/device/v1/motd-banner-settings"),
    _ep("/config/device/v1/service-route"),
    _ep("/config/device/v1/service-settings"),
    _ep("/config/device/v1/session-settings"),
    _ep("/config/device/v1/session-timeouts"),
    _ep("/config/device/v1/tcp-settings"),
    _ep("/config/device/v1/update-schedule"),
    _ep("/config/device/v1/vpn-settings"),
    _ep("/config/device/v1/authentication-settings"),
    _ep("/config/device/v1/content-id-settings"),
    _ep("/config/device/v1/content-cloud-settings"),
    _ep("/config/device/v1/device-redistribution-collector"),
    _ep("/config/device/v1/autoscale",
        import_note="settings object without an id-based update API; re-apply manually"),
    # --- rules (last: they reference everything above)
    _ep("/config/security/v1/security-rules", rules=True, movable=True),
    _ep("/config/security/v1/decryption-rules", rules=True, movable=True),
    _ep("/config/security/v1/app-override-rules", rules=True, movable=True),
    _ep("/config/identity/v1/authentication-rules", rules=True, movable=True),
    _ep("/config/network/v1/nat-rules", rules=True),
    _ep("/config/network/v1/qos-policy-rules", rules=True, movable=True),
    _ep("/config/network/v1/pbf-rules"),
    _ep("/config/network/v1/sdwan-rules"),
    _ep("/config/security/v1/dos-protection-rules"),
)

ENDPOINTS_BY_KEY = {ep.key: ep for ep in ENDPOINTS}


def strip_server_fields(item: dict) -> dict:
    """Return a copy of an item without the fields SCM manages itself."""
    return {k: v for k, v in item.items() if k not in SERVER_FIELDS}


def is_builtin_snippet(snippet: dict, shared_in: frozenset = frozenset()) -> bool:
    """Palo Alto Networks snippets: type predefined, or read-only and not shared to us.

    A snippet shared from another tenant is also read-only here, but it is
    customer content, so it is not built-in.
    """
    if snippet.get("name") in BUILTIN_SNIPPET_NAMES:
        return True
    if snippet.get("name") in shared_in:
        return False
    return snippet.get("type") in BUILTIN_SNIPPET_TYPES


def is_owned_custom(item: dict, kind: str, name: str) -> bool:
    """True when the item is defined in this folder/snippet and is not built-in content.

    A folder or snippet query also returns what the container inherits: parent
    folders, built-in content, and the content of snippets applied to the
    folder. Those items carry another folder name or a snippet name, so only
    items whose own scope field names this container are kept.
    """
    if item.get("folder") in BUILTIN_FOLDERS or item.get("snippet") in BUILTIN_SNIPPET_NAMES:
        return False
    if set(item) <= {"count"}:  # some empty collections answer [{"count": 0}]
        return False
    if kind == "snippet":
        owner = item.get("snippet")
        return owner == name if owner is not None else item.get("folder") is None
    if item.get("snippet"):
        return False  # belongs to a snippet applied to this folder
    owner = item.get("folder")
    return owner is None or owner in (name, FOLDER_ALIASES.get(name))


def is_placeholder_rule(item: dict) -> bool:
    return set(item) <= PLACEHOLDER_RULE_FIELDS


def read_owned(client: Any, endpoint: Endpoint, kind: str, name: str) -> Any:
    """Custom items of one config type defined in one folder/snippet, as SCM returns them.

    Rule types return {"pre": [...], "post": [...]}. The position filter of the
    API is not reliable (snippet rules come back for both, some post rules for
    pre), so each rule goes to the rulebase named in its own "position" field and
    appears only once. Snippet placeholder rows are dropped.
    """
    params = {kind: name}
    if not endpoint.rules:
        return [i for i in client.get_all(endpoint.path, params) if is_owned_custom(i, kind, name)
                and not (endpoint.is_rulebase and is_placeholder_rule(i))]
    rules = {pos: [] for pos in RULE_POSITIONS}
    seen = set()
    for pos in RULE_POSITIONS:
        for item in client.get_all(endpoint.path, {**params, "position": pos}):
            if not is_owned_custom(item, kind, name) or is_placeholder_rule(item):
                continue
            identity = item.get("id") or item.get("name")
            if identity in seen:
                continue
            seen.add(identity)
            own = item.get("position")
            rules[own if own in RULE_POSITIONS else pos].append(item)
    return rules


def rulebase_is_reported(kind: str) -> bool:
    """False for snippets: SCM lists every snippet rule under both pre and post and
    gives it no "position" field, so a snippet rule's rulebase cannot be read back."""
    return kind != "snippet"


def match_key(item: dict, key_field: str = "name") -> str:
    """Identity of an object inside one folder/snippet and endpoint.

    Settings objects without a name (device settings, authentication portal)
    exist once per folder/snippet, so they share one fixed key.
    """
    value = item.get(key_field)
    return str(value) if value not in (None, "") else "__settings__"
