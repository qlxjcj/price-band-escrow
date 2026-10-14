# Price Band Escrow

**Escrow that pays the seller only at a verified market price.**

A buyer escrows funds and agrees a price band. At settlement the contract pulls
the live quote from every allowed provider, validates each one's schema, unit
and freshness, refuses the set if the providers disagree beyond the agreed
tolerance, and pays out at the median. Out of band, unverifiable, or
underfunded — the whole escrow goes home.

Deployed on GenLayer Bradbury: **`0x4ab3869c5D3C1C77d92Df8a352F9DEfde273cBDE`**
([explorer](https://explorer-bradbury.genlayer.com/address/0x4ab3869c5D3C1C77d92Df8a352F9DEfde273cBDE))

---

## The problem with "the price is fine"

Anyone can claim a number is the market price. Getting it *right* means naming
the asset the way each venue names it, in the unit you asked for, at a time
that is still fresh, from more than one place — and then deciding what to do
when those places disagree. Every one of those is where a naive integration
quietly quotes the wrong thing.

## Real providers, real adapters

Three venues, three identifiers, three payload shapes, three freshness formats:

| provider | asset id | price field | freshness | shape |
|---|---|---|---|---|
| CoinGecko | `bitcoin` | `bitcoin.usd` | `last_updated_at` (unix s) | nested **number** |
| Bitstamp | `btcusd` | `last` | `timestamp` (unix s) | flat **strings** |
| CoinPaprika | `btc-bitcoin` | `quotes.USD.price` | `last_updated` (**ISO**) | nested number |

A generic URL that forgets these differences is exactly how a quote for the
wrong asset gets accepted as real data. Each adapter builds its own endpoint
from the trade's asset and unit, then validates the response it gets back.

**Nothing is trusted because it is non-empty.** A payload must parse as JSON,
carry the price in the asked unit, name the expected asset, and report a
timestamp that is neither stale nor in the future. A 500 page, a payload with
no freshness, a quote for another asset — all refused, with a recorded reason.

**No floats touch the price.** A decimal literal is converted to an integer at
`1e8` scale by string arithmetic. A float in that path rounds differently
across validators and silently breaks consensus on the stored price.

## What the model does

One narrow question per settlement: *do these provider responses all describe
the same asset?* The model returns the short label each response uses, and
**code then checks each label literally occurs in that response**. A model can
therefore only lose credit, never create it. `unclear` is not a yes.

Everything after that is arithmetic: median, spread, band comparison.

## Consensus

Fetching, parsing and the model call share **one equivalence-principle block**,
so a settlement has a single consensus point and validators agree on the
decision fields — `verdict`, `valid`, `reference`, `same` and which providers
were accepted — rather than on the wording of a leader's answer.

## Money

`cost = qty * reference / 1e8`. Then:

| outcome | seller | buyer |
|---|---|---|
| verified, in band, funded | `cost` | `escrow - cost` (the change) |
| verified but out of band | — | the whole escrow |
| providers disagree, or unverifiable | — | the whole escrow |
| escrow does not cover the cost | — | the whole escrow |
| cancelled before the window opens | — | the whole escrow |
| deadline passes with no settlement | — | the whole escrow |

`seller_paid + buyer_refund == escrow` on every path. There is no path on which
a unit of the escrow can go missing.

## App

`index.html` is a working app: open a trade, browse trades, inspect one and
settle, cancel or expire it.

* **Results are bound to a trade id.** Every read is `get_trade(id)` or
  `get_quote(id)`. There is no `get_latest()` anywhere.
* **Wallet writes** go through the injected provider
  (`window.ethereum` → `createClient({ account, provider })`), with loading
  states and surfaced errors.
* **DOM APIs only** — `textContent` and `createElement`, never `innerHTML` with
  data.

## API

| method | note |
|---|---|
| `open_trade(seller, asset, unit, qty, band_low, band_high, tolerance_bps, providers, settle_after, deadline)` | **payable** |
| `settle(trade_id)` | pulls providers, validates, cross-checks, pays at the median |
| `cancel(trade_id)` | buyer only, before the settle window opens |
| `expire(trade_id)` | anyone, after the deadline |
| `get_trade` / `get_quote` / `get_trade_count` / `get_catalogue` | all views return JSON strings or plain ints |

## Testing

45 direct-mode tests in `tests/direct/test_price_band_escrow.py`, in eight
groups: trade creation, provider adapters, cross-validation, identity,
settlement and funds, exits, uniform write preconditions, views.

The provider payloads in `conftest.py` are the **real shapes** captured from the
three venues. Direct mode does not track native value, so two accounting hooks
are installed so every payout assertion is a real balance change. There is
deliberately no catch-all model mock.

```
python -m pytest tests/direct -q        # 45 passed
```

## Source integrity

Deployed and repository source are byte identical:

```
sha256  b8f11f442f8d3cde5c9daa723ab3652036c421285659b00be8e30dcbd0db3a6c
```
