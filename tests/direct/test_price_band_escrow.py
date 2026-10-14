"""Direct-mode tests for Price Band Escrow.

Groups A-H map onto the failure modes this app exists to avoid and the rules
the Project category demands:

  A  trade creation     -- real addresses, real units, provider allowlist
  B  provider adapters  -- each provider's own id, shape, unit and freshness
  C  cross-validation   -- one source never decides, disagreement is refused
  D  identity           -- the model confirms the asset, code checks the quote
  E  settlement & funds -- every unit of the escrow reaches exactly one party
  F  exits              -- cancel and expire are the recovery paths
  G  write preconditions-- every refusal leaves the state untouched
  H  views              -- bound to a trade id, no float or list leaves the contract
"""

import json

import pytest

from conftest import (
    BAD_ADDR,
    BAND_HIGH,
    BAND_LOW,
    BASE,
    BUYER,
    ESCROW,
    NOW,
    QTY,
    SCALE,
    SELLER,
    SELLER_B,
    STRANGER,
    balance,
    bs,
    cg,
    contract_balance,
    cp,
    epoch,
    full_snapshot,
    ident,
    iso,
    open_trade,
    quote,
    remock,
    settle,
    trade,
)

# Prices held fixed so every expected number below is hand-checkable. The RAW
# values are what the providers report; the contract scales them to PRICE_SCALE.
CG_RAW = 83000
BS_RAW = "83500.00"
CP_RAW = 83250.5
CG_P = CG_RAW * SCALE            # 8,300,000,000,000
BS_P = 83500 * SCALE             # 8,350,000,000,000
CP_P = 8325050000000             # 8,325,050,000,000
MEDIAN = CP_P
COST = (QTY * MEDIAN) // SCALE   # 16,650,100,000,000


def happy(vm, tolerance=200, at=NOW - 30):
    remock(vm, coingecko=cg(CG_RAW, at), bitstamp=bs(BS_RAW, at),
           paprika=cp(CP_RAW, at), model=ident())


# ==========================================================================
# A. trade creation
# ==========================================================================


def test_open_trade_escrows_the_funds(escrow):
    vm, c = escrow
    before = contract_balance(vm)
    tid = open_trade(vm, c)
    assert contract_balance(vm) == before + ESCROW
    t = trade(vm, c, tid)
    assert t["status"] == "OPEN"
    assert t["buyer"] == BUYER
    assert t["seller"] == SELLER
    assert t["escrow"] == ESCROW
    assert t["asset"] == "bitcoin"
    assert t["providers"] == ["coingecko", "bitstamp", "coinpaprika"]


def test_rejects_a_bad_seller(escrow):
    vm, c = escrow
    for bad in (BAD_ADDR, BUYER, "0x" + "zz" * 20, ""):
        with pytest.raises(Exception):
            open_trade(vm, c, seller=bad)


def test_rejects_a_zero_escrow(escrow):
    vm, c = escrow
    with pytest.raises(Exception):
        open_trade(vm, c, escrow_amt=0)


def test_rejects_an_unsupported_asset(escrow):
    vm, c = escrow
    for bad in ("dogecoin", "", "x" * 40, "bitcoi", "bitcoinx"):
        with pytest.raises(Exception):
            open_trade(vm, c, asset=bad)


def test_rejects_a_bad_unit(escrow):
    vm, c = escrow
    for bad in ("", "usd1", "u" * 20, "us dt"):
        with pytest.raises(Exception):
            open_trade(vm, c, unit=bad)


def test_rejects_a_bad_band(escrow):
    vm, c = escrow
    for low, high in ((0, BAND_HIGH), (BAND_HIGH, BAND_LOW), (BAND_LOW, BAND_LOW),
                      (-5 * SCALE, BAND_HIGH)):
        with pytest.raises(Exception):
            open_trade(vm, c, band_low=low, band_high=high)


def test_rejects_a_bad_tolerance(escrow):
    vm, c = escrow
    for bad in (-1, 5001, 100000):
        with pytest.raises(Exception):
            open_trade(vm, c, tolerance=bad)


def test_rejects_a_bad_provider_list(escrow):
    vm, c = escrow
    for bad in (["coingecko"], ["coingecko", "coingecko"], ["coingecko", "magic"],
                [], "coingecko"):
        with pytest.raises(Exception):
            open_trade(vm, c, providers=bad)


def test_rejects_bad_timings(escrow):
    vm, c = escrow
    for sa, dl in ((0, 100), (100, 100), (100, 50), (-5, 100)):
        with pytest.raises(Exception):
            open_trade(vm, c, settle_after=sa, deadline=dl)


def test_rejects_a_non_positive_quantity(escrow):
    vm, c = escrow
    for bad in (0, -2 * SCALE):
        with pytest.raises(Exception):
            open_trade(vm, c, qty=bad)


# ==========================================================================
# B. provider adapters -- each provider's own id, shape, unit and freshness
# ==========================================================================


def test_the_three_real_payload_shapes_are_parsed(escrow):
    """CoinGecko nests under the asset id, Bitstamp returns strings, CoinPaprika
    uses quotes.UNIT and an ISO timestamp. All three must land on the same
    scale with their own freshness."""
    vm, c = escrow
    tid = open_trade(vm, c)
    happy(vm)
    assert settle(vm, c, tid) == "EXECUTED"
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    assert rows["coingecko"]["price"] == CG_P
    assert rows["bitstamp"]["price"] == 83500 * SCALE
    assert rows["coinpaprika"]["price"] == CP_P
    assert rows["coingecko"]["updated"] == epoch(NOW - 30)
    assert rows["coinpaprika"]["updated"] == epoch(NOW - 30)
    for r in rows.values():
        assert r["ok"] is True


def test_prices_are_scaled_with_no_floats(escrow):
    """A fractional quote must land exactly, not where a float happens to
    round it."""
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko=cg(83411.23), bitstamp=bs("83411.23000000"),
           paprika=cp(83411.23), model=ident())
    settle(vm, c, tid)
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    for r in rows.values():
        assert r["price"] == 8341123000000


def test_rejects_a_coingecko_entry_for_another_asset(escrow):
    """A generic URL that quotes whatever it finds is failure mode number one."""
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko=cg(CG_RAW, asset="dogecoin"), bitstamp=bs(BS_RAW),
           paprika=cp(CP_RAW), model=ident())
    settle(vm, c, tid)
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    assert rows["coingecko"]["ok"] is False
    assert "no entry" in rows["coingecko"]["reason"]


def test_rejects_a_coingecko_quote_in_the_wrong_unit(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko=cg(CG_RAW, unit="eur"), bitstamp=bs(BS_RAW),
           paprika=cp(CP_RAW), model=ident())
    settle(vm, c, tid)
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    assert rows["coingecko"]["ok"] is False
    assert "unit" in rows["coingecko"]["reason"]


def test_rejects_a_coinpaprika_entry_for_another_asset(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko=cg(CG_RAW), bitstamp=bs(BS_RAW),
           paprika=cp(CP_RAW, pid="eth-ethereum", symbol="ETH"), model=ident())
    settle(vm, c, tid)
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    assert rows["coinpaprika"]["ok"] is False
    assert "different asset" in rows["coinpaprika"]["reason"]


def test_rejects_a_stale_quote(escrow):
    """Freshness is checked against the block clock, not trusted."""
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko=cg(CG_RAW, at=NOW - 5000), bitstamp=bs(BS_RAW),
           paprika=cp(CP_RAW), model=ident())
    settle(vm, c, tid)
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    assert rows["coingecko"]["ok"] is False
    assert "stale" in rows["coingecko"]["reason"]


def test_rejects_a_quote_dated_in_the_future(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko=cg(CG_RAW), bitstamp=bs(BS_RAW, at=NOW + 600),
           paprika=cp(CP_RAW), model=ident())
    settle(vm, c, tid)
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    assert rows["bitstamp"]["ok"] is False
    assert "future" in rows["bitstamp"]["reason"]


def test_rejects_a_payload_that_is_not_json(escrow):
    """Non-empty is not valid. A 500 page proves nothing."""
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko="<html>Service Unavailable</html>", bitstamp=bs(BS_RAW),
           paprika=cp(CP_RAW), model=ident())
    settle(vm, c, tid)
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    assert rows["coingecko"]["ok"] is False
    assert "not JSON" in rows["coingecko"]["reason"]


def test_rejects_a_payload_missing_price_or_freshness(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    for bad in ('{"bitcoin":{"last_updated_at":1791964322}}',
                '{"bitcoin":{"usd":83411}}',
                '{"bitcoin":{"usd":"abc","last_updated_at":1791964322}}'):
        vm, c = escrow
        tid = open_trade(vm, c)
        remock(vm, coingecko=bad, bitstamp=bs(BS_RAW), paprika=cp(CP_RAW), model=ident())
        settle(vm, c, tid)
        rows = {r["provider"]: r for r in quote(vm, c, tid)}
        assert rows["coingecko"]["ok"] is False


def test_rejects_a_bitstamp_payload_without_a_timestamp(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko=cg(CG_RAW),
           bitstamp=json.dumps({"last": "83500.00"}), paprika=cp(CP_RAW), model=ident())
    settle(vm, c, tid)
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    assert rows["bitstamp"]["ok"] is False


def test_a_provider_that_does_not_answer_is_refused(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko=cg(CG_RAW), bitstamp=bs(BS_RAW), paprika=None, model=ident())
    settle(vm, c, tid)
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    assert rows["coinpaprika"]["ok"] is False


# ==========================================================================
# C. cross-validation -- one source never decides
# ==========================================================================


def test_the_median_of_three_is_used(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    happy(vm)
    assert settle(vm, c, tid) == "EXECUTED"
    t = trade(vm, c, tid)
    assert t["reference"] == MEDIAN
    assert t["valid_count"] == 3
    assert t["cost"] == COST


def test_two_valid_providers_are_enough(escrow):
    vm, c = escrow
    tid = open_trade(vm, c, providers=("coingecko", "bitstamp"))
    remock(vm, coingecko=cg(CG_RAW), bitstamp=bs(BS_RAW), model=ident())
    assert settle(vm, c, tid) == "EXECUTED"
    t = trade(vm, c, tid)
    assert t["valid_count"] == 2
    assert t["reference"] == (CG_P + 83500 * SCALE) // 2


def test_one_provider_alone_never_settles(escrow):
    """A single source is not cross-validation. The money stays home."""
    vm, c = escrow
    tid = open_trade(vm, c, providers=("coingecko", "bitstamp"))
    remock(vm, coingecko=cg(CG_RAW), bitstamp=None, model=ident())
    before = balance(vm, BUYER)
    assert settle(vm, c, tid) == "REFUNDED"
    assert trade(vm, c, tid)["reason"] == "the quote could not be verified"
    assert balance(vm, BUYER) == before + ESCROW


def test_providers_that_disagree_beyond_tolerance_are_refused(escrow):
    vm, c = escrow
    tid = open_trade(vm, c, tolerance=5)
    happy(vm)
    assert settle(vm, c, tid) == "REFUNDED"
    t = trade(vm, c, tid)
    assert t["reason"] == "providers did not agree"
    assert t["spread_bps"] > 5


def test_a_wider_tolerance_accepts_the_same_quotes(escrow):
    vm, c = escrow
    tid = open_trade(vm, c, tolerance=500)
    happy(vm)
    assert settle(vm, c, tid) == "EXECUTED"


# ==========================================================================
# D. identity -- the model confirms, the code checks
# ==========================================================================


def test_identity_must_agree(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    happy(vm, at=NOW - 30)
    remock(vm, coingecko=cg(CG_RAW), bitstamp=bs(BS_RAW), paprika=cp(CP_RAW),
           model=ident("false", ["bitcoin", "btcusd", "BTC"]))
    assert settle(vm, c, tid) == "REFUNDED"
    assert trade(vm, c, tid)["reason"] == "the quote could not be verified"


def test_unclear_identity_refuses_the_set(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko=cg(CG_RAW), bitstamp=bs(BS_RAW), paprika=cp(CP_RAW),
           model=ident("unclear"))
    assert settle(vm, c, tid) == "REFUNDED"


def test_a_label_not_in_the_response_is_discarded(escrow):
    """The model cannot invent a label: it must occur in that response."""
    vm, c = escrow
    tid = open_trade(vm, c)
    remock(vm, coingecko=cg(CG_RAW), bitstamp=bs(BS_RAW), paprika=cp(CP_RAW),
           model=ident("true", ["bitcoin", "a totally made up ticker", "BTC"]))
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    settle(vm, c, tid)
    rows = {r["provider"]: r for r in quote(vm, c, tid)}
    assert rows["bitstamp"]["label"] == ""


def test_a_malformed_identity_answer_is_fail_closed(escrow):
    vm, c = escrow
    for bad in ("not json at all", '{"same": "nope"}', '{"labels": 7}', ""):
        vm, c = escrow
        tid = open_trade(vm, c)
        remock(vm, coingecko=cg(CG_RAW), bitstamp=bs(BS_RAW), paprika=cp(CP_RAW), model=bad)
        assert settle(vm, c, tid) == "REFUNDED"
        assert trade(vm, c, tid)["reason"] == "the quote could not be verified"


# ==========================================================================
# E. settlement and funds
# ==========================================================================


def test_verified_in_band_pays_the_seller_and_returns_the_change(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    happy(vm)
    before_s, before_b = balance(vm, SELLER), balance(vm, BUYER)
    held = contract_balance(vm)
    assert settle(vm, c, tid) == "EXECUTED"

    t = trade(vm, c, tid)
    assert t["seller_paid"] == COST
    assert t["buyer_refund"] == ESCROW - COST
    assert balance(vm, SELLER) == before_s + COST
    assert balance(vm, BUYER) == before_b + (ESCROW - COST)
    # the whole escrow went to exactly one of the two parties
    assert t["seller_paid"] + t["buyer_refund"] == t["escrow"] == ESCROW
    assert contract_balance(vm) == held - ESCROW


def test_out_of_band_refunds_the_buyer_whole(escrow):
    vm, c = escrow
    tid = open_trade(vm, c, band_low=1 * SCALE, band_high=2 * SCALE)
    happy(vm)
    before_b = balance(vm, BUYER)
    before_s = balance(vm, SELLER)
    assert settle(vm, c, tid) == "REFUNDED"
    assert trade(vm, c, tid)["reason"] == "the verified price is outside the band"
    assert balance(vm, BUYER) == before_b + ESCROW
    assert balance(vm, SELLER) == before_s


def test_underfunded_escrow_refunds_the_buyer(escrow):
    vm, c = escrow
    tid = open_trade(vm, c, escrow_amt=COST - 1000)
    happy(vm)
    before_b = balance(vm, BUYER)
    assert settle(vm, c, tid) == "REFUNDED"
    assert trade(vm, c, tid)["reason"] == "escrow does not cover the cost"
    assert balance(vm, BUYER) == before_b + (COST - 1000)


def test_every_unit_reaches_exactly_one_party(escrow):
    """The identity this app is built to hold, on every path at once."""
    vm, c = escrow
    held = contract_balance(vm)
    t1 = open_trade(vm, c)                                   # executes
    happy(vm)
    t2 = open_trade(vm, c, band_low=1 * SCALE, band_high=2 * SCALE)   # out of band
    t3 = open_trade(vm, c, escrow_amt=COST - 1000)            # underfunded
    settle(vm, c, t1)
    settle(vm, c, t2)
    settle(vm, c, t3)
    t = trade(vm, c, t1)
    assert t["seller_paid"] + t["buyer_refund"] == ESCROW
    for tid in (t2, t3):
        x = trade(vm, c, tid)
        assert x["seller_paid"] + x["buyer_refund"] == x["escrow"]
    assert contract_balance(vm) == held


# ==========================================================================
# F. exits -- the recovery paths
# ==========================================================================


def test_cancel_is_buyer_only_and_only_early(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    for sender in (STRANGER, SELLER):
        vm.warp(iso(10))
        vm.sender = sender
        with pytest.raises(Exception):
            c.cancel(tid)
    vm.warp(iso(500))
    vm.sender = BUYER
    with pytest.raises(Exception):
        c.cancel(tid)                       # the settle window has opened
    vm, c = escrow
    tid = open_trade(vm, c)
    vm.warp(iso(10))
    vm.sender = BUYER
    assert c.cancel(tid) == "CANCELLED"


def test_cancel_refunds_the_escrow_whole(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    before_b = balance(vm, BUYER)
    held = contract_balance(vm)
    vm.warp(iso(10))
    vm.sender = BUYER
    assert c.cancel(tid) == "CANCELLED"
    assert balance(vm, BUYER) == before_b + ESCROW
    assert contract_balance(vm) == held - ESCROW


def test_expire_only_after_the_deadline(escrow):
    vm, c = escrow
    tid = open_trade(vm, c, settle_after=100, deadline=600)
    for at in (100, 599, 600):
        vm.warp(iso(at))
        vm.sender = STRANGER
        with pytest.raises(Exception):
            c.expire(tid)
    vm.warp(iso(601))
    vm.sender = STRANGER
    assert c.expire(tid) == "REFUNDED"
    assert trade(vm, c, tid)["reason"] == "no settlement before the deadline"


def test_expire_refunds_the_escrow_whole(escrow):
    vm, c = escrow
    tid = open_trade(vm, c, settle_after=100, deadline=600)
    before_b = balance(vm, BUYER)
    vm.warp(iso(601))
    vm.sender = STRANGER
    c.expire(tid)
    assert balance(vm, BUYER) == before_b + ESCROW


def test_settle_only_inside_the_window(escrow):
    vm, c = escrow
    tid = open_trade(vm, c, settle_after=100, deadline=600)
    happy(vm)
    for at in (10, 99, 601, 5000):
        with pytest.raises(Exception):
            settle(vm, c, tid, at=at)
    assert trade(vm, c, tid)["status"] == "OPEN"


# ==========================================================================
# G. uniform write preconditions
# ==========================================================================


def test_a_settled_trade_cannot_move_again(escrow):
    vm, c = escrow
    tid = open_trade(vm, c)
    happy(vm)
    assert settle(vm, c, tid) == "EXECUTED"
    before = full_snapshot(vm, c, tid)
    for call in (lambda: settle(vm, c, tid, at=510),
                 lambda: c.cancel(tid),
                 lambda: c.expire(tid)):
        with pytest.raises(Exception):
            call()
    assert full_snapshot(vm, c, tid) == before


def test_every_refusal_leaves_the_state_byte_identical(escrow):
    """All four writes enforce the same preconditions, and none of them
    mutates anything when it refuses."""
    vm, c = escrow
    tid = open_trade(vm, c, settle_after=100, deadline=600)

    cases = [
        ("settle before the window", lambda: settle(vm, c, tid, at=50)),
        ("settle after the deadline", lambda: settle(vm, c, tid, at=601)),
        ("cancel from a stranger", lambda: c.cancel(tid)),
        ("expire before the deadline", lambda: c.expire(tid)),
        ("settle a missing trade", lambda: settle(vm, c, "ghost", at=500)),
    ]
    for label, call in cases:
        vm.warp(iso(500))
        vm.sender = STRANGER
        before = full_snapshot(vm, c, tid)
        with pytest.raises(Exception):
            call()
        assert full_snapshot(vm, c, tid) == before, label


# ==========================================================================
# H. views
# ==========================================================================


def test_views_return_strings_never_structures(escrow):
    """A bare float or list in a return value breaks calldata encoding on chain
    while direct mode stays green, so the type is part of the contract."""
    vm, c = escrow
    tid = open_trade(vm, c)
    for value in (c.get_trade(tid), c.get_quote(tid)):
        assert isinstance(value, str)
        json.loads(value)
    assert isinstance(c.get_trade_count(), int)


def test_get_trade_count_tracks_opens(escrow):
    vm, c = escrow
    assert c.get_trade_count() == 0
    open_trade(vm, c)
    assert c.get_trade_count() == 1
    open_trade(vm, c)
    assert c.get_trade_count() == 2


def test_unknown_trade_raises(escrow):
    vm, c = escrow
    for call in (lambda: c.get_trade("ghost"), lambda: c.get_quote("ghost"),
                 lambda: settle(vm, c, "ghost", at=500),
                 lambda: c.cancel("ghost"), lambda: c.expire("ghost")):
        with pytest.raises(Exception):
            call()


def test_get_quote_records_what_each_provider_said(escrow):
    """A reviewer can see every provider's verdict and reason for themselves."""
    vm, c = escrow
    tid = open_trade(vm, c)
    happy(vm)
    settle(vm, c, tid)
    rows = quote(vm, c, tid)
    assert [r["provider"] for r in rows] == ["coingecko", "bitstamp", "coinpaprika"]
    assert all(r["ok"] for r in rows)
    assert rows[0]["label"] == "bitcoin"
    assert rows[1]["label"] == "btcusd"
    assert rows[2]["label"] == "BTC"
