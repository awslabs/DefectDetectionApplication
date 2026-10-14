# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The Local_Credential_Cache: load/parse the synchronized account file and
verify Local_Login credentials against it (Requirements 8.3, 8.4, 8.6, 11.3).

The cache file (``/aws_dda/local_credential_cache.json``, written atomically
by the user-accounts sync agent) holds the full synchronized account set:

.. code-block:: json

    {
      "version": 1,
      "syncId": "3f2a...",
      "appliedAt": 1700000000000,
      "accounts": {
        "operator1": {
          "email": "op1@example.com", "role": "Operator", "enabled": true,
          "verifier": {"algorithm": "pbkdf2-sha256", "iterations": 210000,
                        "salt": "b64...", "hash": "b64..."}
        }
      }
    }

Design points:

- **Constant-shape verification (8.3)**: ``verify_credentials`` always runs
  exactly one PBKDF2-HMAC-SHA256 computation — against the stored verifier
  when the username resolves to a usable one, and against a dummy verifier
  otherwise (unknown username, missing/malformed verifier, empty cache) —
  so the timing and the ``None`` outcome are identical whether or not the
  submitted username exists.
- **Purely local (8.4)**: everything here is file reads plus stdlib
  ``hashlib``/``hmac``; no network access anywhere.
- **Disabled accounts (8.6)**: the account is returned only when the
  verifier matches AND ``enabled`` is exactly ``true``; token validation
  re-checks ``get_account(...).enabled`` on every request (design D6).
- **Missing/empty/corrupt cache (11.3)**: treated as an empty account set —
  every login fails uniformly and a "no synchronized accounts available"
  diagnostic is logged.

The cache file is re-read on every call, so a sync applied by the agent
(``os.replace`` of the whole file) takes effect immediately. The path is
injectable for tests; the on-device default is
``/aws_dda/local_credential_cache.json``.
"""
import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = "/aws_dda/local_credential_cache.json"

#: The only verifier algorithm the portal produces (design D4).
SUPPORTED_ALGORITHM = "pbkdf2-sha256"

#: Portal-side PBKDF2 iteration count (design D4); used for the dummy
#: verifier so unknown-username verification costs the same as a real one.
DEFAULT_ITERATIONS = 210000

_SALT_LENGTH = 16
_HASH_LENGTH = 32


@dataclass(frozen=True)
class Account:
    """One synchronized account as seen by the local auth subsystem."""

    username: str
    email: str
    role: str
    enabled: bool


@dataclass(frozen=True)
class _Verifier:
    """A parsed, usable PBKDF2 verifier."""

    iterations: int
    salt: bytes
    hash: bytes


#: Dummy verifier for constant-shape verification (8.3): the all-zero target
#: hash never equals a PBKDF2 output for any realistic input, so the compare
#: always fails, but the PBKDF2 cost and code path match a real lookup.
_DUMMY_VERIFIER = _Verifier(
    iterations=DEFAULT_ITERATIONS,
    salt=bytes(_SALT_LENGTH),
    hash=bytes(_HASH_LENGTH),
)


def _parse_verifier(raw: Any) -> Optional[_Verifier]:
    """Parse a stored verifier record into a usable :class:`_Verifier`, or
    ``None`` when it is absent or malformed (the caller substitutes the
    dummy verifier so the failure is indistinguishable)."""
    if not isinstance(raw, Mapping):
        return None
    if raw.get("algorithm") != SUPPORTED_ALGORITHM:
        return None
    iterations = raw.get("iterations")
    if not isinstance(iterations, int) or iterations <= 0:
        return None
    try:
        salt = base64.b64decode(raw.get("salt") or "", validate=True)
        target = base64.b64decode(raw.get("hash") or "", validate=True)
    except (ValueError, TypeError):
        return None
    if not salt or not target:
        return None
    return _Verifier(iterations=iterations, salt=salt, hash=target)


def _verifier_matches(verifier: _Verifier, password: str) -> bool:
    """One PBKDF2-HMAC-SHA256 computation + constant-time compare."""
    computed = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), verifier.salt, verifier.iterations
    )
    return hmac.compare_digest(computed, verifier.hash)


class CredentialCache:
    """Read-only view over the Local_Credential_Cache file.

    The path is injectable so tests point at a tmp file; the on-device
    default is ``/aws_dda/local_credential_cache.json``.
    """

    def __init__(self, path: str = DEFAULT_CACHE_PATH):
        self._path = path

    @property
    def path(self) -> str:
        return self._path

    def load_accounts(self) -> Dict[str, Dict[str, Any]]:
        """The raw synchronized account records ``{username: record}``.

        A missing, empty, or corrupt cache file — or one without a usable
        ``accounts`` mapping — is treated as an empty account set (11.3);
        corruption is logged, absence is expected before the first sync.
        """
        try:
            with open(self._path, "r", encoding="utf-8") as cache_file:
                raw = json.load(cache_file)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning(
                "Local credential cache %s is unreadable or corrupt; "
                "treating it as empty",
                self._path,
            )
            return {}

        accounts = raw.get("accounts") if isinstance(raw, dict) else None
        if not isinstance(accounts, dict):
            logger.warning(
                "Local credential cache %s has an unexpected shape; "
                "treating it as empty",
                self._path,
            )
            return {}
        return {
            str(username): record
            for username, record in accounts.items()
            if isinstance(record, dict)
        }

    def get_account(self, username: str) -> Optional[Account]:
        """The synchronized account for ``username`` (exact cache key), or
        ``None`` when absent. The ``enabled`` flag is reported as stored so
        token validation can reject disabled accounts per request (8.6, D6).
        """
        record = self.load_accounts().get(username)
        if record is None:
            return None
        return _to_account(username, record)

    def verify_credentials(self, username: str, password: str) -> Optional[Account]:
        """Constant-shape credential verification (8.3, 8.4, 8.6, 11.3).

        Returns the account only when the stored verifier matches the
        submitted password AND the account is enabled; ``None`` otherwise.
        Every failure path (unknown username, wrong password, disabled
        account, missing verifier, empty cache) runs the same single PBKDF2
        computation and returns the same ``None``.
        """
        accounts = self.load_accounts()
        if not accounts:
            logger.warning(
                "Local login rejected: no synchronized accounts available "
                "(credential cache %s is missing or empty)",
                self._path,
            )

        # The portal normalizes usernames to lowercase; accept an exact key
        # first, then the lowercased form. Both are constant-time lookups.
        key = username if username in accounts else username.lower()
        record = accounts.get(key)

        verifier = _parse_verifier(record.get("verifier")) if record else None
        matched = _verifier_matches(verifier or _DUMMY_VERIFIER, password)

        if record is None or verifier is None or not matched:
            return None
        account = _to_account(key, record)
        if not account.enabled:
            return None
        return account


def _to_account(username: str, record: Mapping[str, Any]) -> Account:
    return Account(
        username=username,
        email=str(record.get("email") or ""),
        role=str(record.get("role") or ""),
        enabled=record.get("enabled") is True,
    )


# Module-level default instance: the login endpoint and the authorization
# dependency share one cache view over the on-device path.
_default_cache = CredentialCache()


def get_default_cache() -> CredentialCache:
    return _default_cache


def verify_credentials(username: str, password: str) -> Optional[Account]:
    """Verify against the default on-device cache (see
    :meth:`CredentialCache.verify_credentials`)."""
    return _default_cache.verify_credentials(username, password)


def get_account(username: str) -> Optional[Account]:
    """Look up an account in the default on-device cache (see
    :meth:`CredentialCache.get_account`)."""
    return _default_cache.get_account(username)
