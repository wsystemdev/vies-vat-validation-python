# vies-vat-validation-python

Validate EU VAT numbers against the official **VIES** service from Python, with **no dependencies**, proper retries for `MS_UNAVAILABLE` / `TIMEOUT`, CSV batch mode and VIES consultation numbers. One file you can copy into your project: [`vies_check.py`](vies_check.py).

```bash
python vies_check.py IE6388047V DE811569869
# IE6388047V         valid        GOOGLE IRELAND LIMITED
# DE811569869        valid
```

## Why not just call VIES?

You can, and this script does exactly that (the European Commission's REST API at `https://ec.europa.eu/taxation_customs/vies/rest-api`). It just handles the parts that usually go wrong:

| Problem | What the script does |
|---|---|
| Users type `de 811 569 869`, `GR...` | Normalises spaces, dots and dashes, maps `GR` to `EL` (VIES uses `EL` for Greece) |
| Typos waste requests | Checks the national format first (27 member states + `XI`), returns `bad_format` without a network call |
| `MS_UNAVAILABLE`, `TIMEOUT`, `MS_MAX_CONCURRENT_REQ` | Retries with exponential backoff and jitter |
| A member-state system is down for hours | Returns `status: "unavailable"` and `valid: null`, **never** `invalid`. Rejecting a real customer because their country's system was offline is the most common VIES bug |
| You need proof that you checked | `--requester <your VAT number>` returns the VIES consultation number (`requestIdentifier`) |
| Many numbers | `--csv in.csv --out results.csv` (uses the `vat` column or the first column), with a polite delay between calls |

## Usage

Python 3.9+ only, nothing to install.

```bash
# single numbers
python vies_check.py IE6388047V "de 811 569 869"

# JSON lines, with a consultation number for your records
python vies_check.py --json --requester HU33119856 IE6388047V

# batch
python vies_check.py --csv customers.csv --out results.csv

# which member states are up right now (official check-status endpoint)
python vies_check.py --status
```

Exit code is `0` when every number got a definite answer (`valid`, `invalid` or `bad_format`) and `2` if any were `unavailable` or `error`, so you can re-run those later from cron.

From code:

```python
from vies_check import check_vies

r = check_vies("IE6388047V", requester="HU33119856")
if r["status"] == "valid":
    ...  # apply reverse charge, store r["consultation_number"]
elif r["status"] == "unavailable":
    ...  # VIES down for this country: queue a re-check, don't reject the customer
```

Result fields: `status` (`valid` | `invalid` | `bad_format` | `unavailable` | `error`), `valid` (`true`/`false`/`null`), `name`, `address` (many countries return none), `consultation_number`, `request_date`, `source`, `error`.

## VIES error codes

| Code | Meaning | Retry? |
|---|---|---|
| `INVALID_INPUT` | Country code or number malformed | No |
| `INVALID_REQUESTER_INFO` | Your requester VAT number is wrong | No |
| `MS_UNAVAILABLE` | That member state's system is down | Yes, later |
| `TIMEOUT` | Member state did not answer in time | Yes |
| `MS_MAX_CONCURRENT_REQ` / `GLOBAL_MAX_CONCURRENT_REQ` | Too many requests right now | Yes, with backoff |
| `SERVICE_UNAVAILABLE` | VIES itself is down | Yes, later |
| `VAT_BLOCKED` / `IP_BLOCKED` | Blocked by VIES | No |

## Optional: fallback when VIES stays down

*Disclosure: I'm the maker of the W System API (W System), which this optional flag calls. Everything above works without it.*

When a member state stays unavailable after the retries, `--fallback wsystem` asks the [W System EU VAT validation API](https://wsystem.dev/eu-vat-number-validation-api), which retries VIES and, for some countries, falls back to the official national register (PL Biała lista, CZ ARES, FI YTJ, FR SIRENE) or the last successful VIES result. Fallback answers are flagged `fallback: true` with their `source` and carry no consultation number. See [VIES is down: how to handle MS_UNAVAILABLE](https://wsystem.dev/vies-down-fallback).

```bash
python vies_check.py --fallback wsystem CZ12345678
```

Pricing: 20 free calls per day per IP with no API key; beyond that $0.01 per check via x402 (USDC on Base) or a monthly plan with an API key (set `WSYSTEM_API_KEY`). The fallback is only called for numbers that came back `unavailable`.

The same checks are available as a remote MCP server for AI agents: [EU business data MCP server](https://wsystem.dev/eu-business-data-mcp-server) (source: [wsystemdev/eu-business-data-mcp](https://github.com/wsystemdev/eu-business-data-mcp)).

## Tests

```bash
python -m unittest -v   # offline, mocks the network
```

## Notes

- VIES says whether a number is registered for intra-EU trade on the day you check. It is not legal or tax advice, and this project is not affiliated with the European Commission.
- Please keep the delay between calls; VIES rate-limits aggressive clients.

MIT licensed.
