
import base64
import csv
import gzip
import io
import json
import logging
import time
from urllib.parse import quote

import requests
from requests.exceptions import ConnectionError as ReqConnectionError, Timeout

_logger = logging.getLogger(__name__)

# Retry config
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5  # seconds — 1.5, 3, 6
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

REGION_ENDPOINTS = {
    'na': 'https://sellingpartnerapi-na.amazon.com',
    'eu': 'https://sellingpartnerapi-eu.amazon.com',
    'fe': 'https://sellingpartnerapi-fe.amazon.com',
}

# ── Report types ──
REPORT_MERCHANT_LISTINGS = 'GET_MERCHANT_LISTINGS_ALL_DATA'
REPORT_FBA_INVENTORY = 'GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA'
REPORT_FBA_SHIPMENT = 'GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL'
REPORT_FBA_RETURNS = 'GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA'
REPORT_FBA_INVENTORY_ADJUSTMENT = 'GET_FBA_INVENTORY_ADJUSTMENTS_DATA'
REPORT_SETTLEMENT = 'GET_V2_SETTLEMENT_REPORT_DATA_FLAT_FILE'
REPORT_SELLER_FEEDBACK = 'GET_SELLER_FEEDBACK_DATA'
REPORT_REMOVAL = 'GET_FBA_RECOMMENDED_REMOVAL_DATA'
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


class AmazonAPI():

    # ══════════════════════════════════════════════════
    # SP-API requests
    # ══════════════════════════════════════════════════

    def _amazon_request(self, instance, access_token, method, path, params=None,
                        data=None, json_data=None, headers=None, body=None,
                        raw_body=None, extra_headers=None):
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
        for attempt in range(MAX_RETRIES + 1):
            try:
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
                response = requests.request(method, url, **kwargs)

                # 400 = validation error with useful JSON — return as-is
                if response.status_code == 400:
                    _logger.warning("Amazon %s %s returned 400: %s", method, url, response.text[:500])
                    return response

                # 403 = authorization failure. Amazon returns the real reason
                # (missing role, marketplace not authorized, expired consent) in
                # the JSON body. Log it so the cause is visible — otherwise the
                # caller only sees a bare "403 Client Error".
                if response.status_code == 403:
                    _logger.warning("Amazon %s %s returned 403: %s", method, url, response.text[:500])

                # Retryable server errors (429, 5xx)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                    # Respect Retry-After header if present
                    retry_after = response.headers.get('Retry-After')
                    if retry_after:
                        try:
                            wait = max(wait, float(retry_after))
                        except (ValueError, TypeError):
                            pass
                    _logger.warning(
                        "Amazon %s %s returned %s — retrying in %.1fs (attempt %d/%d)",
                        method, url, response.status_code, wait, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue

                response.raise_for_status()
                return response

            except (ReqConnectionError, Timeout) as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                    _logger.warning(
                        "Amazon %s %s network error: %s — retrying in %.1fs (attempt %d/%d)",
                        method, url, exc, wait, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        # If we exhausted retries on a retryable status code, raise
        if response is not None:
            response.raise_for_status()
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
        response = requests.post(url, data=payload, timeout=30)
        try:
            data = response.json()
        except ValueError as exc:
            response.raise_for_status()
            raise requests.exceptions.RequestException(
                "Amazon OAuth endpoint returned a non-JSON response."
            ) from exc
        if not response.ok:
            error = data.get("error") or "oauth_error"
            description = data.get("error_description") or data.get("message") or response.text
            raise requests.exceptions.HTTPError(
                "Amazon OAuth error %s: %s" % (error, description),
                response=response,
            )
        response.raise_for_status()
        if not data.get("access_token"):
            error = data.get("error") or "missing_access_token"
            description = data.get("error_description") or data.get("message") or "No access token returned."
            raise requests.exceptions.RequestException(
                "Amazon OAuth error %s: %s" % (error, description)
            )
        return data.get("access_token")

    # ══════════════════════════════════════════════════
    # Reports API
    # ══════════════════════════════════════════════════

    def create_report(self, instance, access_token, report_type, start_date=None, end_date=None):
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

    def download_report_document(self, document_url, compression=None, encryption=None):
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
        response = requests.get(document_url, timeout=120)
        response.raise_for_status()
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

    def fetch_report_rows(self, instance, access_token, report_type, delimiter='\t', start_date=None, end_date=None):
        """Generic: request a report, wait, download, parse TSV/CSV → list of dicts."""
        create_resp = self.create_report(instance, access_token, report_type, start_date, end_date)
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
        )
        # Amazon TSV reports often contain unquoted fields (product titles,
        # descriptions) with embedded newlines, quote characters, and \r\n
        # line endings, which csv.DictReader cannot parse with default
        # settings on Python 3.12+ ("new-line character seen in unquoted
        # field"). Strip BOM, normalise line endings, and disable quote
        # interpretation since Amazon reports don't use CSV-style quoting.
        raw_text = raw_text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
        reader = csv.DictReader(io.StringIO(raw_text), delimiter=delimiter, quoting=csv.QUOTE_NONE)
        return list(reader)

    def fetch_merchant_listings_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_MERCHANT_LISTINGS)

    def fetch_fba_inventory_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_FBA_INVENTORY)

    def fetch_fba_shipment_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_FBA_SHIPMENT)

    def fetch_fba_returns_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_FBA_RETURNS)

    def fetch_fba_inventory_adjustment_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_FBA_INVENTORY_ADJUSTMENT)

    def fetch_settlement_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_SETTLEMENT)

    def fetch_seller_feedback_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_SELLER_FEEDBACK)

    def fetch_removal_report(self, instance, access_token):
        return self.fetch_report_rows(instance, access_token, REPORT_REMOVAL)

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

    def get_orders(self, instance, access_token, created_after=None, order_statuses=None, fulfillment_channels=None, next_token=None):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/orders/v0/orders"
        params = {"MarketplaceIds": instance.marketplace_id}
        if created_after:
            params["CreatedAfter"] = created_after
        if order_statuses:
            params["OrderStatuses"] = order_statuses
        if fulfillment_channels:
            params["FulfillmentChannels"] = fulfillment_channels
        if next_token:
            params["NextToken"] = next_token
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

    def upload_feed_document(self, upload_url, content, content_type='text/xml; charset=UTF-8'):
        """Upload feed content to pre-signed S3 URL."""
        headers = {'Content-Type': content_type}
        response = requests.put(upload_url, data=content.encode('utf-8'), headers=headers, timeout=60)
        response.raise_for_status()

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
        self.upload_feed_document(upload_url, content, content_type=content_type)
        feed = self.create_feed(instance, access_token, feed_type, doc_id)
        return feed

    # ── Feed XML builders ──

    def build_price_feed_xml(self, items):
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
            lines.append('<StandardPrice currency="%s">%s</StandardPrice>' % (item.get('currency', 'USD'), item['price']))
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
                            "currency": item.get('currency', 'USD'),
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

        # Make the token-authenticated request but handle HTTP errors ourselves.
        try:
            resp = self._amazon_request(instance, access_token, method, url, params=params, body=body)
        except requests.exceptions.HTTPError as exc:
            # Try to extract JSON error from response body
            if exc.response is not None:
                try:
                    data = exc.response.json()
                    _logger.error("Amazon %s listing %s HTTP %s response: %s",
                                  method, sku, exc.response.status_code, data)
                    return data
                except Exception:
                    pass
            raise

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

    def get_inventory_summaries(self, instance, access_token, next_token=None):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/fba/inventory/v1/summaries"
        params = {
            "granularityType": "Marketplace",
            "granularityId": instance.marketplace_id,
            "marketplaceIds": instance.marketplace_id,
        }
        if next_token:
            params["nextToken"] = next_token
        resp = self._amazon_request(instance, access_token, 'GET', url, params=params)
        return resp.json()

    # ══════════════════════════════════════════════════
    # Fulfillment Inbound API (v2024)
    # ══════════════════════════════════════════════════

    def create_inbound_plan(self, instance, access_token, body):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans"
        resp = self._amazon_request(instance, access_token, 'POST', url, body=body)
        return resp.json()

    def get_inbound_plan(self, instance, access_token, plan_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

    def get_shipment(self, instance, access_token, plan_id, shipment_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}/shipments/{shipment_id}"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

    def get_shipment_items(self, instance, access_token, plan_id, shipment_id):
        endpoint = self._get_endpoint(instance)
        url = f"{endpoint}/inbound/fba/2024-03-20/inboundPlans/{plan_id}/shipments/{shipment_id}/items"
        resp = self._amazon_request(instance, access_token, 'GET', url)
        return resp.json()

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
