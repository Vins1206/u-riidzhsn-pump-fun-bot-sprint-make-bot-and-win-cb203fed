"""Quiet-launch sniper for the Pump.fun Bot Sprint (make.fun).

Strategy, tuned on the kit's 24h dev replay (128,058 events, 1,222 coins):
buy a coin in its first 30 slots only when the launch still looks quiet --
at most 1 distinct non-creator buyer so far, and the creator bought
0.1-3.0 SOL in the create slot (a dev-funded launch nobody has crowded
into yet) -- once real SOL reserves reach 1 SOL, so the 1%-of-reserve size
cap lets the buy be at least 0.01 SOL. One buy per coin, at most 0.1 SOL.
Exit fast: take profit at +15% of cost basis, cut at -20%, or sell 5 slots
after the buy was decided at the latest, and leave at once if the creator sells.

Dev-window result for this exact parameter set (own replay simulator
built from the harness economics in the kit): -0.037 SOL over 24h with
122 distinct coins round-tripped at >= 0.01 SOL and max drawdown 0.007
(about -0.009 SOL and ~30 distinct coins per 6h quarter, the size of the
final window; one quarter was +0.024). Every earlier family tested on
the same window lost more: plain momentum -0.78 to -9.38 SOL, buy-everything
-0.29 to -2.57 SOL, wide-filter sniper -0.30 SOL. The edge here is small
and the final tape is unseen, so the design keeps stakes tiny, holds
short, and never chases a pump.

Pure standard library. Deterministic: no clock, no environment, no
randomness, no hash-order dependence -- iteration over held positions is
sorted, entries follow the batch's event order, client ids are a
sequential counter. Two runs over the same replay produce byte-identical
action logs.
"""

import json
import sys

PROTOCOL = "agent-fun/replay/v2"
BPS = 10_000
LAMPORTS_PER_SOL = 1_000_000_000

# --- tuned parameters (see module docstring for the evidence) ---
MAX_BUYERS = 1                    # distinct non-creator buyers before entry
MIN_DEV_BUY = 100_000_000        # creator buys in create slot, >= 0.1 SOL
MAX_DEV_BUY = 3_000_000_000       # creator buys in create slot, <= 3.0 SOL
MIN_REAL_RESERVES = 300_000_000  # 0.3 SOL real reserves
MAX_REAL_RESERVES = 30_000_000_000  # 30 SOL real reserves
MIN_RQ_ENTRY = 1_000_000_000     # require >= 1 SOL real reserves to buy
MIN_AGE_SLOTS = 0
MAX_AGE_SLOTS = 30
STAKE_TARGET = 100_000_000       # 0.1 SOL per coin (size cap usually binds first)
STAKE_MIN = 5_000_000             # 0.005 SOL
TAKE_PROFIT_NUM = 115            # exit when close value >= 115% of cost basis
TAKE_PROFIT_DEN = 100
STOP_LOSS_NUM = 80               # exit when close value <= 80% of cost basis
STOP_LOSS_DEN = 100
MAX_HOLD_SLOTS = 5               # never hold past this many slots
EXIT_ON_DEV_SELL = True
MAX_SLIPPAGE_BPS = 10_000


def ceil_div(a, b):
    return -(-a // b)


def sell_quote(vq, vt, tokens, proto_bps, creator_bps):
    """Lamports a sell of `tokens` returns before the tx fee, and its fees.

    Curve math from the kit harness (sim/curve.ts): q = t*vq/(vt+t),
    then protocol and creator fees are each ceil(q*bps/10000).
    """
    q = (tokens * vq) // (vt + tokens)
    fee = ceil_div(q * proto_bps, BPS) + ceil_div(q * creator_bps, BPS)
    return q, fee


class Curve(object):
    __slots__ = ("vq", "vt", "rq", "rt", "complete", "reserves_slot")

    def __init__(self):
        self.vq = 0
        self.vt = 0
        self.rq = 0
        self.rt = 0
        self.complete = False
        self.reserves_slot = -1


class Tape(object):
    """Per-mint launch facts, built only from the batch stream."""

    __slots__ = ("create_slot", "creator", "dev_buy", "buyers", "dev_sold")

    def __init__(self):
        self.create_slot = None
        self.creator = None
        self.dev_buy = 0
        self.buyers = set()
        self.dev_sold = False


class Bot(object):
    def __init__(self, hello):
        self.hello = hello
        self.caps = hello["caps"]
        self.proto_bps = int(hello["fees"]["protocol_bps_fallback"])
        self.creator_bps = int(hello["fees"]["creator_bps_fallback"])
        self.tx_fee = (int(hello["fees"]["base_fee_lamports"])
                       + int(hello["fees"]["priority_fee_lamports"]))
        self.seq = -1
        self.slot = -1
        self.cash = int(hello["capital"]["lamports"])
        self.positions = {}       # mint -> tokens (from the account)
        self.curves = {}          # mint -> Curve
        self.tape = {}            # mint -> Tape
        self.pending = {}         # client_id -> (order dict, decided slot)
        self.decided = set()      # mints entered once (never re-entered)
        self.basis = {}           # mint -> cost basis (buy quotes + tx fees)
        # mint -> slot the buy was DECIDED on. The hold clock runs from the
        # decision slot, not the fill slot: that is what the strategy was
        # tuned on (the replay simulator records the decision slot), and it
        # keeps the 5-slot hold identical in tuning and in grading.
        self.opened_slot = {}
        self.next_id = 1

    def client_id(self):
        cid = "o%d" % self.next_id
        self.next_id += 1
        return cid

    def max_buy_lamports(self, mint):
        curve = self.curves.get(mint)
        if curve is None or curve.complete:
            return 0
        return curve.rq * self.caps["max_order_bps_of_real_reserve"] // BPS

    def free_cash(self, extra_orders):
        free = self.cash
        for order, _decided in self.pending.values():
            free -= self.tx_fee
            if order["side"] == "buy":
                free -= int(order["quote_lamports"])
        for order in extra_orders:
            free -= self.tx_fee
            if order["side"] == "buy":
                free -= int(order["quote_lamports"])
        return free

    def close_value(self, mint, tokens):
        """Lamports selling `tokens` now would return, net of the tx fee."""
        curve = self.curves.get(mint)
        if curve is None or curve.vt <= 0:
            return 0
        q, fees = sell_quote(curve.vq, curve.vt, tokens,
                             self.proto_bps, self.creator_bps)
        if q > curve.rq:
            q = curve.rq
        net = q - fees - self.tx_fee
        return net if net > 0 else 0

    def apply_batch(self, batch):
        self.seq = batch["seq"]
        self.slot = batch["slot"]
        for fill in batch.get("fills", []):
            entry = self.pending.pop(fill["client_id"], None)
            if fill.get("status") != "filled":
                continue
            mint = fill["mint"]
            if fill["side"] == "buy":
                self.basis[mint] = (self.basis.get(mint, 0)
                                    + int(fill["quote_lamports"])
                                    + self.tx_fee)
                if mint not in self.opened_slot and entry is not None:
                    self.opened_slot[mint] = entry[1]
        account = batch["account"]
        self.cash = int(account["cash_lamports"])
        self.positions = {}
        for p in account.get("positions", []):
            tokens = int(p["token_amount"])
            if tokens > 0:
                self.positions[p["mint"]] = tokens
        for mint in list(self.basis):
            if mint not in self.positions:
                del self.basis[mint]
                self.opened_slot.pop(mint, None)
        for mark in batch.get("marks", []):
            mint = mark["mint"]
            curve = self.curves.get(mint)
            if curve is None:
                curve = Curve()
                self.curves[mint] = curve
            if int(mark["slot"]) < curve.reserves_slot:
                continue
            curve.vq = int(mark["virtual_quote_reserves"])
            curve.vt = int(mark["virtual_token_reserves"])
            curve.rq = int(mark["real_quote_reserves"])
            curve.rt = int(mark["real_token_reserves"])
            curve.reserves_slot = int(mark["slot"])
        for event in batch.get("events", []):
            mint = event["mint"]
            kind = event["event"]
            tape = self.tape.get(mint)
            if tape is None:
                tape = Tape()
                self.tape[mint] = tape
            if kind == "CreateEvent":
                curve = self.curves.get(mint)
                if curve is None:
                    curve = Curve()
                    self.curves[mint] = curve
                curve.vq = int(event["virtual_quote_reserves"])
                curve.vt = int(event["virtual_token_reserves"])
                curve.rq = 0
                curve.rt = int(event["real_token_reserves"])
                curve.reserves_slot = int(event["slot"])
                tape.create_slot = int(event["slot"])
                tape.creator = event["creator"]
            elif kind == "TradeEvent":
                curve = self.curves.get(mint)
                if curve is None:
                    curve = Curve()
                    self.curves[mint] = curve
                curve.vq = int(event["virtual_quote_reserves"])
                curve.vt = int(event["virtual_token_reserves"])
                curve.rq = int(event["real_quote_reserves"])
                curve.rt = int(event["real_token_reserves"])
                curve.reserves_slot = int(event["slot"])
                user = event["user"]
                if tape.creator is not None and user == tape.creator:
                    if event["is_buy"]:
                        if int(event["slot"]) == tape.create_slot:
                            tape.dev_buy += int(event["sol_amount"])
                    else:
                        tape.dev_sold = True
                elif event["is_buy"]:
                    tape.buyers.add(user)
            elif kind == "CompleteEvent":
                curve = self.curves.get(mint)
                if curve is None:
                    curve = Curve()
                    self.curves[mint] = curve
                curve.complete = True

    def decide(self, batch):
        orders = []
        max_orders = self.caps["max_orders_per_batch"]
        pending_sell = set(o["mint"] for o, _ in self.pending.values()
                           if o["side"] == "sell")
        # --- exits first (sorted mint order: deterministic) ---
        for mint in sorted(self.positions):
            if mint in pending_sell:
                continue
            tokens = self.positions[mint]
            basis = self.basis.get(mint)
            if basis is None:
                # Position without a tracked fill (should not happen):
                # fall back to the account cost plus one tx fee, and treat
                # it as freshly opened so the hold rule still applies.
                basis = 0
                for p in batch["account"].get("positions", []):
                    if p["mint"] == mint:
                        basis = int(p["cost_lamports"]) + self.tx_fee
                        break
                opened = self.slot
            else:
                opened = self.opened_slot.get(mint, self.slot)
            value = self.close_value(mint, tokens)
            sell = False
            if value * TAKE_PROFIT_DEN >= basis * TAKE_PROFIT_NUM:
                sell = True
            elif value * STOP_LOSS_DEN <= basis * STOP_LOSS_NUM:
                sell = True
            elif self.slot - opened >= MAX_HOLD_SLOTS:
                sell = True
            elif EXIT_ON_DEV_SELL:
                tape = self.tape.get(mint)
                if tape is not None and tape.dev_sold:
                    sell = True
            if sell and len(orders) < max_orders:
                orders.append({
                    "side": "sell",
                    "client_id": self.client_id(),
                    "mint": mint,
                    "token_amount": str(tokens),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                })
        if len(orders) >= max_orders:
            return orders
        # --- entries (batch event order: deterministic) ---
        committed = len(self.positions) + sum(
            1 for o, _ in self.pending.values() if o["side"] == "buy")
        for event in batch.get("events", []):
            if len(orders) >= max_orders:
                break
            mint = event["mint"]
            if mint in self.decided or mint in self.positions:
                continue
            curve = self.curves.get(mint)
            if curve is None or curve.complete:
                continue
            tape = self.tape.get(mint)
            if tape is None or tape.create_slot is None:
                continue
            age = self.slot - tape.create_slot
            if age < MIN_AGE_SLOTS or age > MAX_AGE_SLOTS:
                continue
            if curve.rq < MIN_REAL_RESERVES or curve.rq > MAX_REAL_RESERVES:
                continue
            if curve.rq < MIN_RQ_ENTRY:
                continue
            if tape.dev_buy < MIN_DEV_BUY or tape.dev_buy > MAX_DEV_BUY:
                continue
            if tape.dev_sold:
                continue
            if len(tape.buyers) > MAX_BUYERS:
                continue
            cap = self.max_buy_lamports(mint)
            if cap < STAKE_MIN:
                continue
            stake = STAKE_TARGET if STAKE_TARGET < cap else cap
            if committed >= self.caps["max_open_positions"]:
                break
            if self.free_cash(orders) < stake + self.tx_fee:
                continue
            self.decided.add(mint)
            orders.append({
                "side": "buy",
                "client_id": self.client_id(),
                "mint": mint,
                "quote_lamports": str(stake),
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            })
            committed += 1
        return orders

    def record_orders(self, orders):
        for order in orders:
            self.pending[order["client_id"]] = (order, self.slot)


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            bot = Bot(msg)
        elif kind == "batch":
            bot.apply_batch(msg)
            orders = bot.decide(msg)
            bot.record_orders(orders)
            reply = json.dumps({"type": "reply", "seq": msg["seq"],
                                "orders": orders},
                               separators=(",", ":"), ensure_ascii=False)
            out.write(reply + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    main()
