# desk-poller

24/7 public-data observer for a **paper-only** meme-coin research desk. No exchange keys, no wallets, no orders, no secrets.

- `poller.py` (Python stdlib only) runs every 5 minutes on GitHub Actions (`*/5`, plus manual `workflow_dispatch`).
- Reads `inputs.json` from the `inputs` branch: token mints of open paper positions, pump.fun bonding curves,
  public wallet addresses to observe, and wallets to scan.
- Writes `poll.json` to the `data` branch (one force-updated commit, only when the data changed): timestamped DexScreener
  quotes with pool reserves, bonding-curve account data, SOL/USD, leader swaps seen on public RPC, wallet-history swaps.
- The owner's machine is the only ledger writer. It replays these observations and books paper exits at the observed
  time and price ("poller-seen").

Public sources: DexScreener, Solana public RPC (publicnode, mainnet-beta, rate-limited to ~3 calls/s and a per-run budget),
RugCheck public report, Bitget/Coinbase public SOL ticker. Nothing here is investment advice.

Note: GitHub pauses scheduled workflows in public repos after 60 days without repository activity.
