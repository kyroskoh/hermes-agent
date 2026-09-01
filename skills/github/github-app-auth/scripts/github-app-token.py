#!/usr/bin/env python3
"""
GitHub App authentication adapter for Hermes Agent.

Mints short-lived installation access tokens for a GitHub App and prints
them to stdout. Designed to be sourced by shell helpers (github-app-env.sh)
and called from webhook deliveries so every GitHub API action performed by
Hermes is attributed to the App bot identity, not the operator's personal
account.

Authentication flow (RFC 7519 JWT + GitHub Apps):

    GITHUB_APP_ID + private key (PEM)
            │
            ▼
    RS256 JWT signed with iss=<app_id>, iat=now, exp=now+10m
            │
            ▼
    POST https://api.github.com/app/installations/{installation_id}/access_tokens
            │
            ▼
    installation token (TTL ~60 min, returned in JSON)
            │
            ▼
    cached in $HERMES_HOME/.cache/github-app/<installation_id>.json
            │
            ▼
    printed to stdout (capture into $GH_TOKEN / $GITHUB_TOKEN)

Security properties:
    * The private key is read from GITHUB_APP_PRIVATE_KEY_PATH or PEM env var
      — it is NEVER logged or written to the cache.
    * The installation token is written to a 0600 file under
      HERMES_HOME/.cache/github-app/, refreshed 5 min before expiry.
    * The script returns nothing but the token string on stdout; all other
      output goes to stderr.
    * No long-running state. Each invocation re-evaluates the cache.

Required env vars (set in ~/.hermes/.env):
    GITHUB_APP_ID                  numeric App ID
    GITHUB_APP_INSTALLATION_ID     numeric installation ID for this bot
    GITHUB_APP_PRIVATE_KEY_PATH    absolute path to the .pem file
    GITHUB_APP_PRIVATE_KEY         (alternative: PEM inline — discouraged)

Optional:
    GITHUB_API_BASE                override for GitHub Enterprise Server
    GITHUB_APP_TOKEN_REPOS         comma-separated "owner/repo" pairs that
                                   must match the installation; prints the
                                   resolved installation token only if the
                                   requested repo is covered. Empty = always.

Usage:
    # Print the installation token to stdout
    github-app-token.py

    # Print metadata as JSON instead of the token
    github-app-token.py --status

    # Force a refresh (ignore cache)
    github-app-token.py --refresh

    # Validate config without making network calls
    github-app-token.py --check

Exit codes:
    0   success — token printed to stdout
    1   configuration error
    2   network / GitHub API error
    3   cache I/O error

This script uses only the Python standard library so it runs on the same
minimal Python that ships with Hermes. No PyJWT dependency.
"""

from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_JWT_TTL_SECONDS = 9 * 60          # GitHub rejects >10 min; 9 keeps a safety margin
DEFAULT_TOKEN_TTL_GRACE_SECONDS = 5 * 60  # refresh 5 min before expiry
CACHE_TTL_FALLBACK_SECONDS = 50 * 60      # used if GitHub omits expires_at
NETWORK_TIMEOUT_SECONDS = 15

CACHE_ROOT_HINT = "HERMES_HOME"           # env var pointing at ~/.hermes (or profile dir)
CACHE_SUBDIR = Path(".cache") / "github-app"

# Sanity bounds — refuse values that look wrong so a misconfigured app
# never burns a token-mint slot in vain.
_APP_ID_RE = re.compile(r"^\d{1,12}$")
_INSTALLATION_ID_RE = re.compile(r"^\d{1,16}$")


# ─── Pretty errors ───────────────────────────────────────────────────────────


def _die(msg: str, code: int = 1) -> "None":
    print(f"github-app-token: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ─── Env loading (sourced from $HERMES_HOME/.env) ────────────────────────────


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE file. No shell expansion; no comments beyond '#'."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _die(f"cannot read {path}: {exc}", code=3)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip one pair of surrounding quotes if present
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _resolve_env(extra_paths: list[Path]) -> dict[str, str]:
    """Real env wins over .env file; .env values fill in missing keys."""
    hermes_home = os.environ.get(CACHE_ROOT_HINT) or os.path.expanduser("~/.hermes")
    env_path = Path(hermes_home) / ".env"
    file_env = _load_env_file(env_path)
    for p in extra_paths:
        file_env.update(_load_env_file(p))
    merged: dict[str, str] = {}
    merged.update(file_env)
    merged.update(os.environ)
    return merged


# ─── Configuration parsing ──────────────────────────────────────────────────


class AppConfig:
    __slots__ = (
        "app_id",
        "installation_id",
        "private_key_pem",
        "api_base",
        "allowed_repos",
        "cache_dir",
        "jwt_ttl_seconds",
        "token_ttl_grace_seconds",
    )

    def __init__(
        self,
        app_id: str,
        installation_id: str,
        private_key_pem: str,
        api_base: str,
        allowed_repos: list[str],
        cache_dir: Path,
        jwt_ttl_seconds: int,
        token_ttl_grace_seconds: int,
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key_pem = private_key_pem
        self.api_base = api_base
        self.allowed_repos = allowed_repos
        self.cache_dir = cache_dir
        self.jwt_ttl_seconds = jwt_ttl_seconds
        self.token_ttl_grace_seconds = token_ttl_grace_seconds

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "AppConfig":
        app_id = (env.get("GITHUB_APP_ID") or "").strip()
        installation_id = (env.get("GITHUB_APP_INSTALLATION_ID") or "").strip()
        key_path = (env.get("GITHUB_APP_PRIVATE_KEY_PATH") or "").strip()
        key_inline = (env.get("GITHUB_APP_PRIVATE_KEY") or "").strip()

        if not _APP_ID_RE.match(app_id):
            _die(
                "GITHUB_APP_ID is missing or not a numeric App ID "
                "(found: %r)" % (env.get("GITHUB_APP_ID") or "<unset>")
            )
        if not _INSTALLATION_ID_RE.match(installation_id):
            _die(
                "GITHUB_APP_INSTALLATION_ID is missing or not numeric "
                "(found: %r)" % (env.get("GITHUB_APP_INSTALLATION_ID") or "<unset>")
            )

        if key_inline:
            pem = key_inline.replace("\\n", "\n")
        elif key_path:
            p = Path(key_path).expanduser()
            if not p.is_file():
                _die(f"GITHUB_APP_PRIVATE_KEY_PATH points at a non-existent file: {p}")
            try:
                pem = p.read_text(encoding="utf-8")
            except OSError as exc:
                _die(f"cannot read private key {p}: {exc}")
        else:
            _die(
                "set GITHUB_APP_PRIVATE_KEY_PATH (recommended) or "
                "GITHUB_APP_PRIVATE_KEY in ~/.hermes/.env"
            )
        if "BEGIN PRIVATE KEY" not in pem and "BEGIN RSA PRIVATE KEY" not in pem:
            _die(
                "private key does not look like a PEM block "
                "(expected '-----BEGIN PRIVATE KEY-----' (PKCS#8) or "
                "'-----BEGIN RSA PRIVATE KEY-----' (PKCS#1))"
            )

        api_base = (env.get("GITHUB_API_BASE") or DEFAULT_API_BASE).rstrip("/")
        if not api_base.startswith("http"):
            _die(f"GITHUB_API_BASE must be an http(s) URL (got {api_base!r})")

        repos_raw = (env.get("GITHUB_APP_TOKEN_REPOS") or "").strip()
        allowed_repos = []
        for chunk in repos_raw.split(","):
            chunk = chunk.strip()
            if chunk:
                if "/" not in chunk:
                    _die(f"GITHUB_APP_TOKEN_REPOS entry {chunk!r} is not owner/repo")
                allowed_repos.append(chunk)

        cache_dir = (
            Path(env.get(CACHE_ROOT_HINT) or os.path.expanduser("~/.hermes"))
            / CACHE_SUBDIR
        )

        return cls(
            app_id=app_id,
            installation_id=installation_id,
            private_key_pem=pem,
            api_base=api_base,
            allowed_repos=allowed_repos,
            cache_dir=cache_dir,
            jwt_ttl_seconds=DEFAULT_JWT_TTL_SECONDS,
            token_ttl_grace_seconds=DEFAULT_TOKEN_TTL_GRACE_SECONDS,
        )


# ─── JWT signing (RS256, stdlib only) ───────────────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_jwt(cfg: AppConfig) -> str:
    """Return a compact JWS using RS256 over the configured PEM key.

    The header / payload are base64url(JSON) strings; the signature is
    PKCS#1 v1.5 RSA with SHA-256, computed manually so this script does not
    depend on PyJWT or cryptography.
    """
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": now - 30,            # tolerate 30 s clock skew
        "exp": now + cfg.jwt_ttl_seconds,
        "iss": cfg.app_id,
    }
    h = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{h}.{p}".encode("ascii")

    sig = _rsa_pkcs1_v15_sha256(signing_input, cfg.private_key_pem)
    return f"{h}.{p}.{_b64url(sig)}"


def _rsa_pkcs1_v15_sha256(message: bytes, pem: str) -> bytes:
    """Pure-stdlib PKCS#1 v1.5 RSA signature with SHA-256.

    Accepts both PKCS#1 ("-----BEGIN RSA PRIVATE KEY-----") and PKCS#8
    ("-----BEGIN PRIVATE KEY-----") PEM blocks. GitHub Apps issue either
    format; both are RSA keys. The PKCS#8 wrapper is unwrapped to its
    inner OCTET STRING which contains the same PKCS#1 RSAPrivateKey
    sequence.

    Raises SystemExit(1) on any structural problem — the error is fatal
    because without a valid signature the rest of the flow is pointless.
    """
    import hashlib
    import struct

    # Decode PEM → DER
    body = "".join(
        line.strip() for line in pem.splitlines() if line and not line.startswith("-----")
    )
    try:
        der = base64.b64decode(body, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        _die(f"private key PEM is malformed: {exc}")

    is_pkcs8 = "BEGIN PRIVATE KEY" in pem and "BEGIN RSA PRIVATE KEY" not in pem
    n, e, d = _parse_rsa_private_key(der, pkcs8=is_pkcs8)

    # PKCS#1 v1.5 encoding for SHA-256
    digest = hashlib.sha256(message).digest()
    k = (n.bit_length() + 7) // 8
    em = _pkcs1_v15_encode_sha256(digest, k)
    signature_int = _pow_int(em, d, n)
    # Reduce into [0, 2^k) — pow() can produce negatives if d is negative
    # (defensive; real RSA keys always have positive d).
    signature_int &= (1 << (8 * k)) - 1
    sig_bytes = signature_int.to_bytes(k, byteorder="big")
    if len(sig_bytes) != k:
        sig_bytes = b"\x00" * (k - len(sig_bytes)) + sig_bytes
    return sig_bytes


def _read_asn1_length(data: bytes, idx: int) -> tuple[int, int]:
    """Return (length, header_end_offset)."""
    if idx >= len(data):
        _die("private key: truncated ASN.1 length")
    first = data[idx]
    idx += 1
    if first < 0x80:
        return first, idx
    nbytes = first & 0x7F
    if nbytes == 0 or nbytes > 4:
        _die("private key: unsupported ASN.1 length encoding")
    if idx + nbytes > len(data):
        _die("private key: truncated ASN.1 length body")
    length = int.from_bytes(data[idx : idx + nbytes], "big")
    return length, idx + nbytes


def _read_asn1_tlv(data: bytes, idx: int) -> tuple[int, int, int]:
    """Read a TLV at idx; return (tag, content_start, content_end)."""
    if idx >= len(data):
        _die("private key: truncated ASN.1 TLV header")
    tag = data[idx]
    length, content_start = _read_asn1_length(data, idx + 1)
    content_end = content_start + length
    if content_end > len(data):
        _die("private key: ASN.1 TLV content overruns buffer")
    return tag, content_start, content_end


def _parse_rsa_private_key(der: bytes, *, pkcs8: bool) -> tuple[int, int, int]:
    """Walk the ASN.1 and return (n, e, d) for the RSA key.

    PKCS#1 (RSAPrivateKey):
        SEQUENCE { n, e, d, p, q, ... }

    PKCS#8 (OneAsymmetricKey):
        SEQUENCE {
            INTEGER version
            SEQUENCE { OID, ... } algorithm
            OCTET STRING {                    <-- contains PKCS#1 RSAPrivateKey
                SEQUENCE { n, e, d, ... }
            }
        }

    We only need n, e, d to sign.
    """
    if pkcs8:
        # Top-level SEQUENCE
        tag, content_start, content_end = _read_asn1_tlv(der, 0)
        if tag != 0x30:
            _die("private key (PKCS#8): expected top-level SEQUENCE")
        cursor = content_start
        # version (INTEGER)
        tag, content_start, content_end = _read_asn1_tlv(der, cursor)
        if tag != 0x02:
            _die("private key (PKCS#8): expected INTEGER version")
        cursor = content_end
        # algorithm (SEQUENCE) — skip it
        tag, content_start, content_end = _read_asn1_tlv(der, cursor)
        if tag != 0x30:
            _die("private key (PKCS#8): expected SEQUENCE algorithm")
        cursor = content_end
        # privateKey (OCTET STRING) — unwrap
        tag, content_start, content_end = _read_asn1_tlv(der, cursor)
        if tag != 0x04:
            _die("private key (PKCS#8): expected OCTET STRING privateKey")
        inner = der[content_start:content_end]
        # Optional [0] attributes present in v1/v2 keys may follow — but
        # the OCTET STRING content already holds a complete RSAPrivateKey.
        return _parse_rsa_private_key(inner, pkcs8=False)

    # PKCS#1 RSAPrivateKey
    tag, content_start, content_end = _read_asn1_tlv(der, 0)
    if tag != 0x30:
        _die("private key (PKCS#1): expected top-level SEQUENCE")
    cursor = content_start

    def _next_int(name: str) -> int:
        nonlocal cursor
        tag, cs, ce = _read_asn1_tlv(der, cursor)
        if tag != 0x02:
            _die(f"private key: expected INTEGER {name}")
        body = der[cs:ce]
        if not body:
            _die(f"private key: empty INTEGER {name}")
        value = int.from_bytes(body, "big")
        # ASN.1 INTEGER is signed big-endian. If the leading byte has the
        # high bit set, DER requires a leading 0x00 padding byte so the
        # integer parses as positive. Strip that padding for the math.
        if body[0] == 0x00 and len(body) > 1 and body[1] & 0x80:
            value = int.from_bytes(body[1:], "big")
        cursor = ce
        return value

    # RSAPrivateKey has a leading "version" INTEGER (=0) before n.
    _next_int("version")
    n = _next_int("n")
    e = _next_int("e")
    d = _next_int("d")
    return n, e, d


def _pkcs1_v15_encode_sha256(digest: bytes, k: int) -> int:
    if k < 11 + len(digest) + 10:
        _die("private key modulus too short for SHA-256 PKCS#1 v1.5")
    digest_info = bytes.fromhex(
        "3031300d060960864801650304020105000420"
    ) + digest
    pad_len = k - len(digest_info) - 3
    em = b"\x00\x01" + (b"\xff" * pad_len) + b"\x00" + digest_info
    return int.from_bytes(em, "big")


def _pow_int(base: int, exponent: int, modulus: int) -> int:
    return pow(base, exponent, modulus)


# ─── Cache (installation token persistence) ─────────────────────────────────


def _cache_file(cfg: AppConfig) -> Path:
    return cfg.cache_dir / f"installation-{cfg.installation_id}.json"


def _cache_load(cfg: AppConfig) -> Optional[dict[str, Any]]:
    path = _cache_file(cfg)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    expires_at = data.get("expires_at")
    if not isinstance(token, str) or not isinstance(expires_at, (int, float)):
        return None
    return data


def _cache_save(cfg: AppConfig, payload: dict[str, Any]) -> None:
    try:
        cfg.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        _die(f"cannot create cache dir {cfg.cache_dir}: {exc}", code=3)
    path = _cache_file(cfg)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        _die(f"cannot write cache {path}: {exc}", code=3)


# ─── GitHub API calls ──────────────────────────────────────────────────────


def _http_post_json(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    raw = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=raw, method="POST", headers={"Accept": "application/vnd.github+json", **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = ""
        _die(
            f"GitHub API POST {url} -> HTTP {exc.code}: {detail.strip()[:300] or exc.reason}",
            code=2,
        )
    except urllib.error.URLError as exc:
        _die(f"GitHub API POST {url} failed: {exc.reason}", code=2)


def _http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/vnd.github+json", **headers}
    )
    try:
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = ""
        _die(
            f"GitHub API GET {url} -> HTTP {exc.code}: {detail.strip()[:300] or exc.reason}",
            code=2,
        )
    except urllib.error.URLError as exc:
        _die(f"GitHub API GET {url} failed: {exc.reason}", code=2)


def _mint_installation_token(cfg: AppConfig) -> dict[str, Any]:
    jwt_token = _sign_jwt(cfg)
    url = f"{cfg.api_base}/app/installations/{cfg.installation_id}/access_tokens"
    body = {}
    if cfg.allowed_repos:
        body["repositories"] = cfg.allowed_repos
        body["repository_ids"] = []
    data = _http_post_json(
        url,
        body=body,
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-github-app-auth/1.0",
        },
    )
    if "token" not in data or "expires_at" not in data:
        _die(f"GitHub response missing token/expires_at: {json.dumps(data)[:300]}", code=2)
    expires_at = _parse_iso8601(data["expires_at"])
    payload = {
        "token": data["token"],
        "expires_at": expires_at,
        "permissions": data.get("permissions", {}),
        "repository_selection": data.get("repository_selection"),
        "installation_id": cfg.installation_id,
        "app_id": cfg.app_id,
        "minted_at": int(time.time()),
    }
    _cache_save(cfg, payload)
    return payload


def _parse_iso8601(value: str) -> int:
    # GitHub returns "2024-05-01T12:34:56Z" — handle the common shape without
    # pulling in datetime.fromisoformat (which only added 'Z' support in 3.11).
    if not isinstance(value, str):
        return int(time.time()) + CACHE_TTL_FALLBACK_SECONDS
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return int(time.time()) + CACHE_TTL_FALLBACK_SECONDS


# ─── Public entry point ─────────────────────────────────────────────────────


def get_installation_token(
    cfg: AppConfig, *, force_refresh: bool = False, allowed_repo: Optional[str] = None
) -> str:
    """Return a fresh installation token, refreshing the cache if needed."""
    if cfg.allowed_repos and allowed_repo and allowed_repo not in cfg.allowed_repos:
        _die(f"repository {allowed_repo!r} is not in GITHUB_APP_TOKEN_REPOS")
    now = int(time.time())
    cached = None if force_refresh else _cache_load(cfg)
    if cached:
        expires_at = int(cached.get("expires_at", 0))
        if expires_at - cfg.token_ttl_grace_seconds > now:
            return cached["token"]  # type: ignore[return-value]
    payload = _mint_installation_token(cfg)
    return payload["token"]


def status(cfg: AppConfig) -> dict[str, Any]:
    """Return JSON-safe diagnostic info — never includes the token itself."""
    cached = _cache_load(cfg) or {}
    now = int(time.time())
    expires_at = int(cached.get("expires_at", 0))
    return {
        "app_id": cfg.app_id,
        "installation_id": cfg.installation_id,
        "api_base": cfg.api_base,
        "cache_file": str(_cache_file(cfg)),
        "cached": bool(cached),
        "expires_at": expires_at,
        "expires_in_seconds": (expires_at - now) if expires_at else None,
        "allowed_repos": cfg.allowed_repos,
        "permissions": cached.get("permissions"),
        "repository_selection": cached.get("repository_selection"),
    }


# ─── CLI ────────────────────────────────────────────────────────────────────


def _arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="github-app-token",
        description="Mint a GitHub App installation token for Hermes.",
    )
    p.add_argument(
        "--repo",
        default=None,
        help="owner/repo — if set, must be in GITHUB_APP_TOKEN_REPOS",
    )
    p.add_argument(
        "--refresh", action="store_true", help="ignore the on-disk cache"
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="print diagnostic JSON to stdout (no token)",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="validate config without contacting GitHub",
    )
    p.add_argument(
        "--env-files",
        default="",
        help="comma-separated extra env files to read (debugging only)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _arg_parser().parse_args(argv)
    extra_paths = [Path(p).expanduser() for p in args.env_files.split(",") if p.strip()]
    env = _resolve_env(extra_paths)
    cfg = AppConfig.from_env(env)

    if args.check:
        # Validates every config key and exits without writing anything.
        # Also dry-runs the JWT signer over a dummy payload so a broken key
        # shows up immediately instead of on first use.
        try:
            jwt_token = _sign_jwt(cfg)
            if jwt_token.count(".") != 2:
                _die("signed JWT does not have three segments")
        except SystemExit:
            raise
        except Exception as exc:
            _die(f"config check failed: {exc}")
        print("ok")
        return 0

    if args.status:
        print(json.dumps(status(cfg), indent=2))
        return 0

    token = get_installation_token(
        cfg, force_refresh=args.refresh, allowed_repo=args.repo
    )
    # Single line, no trailing whitespace — safe to capture in shell.
    sys.stdout.write(token)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
