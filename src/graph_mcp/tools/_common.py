from __future__ import annotations

import html
import re
from typing import Any

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n{3,}")


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = _WS.sub(" ", s)
    return _NL.sub("\n\n", s).strip()


def addr(a: dict[str, Any] | None) -> str:
    if not a:
        return ""
    e = a.get("emailAddress", a)
    name, address = e.get("name"), e.get("address")
    return f"{name} <{address}>" if name and name != address else (address or "")


def addrs(xs: list[dict[str, Any]] | None) -> list[str]:
    return [addr(x) for x in xs or []]


def recipients(emails: list[str] | None) -> list[dict[str, Any]]:
    out = []
    for e in emails or []:
        m = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", e)
        if m:
            out.append({"emailAddress": {"name": m.group(1).strip('" '), "address": m.group(2)}})
        else:
            out.append({"emailAddress": {"address": e.strip()}})
    return out


def compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop None/empty values so tool output stays small."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}
