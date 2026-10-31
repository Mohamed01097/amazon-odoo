"""Signature-verification tests using a locally generated throwaway keypair.

The keys here are generated per test run and never leave the process; they are
not related to any real Wix app credential.
"""

import base64
import json
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.sdlc_wix_connector.models.wix_api import (
    WixSignatureError,
    extract_event_payload,
    sanitize_for_log,
    verify_wix_jwt,
)


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, public_pem


def make_jwt(private_key, claims, alg='RS256'):
    header = _b64url(json.dumps({'alg': alg, 'typ': 'JWT'}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode('ascii')
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


@tagged('post_install', '-at_install')
class TestWixJwtVerification(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_key, cls.public_pem = generate_keypair()
        cls.other_private_key, cls.other_public_pem = generate_keypair()

    def test_valid_signature_returns_claims(self):
        claims = {'data': '{"eventType":"OrderCreated"}', 'exp': int(time.time()) + 600}
        token = make_jwt(self.private_key, claims)
        decoded = verify_wix_jwt(token, self.public_pem)
        self.assertEqual(decoded['data'], claims['data'])

    def test_signature_from_other_key_is_rejected(self):
        token = make_jwt(self.other_private_key, {'data': '{}'})
        with self.assertRaises(WixSignatureError):
            verify_wix_jwt(token, self.public_pem)

    def test_tampered_payload_is_rejected(self):
        token = make_jwt(self.private_key, {'data': '{"amount":"1.00"}'})
        header, payload, signature = token.split('.')
        forged_payload = _b64url(json.dumps({'data': '{"amount":"9999.00"}'}).encode())
        with self.assertRaises(WixSignatureError):
            verify_wix_jwt(f"{header}.{forged_payload}.{signature}", self.public_pem)

    def test_expired_token_is_rejected(self):
        claims = {'data': '{}', 'exp': int(time.time()) - 86400}
        token = make_jwt(self.private_key, claims)
        with self.assertRaises(WixSignatureError):
            verify_wix_jwt(token, self.public_pem)

    def test_alg_none_is_rejected(self):
        """An unsigned 'none' token must never be accepted."""
        header = _b64url(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())
        payload = _b64url(json.dumps({'data': '{}'}).encode())
        with self.assertRaises(WixSignatureError):
            verify_wix_jwt(f"{header}.{payload}.", self.public_pem)

    def test_malformed_tokens_are_rejected(self):
        for bad in ('', '   ', 'not-a-jwt', 'only.two'):
            with self.assertRaises(WixSignatureError):
                verify_wix_jwt(bad, self.public_pem)

    def test_missing_public_key_is_rejected(self):
        token = make_jwt(self.private_key, {'data': '{}'})
        for empty in (None, '', '   '):
            with self.assertRaises(WixSignatureError):
                verify_wix_jwt(token, empty)

    def test_invalid_pem_is_rejected(self):
        token = make_jwt(self.private_key, {'data': '{}'})
        with self.assertRaises(WixSignatureError):
            verify_wix_jwt(token, 'this is not a PEM key')

    def test_extract_event_payload_unwraps_nested_json_strings(self):
        inner = {'entityId': 'order-1', 'createdEvent': {'entity': {'id': 'order-1'}}}
        claims = {'data': json.dumps({
            'eventType': 'OrderCreated',
            'entityFqdn': 'wix.ecom.v1.order',
            'slug': 'created',
            'data': json.dumps(inner),
        })}
        event = extract_event_payload(claims)
        self.assertEqual(event['entityFqdn'], 'wix.ecom.v1.order')
        self.assertEqual(event['data']['entityId'], 'order-1')

    def test_extract_event_payload_tolerates_plain_objects(self):
        claims = {'data': {'entityFqdn': 'wix.ecom.v1.order', 'data': {'entityId': 'o2'}}}
        event = extract_event_payload(claims)
        self.assertEqual(event['data']['entityId'], 'o2')

    def test_extract_event_payload_tolerates_garbage(self):
        for claims in ({}, {'data': 'not json'}, {'data': None}):
            self.assertIsInstance(extract_event_payload(claims), dict)

    def test_sanitize_redacts_credentials(self):
        cleaned = sanitize_for_log({
            'api_key': 'super-secret',
            'nested': {'authorization': 'Bearer x', 'keep': 'visible'},
        })
        self.assertEqual(cleaned['api_key'], '***redacted***')
        self.assertEqual(cleaned['nested']['authorization'], '***redacted***')
        self.assertEqual(cleaned['nested']['keep'], 'visible')
