
import base64
import csv
import gzip
import io
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from requests.exceptions import ConnectionError as ReqConnectionError, Timeout

_logger = logging.getLogger(__name__)

# Retry config
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5  # seconds — 1.5, 3, 6
MAX_RETRY_SLEEP = 20.0
AMAZON_SAFE_BEFORE_DELAY = timedelta(minutes=3)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
REDACTED = '***REDACTED***'
SENSITIVE_KEYS = {
    'access_token', 'authorization', 'client_secret', 'lwa_access_token',
    'password', 'refresh_token', 'secret', 'signature', 'token',
    'x-amz-access-token', 'x-amz-security-token',
}

REGION_ENDPOINTS = {
    'na': 'https://sellingpartnerapi-na.amazon.com',
    'eu': 'https://sellingpartnerapi-eu.amazon.com',
    'fe': 'https://sellingpartnerapi-fe.amazon.com',
}


def amazon_to_utc_naive(value):
    """Normalize Odoo/Amazon datetime values to UTC-naive datetimes."""
    if not value:
        return False
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith('Z'):
            normalized = normalized[:-1] + '+00:00'
        value = datetime.fromisoformat(normalized)
    if value.tzinfo:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def amazon_safe_before_dt(requested_before=None):
    """Return a safe Amazon Orders API upper bound with a 3-minute UTC delay."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    safe_before = now_utc - AMAZON_SAFE_BEFORE_DELAY
    requested_before = amazon_to_utc_naive(requested_before)
    if not requested_before:
        return safe_before
    return min(requested_before, safe_before)


def amazon_safe_before_iso(requested_before=None):
    """Return a safe Amazon Orders API upper bound formatted as UTC ISO-8601."""
    return amazon_safe_before_dt(requested_before).strftime('%Y-%m-%dT%H:%M:%SZ')

# ── Report types ──
REPORT_MERCHANT_LISTINGS = 'GET_MERCHANT_LISTINGS_ALL_DATA'
REPORT_FBA_INVENTORY = 'GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA'
REPORT_FBA_SHIPMENT = 'GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL'
REPORT_FBA_RETURNS = 'GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA'
REPORT_FBA_INVENTORY_ADJUSTMENT = 'GET_LEDGER_DETAIL_VIEW_DATA'
REPORT_FBA_REIMBURSEMENTS = 'GET_FBA_REIMBURSEMENTS_DATA'
REPORT_SETTLEMENT = 'GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE'
REPORT_SELLER_FEEDBACK = 'GET_SELLER_FEEDBACK_DATA'
REPORT_REMOVAL_ORDER_DETAIL = 'GET_FBA_FULFILLMENT_REMOVAL_ORDER_DETAIL_DATA'
REPORT_REMOVAL_SHIPMENT_DETAIL = 'GET_FBA_FULFILLMENT_REMOVAL_SHIPMENT_DETAIL_DATA'
REPORT_VCS_TAX = 'GET_FLAT_FILE_VAT_INVOICE_DATA_REPORT'
REPORT_FBA_LIVE_STOCK = 'GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA'

# ── Feed types ──
# Listings data (inventory + pricing) must use JSON_LISTINGS_FEED. The legacy
# XML feed types below were removed by Amazon on 2025-07-31 and now return 403.
# See: https://developer-docs.amazon.com/sp-api/changelog/update-removal-date-of-feeds-api-support-for-xml-and-flat-file-listings-feeds-changed-to-july-31-2025-1
FEED_JSON_LISTINGS = 'JSON_LISTINGS_FEED'
FEED_POST_PRODUCT_PRICING = 'POST_PRODUCT_PRICING_DATA'   # deprecated
FEED_POST_INVENTORY = 'POST_INVENTORY_AVAILABILITY_DATA'  # deprecated
FEED_ORDER_FULFILLMENT = 'POST_ORDER_FULFILLMENT_DATA'
FEED_INVOICE_UPLOAD = 'UPLOAD_VAT_INVOICE'
FEED_FBA_CREATE_REMOVAL = 'POST_FLAT_FILE_FBA_CREATE_REMOVAL'


class AmazonAPI():

    # ══════════════════════════════════════════════════
    # SP-API requests
    # ══════════════════════════════════════════════════

    @classmethod
    def _is_sensitive_key(cls, key):
        key = str(key or '').lower()
        return any(part in key for part in SENSITIVE_KEYS)

    @classmethod
    def _sanitize_for_log(cls, value):
        if isinstance(value, dict):
            clean = {}
            for key, val in value.items():
                clean[key] = REDACTED if cls._is_sensitive_key(key) else cls._sanitize_for_log(val)
            return clean
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_for_log(item) for item in value]
        if isinstance(value, (bytes, bytearray)):
            if len(value) > 2048:
                return '<%d bytes>' % len(value)
            try:
                return value.decode('utf-8')
            except UnicodeDecodeError:
                return '<%d bytes>' % len(value)
        return value

    @staticmethod
    def _headers_to_dict(headers):
        return dict(headers or {})

    @classmethod
    def _sanitize_headers(cls, headers):
        return cls._sanitize_for_log(cls._headers_to_dict(headers))

    @staticmethod
    def _safe_response_json(response):
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def _amazon_request_id(response):
        if response is None:
            return ''
        return (
            response.headers.get('x-amzn-RequestId')
            or response.headers.get('x-amzn-requestid')
            or response.headers.get('x-amz-request-id')
            or response.headers.get('x-amz-id-2')
            or ''
        )

    @classmethod
    def _json_response_with_request_id(cls, response):
        """Return JSON while preserving the Amazon request ID for callers."""
        data = response.json()
        if not isinstance(data, dict):
            return {
                '_amazon_response': data,
                '_amazon_request_id': cls._amazon_request_id(response),
            }
        data = dict(data)
        request_id = cls._amazon_request_id(response)
        if request_id:
            data['_amazon_request_id'] = request_id
        return data

    @staticmethod
    def _extract_amazon_error(response_json):
        error = {}
        if isinstance(response_json, dict):
            errors = response_json.get('errors')
            if isinstance(errors, list) and errors:
                error = errors[0] if isinstance(errors[0], dict) else {'message': errors[0]}
            elif isinstance(response_json.get('error'), dict):
                error = response_json.get('error') or {}
            else:
                error = response_json
        return {
            'code': error.get('code') or error.get('error') or '',
            'message': error.get('message') or error.get('error_description') or '',
            'details': error.get('details') or error.get('detail') or '',
        }

    @staticmethod
    def _safe_url_for_log(url):
        parts = urlsplit(url or '')
        if not parts.query:
            return url or ''
        return urlunsplit((parts.scheme, parts.netloc, parts.path, '<redacted>', parts.fragment))

    def _log_amazon_request(self, instance, method, url, request_headers=None,
                            params=None, payload=None, response=None, elapsed=0.0,
                            error=None, response_body=None):
        """Write a raw Amazon HTTP exchange to Sync Logs without failing the call."""
        if not instance or not getattr(instance, 'env', False):
            return
        try:
            response_json = self._safe_response_json(response) if response is not None else None
            if response_json is not None:
                response_json = self._sanitize_for_log(response_json)
            if response is not None and response_body is None:
                response_body = (
                    json.dumps(response_json, default=str, indent=2)
                    if response_json is not None else response.text
                )
            request_data = {
                'endpoint': url,
                'method': method,
                'params': self._sanitize_for_log(params or {}),
                'payload': self._sanitize_for_log(payload),
                'headers': self._sanitize_headers(request_headers),
                'execution_time_seconds': round(elapsed or 0.0, 6),
            }
            response_data = {
                'status_code': response.status_code if response is not None else None,
                'amazon_request_id': self._amazon_request_id(response),
                'headers': self._sanitize_headers(response.headers if response is not None else {}),
                'response_body': response_body or '',
                'response_json': response_json,
            }
            instance.env['amazon.sync.log'].sudo().log_api_request(
                instance,
                request_data=request_data,
                response_data=response_data,
                error_message=str(error) if error else '',
                duration_seconds=elapsed or 0.0,
            )
        except Exception as log_exc:
            _logger.warning("Could not write Amazon API sync log: %s", log_exc)

    @classmethod
    def format_response_diagnostic(cls, response, method=None, url=None, request_headers=None,
                                   payload=None, elapsed=None):
        response_json = cls._safe_response_json(response)
        error = cls._extract_amazon_error(response_json)
        req = getattr(response, 'request', None)
        req_headers = request_headers or (req.headers if req is not None else {})
        req_method = method or (req.method if req is not None else '')
        req_url = url or getattr(response, 'url', '') or (req.url if req is not None else '')
        details = error.get('details')
        if isinstance(details, (dict, list)):
            details = json.dumps(details, default=str, indent=2)
        response_json_text = (
            json.dumps(response_json, default=str, indent=2)
            if response_json is not None else 'No JSON response. Raw response text is shown in Response Body.'
        )
        lines = [
            'HTTP Status: %s' % response.status_code,
            'Amazon Error Code: %s' % (error.get('code') or 'N/A'),
            'Amazon Error Message: %s' % (error.get('message') or 'N/A'),
            'Amazon Details: %s' % (details or 'N/A'),
            'Amazon Request ID: %s' % (cls._amazon_request_id(response) or 'N/A'),
            'Request URL: %s' % req_url,
            'Request Method: %s' % req_method,
            'Request Headers: %s' % json.dumps(cls._sanitize_headers(req_headers), default=str, indent=2),
            'Request Payload: %s' % json.dumps(cls._sanitize_for_log(payload), default=str, indent=2),
            'Response Headers: %s' % json.dumps(cls._sanitize_headers(response.headers), default=str, indent=2),
            'Response Body: %s' % (response.text or ''),
            'Response JSON: %s' % response_json_text,
        ]
        if elapsed is not None:
            lines.append('Execution Time: %.3fs' % elapsed)
        return '\n'.join(lines)

    @classmethod
    def format_exception(cls, exc):
        response = getattr(exc, 'response', None)
        if response is not None:
            return cls.format_response_diagnostic(response)
        return str(exc)

    def _raise_amazon_http_error(self, response, method, url, request_headers=None,
                                 payload=None, elapsed=None):
        diagnostic = self.format_response_diagnostic(
            response,
            method=method,
            url=url,
            request_headers=request_headers,
            payload=payload,
            elapsed=elapsed,
        )
        error = requests.exceptions.HTTPError(diagnostic, response=response)
        error.amazon_diagnostic = diagnostic
        raise error

    def _amazon_request(self, instance, access_token, method, path, params=None,
                        data=None, json_data=None, headers=None, body=None,
                        raw_body=None, extra_headers=None, max_retries=MAX_RETRIES):
        """Make an LWA token-authenticated SP-API request.

        Amazon SP-API no longer needs IAM credentials or AWS Signature V4 for
        these calls. The LWA access token is sent in ``x-amz-access-token``.
        """
        endpoint = self._get_endpoint(instance)
        url = path if str(path).startswith(('http://', 'https://')) else "%s/%s" % (
            endpoint.rstrip('/'), str(path).lstrip('/'),
        )
        request_headers = {
            'x-amz-access-token': access_token,
            'content-type': 'application/json',
            'accept': 'application/json',
            'user-agent': 'Odoo Amazon Connector / Odoo 19',
        }
        if body is not None and json_data is None:
            json_data = body
        if raw_body is not None:
            data = raw_body
            json_data = None
        if extra_headers:
            request_headers.update(extra_headers)
        if headers:
            request_headers.update(headers)

        method = method.upper()
        if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
            raise ValueError("Unsupported HTTP method: %s" % method)

        # Retry with exponential backoff for transient failures
        last_exc = None
        response = None
        for attempt in range(max_retries + 1):
            try:
                payload_for_log = json_data if json_data is not None else data
                kwargs = {
                    'headers': request_headers,
                    'params': params,
                    'timeout': 30,
                }
                if method in ('POST', 'PUT', 'PATCH'):
                    if json_data is not None:
                        kwargs['json'] = json_data
                    elif data is not None:
                        kwargs['data'] = data
                start = time.monotonic()
                response = requests.request(method, url, **kwargs)
                elapsed = time.monotonic() - start
                self._log_amazon_request(
                    instance, method, url, request_headers=request_headers,
                    params=params, payload=payload_for_log, response=response,
                    elapsed=elapsed,
                )

                # Retryable server errors (429, 5xx)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
                    wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                    # Respect Retry-After header if present
                    retry_after = response.headers.get('Retry-After')
                    retry_after_seconds = None
                    if retry_after:
                        try:
                            retry_after_seconds = float(retry_after)
                            wait = max(wait, retry_after_seconds)
                        except (ValueError, TypeError):
                            pass
                    if response.status_code == 429 and retry_after_seconds and retry_after_seconds > MAX_RETRY_SLEEP:
                        _logger.warning(
                            "Amazon %s %s returned 429 with Retry-After %.1fs; "
                            "raising for caller-level deferred retry.",
                            method, url, retry_after_seconds,
                        )
                        self._raise_amazon_http_error(
                            response, method, url, request_headers=request_headers,
                            payload=payload_for_log, elapsed=elapsed,
                        )
                    wait += random.uniform(0.0, min(wait * 0.25, 2.0))
                    wait = min(wait, MAX_RETRY_SLEEP)
                    _logger.warning(
                        "Amazon %s %s returned %s — retrying in %.1fs (attempt %d/%d)",
                        method, url, response.status_code, wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    continue

                if response.status_code >= 400:
                    self._raise_amazon_http_error(
                        response, method, url, request_headers=request_headers,
                        payload=payload_for_log, elapsed=elapsed,
                    )
                return response

            except (ReqConnectionError, Timeout) as exc:
                last_exc = exc
                elapsed = time.monotonic() - start if 'start' in locals() else 0.0
                self._log_amazon_request(
                    instance, method, url, request_headers=request_headers,
                    params=params, payload=payload_for_log if 'payload_for_log' in locals() else None,
                    response=None, elapsed=elapsed, error=exc,
                )
                if attempt < max_retries:
                    wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                    wait += random.uniform(0.0, min(wait * 0.25, 2.0))
                    _logger.warning(
                        "Amazon %s %s network error: %s — retrying in %.1fs (attempt %d/%d)",
                        method, url, exc, wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    continue
                raise

        # If we exhausted retries on a retryable status code, raise
        if response is not None:
            self._raise_amazon_http_error(
                response, method, url, request_headers=request_headers,
                payload=payload_for_log if 'payload_for_log' in locals() else None,
            )
        if last_exc:
            raise last_exc
        return response

    def _signed_request(self, instance, access_token, method, url, params=None,
                        body=None, raw_body=None, extra_headers=None):
        """Backward-compatible alias for older call sites.

        The implementation is intentionally token-only; no AWS SigV4
        Authorization header is generated.
        """
        data = raw_body
        json_data = None if raw_body is not None else body
        return self._amazon_request(
            instance, access_token, method, url, params=params,
            data=data, json_data=json_data, headers=extra_headers,
        )

    # ══════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════

    def _get_endpoint(self, instance):
        return REGION_ENDPOINTS.get(instance.region, REGION_ENDPOINTS['fe'])

    def get_access_token(self, instance):
        """Get LWA access token (no AWS signing needed)."""
        url = "https://api.amazon.com/auth/o2/token"
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": instance.refresh_token,
            "client_id": instance.client_id,
            "client_secret": instance.client_secret,
        }
        headers = {'content-type': 'application/x-www-form-urlencoded', 'accept': 'application/json'}
        start = time.monotonic()
        response = requests.post(url, data=payload, headers=headers, timeout=30)
        elapsed = time.monotonic() - start
        self._log_amazon_request(
            instance, 'POST', url, request_headers=headers, payload=payload,
            response=response, elapsed=elapsed,
        )
        try:
            data = response.json()
        except ValueError as exc:
            if not response.ok:
                self._raise_amazon_http_error(
                    response, 'POST', url, request_headers=headers,
                    payload=payload, elapsed=elapsed,
                )
            raise requests.exceptions.RequestException(
                "Amazon OAuth endpoint returned a non-JSON response:\n%s"
                % self.format_response_diagnostic(
                    response, method='POST', url=url,
                    request_headers=headers, payload=payload, elapsed=elapsed,
                )
            ) from exc
        if not response.ok:
            self._raise_amazon_http_error(
                response, 'POST', url, request_headers=headers,
                payload=payload, elapsed=elapsed,
            )
        if not data.get("access_token"):
            error = data.get("error") or "missing_access_token"
            description = data.get("error_description") or data.get("message") or "No access token returned."
            raise requests.exceptions.RequestException(
                "Amazon OAuth error %s: %s\n%s" % (
                    error,
                    description,
                    self.format_response_diagnostic(
                        response, method='POST', url=url,
                        request_headers=headers, payload=payload, elapsed=elapsed,
                    ),
                )
            )
        return data.get("access_token")

    def get_marketplace_participations(self, instance, access_token):
        """Lightweight read-only Sellers API v1 authorization check."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/sellers/v1/marketplaceParticipations"
        response = self._amazon_request(instance, access_token, 'GET', url)
        return self._json_response_with_request_id(response)

    # ══════════════════════════════════════════════════
    # Reports API
    # ══════════════════════════════════════════════════

    def create_report(self, instance, access_token, report_type, start_date=None,
                      end_date=None, report_options=None):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/reports/2021-06-30/reports"
        body = {
            "reportType": report_type,
            "marketplaceIds": [instance.marketplace_id],
        }
        if start_date:
            body["dataStartTime"] = start_date
        if end_date:
            body["dataEndTime"] = end_date
        if report_options:
            body["reportOptions"] = report_options
        resp = self._amazon_request(instance, access_token, 'POST', url, body=body)
        return resp.json()

    def get_report(self, instance, access_token, report_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/reports/2021-06-30/reports/{report_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

    def get_report_document(self, instance, access_token, document_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/reports/2021-06-30/documents/{document_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

    def download_report_document(self, document_url, compression=None, encryption=None, instance=None):
        """Download a report from a pre-signed S3 URL and return decoded text.

        Amazon's report-document metadata (returned by ``get_report_document``)
        may declare a ``compressionAlgorithm`` ("GZIP") and/or an
        ``encryptionDetails`` block. The S3 body is the *raw* compressed and/or
        encrypted bytes — there is no Content-Encoding header, so requests will
        not auto-decompress, and using ``.text`` on binary input produces a
        garbage string that contains NUL bytes and crashes csv.DictReader with
        ``_csv.Error: line contains NUL``.

        Callers MUST pass through the document metadata so we can reverse the
        encoding correctly.

        :param document_url: pre-signed S3 URL from ``get_report_document``
        :param compression: ``compressionAlgorithm`` value (e.g. ``"GZIP"``) or None
        :param encryption: ``encryptionDetails`` dict or None
        :return: decoded UTF-8 text of the report
        """
        safe_url = self._safe_url_for_log(document_url)
        start = time.monotonic()
        response = requests.get(document_url, timeout=120)
        elapsed = time.monotonic() - start
        self._log_amazon_request(
            instance, 'GET', safe_url, response=response, elapsed=elapsed,
            response_body='<%d bytes>' % len(response.content or b''),
        )
        if response.status_code >= 400:
            self._raise_amazon_http_error(response, 'GET', safe_url, elapsed=elapsed)
        data = response.content  # bytes — never use .text here, payload may be binary

        if encryption:
            data = self._decrypt_report_payload(data, encryption)

        if (compression or '').upper() == 'GZIP':
            data = gzip.decompress(data)

        return data.decode('utf-8')

    @staticmethod
    def _decrypt_report_payload(ciphertext, encryption_details):
        """AES-256-CBC decrypt + PKCS#7 unpad. Used when a report document
        declares ``encryptionDetails``. Defers the ``cryptography`` import so
        accounts that never see encrypted documents do not require the package.
        """
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError as exc:
            raise RuntimeError(
                "Amazon report document is encrypted but the 'cryptography' "
                "Python package is not installed; cannot decrypt."
            ) from exc
        key = base64.b64decode(encryption_details['key'])
        iv = base64.b64decode(encryption_details['initializationVector'])
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        pad_len = padded[-1]
        if pad_len < 1 or pad_len > 16:
            raise ValueError("Invalid PKCS#7 padding in decrypted report payload.")
        return padded[:-pad_len]

    def wait_for_report(self, instance, access_token, report_id, max_wait=600):
        """Poll until report is DONE or FATAL."""
        elapsed = 0
        interval = 15
        while elapsed < max_wait:
            report_data = self.get_report(instance, access_token, report_id)
            status = report_data.get('processingStatus', '')
            if status == 'DONE':
                return report_data
            if status in ('CANCELLED', 'FATAL'):
                raise requests.exceptions.RequestException(
                    "Report generation failed with status: %s" % status
                )
            time.sleep(interval)
            elapsed += interval
            if elapsed > 60:
                interval = 30
        raise requests.exceptions.RequestException(
            "Report generation timed out after %d seconds." % max_wait
        )

    def fetch_report_rows(self, instance, access_token, report_type, delimiter='\t',
                          start_date=None, end_date=None, report_options=None):
        """Generic: request a report, wait, download, parse TSV/CSV → list of dicts."""
        create_resp = self.create_report(
            instance, access_token, report_type, start_date, end_date,
            report_options=report_options,
        )
        report_id = create_resp.get('reportId')
        if not report_id:
            raise requests.exceptions.RequestException("No reportId returned.")
        _logger.info("Amazon report %s created: %s", report_type, report_id)

        report_data = self.wait_for_report(instance, access_token, report_id)
        document_id = report_data.get('reportDocumentId')
        if not document_id:
            raise requests.exceptions.RequestException("Report DONE but no reportDocumentId.")

        doc_data = self.get_report_document(instance, access_token, document_id)
        download_url = doc_data.get('url')
        if not download_url:
            raise requests.exceptions.RequestException("No download URL in report document.")

        raw_text = self.download_report_document(
            download_url,
            compression=doc_data.get('compressionAlgorithm'),
            encryption=doc_data.get('encryptionDetails'),
            instance=instance,
        )
        # Amazon TSV reports use CRLF row endings, but text fields can contain
        # stray carriage returns. Keep real CRLF row breaks and turn lone CRs
        # into spaces so DictReader does not produce malformed continuation rows.
        raw_text = raw_text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', ' ')
        reader = csv.DictReader(
            io.StringIO(raw_text),
            delimiter=delimiter,
            quoting=csv.QUOTE_NONE,
            restkey='_extra_fields',
            restval='',
        )
        return list(reader)

    def fetch_merchant_listings_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_MERCHANT_LISTINGS)

    def fetch_fba_inventory_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_FBA_INVENTORY)

    def fetch_fba_shipment_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_FBA_SHIPMENT)

    def fetch_fba_returns_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_FBA_RETURNS)

    def fetch_fba_inventory_adjustment_report(self, instance, access_token,
                                              start_date=None, end_date=None):
        return self.fetch_report_rows(
            instance, access_token, REPORT_FBA_INVENTORY_ADJUSTMENT,
            start_date=start_date, end_date=end_date,
            report_options={'eventType': 'Adjustments'},
        )

    def fetch_fba_reimbursements_report(self, instance, access_token,
                                        start_date=None, end_date=None):
        return self.fetch_report_rows(
            instance, access_token, REPORT_FBA_REIMBURSEMENTS,
            start_date=start_date, end_date=end_date,
        )

    def fetch_settlement_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_SETTLEMENT)

    def fetch_seller_feedback_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_SELLER_FEEDBACK)

    def fetch_removal_order_detail_report(self, instance, access_token,
                                          start_date=None, end_date=None):
        return self.fetch_report_rows(
            instance, access_token, REPORT_REMOVAL_ORDER_DETAIL,
            start_date=start_date, end_date=end_date,
        )

    def fetch_removal_shipment_detail_report(self, instance, access_token,
                                             start_date=None, end_date=None):
        return self.fetch_report_rows(
            instance, access_token, REPORT_REMOVAL_SHIPMENT_DETAIL,
            start_date=start_date, end_date=end_date,
        )

    # Backward-compatible alias. It now returns authoritative removal-order
    # detail, never the recommendations report used by the legacy code.
    def fetch_removal_report(self, instance, access_token):
        return self.fetch_removal_order_detail_report(instance, access_token)

    def fetch_vcs_tax_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_VCS_TAX)

    def get_settlement_reports_list(self, instance, access_token,
                                    processing_statuses=None, max_pages=50):
        """Get every settlement report available to this seller.

        Paginates via ``nextToken`` until the response stops returning one,
        or ``max_pages`` is hit (defensive upper bound). SP-API allows up to
        ``pageSize=100`` per call; we use that to minimise round-trips.

        We intentionally do NOT filter ``processingStatuses`` by default —
        the previous ``DONE``-only filter silently hid IN_PROGRESS settlements
        and made callers think Amazon had no data. Callers that want only
        finished reports can opt in by passing ``processing_statuses='DONE'``.

        :param processing_statuses: optional comma-separated SP-API statuses
            (e.g. ``'DONE,IN_PROGRESS'``). If None, no filter is applied.
        :param max_pages: safety cap on pagination depth.
        :return: list of report metadata dicts, deduplicated by ``reportId``.
        """
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/reports/2021-06-30/reports"
        params = {
            "reportTypes": REPORT_SETTLEMENT,
            "pageSize": "100",
        }
        if processing_statuses:
            params["processingStatuses"] = processing_statuses

        seen_ids = set()
        out = []
        for _ in range(max_pages):
            resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
            data = resp.json()
            for rpt in data.get('reports', []) or []:
                rid = rpt.get('reportId')
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    out.append(rpt)
            next_token = data.get('nextToken')
            if not next_token:
                break
            # Per SP-API spec, follow-up calls send only nextToken; all other
            # filter params must be omitted on continuation pages.
            params = {'nextToken': next_token}
        return out

    # ══════════════════════════════════════════════════
    # Orders API
    # ══════════════════════════════════════════════════

    def get_orders(self, instance, access_token, created_after=None, created_before=None,
                   last_updated_after=None, last_updated_before=None, order_statuses=None,
                   fulfillment_channels=None, next_token=None, max_results_per_page=None):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/orders/v0/orders"
        if next_token:
            params = {"NextToken": next_token}
        else:
            params = {"MarketplaceIds": instance.marketplace_id}
            if created_after:
                params["CreatedAfter"] = created_after
            if created_before:
                params["CreatedBefore"] = amazon_safe_before_iso(created_before)
            if last_updated_after:
                params["LastUpdatedAfter"] = last_updated_after
            if last_updated_before:
                params["LastUpdatedBefore"] = amazon_safe_before_iso(last_updated_before)
            if order_statuses:
                params["OrderStatuses"] = order_statuses
            if fulfillment_channels:
                params["FulfillmentChannels"] = fulfillment_channels
            if max_results_per_page:
                params["MaxResultsPerPage"] = max_results_per_page
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return resp.json()

    def get_order(self, instance, access_token, order_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/orders/v0/orders/{order_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

    def get_order_items(self, instance, access_token, order_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/orders/v0/orders/{order_id}/orderItems"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

    def get_order_address(self, instance, access_token, order_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/orders/v0/orders/{order_id}/address"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

    # ══════════════════════════════════════════════════
    # Feeds API (for price update, stock update, shipping confirm, invoice upload)
    # ══════════════════════════════════════════════════

    def create_feed_document(self, instance, access_token, content_type='text/xml; charset=UTF-8'):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/feeds/2021-06-30/documents"
        body = {"contentType": content_type}
        resp = self._amazon_request(instance, access_token, 'POST', url, body=body)
        return resp.json()

    def upload_feed_document(self, upload_url, content, content_type='text/xml; charset=UTF-8', instance=None):
        """Upload feed content to pre-signed S3 URL."""
        headers = {'Content-Type': content_type}
        body = content if isinstance(content, (bytes, bytearray)) else content.encode('utf-8')
        safe_url = self._safe_url_for_log(upload_url)
        start = time.monotonic()
        response = requests.put(upload_url, data=body, headers=headers, timeout=60)
        elapsed = time.monotonic() - start
        self._log_amazon_request(
            instance, 'PUT', safe_url, request_headers=headers,
            payload={'content_type': content_type, 'bytes': len(body)},
            response=response, elapsed=elapsed,
        )
        if response.status_code >= 400:
            self._raise_amazon_http_error(
                response, 'PUT', safe_url, request_headers=headers,
                payload={'content_type': content_type, 'bytes': len(body)},
                elapsed=elapsed,
            )

    def create_feed(self, instance, access_token, feed_type, document_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/feeds/2021-06-30/feeds"
        body = {
            "feedType": feed_type,
            "marketplaceIds": [instance.marketplace_id],
            "inputFeedDocumentId": document_id,
        }
        resp = self._amazon_request(instance, access_token, 'POST', url, body=body)
        return resp.json()

    def get_feed(self, instance, access_token, feed_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/feeds/2021-06-30/feeds/{feed_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

    def get_feed_document(self, instance, access_token, document_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/feeds/2021-06-30/documents/{document_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

    def submit_feed(self, instance, access_token, feed_type, content, content_type='text/xml; charset=UTF-8'):
        """Full feed submission flow: create doc → upload → create feed.

        content_type defaults to XML for backward compatibility with the
        order-fulfillment and VAT-invoice flows. Listings feeds (inventory,
        pricing) must pass content_type='application/json; charset=UTF-8'
        together with feed_type=FEED_JSON_LISTINGS.
        """
        doc = self.create_feed_document(instance, access_token, content_type=content_type)
        doc_id = doc['feedDocumentId']
        upload_url = doc['url']
        self.upload_feed_document(upload_url, content, content_type=content_type, instance=instance)
        feed = self.create_feed(instance, access_token, feed_type, doc_id)
        return feed

    @staticmethod
    def build_fba_removal_flat_file(order):
        """Build Amazon's documented flat-file FBA removal feed.

        The field names deliberately mirror the official FBA feed table. The
        connector sends one row per SKU and never includes credentials or
        internal database identifiers.
        """
        partner = order.ship_to_partner_id if order.removal_type == 'return_to_address' else False
        headers = [
            'MerchantRemovalOrderID', 'RemovalDisposition', 'MerchantSKU',
            'SellableQuantity', 'UnsellableQuantity', 'AddressName',
            'AddressFieldOne', 'AddressFieldTwo', 'AddressFieldThree',
            'AddressCity', 'AddressCountryCode', 'AddressStateOrRegion',
            'AddressPostalCode', 'ContactPhoneNumber', 'ShippingNotes',
        ]

        def safe(value):
            return str(value or '').replace('\t', ' ').replace('\r', ' ').replace('\n', ' ')

        rows = ['\t'.join(headers)]
        disposition = 'Return' if order.removal_type == 'return_to_address' else 'Disposal'
        for line in order.line_ids:
            sellable = line.requested_quantity if line.disposition.lower() == 'sellable' else 0
            unsellable = line.requested_quantity if line.disposition.lower() != 'sellable' else 0
            values = [
                order.amazon_removal_order_id or order.name,
                disposition,
                line.sku,
                int(sellable),
                int(unsellable),
                partner.name if partner else '',
                partner.street if partner else '',
                partner.street2 if partner else '',
                '',
                partner.city if partner else '',
                partner.country_id.code if partner and partner.country_id else '',
                partner.state_id.code or partner.state_id.name if partner and partner.state_id else '',
                partner.zip if partner else '',
                partner.phone or partner.mobile if partner else '',
                order.shipping_notes,
            ]
            rows.append('\t'.join(safe(value) for value in values))
        return '\n'.join(rows) + '\n'

    # ── Feed XML builders ──

    def _get_feed_currency(self, item, instance=None):
        currency = item.get('currency')
        if currency:
            return currency
        if instance:
            if hasattr(instance, '_get_currency_code'):
                currency = instance._get_currency_code()
                if currency:
                    return currency
            default_currency = getattr(instance, 'default_currency_id', False)
            company = getattr(instance, 'company_id', False)
            company_currency = company.currency_id if company else False
            env_currency = instance.env.company.currency_id if getattr(instance, 'env', False) else False
            currency_rec = default_currency or company_currency or env_currency
            if currency_rec:
                return currency_rec.name
        raise ValueError("Currency is required for Amazon price feeds.")

    def build_price_feed_xml(self, items, instance=None):
        """Build XML for POST_PRODUCT_PRICING_DATA feed.
        items: list of dicts with 'sku' and 'price' keys.
        """
        lines = ['<?xml version="1.0" encoding="utf-8"?>',
                 '<AmazonEnvelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="amzn-envelope.xsd">',
                 '<Header><DocumentVersion>1.01</DocumentVersion><MerchantIdentifier>M_SELLER</MerchantIdentifier></Header>',
                 '<MessageType>Price</MessageType>']
        for i, item in enumerate(items, 1):
            lines.append('<Message><MessageID>%d</MessageID><Price>' % i)
            lines.append('<SKU>%s</SKU>' % item['sku'])
            lines.append('<StandardPrice currency="%s">%s</StandardPrice>' % (self._get_feed_currency(item, instance), item['price']))
            lines.append('</Price></Message>')
        lines.append('</AmazonEnvelope>')
        return '\n'.join(lines)

    def build_inventory_json_feed(self, instance, items):
        """Build JSON_LISTINGS_FEED payload for inventory updates.

        items: list of dicts with 'sku', 'quantity', and optional 'product_type' keys.
        Returns a single JSON document (not JSON Lines) per Amazon's listings
        feed schema v2.
        """
        feed = {
            "header": {
                "sellerId": instance.seller_id,
                "version": "2.0",
                "issueLocale": "en_US",
            },
            "messages": [
                {
                    "messageId": i,
                    "sku": item['sku'],
                    "operationType": "PATCH",
                    "productType": item.get('product_type', 'PRODUCT'),
                    "patches": [{
                        "op": "replace",
                        "path": "/attributes/fulfillment_availability",
                        "value": [{
                            "fulfillment_channel_code": "DEFAULT",
                            "quantity": int(item['quantity']),
                        }],
                    }],
                }
                for i, item in enumerate(items, 1)
            ],
        }
        return json.dumps(feed)

    def build_price_json_feed(self, instance, items):
        """Build JSON_LISTINGS_FEED payload for price updates.

        items: list of dicts with 'sku', 'price', 'currency', and optional 'product_type' keys.
        """
        feed = {
            "header": {
                "sellerId": instance.seller_id,
                "version": "2.0",
                "issueLocale": "en_US",
            },
            "messages": [
                {
                    "messageId": i,
                    "sku": item['sku'],
                    "operationType": "PATCH",
                    "productType": item.get('product_type', 'PRODUCT'),
                    "patches": [{
                        "op": "replace",
                        "path": "/attributes/purchasable_offer",
                        "value": [{
                            "marketplace_id": instance.marketplace_id,
                            "currency": self._get_feed_currency(item, instance),
                            "audience": "ALL",
                            "our_price": [{
                                "schedule": [{
                                    "value_with_tax": "%.2f" % float(item['price']),
                                }],
                            }],
                        }],
                    }],
                }
                for i, item in enumerate(items, 1)
            ],
        }
        return json.dumps(feed)

    def build_inventory_feed_xml(self, items):
        """Build XML for POST_INVENTORY_AVAILABILITY_DATA feed.
        items: list of dicts with 'sku' and 'quantity' keys.

        Deprecated by Amazon on 2025-07-31 — kept only as reference. Inventory
        updates must go through build_inventory_json_feed + FEED_JSON_LISTINGS.
        """
        lines = ['<?xml version="1.0" encoding="utf-8"?>',
                 '<AmazonEnvelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="amzn-envelope.xsd">',
                 '<Header><DocumentVersion>1.01</DocumentVersion><MerchantIdentifier>M_SELLER</MerchantIdentifier></Header>',
                 '<MessageType>Inventory</MessageType>']
        for i, item in enumerate(items, 1):
            lines.append('<Message><MessageID>%d</MessageID><Inventory>' % i)
            lines.append('<SKU>%s</SKU>' % item['sku'])
            lines.append('<Quantity>%d</Quantity>' % int(item['quantity']))
            lines.append('</Inventory></Message>')
        lines.append('</AmazonEnvelope>')
        return '\n'.join(lines)

    def build_order_fulfillment_feed_xml(self, items):
        """Build XML for POST_ORDER_FULFILLMENT_DATA feed.
        items: list of dicts with 'order_id', 'order_item_id', 'carrier', 'tracking', 'ship_date'.
        """
        lines = ['<?xml version="1.0" encoding="utf-8"?>',
                 '<AmazonEnvelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="amzn-envelope.xsd">',
                 '<Header><DocumentVersion>1.01</DocumentVersion><MerchantIdentifier>M_SELLER</MerchantIdentifier></Header>',
                 '<MessageType>OrderFulfillment</MessageType>']
        for i, item in enumerate(items, 1):
            lines.append('<Message><MessageID>%d</MessageID><OrderFulfillment>' % i)
            lines.append('<AmazonOrderID>%s</AmazonOrderID>' % item['order_id'])
            lines.append('<FulfillmentDate>%s</FulfillmentDate>' % item.get('ship_date', ''))
            lines.append('<FulfillmentData>')
            lines.append('<CarrierName>%s</CarrierName>' % item.get('carrier', 'Other'))
            lines.append('<ShippingMethod>Standard</ShippingMethod>')
            lines.append('<ShipperTrackingNumber>%s</ShipperTrackingNumber>' % item.get('tracking', ''))
            lines.append('</FulfillmentData>')
            if item.get('order_item_id'):
                lines.append('<Item><AmazonOrderItemCode>%s</AmazonOrderItemCode>' % item['order_item_id'])
                lines.append('<Quantity>%d</Quantity></Item>' % item.get('quantity', 1))
            lines.append('</OrderFulfillment></Message>')
        lines.append('</AmazonEnvelope>')
        return '\n'.join(lines)

    # ══════════════════════════════════════════════════
    # Catalog Items API
    # ══════════════════════════════════════════════════

    def search_catalog_items(self, instance, access_token, keywords=None, identifiers=None, id_type='ASIN', next_token=None):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/catalog/2022-04-01/catalogItems"
        params = {
            "marketplaceIds": instance.marketplace_id,
            "includedData": "summaries,images",
            "sellerId": instance.seller_id,
        }
        if keywords:
            params["keywords"] = keywords
        if identifiers:
            params["identifiers"] = identifiers
            params["identifiersType"] = id_type
        if next_token:
            params["pageToken"] = next_token
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return resp.json()

    # ══════════════════════════════════════════════════
    # Product Type Definitions API
    # ══════════════════════════════════════════════════

    def search_product_type_definitions(self, instance, access_token, keywords):
        """Search for valid Amazon product types by keyword."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/definitions/2020-09-01/productTypes"
        params = {
            "marketplaceIds": instance.marketplace_id,
            "keywords": keywords,
        }
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return resp.json()

    def get_product_type_definition(self, instance, access_token, product_type):
        """Get full definition/schema for a specific product type."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/definitions/2020-09-01/productTypes/{product_type}"
        params = {
            "marketplaceIds": instance.marketplace_id,
            "requirements": "LISTING",
            "locale": "en_US",
        }
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return resp.json()

    # ══════════════════════════════════════════════════
    # Listings API
    # ══════════════════════════════════════════════════

    def get_listings_item(self, instance, access_token, sku):
        endpoint = self._get_endpoint(instance)
        encoded_sku = quote(sku, safe='')
        url = f"{endpoint}/listings/2021-08-01/items/{instance.seller_id}/{encoded_sku}"
        params = {
            "marketplaceIds": instance.marketplace_id,
            "includedData": "summaries,attributes,fulfillmentAvailability,issues,offers",
        }
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        data = resp.json()
        _logger.info("Amazon GET listing %s response keys: %s", sku, list(data.keys()))
        return data

    def _listings_request(self, instance, access_token, method, sku, body=None):
        """Common method for listings API calls with proper error handling."""
        endpoint = self._get_endpoint(instance)
        encoded_sku = quote(sku, safe='')
        url = f"{endpoint}/listings/2021-08-01/items/{instance.seller_id}/{encoded_sku}"
        params = {"marketplaceIds": instance.marketplace_id}

        _logger.info("Amazon %s listing %s — body keys: %s", method, sku,
                      list(body.keys()) if body else 'none')

        resp = self._amazon_request(instance, access_token, method, url, params=params, body=body)

        try:
            data = resp.json()
        except ValueError:
            _logger.error("Amazon %s listing %s returned non-JSON: %s", method, sku, resp.text[:500])
            return {'status': 'ERROR', 'issues': [{'message': 'Non-JSON response from Amazon'}]}

        _logger.info("Amazon %s listing %s — status: %s, keys: %s",
                      method, sku, data.get('status', 'N/A'), list(data.keys()))
        if data.get('issues'):
            for issue in data['issues'][:5]:
                _logger.warning("  Issue: [%s] %s — %s",
                                issue.get('code', ''), issue.get('message', ''),
                                issue.get('attributeNames', []))
        return data

    def put_listings_item(self, instance, access_token, sku, body):
        """PUT = create or full replace a listing."""
        return self._listings_request(instance, access_token, 'PUT', sku, body)

    def patch_listings_item(self, instance, access_token, sku, body):
        """PATCH = partial update of an existing listing."""
        return self._listings_request(instance, access_token, 'PATCH', sku, body)

    # ══════════════════════════════════════════════════
    # Fulfillment Outbound API (MCF)
    # ══════════════════════════════════════════════════

    def create_fulfillment_order(self, instance, access_token, body):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/fba/outbound/2020-07-01/fulfillmentOrders"
        resp = self._amazon_request(instance, access_token, 'POST', url, body=body)
        return resp.json()

    def get_fulfillment_order(self, instance, access_token, fulfillment_order_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/fba/outbound/2020-07-01/fulfillmentOrders/{fulfillment_order_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

    def cancel_fulfillment_order(self, instance, access_token, fulfillment_order_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/fba/outbound/2020-07-01/fulfillmentOrders/{fulfillment_order_id}/cancel"
        resp = self._amazon_request(instance, access_token, 'PUT', url, body={})
        return resp.json()

    # ══════════════════════════════════════════════════
    # FBA Inventory API
    # ══════════════════════════════════════════════════

    def get_inventory_summaries(self, instance, access_token, next_token=None,
                                seller_skus=None, details=True):
        """Return one official FBA Inventory API v1 summary page.

        ``details=true`` is required by reconciliation because the summary-only
        response does not include the sellable, reserved, unfulfillable, and
        inbound quantity breakdowns.  A startDateTime is deliberately not
        accepted here: Amazon documents that changes to the three inbound
        quantities are not detected by that incremental filter.
        """
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/fba/inventory/v1/summaries"
        params = {
            "granularityType": "Marketplace",
            "granularityId": instance.marketplace_id,
            "marketplaceIds": instance.marketplace_id,
            "details": "true" if details else "false",
        }
        if seller_skus:
            if len(seller_skus) > 50:
                raise ValueError("Amazon FBA Inventory accepts at most 50 sellerSkus.")
            params["sellerSkus"] = ','.join(str(sku) for sku in seller_skus)
        if next_token:
            params["nextToken"] = next_token
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return self._json_response_with_request_id(resp)

    def get_all_inventory_summaries(self, instance, access_token,
                                    seller_skus=None, details=True):
        """Consume all FBA Inventory pages immediately and retain page evidence.

        Amazon inventory pagination tokens expire after 30 seconds, so callers
        receive one complete snapshot instead of persisting tokens for a later
        background pass.
        """
        summaries = []
        pages = []
        request_ids = []
        next_token = None
        seen_tokens = set()
        for _page_number in range(1000):
            page = self.get_inventory_summaries(
                instance,
                access_token,
                next_token=next_token,
                seller_skus=seller_skus,
                details=details,
            )
            if not isinstance(page, dict):
                raise ValueError("Amazon returned an invalid FBA inventory response.")
            payload = page.get('payload')
            page_summaries = payload.get('inventorySummaries') if isinstance(payload, dict) else None
            if not isinstance(page_summaries, list):
                raise ValueError("Amazon returned an invalid inventorySummaries list.")
            summaries.extend(page_summaries)
            pages.append(page)
            if page.get('_amazon_request_id'):
                request_ids.append(page['_amazon_request_id'])
            pagination = page.get('pagination') or {}
            next_token = pagination.get('nextToken') if isinstance(pagination, dict) else None
            if not next_token:
                break
            if next_token in seen_tokens:
                raise ValueError("Amazon repeated an FBA inventory pagination token.")
            seen_tokens.add(next_token)
        else:
            raise ValueError("Amazon FBA inventory pagination exceeded 1000 pages.")
        return {
            'payload': {'inventorySummaries': summaries},
            '_pages': pages,
            '_amazon_request_ids': request_ids,
        }

    # ══════════════════════════════════════════════════
    # Fulfillment Inbound API (v2024)
    # ══════════════════════════════════════════════════

    def create_inbound_plan(self, instance, access_token, body):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans"
        # Amazon documents no idempotency key for createInboundPlan. Retrying an
        # ambiguous timeout/5xx can therefore create a second inbound plan.
        resp = self._amazon_request(
            instance, access_token, 'POST', url, body=body, max_retries=0,
        )
        return self._json_response_with_request_id(resp)

    def get_inbound_operation_status(self, instance, access_token, operation_id):
        """Return the v2024-03-20 asynchronous inbound operation status."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/operations/{operation_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return self._json_response_with_request_id(resp)

    def get_inbound_plan(self, instance, access_token, plan_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return self._json_response_with_request_id(resp)

    def generate_packing_options(self, instance, access_token, plan_id):
        """Start v2024-03-20 packing-option generation."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}/packingOptions"
        # Amazon exposes no idempotency key for this asynchronous write.  An
        # ambiguous transport failure must be reviewed instead of replayed.
        resp = self._amazon_request(instance, access_token, 'POST', url, max_retries=0)
        return self._json_response_with_request_id(resp)

    def list_packing_options(self, instance, access_token, plan_id, page_size=20,
                             pagination_token=None):
        """Return one official listPackingOptions page."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}/packingOptions"
        params = {'pageSize': page_size}
        if pagination_token:
            params['paginationToken'] = pagination_token
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return self._json_response_with_request_id(resp)

    def confirm_packing_option(self, instance, access_token, plan_id, packing_option_id):
        """Start asynchronous confirmation of one packing option."""
        endpoint = self._get_endpoint(instance)
        url = (
            f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}"
            f"/packingOptions/{packing_option_id}/confirmation"
        )
        resp = self._amazon_request(instance, access_token, 'POST', url, max_retries=0)
        return self._json_response_with_request_id(resp)

    def list_packing_group_items(self, instance, access_token, plan_id, packing_group_id,
                                 page_size=20, pagination_token=None):
        """Return one official listPackingGroupItems page."""
        endpoint = self._get_endpoint(instance)
        url = (
            f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}"
            f"/packingGroups/{packing_group_id}/items"
        )
        params = {'pageSize': page_size}
        if pagination_token:
            params['paginationToken'] = pagination_token
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return self._json_response_with_request_id(resp)

    def generate_placement_options(self, instance, access_token, plan_id, body=None):
        """Start v2024-03-20 placement generation with its required JSON body."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}/placementOptions"
        resp = self._amazon_request(
            instance, access_token, 'POST', url, body=body or {}, max_retries=0,
        )
        return self._json_response_with_request_id(resp)

    def list_placement_options(self, instance, access_token, plan_id, page_size=20,
                               pagination_token=None):
        """Return one official listPlacementOptions page."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}/placementOptions"
        params = {'pageSize': page_size}
        if pagination_token:
            params['paginationToken'] = pagination_token
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return self._json_response_with_request_id(resp)

    def confirm_placement_option(self, instance, access_token, plan_id, placement_option_id):
        """Start asynchronous confirmation of one placement option."""
        endpoint = self._get_endpoint(instance)
        url = (
            f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}"
            f"/placementOptions/{placement_option_id}/confirmation"
        )
        resp = self._amazon_request(instance, access_token, 'POST', url, max_retries=0)
        return self._json_response_with_request_id(resp)

    def get_shipment(self, instance, access_token, plan_id, shipment_id):
        """Return one official v2024-03-20 inbound shipment."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}/shipments/{shipment_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return self._json_response_with_request_id(resp)

    def update_shipment_tracking_details(self, instance, access_token, plan_id,
                                         shipment_id, body):
        """Start the official asynchronous tracking-details update."""
        endpoint = self._get_endpoint(instance)
        url = (
            f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}"
            f"/shipments/{shipment_id}/trackingDetails"
        )
        resp = self._amazon_request(instance, access_token, 'PUT', url, body=body)
        return self._json_response_with_request_id(resp)

    def generate_transportation_options(self, instance, access_token, plan_id, body):
        """Start official v2024-03-20 transportation-option generation."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}/transportationOptions"
        resp = self._amazon_request(
            instance, access_token, 'POST', url, body=body, max_retries=0,
        )
        return self._json_response_with_request_id(resp)

    def list_transportation_options(self, instance, access_token, plan_id, page_size=20,
                                    pagination_token=None, placement_option_id=None,
                                    shipment_id=None):
        """Return one official listTransportationOptions page."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}/transportationOptions"
        params = {'pageSize': page_size}
        if pagination_token:
            params['paginationToken'] = pagination_token
        if placement_option_id:
            params['placementOptionId'] = placement_option_id
        if shipment_id:
            params['shipmentId'] = shipment_id
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return self._json_response_with_request_id(resp)

    def confirm_transportation_options(self, instance, access_token, plan_id, body):
        """Start official v2024-03-20 transportation confirmation."""
        endpoint = self._get_endpoint(instance)
        url = (
            f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}"
            "/transportationOptions/confirmation"
        )
        resp = self._amazon_request(
            instance, access_token, 'POST', url, body=body, max_retries=0,
        )
        return self._json_response_with_request_id(resp)

    def list_shipment_items(self, instance, access_token, plan_id, shipment_id,
                            page_size=20, pagination_token=None):
        """Return one official listShipmentItems page."""
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}/shipments/{shipment_id}/items"
        params = {'pageSize': page_size}
        if pagination_token:
            params['paginationToken'] = pagination_token
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return self._json_response_with_request_id(resp)

    def get_shipment_items(self, instance, access_token, plan_id, shipment_id):
        """Backward-compatible alias for the first listShipmentItems page."""
        return self.list_shipment_items(instance, access_token, plan_id, shipment_id)

    def get_inbound_shipment_items_v0(self, instance, access_token,
                                      shipment_confirmation_id, max_pages=100):
        """Return shipped/received quantities through Amazon's preserved v0 operation.

        Fulfillment Inbound v2024-03-20 ``listShipmentItems`` does not expose
        received quantities. Amazon's official migration guide explicitly keeps
        ``getShipmentItemsByShipmentId`` and ``getShipmentItems`` non-deprecated
        for this purpose and requires a v2024 ``shipmentConfirmationID`` in the
        v0 ``shipmentId`` position.

        The initial shipment-scoped operation has no NextToken request parameter.
        Amazon returns continuation tokens through the preserved ``getShipmentItems``
        operation, whose NEXT_TOKEN query continues the original result set.
        """
        confirmation_id = str(shipment_confirmation_id or '').strip()
        if not confirmation_id:
            raise ValueError("shipment_confirmation_id is required")
        endpoint = self._get_endpoint(instance)
        first_url = "%s/fba/inbound/v0/shipments/%s/items" % (
            endpoint, quote(confirmation_id, safe=''),
        )
        response = self._amazon_request(
            instance, access_token, 'GET', first_url,
        )
        page = self._json_response_with_request_id(response)
        pages = [page]
        payload = page.get('payload') or {}
        items = list(payload.get('ItemData', []) or [])
        next_token = payload.get('NextToken')
        page_count = 1
        while next_token:
            if page_count >= max_pages:
                raise requests.exceptions.RequestException(
                    "Amazon inbound shipment items exceeded the %s-page safety limit."
                    % max_pages
                )
            continuation_url = f"{endpoint}/fba/inbound/v0/shipmentItems"
            response = self._amazon_request(
                instance,
                access_token,
                'GET',
                continuation_url,
                params={
                    'QueryType': 'NEXT_TOKEN',
                    'NextToken': next_token,
                    'MarketplaceId': instance.marketplace_id,
                },
            )
            page = self._json_response_with_request_id(response)
            pages.append(page)
            payload = page.get('payload') or {}
            items.extend(payload.get('ItemData', []) or [])
            next_token = payload.get('NextToken')
            page_count += 1
        return {
            'payload': {'ItemData': items},
            '_amazon_request_ids': [
                value for value in (
                    current.get('_amazon_request_id') for current in pages
                ) if value
            ],
            '_pages': pages,
        }

    # ══════════════════════════════════════════════════
    # Product Pricing API
    # ══════════════════════════════════════════════════

    def get_competitive_pricing(self, instance, access_token, asin):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/products/pricing/v0/competitivePrice"
        params = {
            "MarketplaceId": instance.marketplace_id,
            "Asins": asin,
            "ItemType": "Asin",
        }
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return resp.json()
