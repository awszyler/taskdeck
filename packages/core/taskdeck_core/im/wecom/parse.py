from __future__ import annotations

import xml.etree.ElementTree as ET

from .crypto import WecomCryptoError


def parse_outer_encrypt(xml_body: str) -> str:
    """From a WeCom POST body, pull out the <Encrypt>...</Encrypt> content."""
    root = ET.fromstring(xml_body)
    encrypt = root.findtext("Encrypt")
    if encrypt is None:
        raise WecomCryptoError("missing <Encrypt>")
    return encrypt


def parse_inner_message(xml_body: str) -> dict[str, str]:
    """After decrypting the inner XML, return the message fields as a dict."""
    root = ET.fromstring(xml_body)
    out: dict[str, str] = {}
    for child in root:
        if child.tag and child.text is not None:
            out[child.tag] = child.text
    return out
