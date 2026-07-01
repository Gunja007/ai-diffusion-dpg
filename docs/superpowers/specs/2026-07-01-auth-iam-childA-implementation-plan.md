# Child A — Ring 0 + Inter-DPG Service Auth: Implementation Plan

**Design documents:**
- [Umbrella design](https://github.com/Blue-Dots-Economy/ai-diffusion-dpg/blob/feat/auth-iam-v2/docs/superpowers/specs/2026-06-29-auth-iam-v2-umbrella-design.md)
- [Child A design](https://github.com/Blue-Dots-Economy/ai-diffusion-dpg/blob/feat/auth-iam-v2/docs/superpowers/specs/2026-06-29-auth-iam-childA-inter-dpg-service-auth-design.md)

**In-scope:** Ring 0 (Keycloak foundation + `dpg_auth` shared library) + Child A (service-to-service token authentication).
**Out-of-scope:** Child B (end-user OIDC/OTP), Child C (dev-kit tenancy), Child D (MCP `private_key_jwt`).

---

## 1. Background & Problem Statement

Every DPG HTTP endpoint currently accepts requests from any caller with no mutual authentication. A pod on the same Kubernetes network can call `POST /execute` on Action Gateway directly, read Memory Layer session state, or forge Trust Layer results — bypassing Agent Core's consent gate. The only auth today is:

- Static `X-API-Key` header on the `reach-layer → knowledge-engine` ingest chain (`knowledge_engine/src/auth.py`, `reach_layer/web/src/auth.py`).
- Google SSO + HS256 session JWT in reach-web.

No JWT/OIDC/Keycloak/JWKS exists anywhere. This child closes the gap for **service-to-service traffic only** by adopting Keycloak `client_credentials` service-account tokens, verified by a new `dpg_auth` shared library installed as a FastAPI middleware on every service.

---

## 2. Architecture

### 2.1 Trust Model After Child A

```
┌──────────────────────────────────────────────────────────────────────────┐
│  network-common Keycloak                                                  │
│  Realm: ai-diffusion-platform                                             │
│  One service-account client per block (svc-agent-core, svc-trust-layer…) │
│  Protocol mapper: role claim = "service:<block_name>"                    │
└──────────────────────────────────────────────────────────────────────────┘
          │  issues short-lived tokens (client_credentials)
          ▼
┌────────────────────────┐           ┌──────────────────────────────────────┐
│  ServiceAuthClient     │           │  VerifyJwtMiddleware (callee side)    │
│  (caller side)         │           │  + AuthorizeMiddleware                │
│  dpg_auth/client.py    │─ Bearer ─▶│  dpg_auth/middleware/                 │
│  caches + refreshes    │           │  KeycloakAuthProvider (JWKS offline) │
│  own token             │           │  CompositeAuthProvider (routes iss)   │
└────────────────────────┘           └──────────────────────────────────────┘
```

### 2.2 Dual-Realm Architecture

| Realm | Purpose | Issuer |
|---|---|---|
| `ai-diffusion-platform` | Service accounts (always used) | `<KC_URL>/realms/ai-diffusion-platform` |
| `<host-realm>` (future) | End-user tokens (children B/D) | routing by `iss` claim |

The `CompositeAuthProvider` routes verification by the JWT `iss` claim, making children B/D a drop-in. Service auth is therefore independent of the end-user realm decision.

### 2.3 Key Flows

**Caller flow (every outgoing inter-service HTTP request):**
1. `ServiceAuthClient.get_token()` — checks in-memory cache, fetches from Keycloak `/token` if expired, returns `access_token`.
2. httpx wrapper injects `Authorization: Bearer <token>` header.

**Callee flow (every incoming inter-service HTTP request):**
1. `VerifyJwtMiddleware` extracts Bearer token.
2. Token forwarded to `CompositeAuthProvider.verify(token)` → routes by `iss` to `KeycloakAuthProvider`.
3. `KeycloakAuthProvider` fetches JWKS from Keycloak (cached, refresh-ahead), verifies signature + exp + iss + aud.
4. Returns frozen `AuthContext(caller_id, service_role, token_exp)` stored in `contextvars`.
5. `AuthorizeMiddleware` checks `AuthContext.service_role` against the callee's `allow_callers` config list.
6. On failure: 401/403 immediately; on bypass paths (e.g., `/health`): no auth.

**Shadow mode** (rollout gate): middleware logs auth decisions but never blocks. A config flag (`dpg_auth.enforce: false`) keeps all services functional while credentials are provisioned.

---

## 3. Current System Map

### 3.1 Services and Their Create-App Factories

| Block | Entry point | `create_app()` factory | Middleware today |
|---|---|---|---|
| Agent Core | `agent_core/main.py` | `create_orchestration_app()` in `src/servers/orchestration_server.py` | `FastAPIInstrumentor` only |
| Knowledge Engine | `knowledge_engine/main.py` | inline in `main.py` | `FastAPIInstrumentor` |
| Memory Layer | `memory_layer/main.py` | likely same pattern | `FastAPIInstrumentor` |
| Trust Layer | `trust_layer/src/server.py` → `create_app(trust)` | `trust_layer/src/server.py` | `FastAPIInstrumentor` |
| Action Gateway | `action_gateway/src/server.py` | `create_app()` or inline | `FastAPIInstrumentor` |
| Observability Layer | `observability_layer/src/server.py` | inline | `FastAPIInstrumentor` |
| Reach Web | `reach_layer/web/` | `web_reach.py` | Google SSO / HS256 |
| Reach MCP | `reach_layer/mcp/src/server.py` | inline | Static API key (`_authenticate_request`) |

### 3.2 HTTP Clients (Caller Side)

All live in `agent_core/src/http_clients/` (sync) and `agent_core/src/http_clients/async_/` (async). Each is initialised in `agent_core/main.py::_build_app()` and receives the full merged `config` dict. They read endpoint + timeout from `<service>_client` config sections. **No auth header injection today.**

### 3.3 Config System

- Each block: `config/dpg.yaml` + domain YAML deep-merged at startup → strict Pydantic `MergedConfig` (`extra="forbid"`).
- Dev-kit mirrors runtime schemas → runtime-devkit-sync discipline applies to every config field added.
- Shared dependency pattern: `dpg_telemetry` lives in `observability_layer/src/dpg_telemetry/`, consumed via `[tool.uv.sources] observability-layer = { path = "../observability_layer" }` in every module's `pyproject.toml`.
- **`dpg_auth` will follow exactly this pattern.**

### 3.4 Reusable vs Modified Components

| Component | Action | Why |
|---|---|---|
| `dpg_telemetry` package layout | **Model** `dpg_auth` on this | Established shared-package precedent |
| `agent_core/src/http_clients/*.py` | **Modify** — inject auth header from `ServiceAuthClient` | Auth must propagate on every outgoing call |
| `agent_core/src/http_clients/async_/*.py` | **Modify** — same for async path | stream_turn uses async clients |
| Every `create_app()` / server factory | **Modify** — add `VerifyJwtMiddleware` + `AuthorizeMiddleware` | Callee-side enforcement |
| `<block>/src/schema/config.py` | **Modify** — add `dpg_auth` config section | `extra="forbid"` rejects unknown keys |
| `dev-kit/dev_kit/schemas/domain/<block>.py` | **Modify** — mirror new auth section | runtime-devkit-sync discipline |
| `dev-kit/dev_kit/schema.py` | **Modify** — flat-file copy | host-mode deploy gate |
| `automation/docker/docker-compose.dev.yml` | **Modify** — add Keycloak service | Ring 0 foundation |
| `automation/docker/keycloak/` | **Create** — realm import JSON | Baked realm provisioning |

---

## 4. New Package: `dpg_auth/`

### 4.1 Location and Discovery

```
dpg_auth/
├── pyproject.toml
├── src/
│   └── dpg_auth/
│       ├── __init__.py          # public API re-exports
│       ├── config.py            # DpgAuthConfig Pydantic model (self-contained)
│       ├── context.py           # AuthContext frozen dataclass + ContextVar
│       ├── logging.py           # StructuredLogFilter + install_filter()
│       ├── client.py            # ServiceAuthClient (token fetch + httpx wrapper)
│       ├── provider/
│       │   ├── __init__.py
│       │   ├── base.py          # AuthProviderBase (ABC): verify(token) -> AuthContext
│       │   ├── keycloak.py      # KeycloakAuthProvider (offline JWKS + cache)
│       │   ├── composite.py     # CompositeAuthProvider (routes by iss)
│       │   └── static.py        # StaticAuthProvider (tests / CI)
│       └── middleware/
│           ├── __init__.py
│           ├── verify.py        # VerifyJwtMiddleware (Starlette ASGI)
│           └── authorize.py     # AuthorizeMiddleware (per-callee allow_callers check)
└── tests/
    ├── test_config.py
    ├── test_context.py
    ├── test_client.py
    ├── test_provider_keycloak.py
    ├── test_provider_composite.py
    ├── test_provider_static.py
    ├── test_middleware_verify.py
    └── test_middleware_authorize.py
```

All 8 module `pyproject.toml` files gain:
```toml
[tool.uv.sources]
dpg-auth = { path = "../dpg_auth" }
```
And in `[project] dependencies`: `"dpg-auth"`.

### 4.2 `config.py` — `DpgAuthConfig`

> **Critical rule:** `<block>/src/schema/config.py` may only import from `pydantic`, `enum`, `typing`, `__future__`. The `DpgAuthConfig` itself must be self-contained for the Dockerfile bake.

```
DpgAuthConfig
├── enabled: bool = True          # when False: middleware is no-op (future killswitch)
├── enforce: bool = False         # shadow mode default; set True after rollout
├── keycloak_url: str = ""        # e.g. "http://keycloak:8080" (container port)
├── realm: str = "ai-diffusion-platform"
├── client_id: str = ""           # e.g. "svc-agent-core"
├── client_secret: str = ""       # from env; never logged
├── token_ttl_margin_s: int = 30  # refresh this many seconds before exp
├── jwks_cache_ttl_s: int = 300   # re-fetch JWKS after this many seconds
├── allow_callers: list[str] = [] # role values allowed in; empty = allow all service roles
└── bypass_paths: list[str] = ["/health"]  # paths that skip JWT check
```

**Schema isolation strategy:** In each block's `schema/config.py`, inline a mirror of `DpgAuthConfig` (same field names, same types, same defaults). The block schema only needs this to avoid `extra="forbid"` rejections. The `dpg_auth` library validates the dict again at startup with its own `DpgAuthConfig`.

### 4.3 `context.py` — `AuthContext`

```python
@dataclass(frozen=True)
class AuthContext:
    caller_id: str          # e.g. "svc-agent-core"
    service_role: str       # e.g. "service:agent_core"
    token_exp: int          # Unix epoch
    raw_claims: dict        # full decoded payload (for logging)
```

Stored in a `ContextVar[Optional[AuthContext]]` so it is request-scoped and async-safe. Accessors: `get_auth_context()`, `set_auth_context()`.

### 4.4 `provider/base.py` — `AuthProviderBase`

```python
class AuthProviderBase(ABC):
    @abstractmethod
    def verify(self, token: str) -> AuthContext:
        """Verify token. Raises AuthError on any failure."""
```

Concrete: `KeycloakAuthProvider`, `CompositeAuthProvider`, `StaticAuthProvider`.

### 4.5 `provider/keycloak.py` — `KeycloakAuthProvider`

- Fetches JWKS from `<keycloak_url>/realms/<realm>/protocol/openid-connect/certs`.
- Cache: `dict[kid, public_key]` + `fetched_at`. Refreshes when stale or `kid` is unknown.
- Verifies: RS256 signature, `exp`, `iss == <keycloak_url>/realms/<realm>`, role claim presence.
- **Refresh-ahead:** background fetch when `(now - fetched_at) > 0.8 * jwks_cache_ttl_s`.
- Uses `PyJWT` with `cryptography` extra (already transitively available via reach-web).
- Raises `AuthError(reason, message)` — never leaks raw exception text.

### 4.6 `provider/composite.py` — `CompositeAuthProvider`

Routes `verify(token)` by the JWT `iss` claim to the appropriate sub-provider. Drop-in point for children B/C/D — add a provider to the composite at startup, zero middleware changes.

### 4.7 `provider/static.py` — `StaticAuthProvider`

Used in tests/CI only. HS256, known secret, returns a fixed `AuthContext`. Activated when `keycloak_url` is empty string.

### 4.8 `client.py` — `ServiceAuthClient`

```
ServiceAuthClient
├── __init__(config: DpgAuthConfig)
├── get_token() -> str             # blocking; cached or fresh
├── aget_token() -> str            # async variant
├── make_sync_client(**kwargs) -> httpx.Client    # token-injecting wrapper
└── make_async_client(**kwargs) -> httpx.AsyncClient
```

- Token fetched via `POST <keycloak_url>/realms/<realm>/protocol/openid-connect/token` (grant_type=client_credentials).
- Thread-safe via `threading.Lock` (sync) and `asyncio.Lock` (async).
- `make_sync_client` returns an `httpx.Client` subclass that injects `Authorization: Bearer <token>` on every request and retries once on 401 (evicting cached token first).

### 4.9 `middleware/verify.py` — `VerifyJwtMiddleware`

Starlette ASGI middleware added via `app.add_middleware()`.

```
Request path:
  1. path in bypass_paths           → pass-through
  2. not enabled                    → pass-through (killswitch)
  3. Extract Authorization: Bearer
  4. Missing token + enforce=True   → 401
  5. Missing token + enforce=False  → log warning; pass-through (shadow)
  6. provider.verify(token)
  7. AuthError + enforce=True       → 401
  8. AuthError + enforce=False      → log; pass-through (shadow)
  9. set_auth_context(ctx); pass-through
```

Log fields: `operation`, `status`, `caller_id`, `service_role`, `latency_ms`. No raw token logged.

### 4.10 `middleware/authorize.py` — `AuthorizeMiddleware`

Runs after `VerifyJwtMiddleware`. Starlette adds outermost-last, so register AuthorizeMiddleware first.

```
Request path:
  1. path in bypass_paths            → pass-through
  2. ctx is None + enforce=True      → 403
  3. allow_callers is empty          → pass-through (allow all)
  4. ctx.service_role not in allow   → 403 (enforce) or log+pass (shadow)
  5. pass-through
```

---

## 5. Infrastructure: Ring 0 Keycloak

### 5.1 Docker Compose Addition

Add to `automation/docker/docker-compose.dev.yml`:

```yaml
keycloak:
  image: quay.io/keycloak/keycloak:24.0
  container_name: keycloak
  command: ["start-dev", "--import-realm"]
  environment:
    - KEYCLOAK_ADMIN=admin
    - KEYCLOAK_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD:-admin}
  ports:
    - "8180:8080"       # host:8180 -> container:8080
  volumes:
    - ./keycloak/realms:/opt/keycloak/data/import:ro
    - keycloak_data:/opt/keycloak/data
  networks:
    - dpg_net
  healthcheck:
    test: ["CMD-SHELL", "curl -sf http://localhost:8080/health/ready || exit 1"]
    interval: 15s
    timeout: 10s
    retries: 10
    start_period: 60s
  restart: unless-stopped
```

Add `keycloak_data:` to the `volumes:` section.

> [!IMPORTANT]
> Within `dpg_net`, services reach Keycloak at `http://keycloak:8080` (container port). The host port 8180 is for local development browser access only. `keycloak_url` in config YAML must be `http://keycloak:8080`.

### 5.2 Realm Import JSON

`automation/docker/keycloak/realms/ai-diffusion-platform.json`:
- Realm `ai-diffusion-platform`, `enabled: true`, `accessTokenLifespan: 300`.
- One service-account client per block: `svc-agent-core`, `svc-knowledge-engine`, `svc-memory-layer`, `svc-trust-layer`, `svc-action-gateway`, `svc-reach-layer`, `svc-observability-layer`, `svc-dev-kit`.
- All clients: `serviceAccountsEnabled: true`, `publicClient: false`, `clientAuthenticatorType: client-secret`. Client secrets = placeholder values (never real secrets in VCS).
- Protocol mapper: Hardcoded Claim mapper on each client, `claimName: "role"`, `claimValue: "service:<block_name>"`, added to `access_token`. E.g. `svc-agent-core` → `"role": "service:agent_core"`.

### 5.3 Production Onboarding Script

`automation/keycloak/onboard_platform_realm.sh`: idempotent shell script using Keycloak Admin REST API. Parameterised by `KC_URL`, `KC_ADMIN`, `KC_ADMIN_PASSWORD`. Creates realm, clients, mappers. Exports generated secrets to stdout for operator to place in secrets manager.

---

## 6. Config Schema Changes (All 8 Blocks)

### 6.1 Runtime Schema Addition

In each `<block>/src/schema/config.py`, add a local inline `DpgAuthConfig` mirror class and a `dpg_auth` field to the top-level `MergedConfig`:

```python
class DpgAuthConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = True
    enforce: bool = False
    keycloak_url: str = ""
    realm: str = "ai-diffusion-platform"
    client_id: str = ""
    client_secret: str = ""
    token_ttl_margin_s: int = Field(default=30, ge=0)
    jwks_cache_ttl_s: int = Field(default=300, ge=0)
    allow_callers: list[str] = Field(default_factory=list)
    bypass_paths: list[str] = Field(default_factory=lambda: ["/health"])

class MergedConfig(BaseModel):
    ...
    dpg_auth: DpgAuthConfig = Field(default_factory=DpgAuthConfig)
```

This satisfies the "schema/config.py imports from pydantic only" constraint.

### 6.2 Dev-Kit Mirror (per block)

`dev-kit/dev_kit/schemas/domain/<block>.py`: Add `DpgAuthSection` with identical fields. Add `dpg_auth: Optional[DpgAuthSection] = None` to the block's domain section.

### 6.3 Dev-Kit Flat Copy

`dev-kit/dev_kit/schema.py`: Add `DpgAuthSection` and reference it in each block's flat-copy class.

### 6.4 Framework Defaults YAML

`dev-kit/dpg/<block>.yaml`:
```yaml
dpg_auth:
  enabled: true
  enforce: false
  keycloak_url: "http://keycloak:8080"
  realm: "ai-diffusion-platform"
  client_id: ""         # set via env var override at startup
  client_secret: ""     # set via env var; never committed
  token_ttl_margin_s: 30
  jwks_cache_ttl_s: 300
  allow_callers: []
  bypass_paths: ["/health"]
```

### 6.5 Client Secret Injection Pattern

`client_secret` defaults to `""`. Each block's `main.py` reads `os.getenv("DPG_AUTH_CLIENT_SECRET")` (or per-block variant) and mutates `config["dpg_auth"]["client_secret"]` before constructing `ServiceAuthClient`. Docker Compose injects this env var. `ServiceAuthClient.__init__` raises `ValueError` if `client_secret` is empty when `enabled=True` and `keycloak_url` is set.

---

## 7. Caller-Side Changes: HTTP Clients

### 7.1 Pattern

Each HTTP client constructor gains `auth_client: Optional[ServiceAuthClient] = None` (default `None` = backward compatible). When provided, the client uses `auth_client.make_sync_client()` / `make_async_client()` instead of bare `httpx`. The token-injecting wrapper handles 401 retry internally.

### 7.2 Modified Files

**Sync clients** (`agent_core/src/http_clients/`):
- `trust_layer.py`, `knowledge_engine.py`, `memory_layer.py`, `observability_layer.py`, `action_gateway.py`

**Async clients** (`agent_core/src/http_clients/async_/`):
- `trust_layer.py`, `knowledge_engine.py`, `memory_layer.py`, `observability_layer.py`, `action_gateway.py`

**Reach Layer** (caller of KE and Agent Core):
- Wherever Reach Web makes HTTP calls to downstream services.

### 7.3 `_build_app()` Wiring Change

```python
from dpg_auth import ServiceAuthClient, DpgAuthConfig

auth_cfg = DpgAuthConfig.model_validate(config.get("dpg_auth", {}))
# Override client_secret from env
secret = os.getenv("DPG_AUTH_CLIENT_SECRET", "")
if secret:
    auth_cfg = auth_cfg.model_copy(update={"client_secret": secret})
auth_client = ServiceAuthClient(auth_cfg)

memory   = MemoryLayerHttpClient(config, auth_client=auth_client)
trust    = TrustLayerHttpClient(config, auth_client=auth_client)
ke       = HttpKnowledgeEngineClient(config, auth_client=auth_client)
learning = ObservabilityLayerHttpClient(config, auth_client=auth_client)
gateway  = ActionGatewayHttpClient(config, auth_client=auth_client)
# ... async variants similarly
```

---

## 8. Callee-Side Changes: Middleware

### 8.1 Registration Pattern (each server factory)

```python
from dpg_auth import build_auth_provider, DpgAuthConfig
from dpg_auth.middleware.verify import VerifyJwtMiddleware
from dpg_auth.middleware.authorize import AuthorizeMiddleware

auth_cfg = DpgAuthConfig.model_validate(config.get("dpg_auth", {}))
provider = build_auth_provider(auth_cfg)  # KeycloakAuthProvider or StaticAuthProvider

# Starlette wraps outermost-last: add AuthorizeMiddleware first.
app.add_middleware(AuthorizeMiddleware, auth_config=auth_cfg)
app.add_middleware(VerifyJwtMiddleware, auth_provider=provider, auth_config=auth_cfg)
```

### 8.2 `build_auth_provider(auth_cfg)` Factory

In `dpg_auth/__init__.py`:
- Returns `StaticAuthProvider` if `auth_cfg.keycloak_url` is empty.
- Returns `CompositeAuthProvider([KeycloakAuthProvider(auth_cfg)])` otherwise.

### 8.3 Bypass Paths

Default `["/health"]`. Extend per block via config. Health probes never require a token.

### 8.4 Modified Server Factories

All 8 block server factories receive the middleware addition (in shadow mode = enforce:false by default).

---

## 9. Order of Implementation

| Step | What | Why this order |
|---|---|---|
| 1 | `dpg_auth` package skeleton + `DpgAuthConfig` | Foundation; all else imports from here |
| 2 | `AuthContext` + `ContextVar` + `StructuredLogFilter` | No deps; required by all other modules |
| 3 | `StaticAuthProvider` + ABC + tests | Needed by middleware tests; no Keycloak required |
| 4 | `KeycloakAuthProvider` + `CompositeAuthProvider` + tests | Core crypto; mock JWKS in tests |
| 5 | `VerifyJwtMiddleware` + `AuthorizeMiddleware` + tests | Integration point; uses StaticAuthProvider in tests |
| 6 | `ServiceAuthClient` + tests | Caller-side; mock token endpoint in tests |
| 7 | Keycloak Docker Compose + realm import JSON + onboarding script | Ring 0 infrastructure; before enforce:true ever set |
| 8 | Config schema updates for all 8 blocks (runtime + dev-kit + YAML) | Must precede code that loads new config key |
| 9 | Caller-side HTTP client wiring: Agent Core (10 files) | Agent Core is the sole caller of all 5 downstreams |
| 10 | Caller-side HTTP client wiring: Reach Layer | Reach calls KE and Agent Core |
| 11 | Callee-side middleware on all 8 block server factories (enforce=false) | Shadow mode; safe to deploy before full provisioning |
| 12 | Integration test: Docker Compose + live Keycloak | End-to-end validation before enforce:true |
| 13 | Set enforce:true per block in stages (Obs → Trust → Mem → KE → AG → AC) | Incremental lockdown; each block independently |

### Dependencies Between Steps

```
1 → 2 → 3 → 4 → 5
1 → 6
1 → 7 (parallel with 2-6)
8 must precede any container restart with new YAML
9 requires 1,6,8
10 requires 9
11 requires 1,5,8
12 requires 7,9,10,11
13 requires 12
```

---

## 10. Risks and Unknowns

> [!WARNING]
> **Risk 1 — Keycloak startup latency.** Keycloak `start-dev` takes 30–60 seconds. All DPG services need `depends_on: keycloak: condition: service_healthy` added to docker-compose. The healthcheck curl endpoint is `/health/ready` on port 8080.

> [!WARNING]
> **Risk 2 — `ServiceAuthClient` called during `_build_app()`.** `ActionGatewayHttpClient.__init__` calls `_fetch_tool_definitions()` synchronously at construction. If `ServiceAuthClient.get_token()` is also called at construction and Keycloak is not yet ready, startup fails. Mitigation: `get_token()` retries with exponential backoff (3 attempts, 1s/2s delays) before raising.

> [!CAUTION]
> **Risk 3 — `extra="forbid"` schema breakage during rolling deploy.** If the YAML is updated before the code is deployed, startup crashes. Rule: always deploy schema-aware code (Step 8) before deploying new YAML (Step 11).

> [!IMPORTANT]
> **Risk 4 — Client secret management.** `client_secret` in YAML must always be empty string `""`. Secrets are only injected via env var at runtime. Add a CI check scanning for non-empty `client_secret` in committed YAML files.

> [!NOTE]
> **Risk 5 — MCP static key coexistence.** The existing `_authenticate_request()` in `reach_layer/mcp/src/server.py` is a route-level dependency (not middleware). The new `VerifyJwtMiddleware` operates at the ASGI layer. They do not conflict. Both can coexist. MCP remains in shadow mode until Child D replaces the static key.

> [!NOTE]
> **Risk 6 — Reach Web Google SSO coexistence.** The `VerifyJwtMiddleware` must bypass all user-facing routes in Reach Web. `bypass_paths` must include `/auth/*`, `/chat`, `/upload`, `/health`, and any other user-facing endpoints. This bypass list must be finalised before Step 11 deploys middleware on Reach Web.

> [!IMPORTANT]
> **Unknown 1 — Keycloak container port.** Within `dpg_net`, services connect to `keycloak:8080` (container port). `keycloak_url` in YAML must be `http://keycloak:8080`, NOT `8180` (host port). Confirm before writing YAML defaults.

> [!NOTE]
> **Unknown 2 — Keycloak role claim path.** The design specifies a Hardcoded Claim mapper stamping `role: service:<name>` into the access token. Confirm the claim appears at top-level (`claims["role"]`) vs nested (`claims["realm_access"]["roles"]`). `KeycloakAuthProvider` should try `claims.get("role")` first, fall back to `claims.get("realm_access", {}).get("roles", [])`.

> [!NOTE]
> **Unknown 3 — `PyJWT` + `cryptography` availability.** `reach_layer/web/src/auth.py` imports `jwt` (PyJWT). `dpg_auth` should use the same library for RS256 verification. Confirm `cryptography` extra is available in all block virtual environments.

---

## 11. Testing Strategy

### 11.1 `dpg_auth` Package Tests

| Test file | Coverage |
|---|---|
| `test_config.py` | Valid/invalid `DpgAuthConfig` |
| `test_context.py` | Frozen dataclass, ContextVar async isolation |
| `test_provider_static.py` | Valid token, expired, wrong secret |
| `test_provider_keycloak.py` | JWKS fetch (mocked), RS256 verify, exp, kid rotation, cache refresh |
| `test_provider_composite.py` | Routes by iss, unknown issuer → AuthError |
| `test_client.py` | Caching, refresh-before-exp, 401 retry, header injection |
| `test_middleware_verify.py` | Pass, 401 block, shadow pass-through, bypass path |
| `test_middleware_authorize.py` | Role match, role mismatch → 403, shadow → pass |

**Test deps (dpg_auth only):** `pytest`, `pytest-asyncio`, `pytest-httpx` (mock JWKS/token endpoints), `cryptography` (generate test RSA key pair and JWTs).

### 11.2 Per-Block Unit Tests

- `<block>/tests/test_schema_config.py`: Assert `dpg_auth` section accepted; extra keys rejected.
- `<block>/tests/test_http_clients.py` (callers): Assert `Authorization: Bearer` header present when `auth_client` provided; assert no header when `auth_client=None`.

### 11.3 Integration Tests

Docker Compose stack including Keycloak. Verify:
1. `/health` endpoints: succeed without token (bypass path).
2. Trust Layer `POST /check/input` with valid service token: shadow mode logs success.
3. Trust Layer `POST /check/input` without token: shadow mode logs warning, still responds 200.
4. After setting `enforce: true` on Trust Layer: same request without token → 401.

### 11.4 Coverage

- `dpg_auth/`: ≥70% line coverage.
- All three categories per `.claude/rules/testing-requirements.md`.

---

## 12. Dev-Kit Sync Checklist (per block)

Per `.claude/rules/runtime-devkit-sync.md`:

- [ ] `<block>/src/schema/config.py` — inline `DpgAuthConfig` mirror + `dpg_auth` field in `MergedConfig`
- [ ] `dev-kit/dev_kit/schemas/domain/<block>.py` — `DpgAuthSection`
- [ ] `dev-kit/dev_kit/schema.py` — flat-file copy update
- [ ] `dev-kit/dpg/<block>.yaml` — `dpg_auth:` defaults block
- [ ] `dev-kit/tests/schemas/domain/test_<block>.py` — accept-valid + reject-invalid
- [ ] Rebuild dev-kit Docker image; run wizard end-to-end; confirm `"validator": "runtime_baked"`

---

## 13. Backwards Compatibility

| Mechanism | Guarantee |
|---|---|
| `auth_client=None` default on all HTTP clients | Existing tests pass unchanged; behaviour identical to today |
| `enforce: false` default in all YAML | No request ever blocked during rollout |
| `bypass_paths: ["/health"]` default | Docker healthchecks unaffected |
| `allow_callers: []` default | All service roles permitted (no lockout during initial rollout) |
| `enabled: false` killswitch | Middleware becomes a complete no-op; instant recovery |
| `Reason` enum in `reach_layer/web/src/auth.py` | Not touched; scoped to Google SSO |

---

## 14. Security Considerations

1. **No PII or token values in logs** — only `caller_id`, `service_role`, `latency_ms`.
2. **Constant-time comparison in StaticAuthProvider** — `hmac.compare_digest` (consistent with MCP).
3. **Tokens never in URLs** — always `Authorization: Bearer` header.
4. **5-minute token lifetime** — `accessTokenLifespan: 300` in Keycloak; limits blast radius.
5. **JWKS refresh-ahead** — no thundering herd on key rotation.
6. **`enabled: false` killswitch** — instant recovery if Keycloak is unreachable.
7. **Secrets via env vars only** — `client_secret` in YAML always `""`.
8. **`client_secret` typed `str`** in block schema mirrors (Pydantic only — satisfies Dockerfile bake constraint); typed `SecretStr` in the `dpg_auth` library itself for runtime protection.

---

## 15. Assumptions to Validate Before Coding

1. **Keycloak container port is `8080` within `dpg_net`** — `keycloak_url` default must be `http://keycloak:8080`.
2. **Hardcoded Claim mapper places `role` as a top-level JWT claim** — validate by decoding a token after realm import.
3. **`PyJWT` + `cryptography` extra is the chosen JWT library** — avoids introducing `python-jose`.
4. **`pytest-httpx` is acceptable for mocking JWKS/token HTTP calls** in `dpg_auth` tests.
5. **`ServiceAuthClient` retry-on-startup is sufficient** to handle Keycloak readiness — no additional startup probe needed in each block's `main.py`.
6. **Reach Web bypass path list** must be agreed before Step 11.
7. **One schema PR per block (8 PRs) vs one combined PR** — decision affects review coordination.

---

## 16. Open Questions

> [!IMPORTANT]
> **Q1: `PyJWT` vs `python-jose`?** Recommend `PyJWT` + `cryptography` extra (already available via reach-web transitively). Avoids introducing a new library. Needs confirmation.

> [!IMPORTANT]
> **Q2: Schema changes — 1 PR or 8?** The runtime-devkit-sync rule requires all mirrors in the same PR as the runtime schema change. Eight separate PRs (one per block) are smaller but require more coordination. One combined PR is simpler to review but touches many files.

> [!IMPORTANT]
> **Q3: `allow_callers` — framework default or domain config?** Framework default `allow_callers: []` (allow all) is safe for now. Should per-block callee restrictions be a domain-YAML concern (opt-in tightening) or a framework-YAML mandate (explicit allowlist required)? Recommend domain-YAML opt-in for now.

> [!NOTE]
> **Q4: `dpg_auth` top-level directory vs inside `observability_layer/`?** The design doc says "a cleaner home (auth is not observability)." Top-level `dpg_auth/` is strongly recommended. Confirm before Step 1.

> [!NOTE]
> **Q5: CI static-mode override for tests without Keycloak?** Recommend `keycloak_url: ""` in test config YAML to activate `StaticAuthProvider`. More explicit than an env var; consistent with the config-driven philosophy.

---

## 17. Proposed File Change Summary

### New Files

| File | Purpose |
|---|---|
| `dpg_auth/pyproject.toml` | Package manifest (mirrors `observability_layer/pyproject.toml` pattern) |
| `dpg_auth/src/dpg_auth/__init__.py` | Public API; `build_auth_provider()` factory |
| `dpg_auth/src/dpg_auth/config.py` | `DpgAuthConfig` (self-contained Pydantic; no external imports) |
| `dpg_auth/src/dpg_auth/context.py` | `AuthContext` + `ContextVar` |
| `dpg_auth/src/dpg_auth/logging.py` | `StructuredLogFilter` |
| `dpg_auth/src/dpg_auth/client.py` | `ServiceAuthClient` |
| `dpg_auth/src/dpg_auth/provider/base.py` | `AuthProviderBase` ABC |
| `dpg_auth/src/dpg_auth/provider/keycloak.py` | `KeycloakAuthProvider` |
| `dpg_auth/src/dpg_auth/provider/composite.py` | `CompositeAuthProvider` |
| `dpg_auth/src/dpg_auth/provider/static.py` | `StaticAuthProvider` |
| `dpg_auth/src/dpg_auth/middleware/verify.py` | `VerifyJwtMiddleware` |
| `dpg_auth/src/dpg_auth/middleware/authorize.py` | `AuthorizeMiddleware` |
| `dpg_auth/tests/` (8 test files) | Full test coverage |
| `automation/docker/keycloak/realms/ai-diffusion-platform.json` | Baked realm import (placeholder secrets) |
| `automation/keycloak/onboard_platform_realm.sh` | Production onboarding script |

### Modified Files

| File | Change |
|---|---|
| `automation/docker/docker-compose.dev.yml` | Add `keycloak` service + `keycloak_data` volume |
| `agent_core/main.py` | Construct `ServiceAuthClient`; pass to all HTTP clients |
| `agent_core/src/http_clients/*.py` (5 files) | Add `auth_client` param; use token-injecting client |
| `agent_core/src/http_clients/async_/*.py` (5 files) | Same, async |
| `agent_core/src/servers/orchestration_server.py` | Add middlewares |
| `agent_core/src/schema/config.py` | Inline `DpgAuthConfig` mirror + `dpg_auth` field |
| `agent_core/pyproject.toml` | Add `dpg-auth` dependency |
| `trust_layer/src/server.py` | Add middlewares |
| `trust_layer/src/schema/config.py` | Inline `DpgAuthConfig` mirror + `dpg_auth` field |
| `trust_layer/pyproject.toml` | Add `dpg-auth` dep |
| `knowledge_engine/main.py` | Add middlewares |
| `knowledge_engine/src/schema/config.py` | Add `dpg_auth` section |
| `knowledge_engine/pyproject.toml` | Add `dpg-auth` dep |
| `memory_layer/main.py` | Add middlewares |
| `memory_layer/src/schema/config.py` | Add `dpg_auth` section |
| `memory_layer/pyproject.toml` | Add `dpg-auth` dep |
| `action_gateway/src/server.py` | Add middlewares |
| `action_gateway/src/schema/config.py` | Add `dpg_auth` section |
| `action_gateway/pyproject.toml` | Add `dpg-auth` dep |
| `observability_layer/src/server.py` | Add middlewares |
| `observability_layer/src/schema/config.py` | Add `dpg_auth` section |
| `observability_layer/pyproject.toml` | Add `dpg-auth` dep |
| `reach_layer/web/` (factory + HTTP client code) | Add middlewares + caller-side auth |
| `reach_layer/mcp/src/server.py` | Add middlewares (shadow mode only; static key preserved) |
| `reach_layer/*/pyproject.toml` | Add `dpg-auth` dep |
| `dev-kit/dev_kit/schemas/domain/*.py` (8 files) | Add `DpgAuthSection` |
| `dev-kit/dev_kit/schema.py` | Add `DpgAuthSection` to flat-file copy |
| `dev-kit/dpg/*.yaml` (7 files) | Add `dpg_auth:` defaults block |
| `dev-kit/tests/schemas/domain/test_*.py` (8 files) | Add auth config tests |
