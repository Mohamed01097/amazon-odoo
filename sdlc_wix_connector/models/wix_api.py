"""Wix API helpers.

Wix delivers each webhook as an HTTP POST whose body is a single RS256-signed
JWT (see https://dev.wix.com/docs/build-apps/develop-your-app/auth/verify-requests-received-from-wix).
There is no HMAC shared secret and no JWKS endpoint: verification uses the
per-app public key copied from the Wix app dashboard.

Signature verification is implemented directly on top of ``cryptography``
rather than a JWT library so the module adds no runtime dependency. The
algorithm is pinned to RS256 and the token's own ``alg`` header is never
trusted, which removes the algorithm-confusion class of JWT bugs.
"""

import base64
import binascii
import json
import logging
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_logger = logging.getLogger(__name__)


# Only RS256 is accepted. The value is never read from the token header.
_SIGNING_HASH = hashes.SHA256()

# Tolerance applied to `exp` so small clock skew between Wix and this server
# does not reject an otherwise valid webhook.
CLOCK_SKEW_TOLERANCE_SECONDS = 300

# Keys whose values must never reach a log or a stored payload.
SENSITIVE_KEYS = frozenset({
    'api_key', 'apikey', 'authorization', 'access_token', 'refresh_token',
    'client_secret', 'secret', 'password', 'token', 'webhook_public_key',
})


class WixSignatureError(Exception):
    """Raised when an inbound payload is not a valid Wix-signed JWT."""


def _b64url_decode(segment):
    """Decode a base64url segment that may have had its padding stripped."""
    padding_needed = -len(segment) % 4
    try:
        return base64.urlsafe_b64decode(segment + ('=' * padding_needed))
    except (binascii.Error, ValueError) as exc:
        raise WixSignatureError("Malformed base64url segment in JWT.") from exc


def _load_public_key(public_key_pem):
    if not public_key_pem or not public_key_pem.strip():
        raise WixSignatureError(
            "No Wix webhook public key is configured for this connection."
        )
    try:
        key = serialization.load_pem_public_key(public_key_pem.strip().encode())
    except (ValueError, TypeError) as exc:
        raise WixSignatureError("The configured Wix public key is not valid PEM.") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise WixSignatureError("The configured Wix public key is not an RSA key.")
    return key


def verify_wix_jwt(raw_token, public_key_pem, now=None):
    """Verify a Wix-signed JWT and return its decoded claims.

    :param raw_token: the raw request body, as ``str`` or ``bytes``. It must be
        passed through unmodified: re-serializing the JSON breaks the signature.
    :param public_key_pem: the app's public key, PEM encoded.
    :returns: the decoded JWT claim set as a ``dict``.
    :raises WixSignatureError: if the token is malformed, not signed by the
        holder of the configured key, or expired.
    """
    if isinstance(raw_token, bytes):
        token = raw_token.decode('ascii', errors='strict') if raw_token else ''
    else:
        token = raw_token or ''
    token = token.strip()
    if not token:
        raise WixSignatureError("Empty request body; expected a Wix JWT.")

    parts = token.split('.')
    if len(parts) != 3:
        raise WixSignatureError("Expected a three-segment JWT.")
    header_b64, payload_b64, signature_b64 = parts

    signature = _b64url_decode(signature_b64)
    signing_input = f"{header_b64}.{payload_b64}".encode('ascii')

    public_key = _load_public_key(public_key_pem)
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), _SIGNING_HASH)
    except InvalidSignature as exc:
        raise WixSignatureError("JWT signature does not match the configured Wix key.") from exc

    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError) as exc:
        raise WixSignatureError("JWT payload is not valid JSON.") from exc
    if not isinstance(claims, dict):
        raise WixSignatureError("JWT payload is not a JSON object.")

    expires_at = claims.get('exp')
    if expires_at is not None:
        try:
            expires_at = int(expires_at)
        except (TypeError, ValueError):
            raise WixSignatureError("JWT 'exp' claim is not an integer.") from None
        reference = int(now if now is not None else time.time())
        if reference > expires_at + CLOCK_SKEW_TOLERANCE_SECONDS:
            raise WixSignatureError("JWT has expired.")

    return claims


def extract_event_payload(claims):
    """Unwrap the nested Wix event envelope into a single dict.

    Wix nests the event: the JWT's ``data`` claim is itself a JSON *string*
    which, once parsed, holds the event metadata and another JSON string in its
    own ``data`` key. Both levels are tolerated as either a string or an
    already-decoded object, because this envelope shape is the part of the
    contract most likely to differ from the documentation in practice.
    """
    def _maybe_parse(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return {}
        return value if isinstance(value, dict) else {}

    envelope = _maybe_parse(claims.get('data'))
    inner = _maybe_parse(envelope.get('data'))

    # The order entity may sit at either nesting level depending on the event.
    merged = dict(envelope)
    if inner:
        merged.setdefault('eventType', envelope.get('eventType'))
        merged['data'] = inner
    return merged


def sanitize_for_log(value, _depth=0):
    """Return a copy of ``value`` with credential-bearing keys redacted."""
    if _depth > 6:
        return '***truncated***'
    if isinstance(value, dict):
        return {
            key: (
                '***redacted***'
                if isinstance(key, str) and key.lower() in SENSITIVE_KEYS
                else sanitize_for_log(val, _depth + 1)
            )
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_for_log(item, _depth + 1) for item in value]
    return value
