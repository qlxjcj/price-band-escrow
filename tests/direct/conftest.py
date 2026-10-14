"""Fixtures for Price Band Escrow direct-mode tests.

The provider payloads below are the real shapes captured from CoinGecko,
Bitstamp and CoinPaprika: a nested number keyed by the asset id, a flat object
of strings, and a nested number with an ISO timestamp. Each provider names the
asset its own way, which is the whole point of the adapters.

Direct mode does not track native value flow, so two accounting hooks are
installed (mirroring what the chain does) so the tests can genuinely assert
fund movement:

1. payable value: `open_trade` moves `vm.value` sender -> contract
2. `emit_transfer` is intercepted as a PostMessage gl_call and moved
   contract -> recipient

There is deliberately no catch-all model mock: a call a test did not anticipate
must fail rather than silently return something plausible.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

CONTRACT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "price_band_escrow.py",
)

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
SCALE = 10 ** 8

BUYER = "0x" + "aa" * 20
SELLER = "0x" + "bb" * 20
SELLER_B = "0x" + "cc" * 20
STRANGER = "0x" + "44" * 20
BAD_ADDR = "0x1234"

ESCROW = 2 * 10 ** 13          # 200,000 quote units at 1e8 scale
QTY = 2 * 10 ** 8              # 2 units of the asset
BAND_LOW = 80000 * SCALE       # 80,000 quote units
BAND_HIGH = 90000 * SCALE      # 90,000 quote units

NOW = 500                    # the settle instant, seconds after BASE


def iso(offset: int) -> str:
    return (BASE + timedelta(seconds=offset)).isoformat()


def epoch(at: int) -> int:
    return int((BASE + timedelta(seconds=at)).timestamp())


def balance(vm, addr):
    return vm._balances.get(vm._to_bytes(addr), 0)


def contract_balance(vm):
    return vm._balances.get(vm._contract_address, 0)


# ---------------- real provider payload shapes ----------------

def cg(price=83411, at=NOW - 30, asset="bitcoin", unit="usd"):
    """CoinGecko: nested number keyed by the asset id, unix-second freshness."""
    return json.dumps({asset: {unit: price, "last_updated_at": epoch(at)}})


def bs(price="83507.45", at=NOW - 30, pair="btcusd"):
    """Bitstamp: everything is a string, including the unix-second timestamp."""
    return json.dumps({"timestamp": str(epoch(at)), "open": "83093.57",
                       "high": "84064.89", "low": "82880.00", "last": price,
                       "volume": "1280.13010714", "vwap": "83352.81",
                       "bid": "83507.44", "ask": "83507.45", "side": "0",
                       "open_24": "83820.94", "percent_change_24": "-0.37",
                       "market_type": "SPOT", "pair": pair})


def cp(price=83411.23, at=NOW - 30, pid="btc-bitcoin", symbol="BTC", unit="usd"):
    """CoinPaprika: nested number under quotes.UNIT with an ISO timestamp."""
    return json.dumps({
        "id": pid, "name": "Bitcoin", "symbol": symbol, "rank": 1,
        "total_supply": 20098041, "max_supply": 21000000, "beta_value": 1.0461,
        "first_data_at": "2010-07-17T00:00:00Z",
        "last_updated": (BASE + timedelta(seconds=at)).isoformat().replace("+00:00", "Z"),
        "quotes": {unit.upper(): {"price": price, "volume_24h": 123456789,
                                  "market_cap": 1600000000000,
                                  "percent_change_24h": -0.37,
                                  "ath_price": 90000.0}}})


def ident(same="true", labels=None):
    return json.dumps({"same": same, "labels": labels or ["bitcoin", "btcusd", "BTC"]})


def remock(vm, coingecko=None, bitstamp=None, paprika=None, model=None):
    """Install exactly the provider answers and model answer the test expects."""
    vm.clear_mocks()
    if coingecko is not None:
        vm.mock_web(".*api\\.coingecko\\.com.*", {"method": "GET", "status": 200, "body": coingecko})
    if bitstamp is not None:
        vm.mock_web(".*bitstamp\\.net.*", {"method": "GET", "status": 200, "body": bitstamp})
    if paprika is not None:
        vm.mock_web(".*coinpaprika\\.com.*", {"method": "GET", "status": 200, "body": paprika})
    if model is not None:
        # Keyed on the asset name, which the prompt always carries. The pattern
        # must cross newlines: '.' does not match '\n'.
        vm.mock_llm(r"[\s\S]*ASSET ASKED FOR[\s\S]*", model)


@pytest.fixture
def escrow(direct_vm, direct_deploy):
    vm = direct_vm

    def _value_transfer_hook(vm, request):
        if "PostMessage" not in request:
            return None
        msg = request["PostMessage"]
        amount = int(msg.get("value", 0))
        if amount > 0:
            contract = vm._contract_address
            recipient = vm._to_bytes(msg["address"])
            vm._balances[contract] = vm._balances.get(contract, 0) - amount
            vm._balances[recipient] = vm._balances.get(recipient, 0) + amount
        return {"ok": None}

    vm._gl_call_hook = _value_transfer_hook

    remock(vm)
    c = direct_deploy(CONTRACT)

    def _wrap(fn):
        def inner(*args):
            if vm.value > 0:
                sender = vm._to_bytes(vm.sender)
                contract = vm._contract_address
                vm._balances[sender] = vm._balances.get(sender, 0) - vm.value
                vm._balances[contract] = vm._balances.get(contract, 0) + vm.value
            return fn(*args)
        return inner

    c.open_trade = _wrap(c.open_trade)

    for addr in (BUYER, SELLER, SELLER_B, STRANGER):
        vm.deal(addr, 1000 * ESCROW)

    vm.warp(iso(0))
    return vm, c


def open_trade(vm, c, seller=SELLER, asset="bitcoin", unit="usd", qty=QTY,
               band_low=BAND_LOW, band_high=BAND_HIGH, tolerance=100,
               providers=("coingecko", "bitstamp", "coinpaprika"),
               settle_after=100, deadline=10000, escrow_amt=ESCROW,
               sender=BUYER):
    vm.warp(iso(0))
    vm.sender = sender
    vm.value = escrow_amt
    tid = c.open_trade(seller, asset, unit, qty, band_low, band_high, tolerance,
                       list(providers), settle_after, deadline)
    vm.value = 0
    return tid


def settle(vm, c, tid, at=NOW, sender=STRANGER):
    vm.warp(iso(at))
    vm.sender = sender
    return c.settle(tid)


def trade(vm, c, tid):
    return json.loads(c.get_trade(tid))


def quote(vm, c, tid):
    return json.loads(c.get_quote(tid))


def full_snapshot(vm, c, tid):
    return (c.get_trade(tid), c.get_quote(tid))
