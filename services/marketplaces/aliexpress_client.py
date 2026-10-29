"""HTTP client for the AliExpress Affiliate API (TOP gateway protocol).

Every detail here comes from the official AliExpress/TOP documentation and
from live verification against the Affiliate Open Platform:

- gateway: the working HTTPS endpoint for this app key is the Singapore
  Affiliate gateway ``https://api-sg.aliexpress.com/sync``. The older TOP
  formal-environment URL ``https://eco.taobao.com/router/rest`` rejects the
  same key with ``isv.appkey-not-exists`` (code 29). Callers may still
  override ``gateway`` for tests or regional routing;
- public parameters: ``method``, ``app_key``, ``timestamp``, ``format``,
  ``v``, ``sign_method``, ``sign``;
- ``timestamp`` format is ``yyyy-MM-dd HH:mm:ss`` in the GMT+8 time zone,
  and the server tolerates at most 10 minutes of clock skew;
- ``sign_method`` accepts ``md5`` or ``hmac``:
  ``md5``   -> ``MD5(app_secret + sorted(k+v...) + app_secret)``
  ``hmac``  -> ``HMAC_MD5(sorted(k+v...), app_secret)``
  Both results are upper-case hex;
- responses are JSON objects wrapped in a ``<method>_response`` key;
  business failures come back as ``{"error_response": {"code": ...,
  "msg": ..., "sub_code": ..., "sub_msg": ...}}``.

Nothing here is guessed: no undocumented parameter is sent, no response
shape is fabricated, and errors carry only what the API itself returned
(never the app secret or the computed signature).
"""


from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from config.settings import MarketplaceCredentials

logger = logging.getLogger(__name__)

#: Singapore Affiliate HTTPS gateway. Verified to accept the project's
#: app key and return real ``product.query`` payloads with promotion links.
#: Override via ``AliExpressClient(gateway=...)`` when needed.
GATEWAY_URL = "https://api-sg.aliexpress.com/sync"

#: Legacy TOP formal-environment gateway. Kept as a named constant so tests
#: and diagnostics can still target it explicitly; it is NOT the default.
LEGACY_TOP_GATEWAY_URL = "https://eco.taobao.com/router/rest"


SIGN_METHOD_MD5 = "md5"
SIGN_METHOD_HMAC = "hmac"

API_VERSION = "2.0"
RESPONSE_FORMAT = "json"

#: TOP timestamps are GMT+8 (the docs say Asia/Shanghai time explicitly).
GMT_PLUS_8 = timezone(timedelta(hours=8))
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# -- documented method names -------------------------------------------------

METHOD_PRODUCT_QUERY = "aliexpress.affiliate.product.query"
METHOD_HOT_PRODUCT_QUERY = "aliexpress.affiliate.hotproduct.query"
METHOD_PRODUCT_DETAIL = "aliexpress.affiliate.productdetail.get"
METHOD_CATEGORY_GET = "aliexpress.affiliate.category.get"
METHOD_IMAGE_SEARCH = "aliexpress.affiliate.image.search"
METHOD_LINK_GENERATE = "aliexpress.affiliate.link.generate"

#: Retry policy (documented temporary conditions only): timeouts,
#: connection/transport errors and 5xx gateway answers are retried a
#: bounded number of times with a short backoff. Rate-limit answers
#: (HTTP 429), business errors (``error_response``, non-200
#: ``resp_code``) and malformed payloads are NEVER retried - a quota or
#: signature problem must stay visible in the admin diagnostics.
TRANSIENT_HTTP_STATUSES = frozenset({500, 502, 503, 504})
DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_BACKOFF = 1.0


#: ``target_currency`` values the affiliate API documents. Anything else is
#: NOT forwarded - the bot never invents a currency code the API cannot
#: price in. The API then answers in its own default currency.
DOCUMENTED_TARGET_CURRENCIES = frozenset(
    {
        "USD", "GBP", "CAD", "EUR", "UAH", "MXN", "TRY", "RUB",
        "BRL", "AUD", "INR", "JPY", "IDR", "SEK", "KRW",
    }
)

#: ``target_language`` values the affiliate API documents.
DOCUMENTED_TARGET_LANGUAGES = frozenset(
    {
        "EN", "RU", "PT", "ES", "FR", "ID", "IT", "TH", "JA", "AR", "VI",
        "TR", "DE", "HE", "KO", "NL", "PL", "MX", "CL", "IW", "IN",
    }
)


#: Business ``resp_code`` values that mean "the query matched nothing".
#: Verified live against the affiliate API: a ``product.query`` whose
#: keywords have no answer in the full-text index returns
#: ``resp_code=405`` with ``resp_msg="The result is empty"`` - the request
#: itself was perfectly valid. It must therefore NOT be treated as an
#: infrastructure failure: the user gets the ordinary "nothing found"
#: screen and ``/admin`` must not count it as an API error.
EMPTY_RESULT_RESP_CODE = "405"

#: The message that must accompany :data:`EMPTY_RESULT_RESP_CODE` before a
#: response is accepted as a genuinely empty result. A 405 with any other
#: message stays an error - a code alone is never trusted.
EMPTY_RESULT_MARKERS = ("result is empty", "the result is empty")


def is_empty_result(resp_code: Any, resp_msg: Any) -> bool:
    """Whether a non-200 ``resp_code`` is the documented empty result.

    Only the exact combination the API was observed to send qualifies:
    the empty-result code AND a message that says the result is empty.
    Everything else is a real business/API error and must stay visible.
    """

    if resp_code is None or str(resp_code) != EMPTY_RESULT_RESP_CODE:
        return False
    message = str(resp_msg or "").strip().lower()
    return any(marker in message for marker in EMPTY_RESULT_MARKERS)


class AliExpressApiError(RuntimeError):
    """Raised when the API answers with an error instead of data.

    The message contains only what the API reported (HTTP status or the
    ``code``/``sub_code``/``sub_msg`` triple). Secrets and the computed
    signature are never part of it.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        sub_code: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.sub_code = sub_code
        self.status = status


def timestamp_now(moment: datetime | None = None) -> str:
    """Current TOP timestamp (``yyyy-MM-dd HH:mm:ss`` in GMT+8)."""

    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(GMT_PLUS_8).strftime(TIMESTAMP_FORMAT)


def sign_request(
    params: dict[str, Any],
    app_secret: str,
    sign_method: str = SIGN_METHOD_MD5,
) -> str:
    """Sign request parameters exactly as the TOP specification describes.

    Steps (same order as the official reference implementation):
    1. take every parameter except ``sign`` itself, skip empty values and
       sort the rest by name;
    2. concatenate ``name + value`` pairs without separators;
    3. ``md5``  - wrap that string with the app secret on both sides and
       hash it with MD5; ``hmac`` - hash the string with HMAC-MD5 using
       the app secret as the key;
    4. return the digest as upper-case hex.
    """

    items = sorted(
        (name, value)
        for name, value in params.items()
        if name != "sign" and value is not None and str(value) != ""
    )
    joined = "".join(f"{name}{value}" for name, value in items)
    encoded = joined.encode("utf-8")

    if sign_method == SIGN_METHOD_HMAC:
        digest = hmac.new(
            app_secret.encode("utf-8"), encoded, hashlib.md5
        ).hexdigest()
    else:
        payload = app_secret.encode("utf-8") + encoded + app_secret.encode("utf-8")
        digest = hashlib.md5(payload).hexdigest()

    return digest.upper()


def describe_error(payload: dict[str, Any]) -> str | None:
    """Return a readable reason when the payload is an error response."""

    error = payload.get("error_response")
    if not isinstance(error, dict):
        return None

    bits = [str(error.get("sub_msg") or error.get("msg") or "unknown error")]
    if error.get("code") is not None:
        bits.append(f"code={error['code']}")
    if error.get("sub_code"):
        bits.append(f"sub_code={error['sub_code']}")
    return "; ".join(bits)


def unwrap_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the business part of a successful TOP response.

    TOP wraps every answer in a single ``*_response`` key; inside it the
    affiliate APIs expose ``resp_result`` with ``resp_code``/``resp_msg``
    and the actual ``result``. Both shapes are handled, so callers only
    deal with the business payload.
    """

    node: Any = payload
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.endswith("_response") and isinstance(value, dict):
                node = value
                break

    if isinstance(node, dict) and isinstance(node.get("resp_result"), dict):
        return node["resp_result"]
    if isinstance(node, dict):
        return node
    return {}


class AliExpressClient:
    """Thin async wrapper around the documented affiliate methods."""

    def __init__(
        self,
        credentials: MarketplaceCredentials,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        gateway: str = GATEWAY_URL,
        sign_method: str = SIGN_METHOD_MD5,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self.gateway = gateway
        self.sign_method = sign_method
        #: Bounded retry of TRANSIENT failures only (see module comment).
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)
        self._external_client = http_client is not None
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout)
        )
        #: Safe call diagnostics for the admin "AliExpress API" screen
        #: (§49): counters and error CLASS names only - never parameters,
        #: signatures or credentials.
        self.calls_ok = 0
        self.calls_err = 0
        #: Transient retries actually spent (shown in /admin so a flapping
        #: gateway is never silently absorbed).
        self.retries_spent = 0
        self.last_ok_at: str | None = None
        #: Error CLASS of the LATEST call only. Cleared by every successful
        #: call (including an empty result), so diagnostics can never keep
        #: showing a failure that a later request already recovered from.
        self.last_error_class: str | None = None
        #: Successful-but-empty responses (``resp_code=405``): a real answer
        #: to a real query, tracked separately so they are visible in
        #: diagnostics without being counted as failures.
        self.empty_results = 0
        self.last_empty_at: str | None = None

    @property
    def is_configured(self) -> bool:
        return self.credentials.is_configured

    async def aclose(self) -> None:
        if not self._external_client:
            await self._http_client.aclose()

    def build_params(self, method: str, business: dict[str, Any]) -> dict[str, Any]:
        """Assemble the full signed parameter set for one call.

        Kept public so tests can verify the signature deterministically
        without touching the network.
        """

        if not self.is_configured:  # pragma: no cover - guarded by callers
            raise AliExpressApiError("AliExpress credentials are not configured")

        params: dict[str, Any] = {
            "method": method,
            "app_key": self.credentials.app_key,
            "timestamp": timestamp_now(),
            "format": RESPONSE_FORMAT,
            "v": API_VERSION,
            "sign_method": self.sign_method,
        }

        for name, value in business.items():
            if value is None or str(value) == "":
                continue
            params[name] = value

        params["sign"] = sign_request(
            params, self.credentials.app_secret or "", self.sign_method
        )
        return params

    async def _retry(self, error_class: str, attempt: int) -> None:
        """Spend one bounded retry on a documented transient failure.

        Increments ``retries_spent`` (surfaced in /admin) and waits the
        configured backoff before the next attempt. Never raises.
        """

        self.retries_spent += 1
        logger.warning(
            "AliExpress transient failure %s on attempt %d, retrying (%d left)",
            error_class,
            attempt + 1,
            self.max_retries - attempt,
        )
        if self.retry_backoff:
            await asyncio.sleep(self.retry_backoff)

    async def call(self, method: str, business: dict[str, Any] | None = None) -> dict:
        """Perform one documented API call and return its business payload.

        Raises :class:`AliExpressApiError` for HTTP failures, malformed
        payloads and API-reported errors. Missing credentials are checked
        by callers through ``is_configured`` (see
        ``services/marketplaces/base.py``); the parameters themselves are
        never logged, so the signature cannot leak.
        """

        params = self.build_params(method, business or {})
        logger.debug("AliExpress call %s via %s", method, self.gateway)

        response: httpx.Response
        for attempt in range(1 + self.max_retries):
            try:
                response = await self._http_client.post(
                    self.gateway,
                    data=params,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8"
                    },
                )
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    await self._retry("Timeout", attempt)
                    continue
                self.calls_err += 1
                self.last_error_class = "Timeout"
                raise AliExpressApiError(
                    f"AliExpress request timed out after {self.timeout:.0f}s"
                ) from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in TRANSIENT_HTTP_STATUSES and attempt < self.max_retries:
                    await self._retry(f"HTTP {status}", attempt)
                    continue
                self.calls_err += 1
                self.last_error_class = f"HTTP {status}"
                raise AliExpressApiError(
                    f"AliExpress returned HTTP {status}",
                    status=status,
                ) from exc
            except httpx.HTTPError as exc:
                # Transport-level failures (connect/read/write) are
                # transient; anything else is never retried.
                if (
                    isinstance(exc, httpx.TransportError)
                    and attempt < self.max_retries
                ):
                    await self._retry(type(exc).__name__, attempt)
                    continue
                self.calls_err += 1
                self.last_error_class = type(exc).__name__
                raise AliExpressApiError(
                    f"AliExpress connection error: {type(exc).__name__}"
                ) from exc
            break

        try:
            payload = response.json()
        except ValueError as exc:
            # A gateway HTML page instead of JSON is a real technical failure
            # and must reach the diagnostics counters, not just the logs.
            self.calls_err += 1
            self.last_error_class = "NonJsonResponse"
            raise AliExpressApiError("AliExpress returned a non-JSON response") from exc

        if not isinstance(payload, dict):
            self.calls_err += 1
            self.last_error_class = "UnexpectedPayload"
            raise AliExpressApiError("AliExpress returned an unexpected payload")

        reason = describe_error(payload)
        if reason is not None:
            # `error_response` is the TOP envelope for signature/auth/business
            # rejections. It is never a result, so it counts as a failure and
            # keeps its class visible in /admin.
            error = payload["error_response"]
            code = error.get("code")
            self.calls_err += 1
            self.last_error_class = f"error_response code={code}"
            raise AliExpressApiError(
                f"AliExpress error: {reason}",
                code=str(code) if code is not None else None,
                sub_code=error.get("sub_code"),
            )

        result = unwrap_response(payload)
        resp_code = result.get("resp_code")
        if resp_code is not None and str(resp_code) != "200":
            # "The result is empty" (resp_code=405) is a documented answer
            # to a perfectly valid query: no failure, just nothing found.
            # It is recorded separately and reported as an ordinary empty
            # result, so `/admin` error counters only ever show real
            # failures. Any other non-200 code stays a business error.
            if is_empty_result(resp_code, result.get("resp_msg")):
                self.empty_results += 1
                self.last_empty_at = timestamp_now()
                self.calls_ok += 1
                self.last_ok_at = self.last_empty_at
                self.last_error_class = None
                logger.info(
                    "AliExpress %s returned an empty result (resp_code=%s)",
                    method,
                    resp_code,
                )
                return {}

            self.calls_err += 1
            self.last_error_class = f"resp_code={resp_code}"
            raise AliExpressApiError(
                f"AliExpress error: {result.get('resp_msg') or 'unknown'} "
                f"(resp_code={resp_code})",
                code=str(resp_code),
            )

        # A successful call clears the previous failure state: diagnostics
        # must describe the LATEST call, not the worst one of the day.
        self.calls_ok += 1
        self.last_ok_at = timestamp_now()
        self.last_error_class = None
        return result