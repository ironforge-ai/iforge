import json
import os
import re
from ipaddress import AddressValueError, IPv6Address
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit, urlunsplit

import idna
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

# The vault keyring's names (plan 06-25). `VAULT_ENCRYPTION_KEY` is always this version; a key
# version fits the `key_version` column (String(32)) of the versioned secret tables.
VAULT_LEGACY_KEY_VERSION = "vault.v1"
VAULT_KEY_VERSION_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
VAULT_HEX_KEY_PATTERN = re.compile(r"[0-9a-fA-F]{64}")


def _normalize_pem_env_value(value: str) -> str:
    # Environment transports can preserve escaped newlines either as literal
    # "\n" pairs or as a line-continuation backslash followed by a real newline.
    # Build the latter pattern explicitly so Python source line-continuation
    # rules cannot change the string we are matching.
    return value.replace("\\" + "\r\n", "\n").replace("\\" + "\n", "\n").replace("\\n", "\n")


def _canonical_clerk_url(value: str, *, origin_only: bool, label: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError as error:
        raise ValueError(f"{label} must be a valid absolute URL") from error
    host = parsed.hostname
    if not host or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} must be a URL without userinfo")
    try:
        if ":" in host:
            canonical_host = IPv6Address(host).compressed
            rendered_host = f"[{canonical_host}]"
        else:
            canonical_host = idna.encode(host, uts46=True, std3_rules=True).decode("ascii").lower()
            if canonical_host.endswith("."):
                raise ValueError(f"{label} contains an invalid host")
            rendered_host = canonical_host
    except (AddressValueError, idna.IDNAError, UnicodeError) as error:
        raise ValueError(f"{label} contains an invalid host") from error
    scheme = parsed.scheme.lower()
    loopback = canonical_host in {"localhost", "127.0.0.1", "::1"}
    if scheme != "https" and not (scheme == "http" and loopback):
        raise ValueError(f"{label} must use HTTPS (HTTP is allowed only for exact loopback hosts)")
    if parsed.query or parsed.fragment or "?" in raw or "#" in raw:
        raise ValueError(f"{label} must not contain query parameters or a fragment")
    if origin_only:
        if parsed.path not in {"", "/"}:
            raise ValueError(f"{label} must be an origin without a path")
        path = ""
    else:
        path = parsed.path.rstrip("/")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} contains an invalid port") from error
    default_port = 443 if scheme == "https" else 80
    netloc = (
        f"{rendered_host}:{port}" if port is not None and port != default_port else rendered_host
    )
    return urlunsplit((scheme, netloc, path, "", ""))


def canonical_clerk_issuer(value: str) -> str:
    return _canonical_clerk_url(value, origin_only=True, label="Clerk issuer")


def canonical_clerk_authorized_party(value: str) -> str:
    return _canonical_clerk_url(value, origin_only=True, label="Clerk OAuth authorized party")


# The loopback dashboard. `apps/web` binds 3000 by default and every doc says
# so, which is what makes it the origin worth protecting in development.
LOCAL_DASHBOARD_ORIGIN = "http://localhost:3000"


class Settings(BaseSettings):
    # `hide_input_in_errors`: a startup validation error must never print the offending input --
    # for the keyring, and for every other secret here, that input is the key itself.
    model_config = {"env_file": ".env", "extra": "ignore", "hide_input_in_errors": True}

    @field_validator("vault_encryption_key", "encryption_key", mode="before")
    @classmethod
    def _strip_dotenv_comment_placeholders(cls, v: object) -> object:
        # Some .env parsers (including pydantic-settings' dotenv reader) greedily
        # swallow the trailing comment when a value line looks like
        #   VAULT_ENCRYPTION_KEY=  # Generate with: ...
        # producing the literal string "# Generate with: ..." as the value.
        # That passes hex-decoding later with a cryptic error. Normalise it
        # back to the empty string so downstream code treats the key as
        # "not configured" and fails loudly at first use.
        if isinstance(v, str) and v.strip().startswith("#"):
            return ""
        return v

    @field_validator(
        "clerk_secret_key",
        "clerk_pem_public_key",
        "clerk_jwt_issuer",
        "clerk_jwt_audience",
        "clerk_webhook_signing_secret",
        mode="before",
    )
    @classmethod
    def _strip_wrapping_quotes(cls, v: object) -> object:
        # Deployment dashboards can round-trip secret values with literal
        # surrounding quotes (e.g. `'sk_test_...'`). When that env reaches us
        # raw, the quotes end up baked into the Authorization header / JWT public
        # key and Clerk rejects the request with a confusing 401 or signature-
        # verification error. Strip a single matched pair on load so downstream
        # code never has to think about it.
        if isinstance(v, str) and len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if isinstance(v, str) and "BEGIN PUBLIC KEY" in v:
            return _normalize_pem_env_value(v)
        return v

    @field_validator("companion_default_spend_ceiling_usd_micros", mode="before")
    @classmethod
    def _empty_spend_ceiling_is_unset(cls, v: object) -> object:
        # `.env.example` ships `COMPANION_DEFAULT_SPEND_CEILING_USD_MICROS=` with no value. An
        # empty line means "no operator default", never zero and never a startup error: zero would
        # refuse every tenant without a ceiling of its own.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("companion_temporal_jwt_private_key", mode="before")
    @classmethod
    def _normalize_temporal_signing_key(cls, v: object) -> object:
        # A PEM that crossed a single-line env transport arrives with escaped newlines.
        if isinstance(v, str) and "PRIVATE KEY" in v:
            return _normalize_pem_env_value(v.strip())
        return v

    @field_validator("clerk_pem_public_key")
    @classmethod
    def decode_pem_public_key(cls, value: str) -> str:
        """Accept the PEM either verbatim or base64-encoded.

        Multi-line PEM cannot survive dotenv/env-file transports intact,
        so deployments provide it base64-encoded on a single line.
        """
        candidate = value.strip()
        if not candidate:
            return candidate
        if "-----BEGIN" in candidate:
            return value

        import base64
        import binascii

        try:
            return base64.b64decode(candidate, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return value  # not base64 (e.g. test fixtures); leave unchanged

    @field_validator("memory_embedding_service_socket_path")
    @classmethod
    def _validate_memory_embedding_service_socket_path(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or "\x00" in value
            or not os.path.isabs(value)
            or os.path.normpath(value) != value
            or len(os.fsencode(value)) > 103
            or os.path.basename(value) != "embedding.sock"
        ):
            raise ValueError(
                "memory_embedding_service_socket_path must be a bounded absolute "
                "normalized embedding.sock path"
            )
        return value

    @field_validator("clerk_jwt_issuer")
    @classmethod
    def _canonicalize_clerk_issuer(cls, value: str) -> str:
        return canonical_clerk_issuer(value)

    @field_validator("user_prov")
    @classmethod
    def _reject_unimplemented_user_prov(cls, value: str) -> str:
        # "supa" is a legal member of the D-02 seam vocabulary but has no
        # implementation, and NotImplementedError reaching a request path is
        # the same half-applied-delete failure as an outright typo (WR-09).
        # Phase 3.1 deletes this arm when the provider lands.
        if value == "supa":
            raise ValueError(
                "user_prov='supa' is the self-hosted Supabase identity provider and is "
                "not implemented yet — it lands in Phase 3.1; configure user_prov='clerk' "
                "until then"
            )
        return value

    @model_validator(mode="after")
    def _normalize_loaded_env_values(self) -> Self:
        if "BEGIN PUBLIC KEY" in self.clerk_pem_public_key:
            self.clerk_pem_public_key = _normalize_pem_env_value(self.clerk_pem_public_key)
        if (
            self.runtime_observation_hard_retention_days
            < self.runtime_observation_replay_horizon_days
        ):
            raise ValueError(
                "runtime_observation_hard_retention_days must be greater than or equal to "
                "runtime_observation_replay_horizon_days"
            )
        return self

    @field_validator("vault_encryption_keys", mode="before")
    @classmethod
    def _parse_vault_encryption_keys(cls, v: object) -> object:
        # `.env.example` ships `VAULT_ENCRYPTION_KEYS=` empty, which means "no keys beyond the
        # legacy one", never a JSON parse error at startup. `NoDecode` hands us the raw string.
        if isinstance(v, str):
            text = v.strip()
            if not text or text.startswith("#"):
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # No excerpt of the value: it is key material.
                raise ValueError("VAULT_ENCRYPTION_KEYS must be a JSON object") from None
        return v

    @model_validator(mode="after")
    def _validate_vault_keyring(self) -> Self:
        """The keyring is checked at startup; every message names versions, never key material.

        ``VAULT_ENCRYPTION_KEY`` IS ``vault.v1``: a ``VAULT_ENCRYPTION_KEYS`` entry for
        ``vault.v1`` that disagrees with it would make the legacy and versioned paths read the
        same rows with different keys, so it refuses to start rather than pick one.
        """
        for version, hex_key in self.vault_encryption_keys.items():
            if not VAULT_KEY_VERSION_PATTERN.fullmatch(version):
                raise ValueError(
                    "VAULT_ENCRYPTION_KEYS versions must be lowercase names of at most 32 "
                    "characters (letters, digits, '.', '_' or '-')"
                )
            if not VAULT_HEX_KEY_PATTERN.fullmatch(hex_key.strip()):
                raise ValueError(
                    f"VAULT_ENCRYPTION_KEYS[{version!r}] must be 32 bytes (64 hex characters)"
                )
        legacy_entry = self.vault_encryption_keys.get(VAULT_LEGACY_KEY_VERSION)
        if legacy_entry is not None and (
            legacy_entry.strip().lower() != self.vault_encryption_key.strip().lower()
        ):
            raise ValueError(
                f"VAULT_ENCRYPTION_KEYS[{VAULT_LEGACY_KEY_VERSION!r}] disagrees with "
                f"VAULT_ENCRYPTION_KEY, which is always {VAULT_LEGACY_KEY_VERSION}"
            )
        active = self.vault_active_key_version
        if active != VAULT_LEGACY_KEY_VERSION and active not in self.vault_encryption_keys:
            raise ValueError(
                f"VAULT_ACTIVE_KEY_VERSION {active!r} is not in the keyring: add it to "
                "VAULT_ENCRYPTION_KEYS first"
            )
        return self

    @model_validator(mode="after")
    def _keep_local_dashboard_reachable_in_development(self) -> Self:
        """In development only, union the loopback dashboard origin in.

        `CORS_ORIGINS` REPLACES the default rather than extending it, so adding
        one origin silently removes every other — and the one most often added
        locally is a LAN address, for a phone or another machine on the network.
        The result is a checkout whose dashboard cannot call its own backend,
        failing as "We couldn't reach the service" on every panel while auth
        still looks fine: the access gate is server-rendered and never crosses
        an origin, so the one surface that would name the cause is the one
        surface CORS does not touch.

        Development only, and the guard is the point. Staging and production get
        exactly the list they configured — a backend that silently trusted
        localhost would accept a browser on the same host as any deployment, and
        "the dev default leaked into prod" is the failure this narrowness
        exists to prevent.
        """
        if self.environment != "development":
            return self
        if LOCAL_DASHBOARD_ORIGIN not in self.cors_origins:
            self.cors_origins = [*self.cors_origins, LOCAL_DASHBOARD_ORIGIN]
        return self

    app_name: str = "clawdi"
    environment: str = "development"  # development | staging | production
    debug: bool = False
    # Kill switch for recall counting (Memory.access_count++ on agent
    # ranked search, run as a background task). Flip to false via env
    # if the extra write per search ever needs to go away without a
    # deploy. See app/services/memory_recall.py.
    memory_recall_counting: bool = True
    # Browser origins allowed to call this backend. Production lists exactly
    # what it means; development additionally keeps the loopback dashboard
    # reachable — see `_keep_local_dashboard_reachable_in_development`.
    cors_origins: list[str] = ["http://localhost:3000"]

    # Externally reachable URL for THIS backend. Used when the backend embeds
    # its own URL into payloads it hands to other processes (MCP client config,
    # invitation links, webhooks). Dev default is localhost; in prod set to
    # e.g. https://api.clawdi.example.
    public_api_url: str = "http://localhost:8000"

    # Store catalog sync is a deployment opt-in asynchronous, last-known-good
    # cache fill. API requests only read PostgreSQL and never wait on GitHub.
    plugin_catalog_sync_enabled: bool = False
    plugin_catalog_sync_interval_seconds: Annotated[int, Field(ge=30, le=86_400)] = 300
    plugin_catalog_sync_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 10.0

    # Externally reachable URL for the WEB DASHBOARD. The CLI device-flow
    # `verification_uri` resolves through this — backend hands the CLI a URL
    # the user opens in a browser. Dev default is the Vite dev server; in
    # prod set to e.g. https://cloud.clawdi.example.
    web_origin: str = "http://localhost:3000"

    # Trust proxy-set forwarded headers as the source of the real
    # client IP, in this order: `CF-Connecting-IP`, then the
    # rightmost `X-Forwarded-For` entry (the address the nearest
    # proxy appended), then the socket peer. The leftmost XFF
    # entry is client-controlled and never used. Only for
    # deployments behind a reverse proxy or ingress that sets or
    # appends those headers, because uvicorn's
    # `request.client.host` is then the proxy's address, not the
    # user's. Off by default so a misconfigured local dev /
    # direct-uvicorn setup can't be header-spoofed. Read by
    # `app.core.client_ip.trusted_client_ip`, which keys the
    # device-flow and share-redeem per-IP rate limiters; without
    # it, ALL CLI logins behind one proxy share a single 90/min
    # bucket and the third concurrent login 429s.
    trust_forwarded_for: bool = False

    database_url: str = "postgresql+asyncpg://clawdi:clawdi_dev@localhost:5433/clawdi"

    # Ordinary SQLAlchemy pool per process. Budget API processes for the
    # separate two-connection control pool and LISTEN connection as well.
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: float = 30.0
    db_pool_recycle: int = 1800  # recycle connections every 30 min

    # Observability (both optional; no-op if not set)
    sentry_dsn: str = ""
    sentry_environment: str = ""  # falls back to `environment` if empty
    sentry_release: str = ""  # deploy git SHA; ties events to releases
    sentry_traces_sample_rate: float = 0.0
    slow_request_log_ms: float = 750.0
    metrics_bearer_token: str = ""
    metrics_basic_auth_user: str = "prometheus"
    metrics_basic_auth_password: str = ""

    # Optional networkless override. By default Clerk signing keys are fetched
    # from the JWKS endpoint derived from clerk_jwt_issuer.
    clerk_pem_public_key: str = ""
    # Optional: Clerk Backend API secret. Used by the snapshot-email-rebind
    # path to fetch a user's verified primary email when the session token
    # doesn't carry an `email` claim. Only consulted when
    # `enable_snapshot_email_rebind` is true.
    clerk_secret_key: str = ""

    # Required for default JWKS verification and used as optional strict claim
    # binding for dashboard/browser session JWTs when a static PEM overrides it.
    # Keep these separate from the Public OAuth App resource values: Clerk
    # session templates and OAuth access tokens can use different audiences.
    # Empty values preserve the historical signature-only session validation.
    clerk_jwt_issuer: str = ""
    clerk_jwt_audience: str = ""
    # Dedicated to this Clerk webhook endpoint. It must not be shared with a
    # different endpoint because Clerk/Svix issues one secret per endpoint.
    clerk_webhook_signing_secret: SecretStr = SecretStr("")
    clerk_webhook_tolerance_seconds: Annotated[int, Field(gt=0, le=600)] = 300

    # Identity-provider seam (D-02 — these exact key NAMES are locked, and
    # widening the value TYPE below changes no key). "clerk" (default)
    # selects ClerkUserProvisioningProvider; "supa" is the phase 3.1
    # self-hosted Supabase provider and raises NotImplementedError until it
    # lands. Dispatch happens in
    # app.services.user_prov.get_user_provisioning_provider().
    #
    # The value is refused HERE, at settings load, because both call sites
    # resolve the provider late: admin_delete_tenant calls the factory AFTER
    # `await db.commit()` has durably fenced the tenant and scheduled the
    # irreversible purge, and execute_tenant_purge calls it AFTER the /v2
    # teardown and the subscription cancel have already been issued. So a
    # typo in USER_PROV, or a premature USER_PROV=supa, used to surface for
    # the first time as a delete that is irreversibly HALF-APPLIED and then
    # retried forever by the purge worker (WR-09). Refusing at load makes the
    # process decline to start instead. The factory's own ValueError /
    # NotImplementedError arms are untouched and stay defence in depth.
    #
    # Phase 3.1 ("Self-Hosted Supabase Identity Provider" in ROADMAP.md)
    # deletes the "supa" refusal in _reject_unimplemented_user_prov as its
    # first act, when the provider actually lands.
    user_prov: Literal["clerk", "supa"] = "clerk"
    # Connection settings for the phase 3.1 supa provider (base URL + API
    # key). Deliberately unread by the clerk implementation — declared now so
    # the operator vocabulary is stable before 3.1 wires them.
    user_prov_url: str = ""
    user_prov_api: str = ""

    # /v2 deploy service (clawdi-hosted) server-to-server connection. Named
    # generically — the billing gate (03-06) reads GET /v2/subscription and
    # 03-08's purge worker reuses the same pair for DELETE /v2/deployments/{id}
    # and POST /v2/subscription/cancel. Empty defaults keep every outbound
    # /v2 consumer dark until the operator wires the hosted service.
    deploy_api_url: str = ""
    deploy_api_key: str = ""

    # The dedicated DNS zone tenant-installed companions are served under
    # (phase 06): each installation gets one generated, non-guessable label
    # directly beneath it, e.g. `postiz-<12 base32 chars>.<zone>`. It MUST be
    # a registrable domain separate from iforge's own origins: Postiz sets its
    # `auth` cookie on `.<registrable domain>`, so a companion under the
    # control plane's domain would hand that cookie to iforge origins
    # (research F9). Empty keeps companion installs refused by name (503
    # `companion_edge_zone_unconfigured`) until the operator names the zone;
    # no zone value is committed here (OSS boundary).
    companion_edge_zone_name: str = ""

    # Per-installation Postiz media (plan 06-13, D-18/D-19 as amended at CK-C): one R2 bucket per
    # installation, an S3 credential derived from an account API token scoped to that bucket
    # alone, and a `media-<suffix>.<companion zone>` custom domain. Durability is a lifecycle rule
    # plus the export -- R2 implements no versioning, so none is claimed. `cloudflare_r2_api_token`
    # administers buckets, tokens and the companion zone's custom domains;
    # the admin S3 key pair is used only to empty a bucket at teardown. Every value is operator
    # infrastructure: empty defaults keep the media step refusing by name before any call, and
    # no operator value is committed here (OSS boundary).
    cloudflare_account_id: str = ""
    cloudflare_r2_api_token: str = ""
    cloudflare_r2_s3_endpoint: str = ""
    cloudflare_r2_admin_access_key_id: str = ""
    cloudflare_r2_admin_secret_access_key: str = ""
    companion_edge_zone_id: str = ""

    # Each installation's hostname (plan 06-14, D-24/D-25): one remotely-managed Cloudflare Tunnel
    # per installation in `cloudflare_account_id`, and one proxied CNAME in the companion zone
    # (`companion_edge_zone_id`). The token needs Cloudflare Tunnel edit on the account and DNS
    # edit on the companion zone only. Empty keeps the edge steps refusing by name before any
    # call; no operator value is committed here (OSS boundary).
    cloudflare_tunnel_api_token: str = ""

    # Each installation's pgBackRest repository (plan 06-15, D-14/D-15, OPS-04): a Backblaze B2
    # bucket in an account SEPARATE from the compute provider and from the Cloudflare account,
    # created with Object Lock and file versioning on. The operator key here only mints and
    # revokes one key per installation -- listFiles, readFiles and writeFiles, restricted to the
    # bucket and to `<tenant_id>/<installation_id>/` -- so it needs writeKeys, listKeys and
    # deleteKeys. The S3 endpoint is the bucket's host (`s3.<region>.backblazeb2.com`) and the
    # region is that endpoint's region segment. Empty keeps the backup step refusing by name
    # before any call; no operator value is committed here (OSS boundary).
    companion_backup_b2_key_id: str = ""
    companion_backup_b2_application_key: str = ""
    companion_backup_b2_bucket_id: str = ""
    companion_backup_b2_bucket_name: str = ""
    companion_backup_b2_s3_endpoint: str = ""
    companion_backup_b2_region: str = ""

    # The standard tier's shared Temporal (plan 06-13, D-04/D-05 as recorded at CK-A): one clean
    # namespace per installation, created through the frontend's HTTP API with an admin JWT this
    # backend mints, and a per-installation JWT whose only permission is writer on its namespace.
    # `companion_temporal_frontend_address` is the host:port tenant VMs dial;
    # `companion_temporal_http_url` is the same frontend's HTTP API as the control plane reaches
    # it. The RS256 private key's public half must be in the JWKS the frontend's default
    # ClaimMapper trusts, under `companion_temporal_jwt_key_id`. Empty defaults keep the Temporal
    # step refusing by name before any call.
    companion_temporal_frontend_address: str = ""
    companion_temporal_http_url: str = ""
    companion_temporal_tls: bool = True
    companion_temporal_jwt_private_key: str = ""
    companion_temporal_jwt_key_id: str = ""
    companion_temporal_namespace_retention_days: Annotated[int, Field(ge=1, le=90)] = 7

    # The operator's default monthly companion spend ceiling (plan 06-31, OPS-06, D-20), in integer
    # micro-dollars. It applies to a tenant that has not set its own ceiling; the tenant's own
    # setting always wins, above or below it. Unset (the default, and an empty env line) means no
    # default: such a tenant is unbounded, and `GET /v1/companions/spend-ceiling` reports `none`.
    # Zero is a value, refusing every agent call and new install for tenants without a ceiling of
    # their own. The repetition breaker applies either way. No operator value is committed here
    # (OSS boundary).
    companion_default_spend_ceiling_usd_micros: Annotated[int, Field(ge=0, le=2**63 - 1)] | None = (
        None
    )

    # Explicit opt-out of D-16's hosted teardown half (WR-04). The tenant
    # purge performs TWO external obligations over /v2 — DELETE
    # /v2/deployments/{id} and POST /v2/subscription/cancel — and it used to
    # skip both silently whenever `deploy_api_url` was empty, then still
    # report the purge `complete`: a false claim about an irreversible,
    # legally-relevant operation, made while the tenant's compute was still
    # running and their subscription still billing.
    #
    # So an empty `deploy_api_url` is now a purge FAILURE unless the operator
    # sets this flag. It exists for a self-hosted deployment that genuinely
    # has no /v2 service; turning it on means the subscription cancel and the
    # deployment teardown are NOT performed by this system, and that fact is
    # recorded in the purge result and in every `tenant.purged` audit row so
    # the completion claim stays honest. Named for what it disables, and
    # deliberately NOT folded into `deploy_api_url` as a mode switch: an
    # unset URL is a configuration gap, not a decision.
    tenant_purge_skip_hosted_teardown: bool = False

    # Grace window between an operator-triggered tenant deletion (the fence)
    # and the irreversible purge sweep (D-15: cancellable, 30-day default).
    #
    # Bounded because this is the single value protecting the one
    # irreversible operation in the tenant lifecycle. claim_tenant_purge's
    # predicate is `purge_after <= now`, and admin_delete_tenant stores
    # now + timedelta(days=this) into both purge_after and next_attempt_at —
    # so 0 (or a negative value) makes the row due the instant it commits and
    # TenantPurgeWorker's 1-second poll runs the sweep about a second after
    # the operator clicks Delete, removing their only cancel opportunity
    # while the confirmation dialog still says 30 days. The upper bound stops
    # a typo parking a tombstone row in the claim query effectively forever.
    tenant_purge_grace_days: Annotated[int, Field(ge=1, le=365)] = 30

    # Opt-in for the email-rebind authentication path. When true, an
    # incoming Clerk JWT whose `sub` doesn't match any existing user is
    # rebound onto an existing row by exact email match (using the JWT's
    # email claim, or falling back to a Clerk Backend API lookup if
    # `clerk_secret_key` is set). Designed for preview deployments fed
    # from a production snapshot whose users were issued by a different
    # Clerk instance — sign-in needs to reattach to the snapshot row.
    #
    # Must NEVER be true in production: the rebind treats email as an
    # identity claim, which is only safe when (a) the rebind is gated to
    # snapshot data the operator already trusts, and (b) account takeover
    # is bounded by that snapshot's contents.
    enable_snapshot_email_rebind: bool = False

    # Local browser-dev bypass. When true AND `environment == "development"`,
    # the backend accepts `Authorization: Bearer <dev_auth_token>` as a
    # dashboard user for quick Playwright/manual UI testing without Clerk.
    # Keep this false in every deployed environment.
    dev_auth_bypass: bool = False
    dev_auth_token: str = "dev-bypass"
    dev_auth_clerk_id: str = "dev_browser"
    dev_auth_email: str = "dev@iforge.local"
    dev_auth_name: str = "Dev User"

    vault_encryption_key: str = ""
    # The vault keyring (plan 06-25, D-11). `VAULT_ENCRYPTION_KEY` above is always the `vault.v1`
    # member; this JSON object adds more (`{"vault.v2": "<64 hex>"}`), and
    # `VAULT_ACTIVE_KEY_VERSION` names the one new VERSIONED writes use -- hosted runtime secrets
    # and companion installation atoms, the tables that record a `key_version`. Every unversioned
    # table (vault items, channel tokens, AI provider credentials) stays on `vault.v1`.
    vault_encryption_keys: Annotated[dict[str, str], NoDecode] = {}
    vault_active_key_version: str = VAULT_LEGACY_KEY_VERSION
    encryption_key: str = ""  # Channel credential derivation and cursor-key fallback

    # Strict-v2 runtime observation companion. Cursor tokens are encrypted and
    # opaque to Hosted; empty uses encryption_key so existing deployments need
    # no additional secret before adopting the protocol.
    runtime_observation_cursor_key: str = ""
    # Runtime reports every 60 +/- 15 seconds. Allow two full intervals before
    # treating the latest observation as stale.
    runtime_observation_freshness_seconds: Annotated[int, Field(gt=0, le=86_400)] = 150
    runtime_observation_max_future_skew_seconds: Annotated[int, Field(gt=0, le=86_400)] = 300
    runtime_observation_max_capture_age_days: Annotated[int, Field(gt=0, le=365)] = 30
    runtime_observation_replay_horizon_days: Annotated[int, Field(gt=0, le=365)] = 7
    runtime_observation_hard_retention_days: Annotated[int, Field(gt=0, le=3_650)] = 30
    runtime_observation_cleanup_batch_size: Annotated[int, Field(gt=0, le=10_000)] = 500

    # Billing gate (ACCESS-02, D-09; 06.3 D-03). When true, every auth resolver
    # except get_auth_billing_exempt refuses a user without an active or
    # trialing subscription (403 {"code": "billing_required"}), and
    # GET /v1/access/status reads it to report billing_required. Sign-up itself
    # is Clerk's decision (06.3 D-02). Default false so self-hosted deployments
    # are unaffected.
    billing_gate_enabled: bool = False
    # INT-1: how often an unlocked user's subscription is re-read from /v2,
    # and how long a lapse (neither active nor trialing) is tolerated before
    # the user is locked again. The grace runs from the first re-read that
    # sees the lapse, so a user is locked at most one interval after it ends.
    billing_recheck_interval_seconds: Annotated[int, Field(gt=0, le=86_400)] = 3_600
    billing_lapse_grace_seconds: Annotated[int, Field(ge=0, le=30 * 86_400)] = 86_400

    # Admin endpoints (POST/DELETE /v1/admin/auth/keys) auth.
    # Empty string disables them entirely (returns 503). Set in
    # production to a strong secret (e.g. `openssl rand -hex 32`)
    # to enable batch operations: SaaS migration tooling for live
    # sync, ops-side revocation, etc. Compared with constant-time
    # comparison.
    admin_api_key: str = ""

    # Legacy shared-root auth remains available only for the six canonical
    # /v1/platform mutations while cohorts move to workload OAuth.
    platform_legacy_admin_auth_enabled: bool = True
    # This authorization server is independent from tenant -> Hosted OAuth.
    platform_workload_signing_key_refs: dict[str, str] = {}
    platform_workload_issuer: str = "clawdi-cloud-platform"
    # Exact RFC 7523 assertion audience. Empty derives from PUBLIC_API_URL.
    platform_workload_token_endpoint: str = ""

    composio_api_key: str = ""
    composio_api_base_url: str = "https://backend.composio.dev"

    # File store selection. `local` stores under FILE_STORE_LOCAL_PATH.
    # `s3` uses S3-compatible object storage, including R2, via boto3.
    file_store_type: str = "local"
    file_store_local_path: str = "./data/files"
    file_store_s3_bucket: str = ""
    file_store_s3_region: str = "auto"
    file_store_s3_endpoint_url: str = ""
    file_store_s3_access_key_id: str = ""
    file_store_s3_secret_access_key: str = ""
    file_store_s3_force_path_style: bool = False

    # Memory embedder for the Builtin memory provider.
    # - "local": run paraphrase-multilingual-mpnet-base-v2 via fastembed
    #   (ONNX, ~1GB download on first use, no API key needed). Default.
    # - "api":   call an OpenAI-compatible embeddings endpoint. Set
    #   memory_embedding_api_key, and optionally memory_embedding_base_url
    #   (e.g. https://openrouter.ai/api/v1) and memory_embedding_model.
    # - "local-service": call one host-local FastEmbed worker over a Unix
    #   socket so multiple API workers do not each load the ONNX model.
    memory_embedding_mode: str = "local"
    memory_embedding_api_key: str = ""
    memory_embedding_base_url: str = ""
    memory_embedding_model: str = "text-embedding-3-small"
    memory_embedding_service_socket_path: str = "/run/iforge-embedding/embedding.sock"
    memory_embedding_service_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    # Zero preserves FastEmbed/ONNX Runtime automatic thread selection. A
    # dedicated worker should set this to its container CPU quota.
    memory_embedding_threads: Annotated[int, Field(ge=0, le=256)] = 0
    # Initial local-service rollout runs one inference at a time through one
    # shared ONNX session. Raise only after latency and rejection metrics show
    # that concurrent session use improves throughput within the CPU quota.
    memory_embedding_worker_max_concurrency: Annotated[int, Field(ge=1, le=256)] = 1

    # Channel emulation waits. PostgreSQL notifications wake normal long polls;
    # the interval is a bounded fallback for listener failure or notification loss.
    channel_long_poll_max_seconds: float = 30.0
    channel_long_poll_interval_seconds: float = 5.0
    discord_gateway_poll_interval_seconds: float = 1.0
    channel_message_retention_days: Annotated[int, Field(gt=0, le=3_650)] = 30
    channel_unbound_message_retention_hours: Annotated[int, Field(gt=0, le=87_600)] = 24
    channel_message_cleanup_batch_size: Annotated[int, Field(gt=0, le=10_000)] = 500
    channel_message_cleanup_max_batches: Annotated[int, Field(gt=0, le=1_000)] = 20
    channel_message_stuck_pending_hours: Annotated[int, Field(gt=0, le=8_760)] = 24

    # Shared LLM credentials for any feature that needs chat completions
    # (memory extraction today; session summarization, auto-tagging, etc.
    # tomorrow). OpenAI-compatible endpoint — works with OpenAI itself,
    # OpenRouter, Anthropic-via-proxy, local llama.cpp, etc. Empty
    # `api_key` is the disable signal — features that depend on the LLM
    # return 503 with a clear hint when it's missing. `llm_model` is a
    # process-wide default; individual features can override at the call
    # site if they need a stronger/cheaper model.
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    # JSON object keyed by provider/tool id. Each value can include:
    # authorization_url, token_url, client_id, client_secret, redirect_uri,
    # scope, audience, and extra_authorize_params. Codex has an official
    # built-in default; this
    # setting can override it or add future verified adapters without routing
    # AI Provider auth through local agent CLIs.
    ai_provider_oauth_config_json: str = ""

    # Channels provider endpoints. The backend owns channel state directly;
    # these base URLs are only for outbound provider calls.
    channel_telegram_api_base_url: str = "https://api.telegram.org"
    channel_discord_api_base_url: str = "https://discord.com/api/v10"
    channel_discord_gateway_url: str = "wss://gateway.discord.gg"
    # One business-neutral provider service owns isolated physical sessions for
    # both Shared and Custom WhatsApp accounts.
    channel_whatsapp_baileys_sidecar_token: SecretStr = SecretStr("")
    channel_whatsapp_baileys_sidecar_url: str = ""


settings = Settings()
