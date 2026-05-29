"""eBay Trading API client.

The Trading API is XML over HTTPS POST — not actual SOAP, despite eBay
calling it that historically. Request bodies use the eBay namespace
`urn:ebay:apis:eBLBaseComponents` as default; responses are in the same
namespace.

We use OAuth2 IAF tokens via the `X-EBAY-API-IAF-TOKEN` header. Legacy
Auth'n'Auth `<RequesterCredentials><eBayAuthToken>` flow is NOT supported
here — see DESIGN.md §3.

We build request envelopes with stdlib `xml.etree.ElementTree` (no `lxml`
dependency) and parse responses into nested dicts with namespace prefixes
stripped from tag names.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import httpx

from ebay_mcp.auth import get_user_token
from ebay_mcp.config import Config
from ebay_mcp.urls import urls_for_host

TRADING_NS = "urn:ebay:apis:eBLBaseComponents"
COMPATIBILITY_LEVEL = "1267"
SITE_ID_US = "0"


class TradingApiError(Exception):
    """Trading API returned Ack=Failure (or PartialFailure with hard errors).

    `errors` is the parsed Errors block — a dict or list of dicts depending
    on whether there were one or many errors, mirroring eBay's XML.
    """

    def __init__(self, message: str, *, errors: Any = None, call_name: str = ""):
        super().__init__(message)
        self.errors = errors
        self.call_name = call_name


def _strip_ns(tag: str) -> str:
    """Strip the `{namespace}` prefix from an ElementTree tag."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _populate(parent: ET.Element, value: Any) -> None:
    """Recursively populate XML children from a nested Python value.

    - dict → one child per key
    - dict with `_value` key → element text + XML attributes from other keys
      (mirrors `parse_response_xml`'s output shape for attribute-bearing
      elements like `<MaxBid currencyID="USD">25.00</MaxBid>`, so request
      builders and response parsers share one in-memory representation)
    - list → repeated child elements (caller must pre-element them)
    - scalar (str/int/float/bool) → element text
    """
    if isinstance(value, dict):
        if "_value" in value:
            parent.text = str(value["_value"])
            for k, v in value.items():
                if k == "_value":
                    continue
                parent.set(k, str(v))
            return
        for k, v in value.items():
            child = ET.SubElement(parent, k)
            _populate(child, v)
    elif isinstance(value, list):
        # A list at this level repeats the PARENT element. We can't actually
        # do that with ElementTree mid-tree; callers building list-of-elements
        # must wrap with a structured parent. Raise to catch misuse.
        raise ValueError(
            "lists must be wrapped in a parent dict key; raw list "
            "cannot be populated directly into an element"
        )
    elif isinstance(value, bool):
        # XML wants "true"/"false" lowercase, not Python's "True"/"False".
        parent.text = "true" if value else "false"
    else:
        parent.text = str(value)


def build_request_xml(call_name: str, payload: dict[str, Any]) -> str:
    """Build the Trading API XML request body."""
    root = ET.Element(f"{call_name}Request", {"xmlns": TRADING_NS})
    _populate(root, payload)
    return '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode")


def parse_response_xml(content: bytes) -> dict[str, Any]:
    """Parse a Trading API XML response into a nested dict."""
    root = ET.fromstring(content)
    return _element_to_dict(root)


def _element_to_dict(element: ET.Element) -> Any:
    """Recursively convert an XML element into a Python dict / scalar.

    Empty elements with attributes → {"_value": text, **attrib}
    Empty elements without attributes → text string (or "")
    Elements with children → dict; repeated child tags collapse into a list
    """
    children = list(element)
    if not children:
        text = element.text or ""
        if element.attrib:
            return {"_value": text, **element.attrib}
        return text

    result: dict[str, Any] = {}
    if element.attrib:
        result.update(element.attrib)

    for child in children:
        child_tag = _strip_ns(child.tag)
        child_value = _element_to_dict(child)
        if child_tag in result:
            if not isinstance(result[child_tag], list):
                result[child_tag] = [result[child_tag]]
            result[child_tag].append(child_value)
        else:
            result[child_tag] = child_value
    return result


def trading_call(
    config: Config,
    host: str,
    call_name: str,
    payload: dict[str, Any] | None = None,
    *,
    client: httpx.Client | None = None,
    site_id: str = SITE_ID_US,
) -> dict[str, Any]:
    """Issue a Trading API call. Auto-resolves the user token.

    Args:
        config: loaded Config
        host: "sandbox" or "production"
        call_name: eBay Trading call (e.g. "GetMyeBayBuying", "AddToWatchList")
        payload: request body fields as a nested dict. Empty dict for calls
            with no required parameters.
        client: optional httpx.Client for testing
        site_id: eBay site code; "0" = US (default), "3" = UK, etc.

    Returns:
        The parsed response body (the contents of `<{CallName}Response>`).

    Raises:
        UserNotAuthenticated: no user token cached for this host.
        httpx.HTTPStatusError: HTTP-level failure.
        TradingApiError: eBay returned Ack=Failure. `.errors` has details.
    """
    token = get_user_token(config, host, client=client)
    host_cfg = config.hosts[host]
    cert_id = config.resolve_cert_id(host)
    urls = urls_for_host(host)

    headers = {
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
        "X-EBAY-API-SITEID": site_id,
        "X-EBAY-API-DEV-NAME": host_cfg.dev_id,
        "X-EBAY-API-APP-NAME": host_cfg.app_id,
        "X-EBAY-API-CERT-NAME": cert_id,
        "X-EBAY-API-IAF-TOKEN": token,
        "Content-Type": "text/xml; charset=utf-8",
    }
    body = build_request_xml(call_name, payload or {})

    if client is not None:
        response = client.post(urls["trading"], headers=headers, content=body, timeout=60)
    else:
        with httpx.Client() as c:
            response = c.post(urls["trading"], headers=headers, content=body, timeout=60)
    response.raise_for_status()

    parsed = parse_response_xml(response.content)
    ack = parsed.get("Ack", "")
    if ack == "Failure":
        raise TradingApiError(
            f"Trading API call {call_name} failed (Ack=Failure)",
            errors=parsed.get("Errors"),
            call_name=call_name,
        )
    return parsed
