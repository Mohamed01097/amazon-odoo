"""Bosta REST API client.

Verified against Bosta's official documentation
(https://docs.bosta.co/docs/how-to/get-delivery-status-via-webhook/, which
documents the v2 delivery path):

* Base path is ``/api/v2``; creating a delivery is
  ``POST /api/v2/deliveries?apiVersion=1``.
* The API key is sent raw in the ``Authorization`` header, with **no**
  ``Bearer`` prefix.
* A per-order webhook can be registered by including ``webhookUrl`` and
  ``webhookCustomHeaders`` in the create payload.

Deliberately NOT assumed:

* No staging base URL is documented by Bosta, so the host is configuration on
  ``bosta.instance`` rather than a constant here.
* The exact ``specs`` sub-schema for v2 (``size`` / ``packageType`` /
  ``goodsInfo``) is not consistently documented. It is built in one small
  method, :meth:`build_specs`, so it can be corrected once verified against a
  real API response without touching request handling or the job queue.
"""

import json
import logging
import random
import time

import requests

_logger = logging.getLogger(__name__)

API_BASE_PATH = '/api/v2'
CREATE_DELIVERY_QUERY = {'apiVersion': '1'}

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5
MAX_RETRY_SLEEP = 30
REQUEST_TIMEOUT = 30
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# Delivery types confirmed by Bosta's documentation. Type 20 (RTO) exists as a
# webhook `type` value but is not documented as creatable, so it is omitted.
DELIVERY_TYPE_SEND = 10
DELIVERY_TYPE_CASH_COLLECTION = 15
DELIVERY_TYPE_CUSTOMER_RETURN_PICKUP = 25
DELIVERY_TYPE_EXCHANGE = 30

SENSITIVE_KEYS = frozenset({
    'authorization', 'api_key', 'apikey', 'api_token', 'token', 'secret',
    'webhook_secret', 'webhookcustomheaders', 'password',
})


class BostaError(Exception):
    """A Bosta API call failed.

    :param retryable: whether retrying the same request could succeed.
    """

    def __init__(self, message, status_code=None, retryable=False, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.payload = payload


class BostaAPI:
    """Thin, log-safe wrapper over the Bosta REST API."""

    def __init__(self, base_url, api_key):
        self.base_url = (base_url or '').rstrip('/')
        self._api_key = api_key or ''

    # ------------------------------------------------------------------
    # Log safety
    # ------------------------------------------------------------------

    @classmethod
    def sanitize(cls, value, _depth=0):
        """Return a copy of ``value`` with credential-bearing keys redacted."""
        if _depth > 6:
            return '***truncated***'
        if isinstance(value, dict):
            return {
                key: (
                    '***redacted***'
                    if isinstance(key, str) and key.lower() in SENSITIVE_KEYS
                    else cls.sanitize(val, _depth + 1)
                )
                for key, val in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls.sanitize(item, _depth + 1) for item in value]
        return value

    def _headers(self):
        # Bosta expects the raw key; a "Bearer " prefix is rejected.
        return {
            'Authorization': self._api_key,
            'Content-Type': 'application/json',
            'X-Requested-By': 'odoo-sdlc-bosta-connector',
        }

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _request(self, method, path, payload=None, params=None):
        """Perform one API call, retrying only bounded, retryable failures."""
        if not self.base_url:
            raise BostaError("No Bosta API base URL is configured.")
        if not self._api_key:
            raise BostaError("No Bosta API key is configured.")

        url = f"{self.base_url}{API_BASE_PATH}/{path.lstrip('/')}"
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=payload,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = BostaError(
                    f"Could not reach Bosta: {exc}", retryable=True,
                )
            else:
                if response.status_code in RETRYABLE_STATUS_CODES:
                    last_error = BostaError(
                        self._error_message(response),
                        status_code=response.status_code,
                        retryable=True,
                        payload=self._safe_json(response),
                    )
                    self._sleep_before_retry(attempt, response)
                    continue
                if not response.ok:
                    # 4xx other than 429: the request itself is wrong, so
                    # retrying it unchanged cannot help.
                    raise BostaError(
                        self._error_message(response),
                        status_code=response.status_code,
                        retryable=False,
                        payload=self._safe_json(response),
                    )
                return self._safe_json(response) or {}

            if attempt < MAX_RETRIES:
                self._sleep_before_retry(attempt)

        raise last_error or BostaError("Bosta request failed.", retryable=True)

    @staticmethod
    def _sleep_before_retry(attempt, response=None):
        delay = min(RETRY_BACKOFF_BASE ** attempt, MAX_RETRY_SLEEP)
        if response is not None:
            retry_after = response.headers.get('Retry-After')
            if retry_after:
                try:
                    delay = min(float(retry_after), MAX_RETRY_SLEEP)
                except (TypeError, ValueError):
                    pass
        time.sleep(delay + random.uniform(0, 0.5))

    @staticmethod
    def _safe_json(response):
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def _error_message(cls, response):
        body = cls._safe_json(response)
        if isinstance(body, dict):
            message = body.get('message') or body.get('error')
            if message:
                return f"Bosta returned {response.status_code}: {message}"
        return f"Bosta returned HTTP {response.status_code}."

    # ------------------------------------------------------------------
    # Payload construction
    # ------------------------------------------------------------------

    @staticmethod
    def build_specs(items_count, description=None, weight=None):
        """Build the ``specs`` block of a create-delivery request.

        Isolated on purpose: Bosta's published sources disagree on whether v2
        expects ``specs.size``, ``specs.packageType`` or neither alongside
        ``packageDetails``, so only the consistently documented
        ``packageDetails`` keys are sent. Correct this one method once the
        schema is confirmed against a real response.
        """
        package_details = {'itemsCount': max(1, int(items_count or 1))}
        if description:
            package_details['description'] = description[:500]
        if weight:
            try:
                package_details['weight'] = round(float(weight), 2)
            except (TypeError, ValueError):
                pass
        return {'packageDetails': package_details}

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    def create_delivery(self, payload):
        """Create one delivery. Returns the ``data`` object Bosta replies with."""
        result = self._request(
            'post', 'deliveries', payload=payload, params=CREATE_DELIVERY_QUERY,
        )
        data = result.get('data') if isinstance(result, dict) else None
        if not isinstance(data, dict) or not data.get('trackingNumber'):
            raise BostaError(
                "Bosta accepted the request but returned no tracking number.",
                retryable=False,
                payload=result,
            )
        return data

    def get_delivery_tracking(self, tracking_number):
        """Read the tracking timeline for one delivery (used for reconciliation)."""
        return self._request('get', f'deliveries/{tracking_number}/tracking')

    def get_cities(self):
        """List Bosta cities. Non-destructive; useful to validate credentials."""
        return self._request('get', 'cities')

    def get_city_zones(self, city_id):
        """List the zones of one city, each with its districts."""
        return self._request('get', f'cities/{city_id}/zones')
