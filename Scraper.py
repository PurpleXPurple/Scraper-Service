"""
Scraper.py — single-file nexus server.

Fixes in this version:
- All Pylance type errors resolved (explicit casts, local narrowing, guards).
- Arabic/Hebrew/Farsi shaping for report PNG (arabic-reshaper + python-bidi).
- RTL-aware Pillow report (mirrored layout, right-anchored labels).
- Memory footprint cut: lazy imports, __slots__, bounded pools, streaming caps.
- Rate limiter garbage collection, cache size caps, starfield retained.

Run: python Scraper.py
"""
from __future__ import annotations

# ─── stdlib ────────────────────────────────────────────────
import asyncio
import base64
import gc
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import socket
import stat
import sys
import time
import unicodedata
import uuid
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from statistics import stdev
from typing import Any, Literal, cast
from urllib.parse import quote_plus, urlencode, urljoin, urlparse
from xml.etree import ElementTree as ET

# ─── required ──────────────────────────────────────────────
try:
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse, ORJSONResponse, Response
    from starlette.exceptions import HTTPException as StarletteHTTPException
except ImportError as exc:
    print(f"[fatal] missing: {exc.name}", file=sys.stderr)
    sys.exit(1)

try:
    import aiosqlite
except ImportError:
    print("[fatal] missing: aiosqlite", file=sys.stderr)
    sys.exit(1)

# ─── optional, with explicit local narrowing ───────────────
_PasswordHasher: Any = None
_VerifyMismatchError: Any = None
_VerificationError: Any = None
_InvalidHashError: Any = None
try:
    from argon2 import PasswordHasher as _PH
    from argon2.exceptions import (InvalidHashError as _IHE,
                                    VerificationError as _VE,
                                    VerifyMismatchError as _VME)
    _PasswordHasher = _PH
    _VerifyMismatchError = _VME
    _VerificationError = _VE
    _InvalidHashError = _IHE
except ImportError:
    pass

_AESGCM: Any = None
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AES
    _AESGCM = _AES
except ImportError:
    pass

_CurlAsyncSession: Any = None
try:
    from curl_cffi.requests import AsyncSession as _CAS
    _CurlAsyncSession = _CAS
except ImportError:
    pass

_HTMLParser: Any = None
try:
    from selectolax.parser import HTMLParser as _HP
    _HTMLParser = _HP
except ImportError:
    pass

_PILImage: Any = None
_PILDraw: Any = None
_PILFont: Any = None
try:
    from PIL import Image as _I
    from PIL import ImageDraw as _D
    from PIL import ImageFont as _F
    _PILImage = _I
    _PILDraw = _D
    _PILFont = _F
except ImportError:
    pass

_pymupdf: Any = None
try:
    import pymupdf as _pm
    _pymupdf = _pm
except ImportError:
    try:
        import fitz as _pm  # type: ignore
        _pymupdf = _pm
    except ImportError:
        pass

_zstd: Any = None
try:
    import zstandard as _z
    _zstd = _z
except ImportError:
    pass

# Arabic/Hebrew shaping (optional)
_reshaper: Any = None
_get_display: Any = None
try:
    import arabic_reshaper as _AR
    from bidi.algorithm import get_display as _GD
    _reshaper = _AR
    _get_display = _GD
except ImportError:
    pass


# ═════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent
VAR_DIR = BASE_DIR / "var"
SECRETS_PATH = VAR_DIR / "secrets.json"
INDEX_PATH = BASE_DIR / "index.html"
ASSETS_DIR = BASE_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
HOST = os.environ.get("NEXUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("NEXUS_PORT", "8000"))
VERSION = "1.2.0"
START_TIME = time.time()

# memory caps
MAX_RESPONSE_BYTES = 8 * 1024 * 1024       # 8 MB per fetch
MAX_CACHE_ENTRIES = 500                    # rows in SQLite cache
MAX_RATE_BUCKETS = 5000                    # sliding windows in memory
MAX_STARS = 120                            # client-side stars
MAX_GRAPH_NODES = 60                       # client-side node budget

log = logging.getLogger("nexus")

# Arabic/Hebrew range detection
_RTL_RANGE = re.compile(r"[\u0590-\u05FF\u0600-\u06FF\u0700-\u074F\u0750-\u077F\u08A0-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFF]")
_ARABIC_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")

# Minimal MSA stopword list — 60 words
_AR_STOPWORDS = frozenset("""
من في على إلى عن أن إن كان كانت يكون مع هذا هذه ذلك تلك التي الذي الذين
هو هي هم هن أنا أنت نحن أنتم أو و لكن ثم قد لقد كل بعض غير بين حتى إذا
لا ما لم لن هل يا أي أية كذا هكذا هناك هنالك هنا حيث عندما بعد قبل الآن
""".split())


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _has_rtl(text: str) -> bool:
    return bool(_RTL_RANGE.search(text))


def _is_arabic(text: str) -> bool:
    return bool(_ARABIC_RANGE.search(text))


def _shape(text: str) -> str:
    """Apply Arabic shaping + bidi reordering for display. Idempotent on Latin."""
    if not text:
        return text
    if _reshaper is None or _get_display is None:
        return text
    try:
        return _get_display(_reshaper.reshape(text))
    except Exception:
        return text


def _strip_diacritics(text: str) -> str:
    return _DIACRITICS.sub("", text)


# ═════════════════════════════════════════════════════════════
# EXCEPTIONS
# ═════════════════════════════════════════════════════════════
class NexusError(Exception): ...
class ConfigError(NexusError): ...
class SecurityError(NexusError): ...
class RateLimitError(NexusError): ...
class UpstreamError(NexusError): ...
class ParseError(NexusError): ...
class ValidationError(NexusError): ...
class NotFoundError(NexusError): ...


# ═════════════════════════════════════════════════════════════
# CONFIG
# ═════════════════════════════════════════════════════════════
_REQUIRED_TOP = ("meta", "http", "security", "storage", "logging")


class Config:
    __slots__ = ("_data", "_path")

    def __init__(self, data: dict[str, Any], path: Path):
        self._data = data
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def get_int(self, dotted: str, default: int) -> int:
        v = self.get(dotted, default)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def get_bool(self, dotted: str, default: bool) -> bool:
        v = self.get(dotted, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes", "on")
        return default

    def get_str(self, dotted: str, default: str) -> str:
        v = self.get(dotted, default)
        return str(v) if v is not None else default

    def get_dict(self, dotted: str) -> dict[str, Any]:
        v = self.get(dotted, {})
        return v if isinstance(v, dict) else {}

    def get_list(self, dotted: str) -> list[Any]:
        v = self.get(dotted, [])
        return v if isinstance(v, list) else []

    def require(self, dotted: str) -> Any:
        sentinel = object()
        v = self.get(dotted, sentinel)
        if v is sentinel:
            raise ConfigError(f"missing required config key: {dotted}")
        return v


def find_config() -> Path:
    env = os.environ.get("NEXUS_CONFIG")
    if env:
        p = Path(env)
        if not p.is_absolute():
            p = BASE_DIR / p
        if p.exists():
            return p
    for name in ("Config.json", "config.json"):
        p = BASE_DIR / name
        if p.exists():
            return p
    raise ConfigError(f"Config.json not found next to {BASE_DIR}")


def load_config(path: Path) -> Config:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name}: invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: root must be a JSON object")
    missing = [k for k in _REQUIRED_TOP if k not in data]
    if missing:
        raise ConfigError(f"{path.name}: missing top-level keys: {', '.join(missing)}")
    return Config(data, path)


# ═════════════════════════════════════════════════════════════
# LOGGING
# ═════════════════════════════════════════════════════════════
_REDACT_KEYS = frozenset({"password", "token", "secret", "authorization",
                           "cookie", "api_key", "session", "refresh"})


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if isinstance(k, str) and k.lower() in _REDACT_KEYS else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


class JsonFormatter(logging.Formatter):
    _EXTRA = ("request_id", "method", "path", "status", "latency_ms", "ip")

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": _utc_iso() + "Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k in self._EXTRA:
            v = getattr(record, k, None)
            if v is not None:
                entry[k] = v
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(_redact(entry), ensure_ascii=False, separators=(",", ":"))


def configure_logging(cfg: Config) -> None:
    level_name = cfg.get_str("logging.level", "info").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("uvicorn.access", "uvicorn.error", "asyncio", "httpcore", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ═════════════════════════════════════════════════════════════
# SECRETS
# ═════════════════════════════════════════════════════════════
_SECRET_GENERATORS = {
    "NEXUS_AES_KEY":    lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
    "NEXUS_JWT_SECRET": lambda: secrets.token_urlsafe(48),
    "NEXUS_HMAC_KEY":   lambda: secrets.token_urlsafe(32),
}
_secrets_done = False


def ensure_secrets() -> dict[str, str]:
    global _secrets_done
    if _secrets_done:
        return {k: os.environ.get(k, "") for k in _SECRET_GENERATORS}

    file_data: dict[str, str] = {}
    if SECRETS_PATH.exists():
        try:
            raw = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                file_data = {k: str(v) for k, v in raw.items() if v}
        except Exception as exc:
            log.warning("could not read %s: %s", SECRETS_PATH, exc)

    generated = False
    for name, gen in _SECRET_GENERATORS.items():
        if os.environ.get(name):
            continue
        if file_data.get(name):
            os.environ[name] = file_data[name]
            continue
        os.environ[name] = gen()
        file_data[name] = os.environ[name]
        generated = True

    if generated:
        try:
            SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = SECRETS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(file_data, indent=2), encoding="utf-8")
            try:
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass
            tmp.replace(SECRETS_PATH)
            log.info("generated secrets at %s", SECRETS_PATH)
        except Exception as exc:
            log.warning("could not persist secrets: %s", exc)

    _secrets_done = True
    return {k: os.environ[k] for k in _SECRET_GENERATORS}


# ═════════════════════════════════════════════════════════════
# CRYPTO
# ═════════════════════════════════════════════════════════════
class AES:
    __slots__ = ("_aes",)

    def __init__(self, key: bytes):
        if _AESGCM is None:
            raise SecurityError("cryptography package not installed")
        if len(key) != 32:
            raise SecurityError("AES-256-GCM key must be 32 bytes")
        self._aes = _AESGCM(key)

    @classmethod
    def from_env(cls, name: str) -> "AES":
        raw = os.environ.get(name)
        if not raw:
            raise SecurityError(f"env var {name} not set")
        try:
            key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except Exception as exc:
            raise SecurityError(f"{name} is not urlsafe base64") from exc
        return cls(key)

    def encrypt(self, pt: bytes, aad: bytes | None = None) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._aes.encrypt(nonce, pt, aad)

    def decrypt(self, blob: bytes, aad: bytes | None = None) -> bytes:
        if len(blob) < 28:
            raise SecurityError("ciphertext too short")
        return self._aes.decrypt(blob[:12], blob[12:], aad)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class JWT:
    __slots__ = ("_secret", "_iss", "_aud", "_access_ttl", "_refresh_ttl", "_leeway")

    def __init__(self, secret: bytes, issuer: str, audience: str,
                 access_ttl: int, refresh_ttl: int, leeway: int = 30):
        self._secret = secret
        self._iss = issuer
        self._aud = audience
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        self._leeway = leeway

    @classmethod
    def from_env(cls, cfg: Config) -> "JWT":
        raw = os.environ.get("NEXUS_JWT_SECRET")
        if not raw:
            raise SecurityError("NEXUS_JWT_SECRET not set")
        j = cfg.get_dict("security.jwt")
        if j.get("algorithm", "HS256") != "HS256":
            raise SecurityError("only HS256 JWT is supported")
        return cls(
            secret=raw.encode(),
            issuer=str(j.get("issuer", "nexus")),
            audience=str(j.get("audience", "nexus-client")),
            access_ttl=int(j.get("access_ttl_sec", 3600)),
            refresh_ttl=int(j.get("refresh_ttl_sec", 2592000)),
            leeway=int(j.get("leeway_sec", 30)),
        )

    def _sign(self, msg: bytes) -> bytes:
        return hmac.new(self._secret, msg, hashlib.sha256).digest()

    def issue(self, sub: str, *, kind: str = "access", extra: dict[str, Any] | None = None) -> str:
        now = int(time.time())
        ttl = self._access_ttl if kind == "access" else self._refresh_ttl
        header = {"alg": "HS256", "typ": "JWT"}
        payload: dict[str, Any] = {"iss": self._iss, "aud": self._aud, "sub": sub,
                                    "iat": now, "nbf": now, "exp": now + ttl, "kind": kind}
        if extra:
            payload.update(extra)
        h = _b64u(json.dumps(header, separators=(",", ":")).encode())
        p = _b64u(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64u(self._sign(f"{h}.{p}".encode()))
        return f"{h}.{p}.{sig}"

    def verify(self, token: str, *, kind: str = "access") -> dict[str, Any]:
        try:
            h, p, s = token.split(".")
        except ValueError as exc:
            raise SecurityError("malformed token") from exc
        expected = self._sign(f"{h}.{p}".encode())
        if not hmac.compare_digest(expected, _b64u_dec(s)):
            raise SecurityError("bad signature")
        try:
            header = json.loads(_b64u_dec(h))
            payload = json.loads(_b64u_dec(p))
        except Exception as exc:
            raise SecurityError("corrupt token") from exc
        if header.get("alg") != "HS256":
            raise SecurityError("unsupported algorithm")
        now = int(time.time())
        if payload.get("iss") != self._iss: raise SecurityError("bad issuer")
        if payload.get("aud") != self._aud: raise SecurityError("bad audience")
        if kind and payload.get("kind") != kind: raise SecurityError("bad token kind")
        if "nbf" in payload and now + self._leeway < payload["nbf"]:
            raise SecurityError("not yet valid")
        if "exp" in payload and now - self._leeway >= payload["exp"]:
            raise SecurityError("expired")
        return payload


class PasswordHasherWrapper:
    __slots__ = ("_ph",)

    def __init__(self, cfg: Config):
        if _PasswordHasher is None:
            raise SecurityError("argon2-cffi not installed")
        a = cfg.get_dict("security.argon2")
        self._ph = _PasswordHasher(
            time_cost=int(a.get("time_cost", 3)),
            memory_cost=int(a.get("memory_cost", 65536)),
            parallelism=int(a.get("parallelism", 4)),
            hash_len=int(a.get("hash_len", 32)),
            salt_len=int(a.get("salt_len", 16)),
        )

    def hash(self, pw: str) -> str:
        return self._ph.hash(pw)

    def verify(self, pw: str, hashed: str) -> bool:
        try:
            return self._ph.verify(hashed, pw)
        except (_VerifyMismatchError, _VerificationError, _InvalidHashError):
            return False


# ═════════════════════════════════════════════════════════════
# SSRF GUARD
# ═════════════════════════════════════════════════════════════
class SSRFGuard:
    __slots__ = ("_schemes", "_blocked")

    def __init__(self, schemes: list[str], blocked_hosts: list[str]):
        self._schemes = set(schemes)
        self._blocked = {h.lower() for h in blocked_hosts}

    @classmethod
    def from_config(cls, cfg: Config) -> "SSRFGuard":
        s = cfg.get_dict("security.ssrf_protection")
        schemes = s.get("scheme_allowlist", ["http", "https"])
        hosts = s.get("blocked_hosts",
                      ["localhost", "127.0.0.1", "0.0.0.0", "::1",
                       "169.254.169.254", "metadata.google.internal"])
        return cls(
            schemes=[str(x) for x in schemes] if isinstance(schemes, list) else ["http", "https"],
            blocked_hosts=[str(x) for x in hosts] if isinstance(hosts, list) else [],
        )

    def check(self, url: str) -> str:
        try:
            parsed = urlparse(url)
        except Exception as exc:
            raise ValidationError("invalid url") from exc
        if (parsed.scheme or "").lower() not in self._schemes:
            raise ValidationError(f"scheme not allowed: {parsed.scheme}")
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValidationError("missing host")
        if host in self._blocked:
            raise ValidationError("host is blocked")
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise ValidationError(f"cannot resolve host: {exc}") from exc
        for info in infos:
            try:
                ip = ip_address(info[4][0])
            except ValueError:
                continue
            if ip.is_loopback or ip.is_private or ip.is_link_local:
                raise ValidationError("blocked address")
            if str(ip) in {"169.254.169.254", "fd00:ec2::254"}:
                raise ValidationError("metadata endpoint blocked")
        return url


# ═════════════════════════════════════════════════════════════
# STORAGE
# ═════════════════════════════════════════════════════════════
class Storage:
    __slots__ = ("conn", "path", "_write_lock")

    def __init__(self, conn: aiosqlite.Connection, path: Path):
        self.conn = conn
        self.path = path
        self._write_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, cfg: Config) -> "Storage":
        raw_path = cfg.get_str("storage.path", "var/nexus.db")
        path = BASE_DIR / raw_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA cache_size=-8000")  # 8 MB page cache
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status INTEGER NOT NULL,
                latency_ms REAL NOT NULL,
                ip_hash TEXT NOT NULL,
                ip_exact TEXT,
                request_id TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_request_log_ts ON request_log(ts);
            CREATE INDEX IF NOT EXISTS ix_request_log_ip ON request_log(ip_hash);

            CREATE TABLE IF NOT EXISTS cache_entry (
                key TEXT PRIMARY KEY,
                ciphertext BLOB NOT NULL,
                compressed INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_cache_expires ON cache_entry(expires_at);
        """)
        await conn.commit()
        return cls(conn, path)

    async def log_request(self, *, method: str, path: str, status: int,
                          latency_ms: float, ip_hash: str,
                          ip_exact: str | None, request_id: str) -> None:
        try:
            async with self._write_lock:
                await self.conn.execute(
                    "INSERT INTO request_log (ts, method, path, status, latency_ms, "
                    "ip_hash, ip_exact, request_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_utc_iso(), method, path, status, latency_ms,
                     ip_hash, ip_exact, request_id),
                )
                await self.conn.commit()
        except Exception as exc:
            log.debug("log_request failed: %s", exc)

    async def cache_get(self, key: str) -> tuple[bytes, bool] | None:
        try:
            async with self.conn.execute(
                "SELECT ciphertext, compressed, expires_at FROM cache_entry WHERE key = ?",
                (key,),
            ) as cur:
                row = await cur.fetchone()
        except Exception:
            return None
        if row is None:
            return None
        ct, comp, expires_at = row
        try:
            if datetime.fromisoformat(str(expires_at)) < datetime.now(timezone.utc).replace(tzinfo=None):
                async with self._write_lock:
                    await self.conn.execute("DELETE FROM cache_entry WHERE key = ?", (key,))
                    await self.conn.commit()
                return None
        except Exception:
            pass
        return bytes(ct), bool(comp)

    async def cache_put(self, key: str, ct: bytes, compressed: bool,
                        ttl_sec: int, size: int) -> None:
        try:
            expires = (datetime.now(timezone.utc).replace(tzinfo=None)
                       + timedelta(seconds=ttl_sec)).isoformat()
            async with self._write_lock:
                await self.conn.execute(
                    "INSERT OR REPLACE INTO cache_entry "
                    "(key, ciphertext, compressed, expires_at, size_bytes, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (key, ct, 1 if compressed else 0, expires, size, _utc_iso()),
                )
                # trim old rows if we're over budget
                await self.conn.execute(
                    "DELETE FROM cache_entry WHERE key NOT IN "
                    "(SELECT key FROM cache_entry ORDER BY created_at DESC LIMIT ?)",
                    (MAX_CACHE_ENTRIES,),
                )
                await self.conn.commit()
        except Exception as exc:
            log.debug("cache_put failed: %s", exc)

    async def close(self) -> None:
        try:
            await self.conn.close()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════
# CACHE
# ═════════════════════════════════════════════════════════════
class Cache:
    __slots__ = ("_s", "_aes", "_hmac_key", "_default_ttl", "_ttl",
                 "_compress", "_max_bytes", "_zc", "_zd")

    def __init__(self, storage: Storage, aes: AES, hmac_key: bytes,
                 default_ttl: int, ttl_by_type: dict[str, Any],
                 compress: bool, max_kb: int):
        self._s = storage
        self._aes = aes
        self._hmac_key = hmac_key
        self._default_ttl = default_ttl
        self._ttl = {str(k): int(v) for k, v in (ttl_by_type or {}).items()
                     if isinstance(v, (int, float))}
        self._compress = compress and _zstd is not None
        self._max_bytes = max_kb * 1024
        if self._compress and _zstd is not None:
            self._zc = _zstd.ZstdCompressor(level=3)
            self._zd = _zstd.ZstdDecompressor()
        else:
            self._zc = None
            self._zd = None

    def key(self, ns: str, payload: str) -> str:
        return hmac.new(self._hmac_key, f"{ns}:{payload}".encode(),
                        hashlib.sha256).hexdigest()

    async def get(self, ns: str, payload: str) -> bytes | None:
        row = await self._s.cache_get(self.key(ns, payload))
        if row is None:
            return None
        ct, compressed = row
        try:
            pt = self._aes.decrypt(ct)
        except Exception:
            return None
        if compressed and self._zd is not None:
            try:
                pt = self._zd.decompress(pt)
            except Exception:
                return None
        return pt

    async def put(self, ns: str, payload: str, value: bytes,
                  *, type_key: str = "url_fetch") -> None:
        ttl = self._ttl.get(type_key, self._default_ttl)
        key = self.key(ns, payload)
        data = value
        compressed = False
        if self._compress and self._zc is not None and len(value) > 512:
            try:
                data = self._zc.compress(value)
                compressed = True
            except Exception:
                data = value
                compressed = False
        if len(data) > self._max_bytes:
            return
        ct = self._aes.encrypt(data)
        await self._s.cache_put(key, ct, compressed, ttl, len(ct))


# ═════════════════════════════════════════════════════════════
# RATE LIMITER
# ═════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class Limit:
    limit: int
    window: float
    burst: int = 0

    @property
    def effective(self) -> int:
        return self.limit + self.burst


class RateLimiter:
    __slots__ = ("_buckets", "_lock", "_hits_since_gc")

    def __init__(self):
        self._buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._hits_since_gc = 0

    async def check(self, route: str, key: str,
                    spec: Limit) -> tuple[bool, float, int]:
        now = time.monotonic()
        cutoff = now - spec.window
        async with self._lock:
            bucket = self._buckets[(route, key)]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            allowed = len(bucket) < spec.effective
            if allowed:
                bucket.append(now)
            remaining = max(0, spec.effective - len(bucket))
            reset = (bucket[0] + spec.window - now) if bucket else 0.0
            self._hits_since_gc += 1
            # aggressive garbage collection: evict empty buckets and cap total
            if self._hits_since_gc >= 2000 or len(self._buckets) > MAX_RATE_BUCKETS:
                self._hits_since_gc = 0
                dead = [k for k, v in self._buckets.items() if not v or v[-1] < cutoff]
                for k in dead:
                    self._buckets.pop(k, None)
        return allowed, reset, remaining


class RateRegistry:
    __slots__ = ("per_ip", "global_", "overrides")

    def __init__(self, cfg: Config):
        r = cfg.get_dict("rate_limit")
        win = float(r.get("window_sec", 60))
        per_ip = r.get("per_ip") if isinstance(r.get("per_ip"), dict) else {}
        glob = r.get("global") if isinstance(r.get("global"), dict) else {}
        self.per_ip = Limit(int(per_ip.get("limit", 60)), win, int(per_ip.get("burst", 10)))
        self.global_ = Limit(int(glob.get("limit", 1000)), win, int(glob.get("burst", 100)))
        ov = r.get("endpoint_overrides")
        self.overrides = ov if isinstance(ov, dict) else {}

    def for_route(self, path: str) -> tuple[Limit, Limit]:
        ov = self.overrides.get(path) if isinstance(self.overrides, dict) else None
        if isinstance(ov, dict) and "per_ip" in ov:
            return (Limit(int(ov["per_ip"]), self.per_ip.window, self.per_ip.burst),
                    self.global_)
        return self.per_ip, self.global_


# ═════════════════════════════════════════════════════════════
# HTTP CLIENT
# ═════════════════════════════════════════════════════════════
_HttpMethod = Literal["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"]


@dataclass(slots=True)
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: float
    profile: str


class HttpClient:
    __slots__ = ("_http", "_fp", "_cooldowns")

    def __init__(self, cfg: Config):
        if _CurlAsyncSession is None:
            raise ConfigError("curl_cffi is not installed")
        self._http = cfg.get_dict("http")
        self._fp = cfg.get_dict("fingerprints")
        self._cooldowns: dict[str, float] = {}

    def _pick_profile(self) -> str:
        now = time.monotonic()
        order = self._fp.get("fallback_order")
        if not isinstance(order, list) or not order:
            order = [str(self._fp.get("default", "chrome"))]
        for pid in order:
            if isinstance(pid, str) and self._cooldowns.get(pid, 0) <= now:
                return pid
        return str(self._fp.get("default", "chrome"))

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        base = self._http.get("default_headers")
        out: dict[str, str] = {}
        if isinstance(base, dict):
            for k, v in base.items():
                if k is not None and v is not None:
                    out[str(k)] = str(v)
        if extra:
            for k, v in extra.items():
                if k is not None and v is not None:
                    out[str(k)] = str(v)
        return out

    async def fetch(self, url: str, *, profile: str | None = None,
                    method: _HttpMethod = "GET", data: bytes | None = None,
                    extra_headers: dict[str, str] | None = None) -> FetchResult:
        prof = profile or self._pick_profile()
        timeout = int(self._http.get("timeout_sec", 20))
        max_redir = int(self._http.get("max_redirects", 8))
        verify = bool(self._http.get("verify_tls", True))
        t0 = time.monotonic()
        try:
            async with _CurlAsyncSession(
                impersonate=prof, timeout=timeout,
                max_redirects=max_redir, verify=verify,
            ) as sess:
                resp = await sess.request(
                    method, url,
                    headers=self._headers(extra_headers),
                    data=data,
                )
        except Exception as exc:
            self._cooldowns[prof] = time.monotonic() + 300
            raise UpstreamError(f"fetch failed: {exc}") from exc
        body = resp.content
        if len(body) > MAX_RESPONSE_BYTES:
            body = body[:MAX_RESPONSE_BYTES]
        return FetchResult(
            url=url,
            status=resp.status_code,
            headers={str(k).lower(): str(v) for k, v in resp.headers.items()},
            body=body,
            elapsed_ms=(time.monotonic() - t0) * 1000,
            profile=prof,
        )

    async def close(self) -> None:
        return None


# ═════════════════════════════════════════════════════════════
# PARSER
# ═════════════════════════════════════════════════════════════
_WS = re.compile(r"[ \t\r\f\v]+")


@dataclass(slots=True)
class ParsedDoc:
    tree: Any
    text: str
    encoding: str


class Parser:
    __slots__ = ()

    def __init__(self, cfg: Config):
        if _HTMLParser is None:
            raise ConfigError("selectolax is not installed")

    @staticmethod
    def _decode(raw: bytes) -> tuple[str, str]:
        m = re.search(rb'charset=["\']?([a-zA-Z0-9_\-]+)', raw[:4096], re.IGNORECASE)
        declared = m.group(1).decode("ascii", "ignore") if m else None
        for enc in (declared, "utf-8", "latin-1", "windows-1252"):
            if not enc:
                continue
            try:
                return raw.decode(enc), enc
            except Exception:
                continue
        return raw.decode("utf-8", errors="replace"), "utf-8"

    def parse(self, raw: bytes) -> ParsedDoc:
        html, enc = self._decode(raw)
        html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
        html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
        html = re.sub(r"<!--[\s\S]*?-->", " ", html)
        try:
            tree = _HTMLParser(html)
        except Exception as exc:
            raise ParseError(f"html parse failed: {exc}") from exc
        parts: list[str] = []
        root = tree.body or tree.root
        if root is not None:
            for node in root.traverse():
                if node.tag in ("script", "style", "noscript"):
                    continue
                if node.tag == "#text":
                    # selectolax >=0.3.21 exposes .text as a method
                    try:
                        txt = node.text(strip=False)
                    except TypeError:
                        # older API: attribute
                        txt = getattr(node, "text", None)
                    if txt:
                        parts.append(str(txt))
        text = _WS.sub(" ", " ".join(parts)).strip()
        return ParsedDoc(tree=tree, text=text, encoding=enc)


# ═════════════════════════════════════════════════════════════
# EXTRACTOR
# ═════════════════════════════════════════════════════════════
@dataclass(slots=True)
class Extracted:
    url: str
    title: str = ""
    description: str = ""
    author: str = ""
    published: str = ""
    language: str = ""
    canonical: str = ""
    body_text: str = ""
    images: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)


class Extractor:
    __slots__ = ("_ex",)

    def __init__(self, cfg: Config):
        self._ex = cfg.get_dict("extractors")

    def _first_text(self, doc: ParsedDoc, key: str) -> str:
        spec = self._ex.get(key) if isinstance(self._ex, dict) else None
        if not isinstance(spec, dict):
            return ""
        sels = spec.get("selectors_priority", [])
        if not isinstance(sels, list):
            return ""
        for sel in sels:
            if not isinstance(sel, str):
                continue
            node = doc.tree.css_first(sel)
            if node is None:
                continue
            try:
                attrs = node.attributes or {}
            except Exception:
                attrs = {}
            val = (attrs.get("content") or attrs.get("href") or
                   node.text(strip=True) or "")
            if val:
                return str(val).strip()
        return ""

    def _dense_block(self, doc: ParsedDoc) -> str:
        best_score = 0
        best = ""
        for tag in ("article", "main", "section", "div"):
            for node in doc.tree.css(tag):
                text = node.text(separator=" ", strip=True) or ""
                if len(text) < 200:
                    continue
                score = len(text) + len(node.css("p")) * 80
                if score > best_score:
                    best_score = score
                    best = text
        return best or doc.text

    def extract(self, doc: ParsedDoc, url: str) -> Extracted:
        e = Extracted(url=url)
        e.title = self._first_text(doc, "title")[:300]
        e.description = self._first_text(doc, "description")[:500]
        e.author = self._first_text(doc, "author")[:120]
        e.published = self._first_text(doc, "published")[:64]
        e.canonical = self._first_text(doc, "canonical")

        root = doc.tree.root
        if root is not None:
            try:
                e.language = (root.attributes or {}).get("lang", "") or ""
            except Exception:
                pass

        content = ""
        spec = self._ex.get("content") if isinstance(self._ex, dict) else None
        if isinstance(spec, dict):
            sels = spec.get("selectors_priority", [])
            if isinstance(sels, list):
                for sel in sels:
                    if not isinstance(sel, str):
                        continue
                    node = doc.tree.css_first(sel)
                    if node is None:
                        continue
                    text = node.text(separator="\n", strip=True) or ""
                    if len(text) >= 200:
                        content = text
                        break
        e.body_text = content or self._dense_block(doc)

        meta: dict[str, str] = {}
        for m in doc.tree.css("meta"):
            try:
                attrs = m.attributes or {}
            except Exception:
                continue
            key = attrs.get("property") or attrs.get("name")
            val = attrs.get("content")
            if key and val:
                meta[str(key)] = str(val)
        e.meta = meta

        for a in doc.tree.css("a[href]")[:200]:
            try:
                attrs = a.attributes or {}
            except Exception:
                continue
            href = (attrs.get("href") or "").strip()
            if not href or href.startswith(("javascript:", "data:")):
                continue
            e.links.append({"href": urljoin(url, href),
                            "text": (a.text(strip=True) or "")[:200]})

        for img in doc.tree.css("img[src]")[:60]:
            try:
                attrs = img.attributes or {}
            except Exception:
                continue
            src = (attrs.get("src") or "").strip()
            if not src or src.startswith("data:"):
                continue
            e.images.append({"src": urljoin(url, src),
                             "alt": (attrs.get("alt") or "")[:200]})
        return e


# ═════════════════════════════════════════════════════════════
# CLEANER
# ═════════════════════════════════════════════════════════════
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZW = re.compile(r"[\u200b-\u200d\ufeff\u202a-\u202e]")  # include bidi overrides
_NL = re.compile(r"\n{3,}")
_BOILER = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"subscribe\s+to\s+our\s+newsletter",
    r"sign\s+up\s+for\s+our",
    r"click\s+here\s+to\s+read\s+more",
    r"we\s+use\s+cookies",
    r"all\s+rights\s+reserved",
    r"share\s+this\s+article",
    r"follow\s+us\s+on",
))


class Cleaner:
    __slots__ = ()

    def __init__(self, cfg: Config):
        pass

    def clean(self, text: str) -> str:
        if not text:
            return ""
        text = unicodedata.normalize("NFC", text)
        text = _CONTROL.sub("", text)
        text = _ZW.sub("", text)
        text = _WS.sub(" ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = _NL.sub("\n\n", text)
        for pat in _BOILER:
            text = pat.sub("", text)
        return text.strip()


# ═════════════════════════════════════════════════════════════
# GITHUB
# ═════════════════════════════════════════════════════════════
class GitHubClient:
    __slots__ = ("_http", "_token")

    BASE = "https://api.github.com"

    def __init__(self, client: HttpClient):
        self._http = client
        self._token = os.environ.get("GITHUB_TOKEN", "")

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.BASE + path
        if params:
            url += "?" + urlencode({k: str(v) for k, v in params.items()})
        res = await self._http.fetch(url, extra_headers=self._headers())
        if res.status == 404:
            raise NotFoundError(f"github: not found — {path}")
        if res.status == 403:
            raise UpstreamError("github: rate limit exceeded or forbidden")
        if res.status >= 400:
            raise UpstreamError(f"github: HTTP {res.status}")
        try:
            return json.loads(res.body)
        except Exception as exc:
            raise ParseError(f"github: invalid JSON — {exc}") from exc

    async def repo(self, owner: str, name: str) -> Any:
        return await self._get(f"/repos/{owner}/{name}")

    async def repo_languages(self, owner: str, name: str) -> Any:
        return await self._get(f"/repos/{owner}/{name}/languages")

    async def user(self, username: str) -> Any:
        return await self._get(f"/users/{username}")

    async def user_repos(self, username: str, limit: int = 30) -> Any:
        return await self._get(f"/users/{username}/repos",
                               {"per_page": limit, "sort": "updated"})

    async def search_repos(self, query: str, limit: int = 20) -> Any:
        return await self._get("/search/repositories",
                               {"q": query, "per_page": limit})


# ═════════════════════════════════════════════════════════════
# HUGGING FACE
# ═════════════════════════════════════════════════════════════
class HuggingFaceClient:
    __slots__ = ("_http", "_token")

    BASE = "https://huggingface.co/api"

    def __init__(self, client: HttpClient):
        self._http = client
        self._token = os.environ.get("HF_TOKEN", "")

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.BASE + path
        if params:
            url += "?" + urlencode({k: str(v) for k, v in params.items()})
        res = await self._http.fetch(url, extra_headers=self._headers())
        if res.status == 404:
            raise NotFoundError(f"hf: not found — {path}")
        if res.status >= 400:
            raise UpstreamError(f"hf: HTTP {res.status}")
        try:
            return json.loads(res.body)
        except Exception as exc:
            raise ParseError(f"hf: invalid JSON — {exc}") from exc

    async def model(self, repo_id: str) -> Any:
        return await self._get(f"/models/{repo_id}")

    async def dataset(self, repo_id: str) -> Any:
        return await self._get(f"/datasets/{repo_id}")

    async def space(self, repo_id: str) -> Any:
        return await self._get(f"/spaces/{repo_id}")

    async def search_models(self, query: str, limit: int = 20) -> Any:
        return await self._get("/models", {"search": query, "limit": limit})


# ═════════════════════════════════════════════════════════════
# SITE RULES
# ═════════════════════════════════════════════════════════════
@dataclass(slots=True)
class SiteRule:
    domain: str
    title_sel: str = ""
    content_sel: str = ""
    strip_selectors: tuple[str, ...] = ()


class StaticSiteRegistry:
    __slots__ = ("_rules",)

    def __init__(self) -> None:
        self._rules: dict[str, SiteRule] = {}
        rules = [
            SiteRule("en.wikipedia.org", "#firstHeading", "#mw-content-text .mw-parser-output",
                     (".mw-editsection", ".reference", ".navbox", ".infobox", ".metadata")),
            SiteRule("ar.wikipedia.org", "#firstHeading", "#mw-content-text .mw-parser-output",
                     (".mw-editsection", ".reference", ".navbox", ".infobox", ".metadata")),
            SiteRule("developer.mozilla.org", "h1", "article.main-page-content", (".sidebar",)),
            SiteRule("docs.python.org", "h1", "div.body", (".sphinxsidebar", ".related")),
            SiteRule("stackoverflow.com", "h1", "div#mainbar", (".sidebar", ".post-menu")),
            SiteRule("news.ycombinator.com", "title", "table.itemlist", (".reply",)),
            SiteRule("lobste.rs", "h1", "div.story_text", (".comments",)),
            SiteRule("reddit.com", "h1", "div[data-test-id=post-content]", (".promoted",)),
            SiteRule("medium.com", "h1", "article", (".ad",)),
            SiteRule("dev.to", "h1", "div#article-body", (".ad",)),
            SiteRule("github.com", "h1", "article.markdown-body", (".ad",)),
            SiteRule("gitlab.com", "h1", "div#content-body", (".ad",)),
            SiteRule("bbc.com", "h1", "article", (".ad",)),
            SiteRule("cnn.com", "h1", "div.article__content", (".ad",)),
            SiteRule("nytimes.com", "h1", "section[name=articleBody]", (".ad",)),
            SiteRule("theguardian.com", "h1", "div.article-body-commercial-selector", (".ad",)),
            SiteRule("reuters.com", "h1", "div.article-body", ()),
            SiteRule("apnews.com", "h1", "div.Article", ()),
            SiteRule("npr.org", "h1", "div.storytext", (".ad",)),
            SiteRule("arstechnica.com", "h1", "div.article-content", (".ad",)),
            SiteRule("theverge.com", "h1", "div.duet--article--article-body", (".ad",)),
            SiteRule("techcrunch.com", "h1", "div.article-content", (".ad",)),
            SiteRule("wired.com", "h1", "div.body__inner-container", (".ad",)),
            SiteRule("readthedocs.io", "h1", "div[role=main]", (".sidebar",)),
            SiteRule("gitbook.io", "h1", "div.page-inner", ()),
            SiteRule("fandom.com", "h1", "div.mw-parser-output", ()),
            SiteRule("arxiv.org", "h1.title", "blockquote.abstract", ()),
            SiteRule("pubmed.ncbi.nlm.nih.gov", "h1.heading-title", "div.abstract-content", ()),
            SiteRule("quora.com", "h1", "div.q-box", (".ad",)),
            SiteRule("substack.com", "h1", "div.available-content", (".ad",)),
            SiteRule("amazon.com", "h1#title", "div#feature-bullets", ()),
            SiteRule("ebay.com", "h1.x-item-title__mainTitle", "div.d-item-description", ()),
            SiteRule("etsy.com", "h1", "div#description-text", ()),
            SiteRule("wiktionary.org", "h1", "div.mw-parser-output", ()),
            SiteRule("wikiwand.com", "h1", "div#content", ()),
            SiteRule("semanticscholar.org", "h1", "div.abstract", ()),
            SiteRule("scholar.google.com", "h1", "div#gs_res_ccl_mid", ()),
            SiteRule("discourse.org", "h1", "div.cooked", ()),
            SiteRule("serverfault.com", "h1", "div#mainbar", ()),
            SiteRule("superuser.com", "h1", "div#mainbar", ()),
            SiteRule("askubuntu.com", "h1", "div#mainbar", ()),
            SiteRule("stackexchange.com", "h1", "div#mainbar", ()),
            SiteRule("apple.com", "h1", "div.section-content", ()),
            SiteRule("microsoft.com", "h1", "main", ()),
        ]
        for r in rules:
            self._rules[r.domain] = r

    def lookup(self, url: str) -> SiteRule | None:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return None
        if not host:
            return None
        if host in self._rules:
            return self._rules[host]
        parts = host.split(".")
        for i in range(1, len(parts) - 1):
            candidate = ".".join(parts[i:])
            if candidate in self._rules:
                return self._rules[candidate]
        return None

    def all_domains(self) -> list[str]:
        return sorted(self._rules.keys())

    def apply(self, rule: SiteRule, doc: ParsedDoc) -> str:
        for sel in rule.strip_selectors:
            for node in doc.tree.css(sel):
                try:
                    node.decompose()
                except Exception:
                    pass
        if rule.content_sel:
            node = doc.tree.css_first(rule.content_sel)
            if node is not None:
                text = node.text(separator="\n", strip=True) or ""
                if len(text) > 100:
                    return text
        return ""


# ═════════════════════════════════════════════════════════════
# SEARCH
# ═════════════════════════════════════════════════════════════
class SearchRouter:
    __slots__ = ("_s", "_client", "_parser")

    def __init__(self, cfg: Config, client: HttpClient, parser: Parser):
        self._s = cfg.get_dict("search")
        self._client = client
        self._parser = parser

    def _resolve(self) -> tuple[str, dict[str, Any]] | None:
        engines = self._s.get("engines") if isinstance(self._s, dict) else None
        if not isinstance(engines, dict):
            return None
        chain = self._s.get("fallback_chain", [])
        if isinstance(chain, list):
            for name in chain:
                spec = engines.get(name) if isinstance(name, str) else None
                if isinstance(spec, dict) and spec.get("enabled"):
                    return name, spec
        dflt = self._s.get("default", "duckduckgo")
        spec = engines.get(dflt) if isinstance(dflt, str) else None
        return (dflt, spec) if isinstance(spec, dict) else None

    async def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        resolved = self._resolve()
        if not resolved:
            return []
        name, spec = resolved
        method = str(spec.get("method", "GET")).upper()
        endpoint = spec.get("endpoint", "")
        if not endpoint:
            return []
        try:
            if method == "POST":
                body = f"{spec['param_query']}={quote_plus(query)}".encode()
                res = await self._client.fetch(
                    endpoint, method="POST", data=body,
                    extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            else:
                url = f"{endpoint}?{spec['param_query']}={quote_plus(query)}"
                res = await self._client.fetch(url, method="GET")
        except Exception as exc:
            log.warning("search failed on %s: %s", name, exc)
            return []
        if res.status >= 400:
            return []
        doc = self._parser.parse(res.body)
        out: list[dict[str, Any]] = []
        result_sel = spec.get("result_selector", "")
        title_sel = spec.get("title_selector", "")
        snippet_sel = spec.get("snippet_selector", "")
        url_sel = spec.get("url_selector", "")
        for node in doc.tree.css(result_sel)[:limit]:
            t_node = node.css_first(title_sel) if title_sel else None
            s_node = node.css_first(snippet_sel) if snippet_sel else None
            u_node = node.css_first(url_sel) if url_sel else None
            if not t_node or not u_node:
                continue
            try:
                href = (u_node.attributes or {}).get("href", "")
            except Exception:
                href = ""
            out.append({
                "title": (t_node.text(strip=True) or "")[:200],
                "url": href,
                "snippet": (s_node.text(strip=True) if s_node else "")[:400],
                "engine": name,
            })
        return out


# ═════════════════════════════════════════════════════════════
# TOKENS (RTL-aware)
# ═════════════════════════════════════════════════════════════
_WORD = re.compile(r"\b\w+\b", re.UNICODE)
_SENT = re.compile(r"(?<=[.!?؟])\s+(?=[A-Z0-9\"'(\u0600-\u06FF])")
_STOP = frozenset({"the", "a", "an", "and", "or", "but", "if", "of", "to", "in",
                   "on", "at", "for", "with", "is", "are", "was", "were", "be",
                   "been", "being", "have", "has", "had", "do", "does", "did",
                   "it", "its", "this", "that", "these", "those", "as", "by",
                   "from", "not", "no"}) | _AR_STOPWORDS


@dataclass(slots=True)
class TokenStats:
    words: int = 0
    tokens_approx: int = 0
    characters: int = 0
    characters_no_spaces: int = 0
    sentences: int = 0
    paragraphs: int = 0
    unique_words: int = 0
    avg_word_length: float = 0.0
    avg_sentence_length: float = 0.0
    reading_time_min: float = 0.0
    top_words: list[tuple[str, int]] = field(default_factory=list)
    script: str = "latin"


class TokenCounter:
    __slots__ = ("_top_n", "_min_len", "_wpm")

    def __init__(self, cfg: Config):
        t = cfg.get_dict("tokens")
        tw = t.get("top_words") if isinstance(t.get("top_words"), dict) else {}
        self._top_n = int(tw.get("count", 5))
        self._min_len = int(tw.get("min_length", 3))
        rt = cfg.get_dict("extractors")
        rtd = rt.get("reading_time") if isinstance(rt.get("reading_time"), dict) else {}
        self._wpm = int(rtd.get("words_per_minute", 220))

    def count(self, text: str) -> TokenStats:
        if not text:
            return TokenStats()
        # strip diacritics for counting
        text_for_count = _strip_diacritics(text)
        words = _WORD.findall(text_for_count)
        lower = [w.lower() for w in words]
        sentences = [s for s in _SENT.split(text_for_count) if s.strip()]
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        punct = len(re.findall(r"[^\w\s]", text_for_count))
        tokens = int(len(words) * 1.3 + punct * 0.5)

        counts: dict[str, int] = {}
        for w in lower:
            if len(w) < self._min_len or w in _STOP:
                continue
            counts[w] = counts.get(w, 0) + 1
        top = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:self._top_n]

        script = "arabic" if _is_arabic(text) else ("rtl" if _has_rtl(text) else "latin")

        return TokenStats(
            words=len(words),
            tokens_approx=tokens,
            characters=len(text),
            characters_no_spaces=sum(1 for c in text if not c.isspace()),
            sentences=len(sentences),
            paragraphs=len(paragraphs),
            unique_words=len(set(lower)),
            avg_word_length=round(sum(len(w) for w in words) / len(words), 2) if words else 0.0,
            avg_sentence_length=round(len(words) / len(sentences), 2) if sentences else 0.0,
            reading_time_min=round(len(words) / self._wpm, 2) if self._wpm else 0.0,
            top_words=top,
            script=script,
        )


# ═════════════════════════════════════════════════════════════
# AI DETECTOR
# ═════════════════════════════════════════════════════════════
_HEDGES = frozenset({"perhaps", "maybe", "somewhat", "arguably", "potentially", "likely", "possibly"})
_CONNECTIVES = frozenset({"moreover", "furthermore", "however", "therefore",
                           "additionally", "consequently", "nevertheless"})


@dataclass(slots=True)
class AIScore:
    probability: float
    verdict: str
    layers: dict[str, float] = field(default_factory=dict)


class AIDetector:
    __slots__ = ()

    def __init__(self, cfg: Config):
        pass

    @staticmethod
    def _burstiness(sentences: list[str]) -> float:
        lens = [len(s) for s in sentences]
        if len(lens) < 2:
            return 0.5
        try:
            sd = stdev(lens)
        except Exception:
            return 0.5
        return 1.0 - min(1.0, sd / 30.0)

    @staticmethod
    def _vocab_richness(words: list[str]) -> float:
        if len(words) < 20:
            return 0.5
        ttr = len({w.lower() for w in words}) / len(words)
        return max(0.0, min(1.0, abs(ttr - 0.52) / 0.25))

    @staticmethod
    def _ngram_rep(words: list[str], n: int = 3) -> float:
        if len(words) < n * 2:
            return 0.5
        grams = [tuple(words[i:i+n]) for i in range(len(words) - n + 1)]
        counts: dict[tuple[str, ...], int] = {}
        for g in grams:
            counts[g] = counts.get(g, 0) + 1
        repeated = sum(c - 1 for c in counts.values())
        return min(1.0, (repeated / max(1, len(grams))) * 5.0)

    @staticmethod
    def _punct_entropy(text: str) -> float:
        counts: dict[str, int] = {}
        for c in text:
            if c in ".,;:!?-—–()[]\"'،؟«»":
                counts[c] = counts.get(c, 0) + 1
        if not counts:
            return 0.5
        total = sum(counts.values())
        h = -sum((v / total) * math.log(v / total + 1e-9) for v in counts.values())
        return min(1.0, h / 3.0)

    @staticmethod
    def _connective_density(words: list[str]) -> float:
        if not words:
            return 0.0
        hits = sum(1 for w in words if w.lower() in _CONNECTIVES)
        return min(1.0, hits / max(1, len(words)) * 100.0)

    @staticmethod
    def _hedge_density(words: list[str]) -> float:
        if not words:
            return 0.0
        hits = sum(1 for w in words if w.lower() in _HEDGES)
        return min(1.0, hits / max(1, len(words)) * 150.0)

    @staticmethod
    def _paragraph_uniformity(text: str) -> float:
        paras = [p for p in text.split("\n\n") if p.strip()]
        if len(paras) < 3:
            return 0.5
        try:
            sd = stdev([len(p) for p in paras])
        except Exception:
            return 0.5
        return 1.0 - min(1.0, sd / 200.0)

    def score(self, text: str) -> AIScore:
        if not text or len(text) < 100:
            return AIScore(0.0, "uncertain origin", {})
        sentences = [s for s in _SENT.split(text) if s.strip()]
        words = _WORD.findall(text)
        metrics = {
            "burstiness": self._burstiness(sentences),
            "vocab_richness": self._vocab_richness(words),
            "ngram_repetition": self._ngram_rep(words),
            "punctuation_mix": self._punct_entropy(text),
            "connective_density": self._connective_density(words),
            "hedge_words": self._hedge_density(words),
            "paragraph_uniform": self._paragraph_uniformity(text),
        }
        weights = {
            "burstiness": 0.20, "vocab_richness": 0.15,
            "ngram_repetition": 0.15, "punctuation_mix": 0.12,
            "connective_density": 0.13, "hedge_words": 0.10,
            "paragraph_uniform": 0.15,
        }
        tv = 0.0
        tw = 0.0
        layers: dict[str, float] = {}
        for k, w in weights.items():
            v = metrics[k]
            layers[k] = round(v, 4)
            tv += w * v
            tw += w
        prob = tv / tw if tw else 0.0
        if prob <= 0.30:
            verdict = "likely human-written"
        elif prob >= 0.78:
            verdict = "likely ai-generated"
        else:
            verdict = "uncertain origin"
        return AIScore(round(prob, 4), verdict, layers)


# ═════════════════════════════════════════════════════════════
# REPORT — RTL-aware
# ═════════════════════════════════════════════════════════════
class ReportBuilder:
    __slots__ = ("_font_cache",)

    def __init__(self, cfg: Config):
        self._font_cache: dict[tuple[str, int], Any] = {}

    def _font(self, size: int, script: str = "latin") -> Any:
        if _PILFont is None:
            raise ConfigError("pillow is not installed")
        key = (script, size)
        cached = self._font_cache.get(key)
        if cached is not None:
            return cached
        candidates: list[Path] = []
        if script in ("arabic", "rtl"):
            candidates += [
                FONTS_DIR / "NotoNaskhArabic-Regular.ttf",
                Path("C:/Windows/Fonts/arial.ttf"),
                Path("C:/Windows/Fonts/tahoma.ttf"),
                Path("/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
            ]
        candidates += [
            FONTS_DIR / "Inter-Regular.ttf",
            Path("C:/Windows/Fonts/arial.ttf"),
            Path("C:/Windows/Fonts/segoeui.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        ]
        for p in candidates:
            try:
                if p.exists():
                    f = _PILFont.truetype(str(p), size)
                    self._font_cache[key] = f
                    return f
            except Exception:
                continue
        f = _PILFont.load_default()
        self._font_cache[key] = f
        return f

    def build(self, *, stats: TokenStats, ai_prob: float,
              ai_verdict: str, domain: str) -> bytes:
        if _PILImage is None or _PILDraw is None or _PILFont is None:
            raise ConfigError("pillow is not installed")

        rtl = stats.script in ("arabic", "rtl")

        # canvas wider for RTL to accommodate longer lines
        w, h = (1400, 1700) if rtl else (1200, 1600)
        img = _PILImage.new("RGB", (w, h), "#ffffff")
        d = _PILDraw.Draw(img)

        fg = "#000000"
        muted = "#737373"
        line = "#e6e6e6"
        pad = 80

        # RTL: title right-aligned
        if rtl:
            def align_text(x: int, y: int, text: str, font: Any, fill: str) -> None:
                shaped = _shape(text)
                try:
                    bbox = d.textbbox((0, 0), shaped, font=font)
                    tw = bbox[2] - bbox[0]
                except Exception:
                    tw = len(shaped) * 8
                d.text((x - tw, y), shaped, font=font, fill=fill)
        else:
            def align_text(x: int, y: int, text: str, font: Any, fill: str) -> None:
                d.text((x, y), text, font=font, fill=fill)

        f_title = self._font(48, stats.script)
        f_label = self._font(22, stats.script)
        f_value = self._font(32, stats.script)
        f_small = self._font(16, stats.script)

        anchor_x = (w - pad) if rtl else pad
        left_x = pad
        right_x = w - pad

        align_text(anchor_x, 60, "nexus · report", f_title, fg)
        align_text(anchor_x, 120, f"single-pass · {domain}", f_label, fg)
        d.line((pad, 180, w - pad, 180), fill=line, width=2)

        # stats grid — two columns; in RTL, mirrored
        items = [
            ("WORDS", stats.words),
            ("TOKENS", stats.tokens_approx),
            ("CHARS", stats.characters),
            ("SENTENCES", stats.sentences),
        ]
        y = 220
        col_w = (w - 2 * pad) // 2
        for i, (label, value) in enumerate(items):
            col = i % 2
            row = i // 2
            if rtl:
                x = right_x - col * col_w
            else:
                x = pad + col * col_w
            yy = y + row * 130
            align_text(x, yy, label, f_label, muted)
            align_text(x, yy + 34, str(value), f_value, fg)

        # AI gauge
        gy = y + 2 * 130 + 40
        align_text(anchor_x, gy, "AI-LIKELIHOOD", f_label, muted)
        gy += 40
        bar_w = w - 2 * pad
        d.rectangle((pad, gy, pad + bar_w, gy + 32), fill=line)
        fw = int(bar_w * max(0.0, min(1.0, ai_prob)))
        if fw > 0:
            d.rectangle((pad, gy, pad + fw, gy + 32), fill=fg)
        gy += 48
        align_text(anchor_x, gy, f"{ai_prob * 100:.1f}%  ·  {ai_verdict}", f_label, fg)

        # Top words
        ty = gy + 80
        d.line((pad, ty - 20, w - pad, ty - 20), fill=line, width=1)
        align_text(anchor_x, ty, "TOP WORDS", f_label, muted)
        ty += 44
        if stats.top_words:
            mx = max(c for _, c in stats.top_words) or 1
            bar_max = bar_w - 320
            for word, cnt in stats.top_words:
                display_word = _shape(word)
                try:
                    bbox = d.textbbox((0, 0), display_word, font=f_value)
                    word_w = bbox[2] - bbox[0]
                except Exception:
                    word_w = len(display_word) * 10
                if rtl:
                    # word on right, bar grows leftward
                    d.text((right_x - word_w, ty), display_word, font=f_value, fill=fg)
                    bw = int(bar_max * (cnt / mx))
                    d.rectangle((right_x - 320 - bw, ty + 12, right_x - 320, ty + 30), fill=fg)
                    d.text((right_x - 380 - bw, ty), str(cnt), font=f_value, fill=fg)
                else:
                    d.text((pad, ty), display_word, font=f_value, fill=fg)
                    bw = int(bar_max * (cnt / mx))
                    d.rectangle((pad + 320, ty + 12, pad + 320 + bw, ty + 30), fill=fg)
                    d.text((w - pad - 60, ty), str(cnt), font=f_value, fill=fg)
                ty += 52

        d.line((pad, h - 100, w - pad, h - 100), fill=line, width=1)
        align_text(anchor_x, h - 70, "generated by nexus · one pass · no follow", f_small, muted)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        # free large objects
        del img, d
        return buf.getvalue()


# ═════════════════════════════════════════════════════════════
# GRAPH
# ═════════════════════════════════════════════════════════════
class GraphBuilder:
    __slots__ = ()

    def __init__(self, cfg: Config):
        pass

    def build(self, doc: Extracted) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = [{"id": "root", "type": "document",
                                         "label": doc.title or doc.url, "size": 24}]
        edges: list[dict[str, Any]] = []
        if doc.author:
            nodes.append({"id": "author", "type": "author",
                          "label": doc.author, "size": 14})
            edges.append({"source": "root", "target": "author", "type": "authored_by"})
        for i, link in enumerate(doc.links[:40]):
            if len(nodes) >= MAX_GRAPH_NODES:
                break
            nid = f"l{i}"
            label = (link.get("text") or link.get("href") or "").strip()[:80]
            nodes.append({"id": nid, "type": "link", "label": label, "size": 10})
            edges.append({"source": "root", "target": nid, "type": "links_to"})
        for i, img in enumerate(doc.images[:15]):
            if len(nodes) >= MAX_GRAPH_NODES:
                break
            nid = f"i{i}"
            nodes.append({"id": nid, "type": "image",
                          "label": (img.get("alt") or "image")[:80], "size": 10})
            edges.append({"source": "root", "target": nid, "type": "contains"})
        return {"version": "1.0.0", "nodes": nodes, "edges": edges}


# ═════════════════════════════════════════════════════════════
# DOCUMENTS
# ═════════════════════════════════════════════════════════════
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def parse_pdf(data: bytes, max_bytes: int = 32 * 1024 * 1024) -> str:
    if not data:
        raise ValueError("pdf input is empty")
    if len(data) > max_bytes:
        raise ValueError("pdf too large")
    if _pymupdf is None:
        raise ValueError("pdf support requires pymupdf")
    try:
        doc = _pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"pdf open failed: {exc}") from exc
    parts: list[str] = []
    try:
        for page in doc:
            try:
                t = page.get_text("text")
            except Exception:
                continue
            if t:
                parts.append(t)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    joined = "\n\n".join(p.strip() for p in parts if p and p.strip())
    if not joined:
        raise ValueError("pdf has no extractable text")
    return joined


def parse_docx(data: bytes, max_bytes: int = 16 * 1024 * 1024) -> str:
    if not data:
        raise ValueError("docx input is empty")
    if len(data) > max_bytes:
        raise ValueError("docx too large")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "word/document.xml" not in zf.namelist():
                raise ValueError("not a valid docx")
            xml_bytes = zf.read("word/document.xml")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"docx is not a zip archive: {exc}") from exc
    except ValueError:
        raise
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"docx xml parse failed: {exc}") from exc
    paras: list[str] = []
    for p in root.iter(f"{_W_NS}p"):
        line = "".join(t.text or "" for t in p.iter(f"{_W_NS}t")).strip()
        if line:
            paras.append(line)
    if not paras:
        raise ValueError("docx has no extractable text")
    return "\n\n".join(paras)


# ═════════════════════════════════════════════════════════════
# PIPELINE
# ═════════════════════════════════════════════════════════════
class Pipeline:
    __slots__ = ("cfg", "client", "parser", "extractor", "cleaner",
                 "tokens", "ai", "report", "graph", "search", "ssrf",
                 "github", "huggingface", "sites")

    def __init__(self, *, cfg: Config, client: HttpClient, parser: Parser,
                 extractor: Extractor, cleaner: Cleaner, tokens: TokenCounter,
                 ai: AIDetector, report: ReportBuilder, graph: GraphBuilder,
                 search: SearchRouter, ssrf: SSRFGuard,
                 github: GitHubClient, huggingface: HuggingFaceClient,
                 sites: StaticSiteRegistry):
        self.cfg = cfg
        self.client = client
        self.parser = parser
        self.extractor = extractor
        self.cleaner = cleaner
        self.tokens = tokens
        self.ai = ai
        self.report = report
        self.graph = graph
        self.search = search
        self.ssrf = ssrf
        self.github = github
        self.huggingface = huggingface
        self.sites = sites

    async def process(self, *, query: str, mode: str | None,
                      language: str | None) -> dict[str, Any]:
        t0 = time.monotonic()
        q = query.strip()

        if q.startswith("github:") or q.startswith("gh:"):
            spec = q[7:].strip() if q.startswith("github:") else q[3:].strip()
            result = await self._handle_github(spec)
        elif q.startswith("hf:"):
            result = await self._handle_hf(q[3:].strip())
        elif q.startswith(("http://", "https://")):
            self.ssrf.check(q)
            result = await self._scrape(q, mode)
        elif q.startswith("search:"):
            result = await self._search(q[7:].strip(), mode)
        else:
            result = await self._search(q, mode)

        meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
        meta.update({
            "input": query,
            "mode": mode,
            "language": language or "auto",
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 2),
        })
        result["meta"] = meta
        return result

    async def _handle_github(self, spec: str) -> dict[str, Any]:
        parts = spec.split("/")
        if len(parts) == 2 and all(parts):
            data = await self.github.repo(parts[0], parts[1])
            return {"output": data, "meta": {"kind": "github_repo", "domain": "github.com"}}
        if len(parts) == 1 and parts[0]:
            data = await self.github.user(parts[0])
            return {"output": data, "meta": {"kind": "github_user", "domain": "github.com"}}
        raise ValidationError("github: use 'github:owner/name' or 'github:username'")

    async def _handle_hf(self, spec: str) -> dict[str, Any]:
        parts = spec.split("/")
        if len(parts) >= 2 and all(parts):
            repo_id = "/".join(parts)
            try:
                data = await self.huggingface.model(repo_id)
                return {"output": data, "meta": {"kind": "hf_model", "domain": "huggingface.co"}}
            except NotFoundError:
                pass
            try:
                data = await self.huggingface.dataset(repo_id)
                return {"output": data, "meta": {"kind": "hf_dataset", "domain": "huggingface.co"}}
            except NotFoundError:
                raise NotFoundError(f"hf: no model or dataset at {repo_id}")
        raise ValidationError("hf: use 'hf:owner/name'")

    async def _scrape(self, url: str, mode: str | None) -> dict[str, Any]:
        parsed = urlparse(url)
        domain = parsed.hostname or ""
        res = await self.client.fetch(url)
        ctype = res.headers.get("content-type", "").lower()
        meta: dict[str, Any] = {"status": res.status, "domain": domain,
                                 "content_type": ctype[:80], "profile": res.profile}

        rule = self.sites.lookup(url)
        if rule:
            meta["site_rule"] = rule.domain

        if "application/pdf" in ctype or url.lower().endswith(".pdf"):
            text = parse_pdf(res.body)
            meta["kind"] = "pdf"
        elif "wordprocessingml" in ctype or url.lower().endswith(".docx"):
            text = parse_docx(res.body)
            meta["kind"] = "docx"
        else:
            doc = self.parser.parse(res.body)
            ex = self.extractor.extract(doc, url)
            if rule and rule.content_sel:
                ruled = self.sites.apply(rule, doc)
                if ruled:
                    text = ruled
                    meta["kind"] = "html+rule"
                    meta["title"] = ex.title
                    meta["language"] = ex.language or "auto"
                else:
                    text = ex.body_text
                    meta.update({"kind": "html", "title": ex.title,
                                 "author": ex.author, "language": ex.language or "auto"})
            else:
                text = ex.body_text
                meta.update({"kind": "html", "title": ex.title,
                             "author": ex.author, "language": ex.language or "auto",
                             "links": len(ex.links), "images": len(ex.images)})
            if mode == "graph":
                return {"output": self.graph.build(ex), "meta": meta}

        clean = self.cleaner.clean(text)
        if mode == "clean":
            return {"output": clean, "meta": meta}

        stats = self.tokens.count(clean)

        if mode == "report":
            ai = self.ai.score(clean)
            png = self.report.build(
                stats=stats, ai_prob=ai.probability,
                ai_verdict=ai.verdict, domain=domain,
            )
            return {
                "output": base64.b64encode(png).decode("ascii"),
                "meta": {**meta, "format": "png", "ai_probability": ai.probability,
                         "script": stats.script},
                "output_type": "image/png",
            }

        return {"output": clean, "meta": meta,
                "stats": {
                    "words": stats.words, "tokens_approx": stats.tokens_approx,
                    "characters": stats.characters, "sentences": stats.sentences,
                }}

    async def _search(self, query: str, mode: str | None) -> dict[str, Any]:
        results = await self.search.search(query, limit=10)
        if not results:
            return {"output": [], "meta": {"kind": "search", "count": 0}}
        if mode == "clean":
            text = "\n\n".join(f"{r['title']}\n{r['url']}\n{r['snippet']}" for r in results)
            return {"output": text, "meta": {"kind": "search", "count": len(results)}}
        return {"output": results, "meta": {"kind": "search", "count": len(results)}}


# ═════════════════════════════════════════════════════════════
# FASTAPI
# ═════════════════════════════════════════════════════════════
_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_secrets()
    cfg_path = find_config()
    cfg = load_config(cfg_path)
    configure_logging(cfg)
    log.info("loading config from %s", cfg_path)

    storage = await Storage.connect(cfg)
    aes = AES.from_env("NEXUS_AES_KEY")
    jwt = JWT.from_env(cfg)
    ssrf = SSRFGuard.from_config(cfg)
    hmac_key = os.environ.get("NEXUS_HMAC_KEY", "").encode() or b"fallback"

    cache = Cache(
        storage=storage, aes=aes, hmac_key=hmac_key,
        default_ttl=cfg.get_int("cache.default_ttl_sec", 900),
        ttl_by_type=cfg.get_dict("cache.ttl_by_type"),
        compress=cfg.get_bool("cache.compression.enabled", True),
        max_kb=cfg.get_int("cache.max_entry_size_kb", 2048),
    )

    client = HttpClient(cfg)
    parser = Parser(cfg)
    extractor = Extractor(cfg)
    cleaner = Cleaner(cfg)
    tokens = TokenCounter(cfg)
    ai = AIDetector(cfg)
    report = ReportBuilder(cfg)
    graph = GraphBuilder(cfg)
    search = SearchRouter(cfg, client, parser)

    github = GitHubClient(client)
    huggingface = HuggingFaceClient(client)
    sites = StaticSiteRegistry()

    pipeline = Pipeline(
        cfg=cfg, client=client, parser=parser, extractor=extractor,
        cleaner=cleaner, tokens=tokens, ai=ai, report=report,
        graph=graph, search=search, ssrf=ssrf,
        github=github, huggingface=huggingface, sites=sites,
    )

    _state.clear()
    _state.update({
        "cfg": cfg, "storage": storage, "jwt": jwt, "ssrf": ssrf,
        "cache": cache, "client": client, "pipeline": pipeline,
        "limiter": RateLimiter(), "registry": RateRegistry(cfg),
        "trusted_proxies": set(cfg.get_list("rate_limit.trusted_proxies") or []),
        "hmac_key": hmac_key,
        "pw": PasswordHasherWrapper(cfg) if _PasswordHasher is not None else None,
        "store_ip": os.environ.get("NEXUS_STORE_EXACT_IP", "0") == "1",
        "sites": sites,
    })

    log.info("nexus v%s ready on http://%s:%d", VERSION, HOST, PORT)
    try:
        yield
    finally:
        try:
            await client.close()
        except Exception:
            pass
        try:
            await storage.close()
        except Exception:
            pass
        _state.clear()
        gc.collect()
        log.info("nexus stopped")


app = FastAPI(
    title="nexus", version=VERSION, lifespan=lifespan,
    default_response_class=ORJSONResponse,
    docs_url=None, redoc_url=None, openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
    max_age=600,
)


def _client_ip(request: Request) -> str:
    direct = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for", "")
    trusted: set[str] = _state.get("trusted_proxies", set())
    if xff and direct in trusted:
        return xff.split(",")[0].strip()
    return direct


def _hmac_hex(v: str, key: bytes) -> str:
    return hmac.new(key, v.encode(), hashlib.sha256).hexdigest()


@app.middleware("http")
async def _middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    t0 = time.monotonic()

    if request.method != "OPTIONS":
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > 1_048_576:
                    return JSONResponse({"error": "payload_too_large", "request_id": request_id},
                                        status_code=413)
            except ValueError:
                return JSONResponse({"error": "invalid_content_length"}, status_code=400)

    try:
        response = await call_next(request)
    except Exception:
        log.exception("middleware caught exception", extra={"request_id": request_id})
        response = JSONResponse({"error": "internal_error", "request_id": request_id},
                                status_code=500)

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers["X-Request-ID"] = request_id

    latency_ms = (time.monotonic() - t0) * 1000.0
    try:
        ip = _client_ip(request)
        hmac_key: bytes = _state.get("hmac_key", b"fallback")
        ip_hash = _hmac_hex(ip, hmac_key)
        log.info("request", extra={"request_id": request_id, "method": request.method,
                                    "path": request.url.path, "status": response.status_code,
                                    "latency_ms": round(latency_ms, 2), "ip": ip_hash})
        storage = _state.get("storage")
        if storage is not None and request.method != "OPTIONS":
            store_ip = bool(_state.get("store_ip", False))
            asyncio.create_task(storage.log_request(
                method=request.method, path=request.url.path,
                status=response.status_code, latency_ms=latency_ms,
                ip_hash=ip_hash, ip_exact=ip if store_ip else None,
                request_id=request_id,
            ))
    except Exception as exc:
        log.debug("post-response logging skipped: %s", exc)

    return response


async def _enforce_rate_limit(request: Request, route: str) -> None:
    limiter: RateLimiter | None = _state.get("limiter")
    registry: RateRegistry | None = _state.get("registry")
    if limiter is None or registry is None:
        return
    ip = _client_ip(request)
    hmac_key: bytes = _state.get("hmac_key", b"fallback")
    ip_hash = _hmac_hex(ip, hmac_key)
    per_ip, global_ = registry.for_route(route)
    for spec, key in ((per_ip, ip_hash), (global_, "all")):
        allowed, reset, _ = await limiter.check(route, key, spec)
        if not allowed:
            raise RateLimitError(f"limit exceeded; retry in {reset:.1f}s")


@app.get("/")
async def root():
    if not INDEX_PATH.exists():
        return JSONResponse({"error": "index_missing",
                             "detail": f"index.html not found at {INDEX_PATH}"},
                            status_code=500)
    return FileResponse(INDEX_PATH, media_type="text/html")


@app.get("/favicon.ico")
async def favicon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="8" fill="#000"/>'
           '<text x="16" y="22" font-family="sans-serif" font-size="18" '
           'font-weight="700" fill="#fff" text-anchor="middle">N</text></svg>')
    return Response(svg.encode(), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": VERSION,
            "uptime_sec": round(time.time() - START_TIME, 2)}


@app.get("/api/live")
async def live():
    return {"status": "alive"}


@app.get("/api/ready")
async def ready():
    checks = {
        "config": _state.get("cfg") is not None,
        "storage": _state.get("storage") is not None,
        "pipeline": _state.get("pipeline") is not None,
    }
    if not all(checks.values()):
        return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)
    return {"status": "ready", "checks": checks}


@app.get("/api/config")
async def get_client_config():
    cfg: Config | None = _state.get("cfg")
    if cfg is None:
        return JSONResponse({"error": "not_ready"}, status_code=503)

    supported = cfg.get_list("languages.supported")
    langs: list[dict[str, Any]] = []
    for item in supported:
        if isinstance(item, dict) and "code" in item and "name" in item:
            langs.append({"code": str(item["code"]), "name": str(item["name"]),
                          "rtl": bool(item.get("rtl", False))})
        elif isinstance(item, str):
            langs.append({"code": item, "name": item, "rtl": _has_rtl(item)})
    if not langs:
        langs = [{"code": "en", "name": "English", "rtl": False},
                 {"code": "ar", "name": "Arabic", "rtl": True},
                 {"code": "es", "name": "Spanish", "rtl": False},
                 {"code": "fr", "name": "French", "rtl": False},
                 {"code": "de", "name": "German", "rtl": False},
                 {"code": "zh-CN", "name": "Chinese (Simplified)", "rtl": False},
                 {"code": "ja", "name": "Japanese", "rtl": False},
                 {"code": "ko", "name": "Korean", "rtl": False},
                 {"code": "he", "name": "Hebrew", "rtl": True}]

    branding = cfg.get_dict("branding")
    sites: StaticSiteRegistry | None = _state.get("sites")

    return {
        "version": VERSION,
        "branding": {
            "name": str(branding.get("name", "nexus")),
            "tagline": str(branding.get("tagline", "single-pass scraper")),
        },
        "languages": langs,
        "defaults": {"language": cfg.get_str("languages.default", "auto"), "mode": None},
        "features": {
            "pdf": _pymupdf is not None,
            "docx": True,
            "report": _PILImage is not None,
            "graph": True,
            "search": bool(cfg.get_dict("search.engines")),
            "cache": _zstd is not None,
            "argon2": _PasswordHasher is not None,
            "crypto": _AESGCM is not None,
            "impersonation": _CurlAsyncSession is not None,
            "github": True,
            "huggingface": True,
            "rtl": _reshaper is not None,
        },
        "limits": {
            "max_input_length": 4096,
            "rate_limit_per_min": cfg.get_int("rate_limit.per_ip.limit", 60),
        },
        "sites_supported": sites.all_domains() if sites is not None else [],
    }


@app.get("/api/sites")
async def list_sites():
    sites: StaticSiteRegistry | None = _state.get("sites")
    if sites is None:
        return JSONResponse({"error": "not_ready"}, status_code=503)
    return {"count": len(sites.all_domains()), "domains": sites.all_domains()}


@app.post("/api/process")
async def process(request: Request):
    try:
        await _enforce_rate_limit(request, "/api/process")
    except RateLimitError as exc:
        return JSONResponse({"error": "rate_limit_exceeded", "detail": str(exc)},
                            status_code=429, headers={"Retry-After": "60"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json", "detail": "body must be valid JSON"},
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_body"}, status_code=400)

    query = body.get("input")
    if not isinstance(query, str) or not query.strip():
        return JSONResponse({"error": "invalid_input",
                             "detail": "input must be a non-empty string"},
                            status_code=400)
    if len(query) > 4096:
        return JSONResponse({"error": "input_too_long"}, status_code=400)

    options = body.get("options") or {}
    if not isinstance(options, dict):
        options = {}
    mode = options.get("mode")
    if mode not in (None, "clean", "report", "graph"):
        return JSONResponse({"error": "invalid_mode",
                             "detail": "mode must be clean, report, or graph"},
                            status_code=400)
    language = options.get("language", "auto")
    if not isinstance(language, str) or len(language) > 64:
        language = "auto"

    pipeline: Pipeline | None = _state.get("pipeline")
    if pipeline is None:
        return JSONResponse({"error": "not_ready"}, status_code=503)

    try:
        result = await pipeline.process(query=query.strip(), mode=mode, language=language)
        return ORJSONResponse({
            "meta": result.get("meta", {}),
            "output": result.get("output"),
            "stats": result.get("stats"),
            "output_type": result.get("output_type", "application/json"),
        })
    except RateLimitError:
        raise
    except NotFoundError as exc:
        return JSONResponse({"error": "not_found", "detail": str(exc)}, status_code=404)
    except ValidationError as exc:
        return JSONResponse({"error": "validation_error", "detail": str(exc)}, status_code=400)
    except SecurityError as exc:
        return JSONResponse({"error": "security_error", "detail": str(exc)}, status_code=403)
    except UpstreamError as exc:
        return JSONResponse({"error": "upstream_error", "detail": str(exc)}, status_code=502)
    except ParseError as exc:
        return JSONResponse({"error": "parse_error", "detail": str(exc)}, status_code=422)
    except NexusError as exc:
        return JSONResponse({"error": "nexus_error", "detail": str(exc)}, status_code=500)
    except Exception:
        log.exception("unexpected error in /api/process")
        return JSONResponse({"error": "internal_error", "detail": "unexpected server error"},
                            status_code=500)


@app.exception_handler(RateLimitError)
async def _h_rl(request: Request, exc: RateLimitError):
    return JSONResponse({"error": "rate_limit_exceeded", "detail": str(exc)},
                        status_code=429, headers={"Retry-After": "60"})


@app.exception_handler(StarletteHTTPException)
async def _h_http(request: Request, exc: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return JSONResponse({"error": code}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def _h_any(request: Request, exc: Exception):
    log.exception("unhandled exception")
    return JSONResponse({"error": "internal_error", "detail": "unexpected server error"},
                        status_code=500)


# ═════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════
def _preflight() -> int:
    missing: list[str] = []
    if _CurlAsyncSession is None: missing.append("curl_cffi")
    if _HTMLParser is None: missing.append("selectolax")
    if _AESGCM is None: missing.append("cryptography")
    if _PasswordHasher is None: missing.append("argon2-cffi")
    if _PILImage is None: missing.append("pillow")
    if missing:
        print("[fatal] missing required packages:", file=sys.stderr)
        for m in missing:
            print(f"        - {m}", file=sys.stderr)
        print("\n        run: pip install -r requirements.txt", file=sys.stderr)
        return 4

    try:
        find_config()
    except ConfigError as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        return 5

    if not INDEX_PATH.exists():
        print(f"[warn] index.html not found at {INDEX_PATH}")

    for sub in ("var", "var/logs", "var/reports"):
        (BASE_DIR / sub).mkdir(parents=True, exist_ok=True)

    ensure_secrets()

    if _pymupdf is None:
        print("[warn] pymupdf not installed; PDF disabled")
    if _zstd is None:
        print("[warn] zstandard not installed; cache compression disabled")
    if _reshaper is None or _get_display is None:
        print("[warn] arabic-reshaper / python-bidi not installed; RTL text will render unshaped")

    return 0


def main() -> int:
    print(f"nexus v{VERSION} · single-pass scraper")
    print(f"base dir:  {BASE_DIR}")

    rc = _preflight()
    if rc != 0:
        return rc

    cfg_path = find_config()
    print(f"config:    {cfg_path}")
    print(f"index:     {INDEX_PATH} ({'found' if INDEX_PATH.exists() else 'missing'})")
    print(f"listening: http://{HOST}:{PORT}")
    print()
    print(f"open http://{HOST}:{PORT}/ in your browser")
    print()
    print("syntax guide:")
    print("  https://example.com       → scrape URL")
    print("  github:owner/repo         → GitHub repo")
    print("  github:user               → GitHub user")
    print("  hf:owner/name             → Hugging Face model or dataset")
    print("  search:query              → force search")
    print()

    try:
        uvicorn.run(app, host=HOST, port=PORT, log_config=None,
                    access_log=False, lifespan="on", workers=1)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 0
    except Exception as exc:
        print(f"\n[fatal] server crashed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 7
    return 0


if __name__ == "__main__":
    sys.exit(main())