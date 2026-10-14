# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
import json
from genlayer import *

SCALE = 100000000
MIN_PROVIDERS = 2
MAX_TOL = 5000
MAX_AGE = 900
SKEW = 120
REF_TOL_BPS = 200

OPEN = "OPEN"
EXECUTED = "EXECUTED"
REFUNDED = "REFUNDED"
CANCELLED = "CANCELLED"

PROVIDERS = ["coingecko", "bitstamp", "coinpaprika"]

# Provider-specific identifiers and endpoints. A generic URL that forgets these
# is exactly how a quote for the wrong asset gets accepted as real data.
ASSETS = {
    "bitcoin": {"coingecko": "bitcoin", "bitstamp": "btcusd",
                "coinpaprika": "btc-bitcoin", "symbol": "BTC"},
    "ethereum": {"coingecko": "ethereum", "bitstamp": "ethusd",
                 "coinpaprika": "eth-ethereum", "symbol": "ETH"},
}

SPACE = " \t\r\n\f\v"


class PriceBandEscrow(gl.Contract):
    """Escrow that pays out only at a verified market price. See README."""

    trades: TreeMap[str, str]
    quotes: TreeMap[str, str]
    trade_count: u256

    def __init__(self):
        self.trade_count = 0

    # ---------------- helpers ----------------

    def _addr_hex(self, a) -> str:
        if isinstance(a, str):
            return a
        h = getattr(a, "as_hex", None)
        if isinstance(h, str):
            return h
        if isinstance(a, (bytes, bytearray)):
            return "0x" + bytes(a).hex()
        return str(a)

    def _is_valid_address(self, addr) -> bool:
        if not isinstance(addr, str) or not addr.startswith("0x") or len(addr) != 42:
            return False
        try:
            int(addr[2:], 16)
        except ValueError:
            return False
        return True

    def _iso_to_epoch(self, s: str) -> int:
        s = s.strip()
        if len(s) < 19:
            return 0
        offset = 0
        tail = s[19:]
        if tail.endswith("Z"):
            tail = tail[:-1]
        if tail and (tail[0] == "+" or tail[0] == "-"):
            try:
                offset = (1 if tail[0] == "+" else -1) * (int(tail[1:3]) * 3600 + int(tail[4:6]) * 60)
            except (ValueError, IndexError):
                offset = 0
        try:
            y, mo, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
            hh, mm, ss = int(s[11:13]), int(s[14:16]), int(s[17:19])
        except (ValueError, IndexError):
            return 0
        if not (1 <= mo <= 12 and 1 <= d <= 31 and hh < 24 and mm < 60 and ss < 61):
            return 0
        yy = y - 1 if mo <= 2 else y
        era = yy // 400
        yoe = yy - era * 400
        doy = (153 * (mo + (-3 if mo > 2 else 9)) + 2) // 5 + d - 1
        doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
        return (era * 146097 + doe - 719468) * 86400 + hh * 3600 + mm * 60 + ss - offset

    def _now(self) -> int:
        raw = getattr(gl, "message_raw", None)
        d = None
        if raw is not None:
            try:
                d = raw["datetime"]
            except Exception:
                d = None
        if d is None:
            d = getattr(gl.message, "datetime", None)
        if d is None:
            return 0
        return self._iso_to_epoch(d if isinstance(d, str) else str(d))

    def _squash(self, text: str) -> str:
        out = []
        for ch in str(text).lower():
            if ch in SPACE:
                continue
            out.append(ch)
        return "".join(out)

    def _dec(self, text) -> int:
        """Decimal text -> integer at SCALE. String arithmetic only: floats round differently across validators."""
        s = str(text).strip()
        if not s or "e" in s or "E" in s:
            return 0
        neg = s.startswith("-")
        if neg:
            s = s[1:]
        if "." in s:
            whole, frac = s.split(".", 1)
        else:
            whole, frac = s, ""
        if whole == "" and frac == "":
            return 0
        if not (whole or "0").isdigit() or not (frac or "0").isdigit():
            return 0
        if whole == "":
            whole = "0"
        value = int(whole) * SCALE + int((frac + "00000000")[:8])
        return -value if neg else value

    def _body(self, content) -> str:
        b = getattr(content, "body", None)
        if b is None:
            return str(content)
        return b.decode("utf-8", errors="replace") if isinstance(b, bytes) else str(b)

    def _transfer(self, to: Address, amount: int):
        if amount > 0:
            gl.get_contract_at(to).emit_transfer(value=u256(amount))

    def _load(self, tid: str) -> dict:
        raw = self.trades.get(str(tid), "")
        if not raw:
            raise gl.vm.UserError("Trade not found")
        return json.loads(raw)

    def _save(self, t: dict):
        self.trades[t["trade_id"]] = json.dumps(t)

    # ---------------- provider adapters ----------------

    def _url(self, pid: str, asset: str, unit: str) -> str:
        if pid == "coingecko":
            return ("https://api.coingecko.com/api/v3/simple/price?ids=" +
                    ASSETS[asset]["coingecko"] + "&vs_currencies=" + unit +
                    "&include_last_updated_at=true")
        if pid == "bitstamp":
            return "https://www.bitstamp.net/api/v2/ticker/" + ASSETS[asset]["bitstamp"] + "/"
        return "https://api.coinpaprika.com/v1/tickers/" + ASSETS[asset]["coinpaprika"]

    def _parse(self, pid: str, body: str, asset: str, unit: str) -> dict:
        bad = {"ok": False, "price": 0, "updated": 0, "label": ""}
        if not body.strip():
            bad["reason"] = pid + " did not answer"
            return bad
        try:
            data = json.loads(body)
        except Exception:
            bad["reason"] = pid + " payload is not JSON"
            return bad
        if not isinstance(data, dict):
            bad["reason"] = pid + " payload is not an object"
            return bad

        if pid == "coingecko":
            key = ASSETS[asset]["coingecko"]
            node = data.get(key)
            if not isinstance(node, dict):
                bad["reason"] = pid + " has no entry for this asset"
                return bad
            if unit not in node:
                bad["reason"] = pid + " quote is in another unit"
                return bad
            price, ts = self._dec(node[unit]), node.get("last_updated_at")
            if not isinstance(ts, int) or price <= 0:
                bad["reason"] = pid + " lacks price or freshness"
                return bad
            return {"ok": True, "price": price, "updated": ts, "label": key,
                    "reason": "", "body": body[:400]}

        if pid == "bitstamp":
            if "last" not in data or "timestamp" not in data:
                bad["reason"] = pid + " lacks price or freshness"
                return bad
            price = self._dec(data["last"])
            ts = self._dec(data["timestamp"]) // SCALE
            if price <= 0 or ts <= 0:
                bad["reason"] = pid + " reports a non-positive quote"
                return bad
            return {"ok": True, "price": price, "updated": ts,
                    "label": ASSETS[asset]["bitstamp"], "reason": "", "body": body[:400]}

        if str(data.get("id", "")) != ASSETS[asset]["coinpaprika"]:
            bad["reason"] = pid + " names a different asset"
            return bad
        quotes = data.get("quotes")
        node = quotes.get(unit.upper()) if isinstance(quotes, dict) else None
        if not isinstance(node, dict) or "price" not in node:
            bad["reason"] = pid + " quote is in another unit"
            return bad
        price = self._dec(node["price"])
        ts = self._iso_to_epoch(str(data.get("last_updated", "")))
        if price <= 0 or ts <= 0:
            bad["reason"] = pid + " lacks price or freshness"
            return bad
        return {"ok": True, "price": price, "updated": ts,
                "label": str(data.get("symbol", "")), "reason": "", "body": body[:400]}

    def _identity(self, rows: list, asset: str) -> dict:
        """Do these responses describe one asset? Labels are code-checked after."""
        task = (
            "Price provider responses. One question:\n"
            "do they all describe the SAME asset? For each return the asset label copied\n"
            "exactly from it, or empty.\n"
            "\n" +
            "\n\n".join(["RESPONSE " + str(i + 1) + ":\n" + str(r.get("body", ""))[:500]
                         for i, r in enumerate(rows)]) +
            "\n\nASSET ASKED FOR: " + asset +
            '\nJSON: {"same":"true"|"false"|"unclear","labels":["..."]}'
        )
        parsed = None
        try:
            raw = gl.nondet.exec_prompt(task)
            if isinstance(raw, str):
                parsed = json.loads(raw.replace("```json", "").replace("```", ""))
            elif isinstance(raw, dict):
                parsed = raw
        except Exception:
            parsed = None
        if not isinstance(parsed, dict):
            parsed = {}
        same = str(parsed.get("same", "unclear")).strip().lower()
        if same not in ("true", "false", "unclear"):
            same = "unclear"
        labels = parsed.get("labels")
        out = []
        for i in range(len(rows)):
            v = labels[i] if isinstance(labels, list) and i < len(labels) else ""
            lab = str(v) if isinstance(v, str) else ""
            if lab and self._squash(lab) not in self._squash(rows[i].get("body", "")):
                lab = ""
            out.append(lab)
        return {"same": same, "labels": out}

    # ---------------- lifecycle ----------------

    @gl.public.write.payable
    def open_trade(self, seller: str, asset: str, unit: str, qty: int,
                   band_low: int, band_high: int, tolerance_bps: int,
                   providers: list, settle_after: int, deadline: int) -> str:
        buyer = self._addr_hex(gl.message.sender_address)
        escrow = int(gl.message.value)
        if not self._is_valid_address(seller) or self._addr_hex(seller).lower() == buyer.lower():
            raise gl.vm.UserError("Seller must be another 0x address")
        if escrow <= 0:
            raise gl.vm.UserError("Escrow must be > 0")
        a = str(asset).strip().lower()
        if a not in ASSETS:
            raise gl.vm.UserError("Asset is not supported")
        u = str(unit).strip().lower()
        if not u or len(u) > 8 or not u.isalpha():
            raise gl.vm.UserError("Unit must be letters only")
        if int(qty) <= 0:
            raise gl.vm.UserError("Quantity must be > 0")
        if int(band_low) <= 0 or int(band_high) <= int(band_low):
            raise gl.vm.UserError("Band must be a positive range")
        if not 0 <= int(tolerance_bps) <= MAX_TOL:
            raise gl.vm.UserError("Tolerance must be 0..5000 bps")
        if not isinstance(providers, list) or len(providers) < MIN_PROVIDERS:
            raise gl.vm.UserError("Need at least two providers")
        plist = []
        for p in providers:
            pid = str(p).strip()
            if pid not in PROVIDERS or pid in plist:
                raise gl.vm.UserError("Providers must be distinct and known")
            plist.append(pid)
        now = self._now()
        if int(settle_after) <= 0 or int(deadline) <= int(settle_after):
            raise gl.vm.UserError("Deadline must follow the settle window")

        tid = "t" + str(int(self.trade_count))
        self.trades[tid] = json.dumps({
            "trade_id": tid, "buyer": buyer, "seller": self._addr_hex(seller),
            "asset": a, "unit": u, "qty": int(qty), "escrow": escrow,
            "band_low": int(band_low), "band_high": int(band_high),
            "tolerance_bps": int(tolerance_bps), "providers": plist,
            "settle_after": now + int(settle_after), "deadline": now + int(deadline),
            "status": OPEN, "reason": "", "reference": 0, "cost": 0,
            "seller_paid": 0, "buyer_refund": 0, "valid_count": 0, "spread_bps": 0,
        })
        self.trade_count = u256(int(self.trade_count) + 1)
        return tid

    @gl.public.write
    def settle(self, trade_id: str) -> str:
        """Pull every provider, refuse the set if they disagree, pay at the median."""
        t = self._load(trade_id)
        if str(t["status"]) != OPEN:
            raise gl.vm.UserError("Trade is " + str(t["status"]))
        now = self._now()
        if now < int(t["settle_after"]):
            raise gl.vm.UserError("Settle window has not opened")
        if now > int(t["deadline"]):
            raise gl.vm.UserError("Settle window has closed")
        a, u = str(t["asset"]), str(t["unit"])

        def run() -> dict:
            rows = []
            for pid in t["providers"]:
                body = ""
                try:
                    body = self._body(gl.nondet.web.get(self._url(pid, a, u)))
                except Exception:
                    body = ""
                r = self._parse(pid, body, a, u)
                r["provider"] = pid
                rows.append(r)

            good = []
            for r in rows:
                if not r.get("ok"):
                    continue
                age = now - int(r["updated"])
                if age > MAX_AGE:
                    r.update(ok=False, reason=r["provider"] + " quote is stale")
                elif int(r["updated"]) > now + SKEW:
                    r.update(ok=False, reason=r["provider"] + " quote is dated in the future")
                else:
                    good.append(r)

            out = [{"provider": str(r.get("provider", "")), "ok": bool(r.get("ok")),
                    "price": int(r.get("price", 0)), "updated": int(r.get("updated", 0)),
                    "reason": str(r.get("reason", "")), "label": str(r.get("label", ""))}
                   for r in rows]
            if len(good) < MIN_PROVIDERS:
                return {"rows": out, "valid": len(good), "spread_bps": 0,
                        "reference": 0, "same": "unclear", "verdict": "UNVERIFIED"}

            prices = sorted([int(r["price"]) for r in good])
            mid = len(prices) // 2
            reference = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) // 2
            spread_bps = ((prices[-1] - prices[0]) * 10000) // reference if reference else 0

            ident = self._identity([{"body": r.get("body", "")} for r in good], a)
            same = str(ident.get("same", "unclear")) if isinstance(ident, dict) else "unclear"
            if isinstance(ident, dict):
                for i, lab in enumerate(ident.get("labels", [])):
                    if i < len(good):
                        for row in out:
                            if row["provider"] == str(good[i].get("provider", "")):
                                row["label"] = str(lab)

            if same != "true":
                verdict = "UNVERIFIED"
            elif spread_bps > int(t["tolerance_bps"]):
                verdict = "DISAGREED"
            else:
                verdict = "VERIFIED"
            return {"rows": out, "valid": len(good), "spread_bps": spread_bps,
                    "reference": reference, "same": same, "verdict": verdict}

        # One consensus point for a settlement; the verdict is computed in code.
        # Prices move between two fetches, so the reference is compared with an
        # explicit numeric tolerance while the decision fields match exactly.
        # Comparing a live price for exact equality can never converge.
        def agrees(leader) -> bool:
            if not isinstance(leader, gl.vm.Return):
                return False
            his = leader.calldata
            mine = run()
            if not isinstance(his, dict):
                return False
            for k in ("verdict", "valid", "same"):
                if str(his.get(k)) != str(mine.get(k)):
                    return False
            hr = int(his.get("reference", 0))
            mr = int(mine.get("reference", 0))
            if hr <= 0 or mr <= 0:
                if hr != mr:
                    return False
            elif abs(hr - mr) * 10000 > hr * REF_TOL_BPS:
                return False
            hrows = his.get("rows")
            mrows = mine.get("rows")
            if not isinstance(hrows, list) or not isinstance(mrows, list) or len(hrows) != len(mrows):
                return False
            for a, b in zip(hrows, mrows):
                if bool(a.get("ok")) != bool(b.get("ok")):
                    return False
                if str(a.get("provider")) != str(b.get("provider")):
                    return False
            return True

        res = gl.vm.run_nondet_unsafe(run, agrees)
        if not isinstance(res, dict):
            res = {}

        rows = res.get("rows")
        self.quotes[trade_id] = json.dumps(rows if isinstance(rows, list) else [])
        t["valid_count"] = int(res.get("valid", 0))
        t["spread_bps"] = int(res.get("spread_bps", 0))
        t["reference"] = int(res.get("reference", 0))
        escrow, qty = int(t["escrow"]), int(t["qty"])
        verdict = str(res.get("verdict", ""))

        if verdict != "VERIFIED":
            t["status"] = REFUNDED
            t["reason"] = ("providers did not agree" if verdict == "DISAGREED"
                           else "the quote could not be verified")
        elif t["reference"] < int(t["band_low"]) or t["reference"] > int(t["band_high"]):
            t["status"] = REFUNDED
            t["reason"] = "the verified price is outside the band"
        else:
            cost = (qty * t["reference"]) // SCALE
            if cost > escrow:
                t["status"] = REFUNDED
                t["reason"] = "escrow does not cover the cost"
            else:
                t["status"] = EXECUTED
                t["reason"] = "verified inside the band"
                t["cost"] = cost
                t["seller_paid"] = cost
                t["buyer_refund"] = escrow - cost
                self._save(t)
                # Every unit goes to exactly one of the two parties.
                self._transfer(Address(t["seller"]), cost)
                self._transfer(Address(t["buyer"]), escrow - cost)
                return EXECUTED

        t["buyer_refund"] = escrow
        t["seller_paid"] = 0
        self._save(t)
        self._transfer(Address(t["buyer"]), escrow)
        return REFUNDED

    @gl.public.write
    def cancel(self, trade_id: str) -> str:
        """Buyer may walk away before the settle window opens."""
        t = self._load(trade_id)
        if str(t["status"]) != OPEN:
            raise gl.vm.UserError("Trade is " + str(t["status"]))
        if self._addr_hex(gl.message.sender_address).lower() != str(t["buyer"]).lower():
            raise gl.vm.UserError("Only the buyer cancels")
        if self._now() >= int(t["settle_after"]):
            raise gl.vm.UserError("The settle window is open")
        t["status"] = CANCELLED
        t["reason"] = "cancelled by the buyer"
        t["buyer_refund"] = int(t["escrow"])
        self._save(t)
        self._transfer(Address(t["buyer"]), int(t["escrow"]))
        return CANCELLED

    @gl.public.write
    def expire(self, trade_id: str) -> str:
        """Nobody settled in time: the escrow goes home, permissionlessly."""
        t = self._load(trade_id)
        if str(t["status"]) != OPEN:
            raise gl.vm.UserError("Trade is " + str(t["status"]))
        if self._now() <= int(t["deadline"]):
            raise gl.vm.UserError("The deadline has not passed")
        t["status"] = REFUNDED
        t["reason"] = "no settlement before the deadline"
        t["buyer_refund"] = int(t["escrow"])
        self._save(t)
        self._transfer(Address(t["buyer"]), int(t["escrow"]))
        return REFUNDED

    # ---------------- views ----------------

    @gl.public.view
    def get_trade(self, trade_id: str) -> str:
        return json.dumps(self._load(trade_id))

    @gl.public.view
    def get_quote(self, trade_id: str) -> str:
        self._load(trade_id)
        raw = self.quotes.get(str(trade_id), "")
        return raw if raw else "[]"

    @gl.public.view
    def get_trade_count(self) -> int:
        return int(self.trade_count)
