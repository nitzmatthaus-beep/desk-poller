#!/usr/bin/env python3
"""desk-poller: 24/7 public-data observer for a PAPER-ONLY meme desk. Python stdlib only.
No keys, no wallets, no orders. It only OBSERVES and writes poll.json; the owner's box is the sole ledger writer.

Inputs (branch 'inputs', written by the box): open position mints, pump.fun bonding curves, copy leaders, wallets to scan.
Output (branch 'data', one force-updated commit, only when the data changed): timestamped samples the box replays.
Public sources: DexScreener tokens/v1 (<=30 mints/call), Solana public RPC (publicnode, then mainnet-beta),
RugCheck public report, Bitget public SOL ticker (Coinbase public spot as fallback).
"""
import base64, json, sys, time, urllib.request, urllib.error

DS = "https://api.dexscreener.com/tokens/v1/solana/"
RPCS = ["https://solana-rpc.publicnode.com", "https://api.mainnet-beta.solana.com"]
QUOTE_MINTS = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
WSOL, USDC, USDT = sorted(QUOTE_MINTS, key=lambda m: ("So1" not in m, "EPj" not in m))
RPC_GAP_S = 0.30          # <= ~3 calls/s, well under public RPC limits
DEFAULT_BUDGET = 120      # RPC calls per run (leaders first, then wallet scan)
KEEP_SAMPLE_S = 6 * 3600  # sample history kept regardless of ack
MAX_RUNS = 400
COPY_MAX_DELAY_S = 120
SIG_PAGE, SCAN_MAX_TX, SCAN_DAYS = 200, 300, 30
UA = {"User-Agent": "desk-poller/1 (paper-only public data)", "Accept": "application/json", "Content-Type": "application/json"}

STATS = {"rpc_calls": 0, "http": 0, "errors": []}


def err(msg):
    STATS["errors"].append(str(msg)[:160])


def http(url, body=None, timeout=20):
    STATS["http"] += 1
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception as e:
        return None, type(e).__name__


_last_rpc = [0.0]
def rpc(method, params):
    last = None
    for url in RPCS:
        for i in range(2):
            wait = RPC_GAP_S - (time.time() - _last_rpc[0])
            if wait > 0: time.sleep(wait)
            _last_rpc[0] = time.time(); STATS["rpc_calls"] += 1
            j, st = http(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            if st == 200 and isinstance(j, dict):
                if "error" not in j: return j["result"]
                last = str(j["error"])[:120]
                if (j["error"] or {}).get("code") not in (429, -32429, -32005): break
            else:
                last = f"http {st}"
            time.sleep(1.0 * (i + 1))
    raise RuntimeError(f"rpc {method}: {last}")


def sol_usd():
    j, st = http("https://api.bitget.com/api/v2/spot/market/tickers?symbol=SOLUSDT")
    try: return float(j["data"][0]["lastPr"]), "bitget"
    except Exception: pass
    j, st = http("https://api.coinbase.com/v2/prices/SOL-USD/spot")
    try: return float(j["data"]["amount"]), "coinbase"
    except Exception: return None, None


def _f(x):
    try: return float(x or 0)
    except Exception: return 0.0


def ds_best(mints):
    """{mint: (status, best pair or None)}. Best = deepest liquidity (same as the box's best_pair)."""
    out = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        d, st = http(DS + ",".join(chunk))
        best = {}
        for p in (d or []) if st == 200 else []:
            m = (p.get("baseToken") or {}).get("address")
            if m and (m not in best or _f((p.get("liquidity") or {}).get("usd")) > _f((best[m].get("liquidity") or {}).get("usd"))):
                best[m] = p
        for m in chunk: out[m] = (st, best.get(m))
        if st != 200: err(f"dexscreener http {st}")
    return out


def pair_row(ts, st, p):
    """[ts, http status, priceUsd, liq_usd, vol_h6, vol_h24, base_reserve_tokens, dexId] (reserves for constant-product impact)."""
    if not p: return [ts, st, None, None, None, None, None, None]
    v = p.get("volume") or {}; L = p.get("liquidity") or {}
    return [ts, st, _f(p.get("priceUsd")) or None, _f(L.get("usd")), _f(v.get("h6")), _f(v.get("h24")), _f(L.get("base")) or None, p.get("dexId")]


def parse_tx(tx, wallet, solpx):
    """Identical to nevsky desk_run.parse_tx: wallet's single-token swap leg, priced by SOL/WSOL/USDC/USDT deltas."""
    if not tx or (tx.get("meta") or {}).get("err") is not None: return []
    meta = tx["meta"]; keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    d = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != wallet: continue
            d[b["mint"]] = d.get(b["mint"], 0) + sign * float(b["uiTokenAmount"].get("uiAmount") or 0)
    sol = 0.0
    if wallet in keys:
        i = keys.index(wallet); sol = (meta["postBalances"][i] - meta["preBalances"][i] + (meta.get("fee", 0) if i == 0 else 0)) / 1e9
    q_usd = (sol + d.get("So11111111111111111111111111111111111111112", 0)) * solpx
    q_usd += d.get("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 0) + d.get("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 0)
    legs = [(m, v) for m, v in d.items() if m not in QUOTE_MINTS and abs(v) > 0]
    if len(legs) != 1 or abs(q_usd) < 1: return []
    m, v = legs[0]
    post = sum(float(b["uiTokenAmount"].get("uiAmount") or 0) for b in meta.get("postTokenBalances") or [] if b.get("owner") == wallet and b["mint"] == m)
    if v > 0 and q_usd < 0: return [(m, "buy", -q_usd, v, post)]
    if v < 0 and q_usd > 0: return [(m, "sell", q_usd, -v, post)]
    return []


def get_tx(sig):
    return rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}])


def rug_report(mint):
    """Same field set as the box's report(); raw RugCheck JSON is not stored."""
    d, st = http(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
    if st != 200 or not isinstance(d, dict): return {"error": f"rugcheck status {st}"}
    holders = d.get("topHolders") or []; risks = d.get("risks") or []; token = d.get("token") or {}
    return {"mint": mint, "symbol": (d.get("tokenMeta") or {}).get("symbol"), "risk_score": d.get("score_normalised"),
            "danger": [x.get("name") for x in risks if x.get("level") == "danger"],
            "warn": [x.get("name") for x in risks if x.get("level") == "warn"], "lp_locked_pct": d.get("lpLockedPct"),
            "top10_pct": round(sum(h.get("pct", 0) for h in holders[:10]), 2) if holders else None,
            "insider_nets": len(d.get("insiderNetworks") or []), "mint_authority": token.get("mintAuthority"),
            "freeze_authority": token.get("freezeAuthority")}


def run(inp, prev, now=None):
    now = int(now or time.time()); t0 = time.time()
    out = {"v": 1, "ds": {}, "curves": {}, "sol": [], "events": [], "lw": {}, "swaps": [], "ws": {}, "runs": []}
    for k in out:
        if k != "v" and isinstance(prev.get(k), type(out[k])): out[k] = prev[k]
    ack = int(inp.get("ack") or 0); budget = int(inp.get("budget") or DEFAULT_BUDGET)
    keep = lambda ts: ts >= now - KEEP_SAMPLE_S or ts > ack
    # 1) prices of open positions (DexScreener, one batch call per 30 mints)
    mints = sorted(set(inp.get("mints") or []))
    if mints:
        for m, (st, p) in ds_best(mints).items():
            out["ds"].setdefault(m, []).append(pair_row(now, st, p))
    out["ds"] = {m: [r for r in rows if keep(r[0])][-120:] for m, rows in out["ds"].items() if m in mints or any(keep(r[0]) for r in rows)}
    solpx, src = sol_usd()
    if solpx: out["sol"].append([now, solpx, src])
    else: err("sol price unavailable")
    out["sol"] = [r for r in out["sol"] if keep(r[0])][-150:]
    # 2) pump.fun bonding curves (issa), raw account data so the box parses it with its own parse_curve
    curves = sorted(set(inp.get("curves") or []))
    if curves:
        try:
            vals = rpc("getMultipleAccounts", [curves[:100], {"encoding": "base64"}])["value"]
            for a, v in zip(curves[:100], vals):
                out["curves"].setdefault(a, []).append([now, 1, (v or {}).get("data", [None])[0] if v else None])
        except Exception as e:
            err(e)
            for a in curves: out["curves"].setdefault(a, []).append([now, 0, None])
    out["curves"] = {a: [r for r in rows if keep(r[0])][-120:] for a, rows in out["curves"].items() if a in curves or any(keep(r[0]) for r in rows)}
    # 3) copy leaders: new swaps since our own cursor (baseline on first sight, never history)
    used0 = STATS["rpc_calls"]
    for l in inp.get("leaders") or []:
        h = l["h"]; last = out["lw"].get(h) or l.get("until")
        if STATS["rpc_calls"] - used0 >= budget or not solpx: break
        try:
            opt = {"limit": 25}
            if last: opt["until"] = last
            sigs = rpc("getSignaturesForAddress", [h, opt])
            if sigs: out["lw"][h] = sigs[0]["signature"]
            if not last: continue
            for s in reversed(sigs):
                if STATS["rpc_calls"] - used0 >= budget: break
                if s.get("err") is not None: continue
                for m, k, usd, tok, post in parse_tx(get_tx(s["signature"]), h, solpx):
                    seen = int(time.time()); bt = s.get("blockTime") or seen
                    ev = {"h": h, "mint": m, "kind": k, "usd": round(usd, 2), "tok": tok, "post": post, "bt": bt, "sig": s["signature"], "seen": seen}
                    st, p = ds_best([m])[m]; ev["pair"] = pair_row(seen, st, p)
                    if k == "buy" and seen - bt <= COPY_MAX_DELAY_S: ev["facts"] = rug_report(m)
                    out["events"].append(ev)
        except Exception as e:
            err(e)
    out["events"] = [e for e in out["events"] if e["seen"] > ack][-500:]
    # 4) wallet history scan for the copy-board (budget left over after leaders)
    for w in inp.get("scan") or []:
        if STATS["rpc_calls"] - used0 >= budget or not solpx: break
        wid = w["w"]; st = out["ws"].get(wid)
        if st is None or st[2] < int(w.get("n_tx") or 0):
            st = [w.get("before"), 0, int(w.get("n_tx") or 0), None, now]
        if st[1]: continue
        before, done, n_tx, oldest = st[0], 0, st[2], st[3]
        try:
            opt = {"limit": SIG_PAGE}
            if before: opt["before"] = before
            sigs = rpc("getSignaturesForAddress", [wid, opt]); o2 = None
            for s in sigs:
                if STATS["rpc_calls"] - used0 >= budget: break
                o2 = s.get("blockTime")
                if o2 and now - o2 > SCAN_DAYS * 86400: sigs = []; break
                before = s["signature"]
                if s.get("err") is not None: continue
                for m, k, usd, tok, _ in parse_tx(get_tx(s["signature"]), wid, solpx):
                    out["swaps"].append([s["signature"], wid, m, k, round(usd, 4), tok, s.get("blockTime") or 0, now])
                n_tx += 1
            oldest = o2 or oldest
            done = int(not sigs or len(sigs) < SIG_PAGE or bool(oldest and now - oldest > SCAN_DAYS * 86400) or n_tx >= SCAN_MAX_TX)
        except Exception as e:
            err(e)
        out["ws"][wid] = [before, done, n_tx, oldest, now]
    out["swaps"] = [r for r in out["swaps"] if r[7] > ack][-20000:]
    out["ws"] = {k: v for k, v in out["ws"].items() if v[4] > ack or not v[1]}
    out["runs"] = (out["runs"] + [[now, round(time.time() - t0, 1), STATS["rpc_calls"], len(STATS["errors"])]])[-MAX_RUNS:]
    out["run"] = {"ts": now, "dur_s": round(time.time() - t0, 1), "rpc_calls": STATS["rpc_calls"], "http": STATS["http"],
                  "errors": STATS["errors"][:10], "n_mints": len(mints), "n_curves": len(curves), "inputs_at": inp.get("at"), "ack": ack}
    return out


def changed(a, b):
    """Data changed = anything besides the run bookkeeping."""
    strip = lambda o: {k: v for k, v in (o or {}).items() if k not in ("run", "runs")}
    return json.dumps(strip(a), sort_keys=True) != json.dumps(strip(b), sort_keys=True)


def main(argv):
    inp = json.load(open(argv[1])) if len(argv) > 1 else {}
    try: prev = json.load(open(argv[2])) if len(argv) > 2 else {}
    except Exception: prev = {}
    out = run(inp, prev)
    json.dump(out, open(argv[3] if len(argv) > 3 else "poll.json", "w"), separators=(",", ":"))
    ch = changed(out, prev)
    print(json.dumps({"changed": ch, **{k: out["run"][k] for k in ("ts", "dur_s", "rpc_calls", "n_mints", "n_curves")}, "errors": len(out["run"]["errors"])}))
    return 0 if ch else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
