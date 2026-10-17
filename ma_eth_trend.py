# Backtest: ETH only, 5m, risk:reward 1:2, MA 9/26 cross + pullback WITH a trend filter.
# SELF-CONTAINED. Data: OKX public candles. NO real money.
# Trend filter: last CLOSED bar of a higher timeframe (15m / 1h / 4h): close above its SMA(50)
# -> only longs; below -> only shorts. No look-ahead. Compared with "none".
# Choose on the FIRST half of the period, confirm on the SECOND half (never the other way).
#   python ma_eth_trend.py 180
import requests, time, sys, csv
from datetime import datetime, timezone

OKX = "https://www.okx.com/api/v5"
LEVS = [1, 3, 5, 10, 15]
RR = 2.0
BUFS = [0.5, 1.0]
FILTERS = [("none", 0), ("15m", 15), ("1h", 60), ("4h", 240)]
TREND_N = 50
WARM_DAYS = 9   # skip the first days so every filter has enough history

CFG = dict(
    fast=9, slow=26, atr_n=14,
    atr_buf=0.1,         # stop = slow MA -/+ this many ATRs
    expire=30,           # candles to wait for the pullback after a cross
    cap=144,             # max candles in a trade (12h), then close at market
    warmup=60,           # ignore signals in the first candles
    fee_pct=0.05,        # taker fee per side
    slip_pct=0.02,       # slippage per side
    margin=20.0,         # fake dollars of own money per trade
    start_balance=100.0,
    maint_pct=0.5,       # maintenance margin for the liquidation check
)


def get_json(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=25,
                             headers={"User-Agent": "paperbot"})
            if r.status_code == 200:
                return r.json()
            print("HTTP", r.status_code, url[-30:])
            if r.status_code in (429, 403):
                time.sleep(5)
        except Exception as e:
            print("net error:", str(e)[:80])
            time.sleep(3)
    return None


def parse(c):
    return dict(ts=int(c[0]) // 1000, o=float(c[1]), h=float(c[2]),
                l=float(c[3]), c=float(c[4]), v=float(c[5]))


def fetch_history(inst, days):
    need = days * 288
    out, after = [], None
    while len(out) < need:
        p = {"instId": inst, "bar": "5m", "limit": 100}
        if after:
            p["after"] = after
        d = (get_json(OKX + "/market/history-candles", p) or {}).get("data") or []
        time.sleep(0.15)
        if not d:
            break
        out.extend(parse(c) for c in d if len(c) >= 9 and c[8] == "1")
        after = d[-1][0]
    seen, rows = set(), []
    for c in sorted(out, key=lambda x: x["ts"]):
        if c["ts"] not in seen:
            seen.add(c["ts"])
            rows.append(c)
    return rows[-need:]


# ---------- indicators and signals ----------
def sma(vals, n):
    out, run = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def atr(cs, n):
    out = [None] * len(cs)
    tr = [None] + [max(cs[i]["h"] - cs[i]["l"], abs(cs[i]["h"] - cs[i - 1]["c"]),
                       abs(cs[i]["l"] - cs[i - 1]["c"])) for i in range(1, len(cs))]
    if len(cs) <= n:
        return out
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(cs)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def find_signals(cs):
    o4 = [(c["o"] + c["h"] + c["l"] + c["c"]) / 4 for c in cs]
    f, s, a = sma(o4, CFG["fast"]), sma(o4, CFG["slow"]), atr(cs, CFG["atr_n"])
    sigs, d, ci, left = [], 0, 0, False
    for i in range(1, len(cs)):
        if None in (f[i], s[i], f[i - 1], s[i - 1], a[i]):
            continue
        if f[i - 1] <= s[i - 1] and f[i] > s[i]:
            d, ci, left = 1, i, False
            continue
        if f[i - 1] >= s[i - 1] and f[i] < s[i]:
            d, ci, left = -1, i, False
            continue
        if d == 0:
            continue
        if i - ci > CFG["expire"] or (d == 1 and f[i] <= s[i]) or (d == -1 and f[i] >= s[i]):
            d = 0
            continue
        c = cs[i]
        buf = CFG["atr_buf"] * a[i]
        if d == 1:
            if not left:
                left = c["l"] > f[i]
            elif c["c"] <= s[i]:
                d = 0
            elif c["l"] <= f[i]:
                sigs.append(dict(i=i, side=1, sl=s[i] - buf))
                d = 0
        else:
            if not left:
                left = c["h"] < f[i]
            elif c["c"] >= s[i]:
                d = 0
            elif c["h"] >= f[i]:
                sigs.append(dict(i=i, side=-1, sl=s[i] + buf))
                d = 0
    return sigs


def simulate(cs, e, side, sl, rr):
    entry = cs[e]["o"]
    risk = abs(entry - sl)
    if risk <= 0 or (side == 1 and sl >= entry) or (side == -1 and sl <= entry):
        return None
    tp = entry + side * rr * risk
    last = min(e + CFG["cap"], len(cs) - 1)
    ex, reason, xi = None, None, None
    for k in range(e, last + 1):
        c = cs[k]
        if k > e:
            if (side == 1 and c["o"] <= sl) or (side == -1 and c["o"] >= sl):
                ex, reason, xi = c["o"], "SL", k
                break
            if (side == 1 and c["o"] >= tp) or (side == -1 and c["o"] <= tp):
                ex, reason, xi = c["o"], "TP", k
                break
        hit_sl = c["l"] <= sl if side == 1 else c["h"] >= sl
        hit_tp = c["h"] >= tp if side == 1 else c["l"] <= tp
        if hit_sl:
            ex, reason, xi = sl, "SL", k
            break
        if hit_tp:
            ex, reason, xi = tp, "TP", k
            break
        if k == e + CFG["cap"]:
            ex, reason, xi = c["c"], "TIME", k
            break
    if ex is None:
        return {"open": True}
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    gross = side * (ex / entry - 1) * 100
    risk_pct = risk / entry * 100
    return dict(entry=entry, sl=sl, tp=tp, exit=ex, reason=reason, exit_idx=xi,
                risk_pct=risk_pct, gross_pct=gross, net_pct=gross - cost,
                net_R=(gross - cost) / risk_pct)


def lev_pnl(margin, lev, net_pct, risk_pct):
    if lev > 1 and risk_pct >= 100.0 / lev - CFG["maint_pct"]:
        return -margin
    return max(margin * lev * net_pct / 100, -margin)


def equity(ts, lev):
    cash, peak, maxdd, taken, wins = CFG["start_balance"], CFG["start_balance"], 0.0, 0, 0
    m = CFG["margin"]
    opened = []

    def settle(upto):
        nonlocal cash, peak, maxdd, opened
        opened.sort(key=lambda x: x[0])
        keep, closed = [], 0
        total = len(opened)
        for ex, pnl in opened:
            if ex <= upto:
                cash += m + pnl
                closed += 1
                eq = cash + m * (total - closed)
                peak = max(peak, eq)
                maxdd = max(maxdd, (peak - eq) / peak * 100)
            else:
                keep.append((ex, pnl))
        opened = keep

    for t in sorted(ts, key=lambda x: x["entry_ts"]):
        settle(t["entry_ts"])
        if cash >= m:
            cash -= m
            pnl = lev_pnl(m, lev, t["net_pct"], t["risk_pct"])
            opened.append((t["exit_ts"], pnl))
            taken += 1
            wins += 1 if pnl > 0 else 0
    settle(10 ** 12)
    return dict(final=cash, ret=(cash / CFG["start_balance"] - 1) * 100,
                dd=maxdd, taken=taken, wins=wins)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def htf_trend(cs, minutes, n=TREND_N):
    per = minutes * 60
    nb = minutes // 5
    buckets = {}
    for i, c in enumerate(cs):
        buckets.setdefault(c["ts"] // per * per, []).append(i)
    closes, info = [], []
    for st in sorted(buckets):
        idxs = buckets[st]
        if len(idxs) < nb:
            continue
        closes.append(cs[idxs[-1]]["c"])
        tr = 0
        if len(closes) >= n:
            sm = sum(closes[-n:]) / n
            tr = 1 if closes[-1] > sm else (-1 if closes[-1] < sm else 0)
        info.append((st + per, tr))
    out, k = [0] * len(cs), -1
    for i, c in enumerate(cs):
        known = c["ts"] + 300
        while k + 1 < len(info) and info[k + 1][0] <= known:
            k += 1
        out[i] = info[k][1] if k >= 0 else 0
    return out


def run(cs, buf, trend, t_start):
    CFG["atr_buf"] = buf
    trades, busy = [], -1
    for sg in find_signals(cs):
        e = sg["i"] + 1
        if e >= len(cs) or e <= busy or sg["i"] < CFG["warmup"]:
            continue
        if cs[sg["i"]]["ts"] < t_start:
            continue
        if trend is not None and trend[sg["i"]] != sg["side"]:
            continue
        t = simulate(cs, e, sg["side"], sg["sl"], RR)
        if t is None or t.get("open"):
            continue
        busy = t["exit_idx"]
        t.update(side=sg["side"], entry_ts=cs[e]["ts"], exit_ts=cs[t["exit_idx"]]["ts"])
        trades.append(t)
    return trades


def summ(ts):
    n = len(ts)
    if n == 0:
        return None
    wins = [t for t in ts if t["net_pct"] > 0]
    gp = sum(t["net_pct"] for t in wins)
    gl = -sum(t["net_pct"] for t in ts if t["net_pct"] <= 0)
    stop = sum(t["risk_pct"] for t in ts) / n
    cost = 2 * (CFG["fee_pct"] + CFG["slip_pct"])
    return dict(n=n, win=100.0 * len(wins) / n, avg=sum(t["net_pct"] for t in ts) / n,
                pf=gp / gl if gl else 99.0, stop=stop,
                be=100.0 * (1 + cost / stop) / (RR + 1))


def fmt(s):
    if s is None:
        return "n=0"
    return "n=%-4d win=%2.0f%% avg=%+.3f%% PF=%.2f be=%2.0f%%" % (
        s["n"], s["win"], s["avg"], s["pf"], s["be"])


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 180
    cs = fetch_history("ETH-USDT-SWAP", days)
    print("ETH candles: %d (%s -> %s)" % (len(cs), iso(cs[0]["ts"]) if cs else "-", iso(cs[-1]["ts"]) if cs else "-"))
    if len(cs) < 5000:
        print("not enough data")
        return
    t_start = cs[0]["ts"] + WARM_DAYS * 86400
    mid = (t_start + cs[-1]["ts"]) // 2
    print("trades from %s | split at %s | RR 1:%.0f | cost %.2f%%/trade | be = break-even win rate" % (
        iso(t_start), iso(mid), RR, 2 * (CFG["fee_pct"] + CFG["slip_pct"])))
    trends = {name: (htf_trend(cs, m) if m else None) for name, m in FILTERS}
    rows = []
    for buf in BUFS:
        for name, m in FILTERS:
            tr = run(cs, buf, trends[name], t_start)
            a = [t for t in tr if t["entry_ts"] < mid]
            b = [t for t in tr if t["entry_ts"] >= mid]
            eq = []
            for lev in (1, 3, 5):
                q = equity(b, lev)
                eq.append("x%d %+.0f%%" % (lev, q["ret"]))
                rows.append([buf, name, lev, len(a), len(b), round(q["ret"], 1), round(q["dd"], 1)])
            print("buf %.1f  filter %-4s" % (buf, name))
            print("   FIRST : " + fmt(summ(a)))
            print("   SECOND: " + fmt(summ(b)) + " | " + " ".join(eq))
    with open("ma_eth_trend_summary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["buffer_atr", "filter", "lev", "trades_first", "trades_second",
                    "return_second_pct", "max_dd_second_pct"])
        w.writerows(rows)


if __name__ == "__main__":
    main()
