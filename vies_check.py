#!/usr/bin/env python3
"""Validate EU VAT numbers against the official VIES REST API.

Zero dependencies (Python 3.8+ standard library only).

- Normalises input (spaces, dots, dashes, "GR" -> "EL") and checks the
  national format before calling VIES, so typos never cost a request.
- Calls the European Commission's VIES REST API.
- Retries the temporary VIES errors (MS_UNAVAILABLE, TIMEOUT,
  MS_MAX_CONCURRENT_REQ, ...) with exponential backoff and jitter.
- Never reports "invalid" when VIES was simply unavailable: the result is
  status="unavailable" so you can decide what to do (queue, accept
  provisionally, or use a fallback source).
- Optional: pass your own VAT number as requester to get a VIES
  consultation number (proof that you checked).
- Optional: --fallback wsystem asks the W System API when VIES stays down
  for a member state (see README for what that does and costs).

Usage:
    python vies_check.py IE6388047V DE811569869
    python vies_check.py --requester HU33119856 IE6388047V
    python vies_check.py --csv numbers.csv --out results.csv
    python vies_check.py --status
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VIES_BASE = "https://ec.europa.eu/taxation_customs/vies/rest-api"
WSYSTEM_VERIFY = "https://api.wsystem.dev/verify"
USER_AGENT = "vies-vat-validation-python/1.0 (+https://github.com/wsystemdev/vies-vat-validation-python)"

# National VAT number formats (without the 2-letter prefix).
FORMATS = {
    "AT": r"U\d{8}",
    "BE": r"[01]\d{9}",
    "BG": r"\d{9,10}",
    "CY": r"\d{8}[A-Z]",
    "CZ": r"\d{8,10}",
    "DE": r"\d{9}",
    "DK": r"\d{8}",
    "EE": r"\d{9}",
    "EL": r"\d{9}",
    "ES": r"[A-Z0-9]\d{7}[A-Z0-9]",
    "FI": r"\d{8}",
    "FR": r"[A-HJ-NP-Z0-9]{2}\d{9}",
    "HR": r"\d{11}",
    "HU": r"\d{8}",
    "IE": r"\d{7}[A-W][A-I]?|\d[A-Z+*]\d{5}[A-W]",
    "IT": r"\d{11}",
    "LT": r"\d{9}|\d{12}",
    "LU": r"\d{8}",
    "LV": r"\d{11}",
    "MT": r"\d{8}",
    "NL": r"\d{9}B\d{2}",
    "PL": r"\d{10}",
    "PT": r"\d{9}",
    "RO": r"\d{2,10}",
    "SE": r"\d{12}",
    "SI": r"\d{8}",
    "SK": r"\d{10}",
    "XI": r"\d{9}|\d{12}|GD\d{3}|HA\d{3}",
}

# VIES errors that are temporary: retry, and never treat as "invalid".
RETRYABLE = {
    "MS_UNAVAILABLE",
    "TIMEOUT",
    "SERVICE_UNAVAILABLE",
    "MS_MAX_CONCURRENT_REQ",
    "MS_MAX_CONCURRENT_REQ_TIME",
    "GLOBAL_MAX_CONCURRENT_REQ",
    "GLOBAL_MAX_CONCURRENT_REQ_TIME",
}


def normalize(raw: str) -> tuple[str, str]:
    """Return (country_code, number) from input like 'de 811 569 869'."""
    s = re.sub(r"[\s.\-/]", "", raw or "").upper()
    if len(s) < 3 or not s[:2].isalpha():
        raise ValueError(f"{raw!r}: missing 2-letter country prefix")
    cc, num = s[:2], s[2:]
    if cc == "GR":
        cc = "EL"  # VIES uses EL for Greece
    return cc, num


def format_ok(cc: str, num: str) -> bool:
    pattern = FORMATS.get(cc)
    return bool(pattern and re.fullmatch(pattern, num))


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get_json(url: str, timeout: float, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except ValueError:
            body = {}
        return e.code, body


def check_vies(raw: str, requester: str | None = None, retries: int = 4, timeout: float = 20.0,
               base_delay: float = 1.0) -> dict:
    """Check one VAT number in VIES. Returns a dict with a 'status' of
    valid | invalid | bad_format | unavailable | error."""
    try:
        cc, num = normalize(raw)
    except ValueError as e:
        return {"input": raw, "status": "bad_format", "valid": False, "error": str(e)}
    out = {"input": raw, "vat": cc + num, "country_code": cc, "vat_number": num}
    if not format_ok(cc, num):
        known = cc in FORMATS
        out.update(status="bad_format", valid=False,
                   error="number does not match the national format" if known else f"{cc} is not a VIES country")
        return out

    payload = {"countryCode": cc, "vatNumber": num}
    if requester:
        rcc, rnum = normalize(requester)
        payload.update(requesterMemberStateCode=rcc, requesterNumber=rnum)

    last_error = None
    for attempt in range(retries + 1):
        try:
            data = _post_json(f"{VIES_BASE}/check-vat-number", payload, timeout)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            data, last_error = None, f"network: {e}"
        if data is not None:
            if data.get("actionSucceed") is False:
                codes = [w.get("error") for w in data.get("errorWrappers", [])]
                last_error = ",".join(c for c in codes if c) or "UNKNOWN"
                if not set(codes) & RETRYABLE:
                    out.update(status="error", valid=None, error=last_error)
                    return out
            else:
                valid = bool(data.get("valid"))
                out.update(
                    status="valid" if valid else "invalid",
                    valid=valid,
                    name=None if data.get("name") in (None, "---") else data.get("name"),
                    address=None if data.get("address") in (None, "---") else data.get("address"),
                    request_date=data.get("requestDate"),
                    consultation_number=data.get("requestIdentifier") or None,
                    source="VIES (European Commission)",
                )
                return out
        if attempt < retries:
            time.sleep(base_delay * (2 ** attempt) + random.uniform(0, base_delay))

    # Still failing: this is NOT an invalid number.
    out.update(status="unavailable", valid=None, error=last_error)
    return out


def check_wsystem(vat: str, timeout: float = 30.0) -> dict:
    """Ask the W System API (VIES with national-register fallback).
    Free: 20 calls/day per IP without a key. Set WSYSTEM_API_KEY for a plan."""
    headers = {}
    if os.environ.get("WSYSTEM_API_KEY"):
        headers["X-API-Key"] = os.environ["WSYSTEM_API_KEY"]
    code, body = _get_json(f"{WSYSTEM_VERIFY}?vat={urllib.parse.quote(vat)}", timeout, headers)
    if code == 402:
        return {"status": "error", "valid": None,
                "error": "W System free tier used up for today (HTTP 402: pay per call via x402 or use an API key)"}
    if code != 200:
        return {"status": "error", "valid": None, "error": f"W System API HTTP {code}"}
    return body


def check(raw: str, requester: str | None = None, fallback: str | None = None, **kw) -> dict:
    res = check_vies(raw, requester=requester, **kw)
    if res["status"] == "unavailable" and fallback == "wsystem":
        fb = check_wsystem(res["vat"])
        res["fallback_result"] = fb
    return res


def vies_status(timeout: float = 20.0) -> list[dict]:
    """Per-member-state availability from the official VIES check-status endpoint."""
    code, body = _get_json(f"{VIES_BASE}/check-status", timeout)
    return body.get("countries", []) if code == 200 else []


FIELDS = ["input", "vat", "status", "valid", "name", "address", "consultation_number", "request_date", "error"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Validate EU VAT numbers against the official VIES REST API.")
    p.add_argument("vat", nargs="*", help="VAT numbers with country prefix, e.g. IE6388047V")
    p.add_argument("--csv", help="input CSV; uses the 'vat' column, or the first column")
    p.add_argument("--out", help="write results as CSV to this file")
    p.add_argument("--json", action="store_true", help="print JSON lines")
    p.add_argument("--requester", help="your own VAT number, to get a VIES consultation number")
    p.add_argument("--retries", type=int, default=4)
    p.add_argument("--delay", type=float, default=0.3, help="seconds between numbers (be nice to VIES)")
    p.add_argument("--fallback", choices=["wsystem"], help="ask the W System API when VIES stays unavailable")
    p.add_argument("--status", action="store_true", help="show VIES availability per member state and exit")
    a = p.parse_args(argv)

    if a.status:
        for c in vies_status():
            print(f"{c.get('countryCode')}  {c.get('availability')}")
        return 0

    numbers = list(a.vat)
    if a.csv:
        with open(a.csv, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if rows:
            header = [h.strip().lower() for h in rows[0]]
            col = header.index("vat") if "vat" in header else 0
            body = rows[1:] if "vat" in header else rows
            numbers += [r[col] for r in body if len(r) > col and r[col].strip()]
    if not numbers:
        p.error("give VAT numbers or --csv")

    results = []
    for i, n in enumerate(numbers):
        if i:
            time.sleep(a.delay)
        r = check(n, requester=a.requester, fallback=a.fallback, retries=a.retries)
        results.append(r)
        if a.json:
            print(json.dumps(r, ensure_ascii=False))
        else:
            extra = r.get("name") or r.get("error") or ""
            print(f"{r.get('vat', n):<18} {r['status']:<12} {extra}")
            if "fallback_result" in r:
                fb = r["fallback_result"]
                print(f"{'':<18} fallback -> {fb.get('status')} (source: {fb.get('source', fb.get('error', '?'))})")

    if a.out:
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(results)
    return 0 if all(r["status"] in ("valid", "invalid", "bad_format") for r in results) else 2


if __name__ == "__main__":
    sys.exit(main())
