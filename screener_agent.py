"""
TradingView CEX Screener Monitor Agent
=======================================
Polls the TradingView Coin Screener (CEX), detects changes between snapshots,
and sends notifications via Email (SMTP) with console fallback.

Usage:
  python screener_agent.py              # single run (use with cron)
  python screener_agent.py --daemon     # continuous loop
  python screener_agent.py --daemon --interval 300   # every 5 min

Setup:
  1. pip install tvscreener
  2. Copy .env.example to .env, fill in your SMTP + email settings
  3. Run it
"""

import os
import sys
import json
import time
import argparse
import hashlib
import logging
import resend
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import tvscreener as tvs
except ImportError:
    sys.exit("Missing dependency: pip install tvscreener")



# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
SNAPSHOT_FILE = BASE_DIR / "last_snapshot.json"
LOG_FILE = BASE_DIR / "agent.log"

# Thresholds
RANK_SHIFT_THRESHOLD = 10  # notify if a coin jumps >=10 ranks
PRICE_CHANGE_THRESHOLD = 10.0  # notify if price changed >10% between checks
VOLUME_SPIKE_THRESHOLD = 3.0  # notify if volume is 3x the previous snapshot

# How many coins to track from the screener (top N by market cap)
SCREENER_LIMIT = 200

# Columns to fetch
COLUMNS = [
    "name",
    "close",
    "change",
    "change|1W",
    "volume",
    "market_cap",
    "Recommend.All",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("screener-agent")


# ---------------------------------------------------------------------------
# Email Notification (SMTP)
# ---------------------------------------------------------------------------
def load_env():
    """Load env vars from .env file."""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def send_email(message: str, subject: str = "CEX Screener Update") -> bool:
    """Send notification email via Resend."""
    load_env()
    api_key = os.environ.get("RESEND_API_KEY", "")
    to_email = os.environ.get("TO_EMAIL", "")

    if not api_key or not to_email:
        return False

    resend.api_key = api_key

    html_body = message.replace("\n", "<br>\n")
    html = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                 max-width: 600px; margin: 0 auto; padding: 20px;
                 background: #111114; color: #e0e0e0;">
      <div style="background: #1a1a1f; border-radius: 8px; padding: 24px;
                  border: 1px solid #2a2a30;">
        {html_body}
      </div>
      <p style="color: #666; font-size: 12px; margin-top: 16px;">
        CEX Screener Agent — automated notification
      </p>
    </body>
    </html>
    """

    try:
        resend.Emails.send({
            "from": "CEX Screener <onboarding@resend.dev>",
            "to": [to_email],
            "subject": subject,
            "html": html,
        })
        log.info(f"Email sent to {to_email}")
        return True
    except Exception as e:
        log.error(f"Email send failed: {e}")
        return False


def notify(message: str):
    """Send notification via Resend (falls back to console)."""
    if not send_email(message):
        log.info(f"[NOTIFICATION — no email sent, printing to console]\n{message}")


# ---------------------------------------------------------------------------
# Screener Data Fetching
# ---------------------------------------------------------------------------
def fetch_screener_data() -> list[dict]:
    """
    Fetch current CEX coin screener data from TradingView.
    Returns a list of dicts sorted by market cap (rank = index).
    """
    # Support both old (CryptoScreener) and new (CoinScreener) tvscreener versions
    if hasattr(tvs, "CoinScreener"):
        screener = tvs.CoinScreener()
        Field = tvs.CoinField
    elif hasattr(tvs, "CryptoScreener"):
        screener = tvs.CryptoScreener()
        Field = tvs.CryptoField
    else:
        raise RuntimeError("tvscreener has no CoinScreener or CryptoScreener")

    # Select columns
    screener.select(
        Field.NAME,
        Field.CLOSE,
        Field.CHANGE,
        Field.CHANGE_1W,
        Field.VOLUME,
        Field.MARKET_CAP,
        Field.RECOMMEND_ALL,
    )

    screener.set_range(0, SCREENER_LIMIT)
    df = screener.get()

    coins = []
    for idx, row in df.iterrows():
        coin = {
            "symbol": str(idx) if isinstance(idx, str) else str(row.get("name", idx)),
            "name": str(row.get("name", "")),
            "price": float(row.get("close", 0) or 0),
            "change_24h": float(row.get("change", 0) or 0),
            "change_1w": float(row.get("change|1W", 0) or 0),
            "volume": float(row.get("volume", 0) or 0),
            "market_cap": float(row.get("market_cap", 0) or 0),
            "recommendation": str(row.get("Recommend.All", "")),
        }
        coins.append(coin)

    # Sort by market cap descending (should already be, but ensure)
    coins.sort(key=lambda c: c["market_cap"], reverse=True)

    # Add rank
    for i, coin in enumerate(coins):
        coin["rank"] = i + 1

    return coins


# ---------------------------------------------------------------------------
# Snapshot Comparison
# ---------------------------------------------------------------------------
def load_snapshot() -> Optional[dict]:
    """Load the previous snapshot from disk."""
    if SNAPSHOT_FILE.exists():
        try:
            return json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"Could not load snapshot: {e}")
    return None


def save_snapshot(coins: list[dict]):
    """Save current data as snapshot."""
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coins": coins,
    }
    SNAPSHOT_FILE.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info(f"Snapshot saved: {len(coins)} coins")


def compare_snapshots(old_coins: list[dict], new_coins: list[dict]) -> dict:
    """
    Compare two snapshots and return changes.
    """
    old_map = {c["symbol"]: c for c in old_coins}
    new_map = {c["symbol"]: c for c in new_coins}

    old_symbols = set(old_map.keys())
    new_symbols = set(new_map.keys())

    changes = {
        "added": [],       # new coins that appeared
        "removed": [],     # coins that disappeared
        "rank_shifts": [], # big ranking changes
        "price_moves": [], # significant price changes
        "volume_spikes": [],  # volume anomalies
    }

    # --- New coins ---
    for sym in new_symbols - old_symbols:
        c = new_map[sym]
        changes["added"].append(c)

    # --- Removed coins ---
    for sym in old_symbols - new_symbols:
        c = old_map[sym]
        changes["removed"].append(c)

    # --- Check existing coins for changes ---
    for sym in old_symbols & new_symbols:
        old = old_map[sym]
        new = new_map[sym]

        # Rank shift
        rank_diff = old["rank"] - new["rank"]  # positive = moved up
        if abs(rank_diff) >= RANK_SHIFT_THRESHOLD:
            changes["rank_shifts"].append({
                "symbol": sym,
                "name": new["name"],
                "old_rank": old["rank"],
                "new_rank": new["rank"],
                "shift": rank_diff,
            })

        # Price move since last check
        if old["price"] > 0:
            pct = ((new["price"] - old["price"]) / old["price"]) * 100
            if abs(pct) >= PRICE_CHANGE_THRESHOLD:
                changes["price_moves"].append({
                    "symbol": sym,
                    "name": new["name"],
                    "old_price": old["price"],
                    "new_price": new["price"],
                    "change_pct": round(pct, 2),
                })

        # Volume spike
        if old["volume"] > 0:
            vol_ratio = new["volume"] / old["volume"]
            if vol_ratio >= VOLUME_SPIKE_THRESHOLD:
                changes["volume_spikes"].append({
                    "symbol": sym,
                    "name": new["name"],
                    "old_volume": old["volume"],
                    "new_volume": new["volume"],
                    "ratio": round(vol_ratio, 1),
                })

    return changes


# ---------------------------------------------------------------------------
# Message Formatting
# ---------------------------------------------------------------------------
def format_changes(changes: dict, timestamp: str) -> Optional[str]:
    """Format changes into a readable notification message."""
    parts = []

    if changes["added"]:
        lines = [f"🟢 <b>Neue Coins im Screener ({len(changes['added'])})</b>"]
        for c in changes["added"][:15]:  # cap at 15
            lines.append(
                f"  • <b>{c['name']}</b> ({c['symbol']}) "
                f"— Rank #{c['rank']}, ${c['price']:,.4f}"
            )
        if len(changes["added"]) > 15:
            lines.append(f"  ... und {len(changes['added']) - 15} weitere")
        parts.append("\n".join(lines))

    if changes["removed"]:
        lines = [f"🔴 <b>Coins verschwunden ({len(changes['removed'])})</b>"]
        for c in changes["removed"][:15]:
            lines.append(f"  • <b>{c['name']}</b> ({c['symbol']}) — war Rank #{c['rank']}")
        if len(changes["removed"]) > 15:
            lines.append(f"  ... und {len(changes['removed']) - 15} weitere")
        parts.append("\n".join(lines))

    if changes["rank_shifts"]:
        shifts = sorted(changes["rank_shifts"], key=lambda x: abs(x["shift"]), reverse=True)
        lines = [f"📊 <b>Ranking-Shifts (≥{RANK_SHIFT_THRESHOLD} Plätze)</b>"]
        for s in shifts[:10]:
            arrow = "⬆️" if s["shift"] > 0 else "⬇️"
            lines.append(
                f"  {arrow} <b>{s['name']}</b> #{s['old_rank']} → #{s['new_rank']} "
                f"({'+' if s['shift'] > 0 else ''}{s['shift']})"
            )
        parts.append("\n".join(lines))

    if changes["price_moves"]:
        moves = sorted(changes["price_moves"], key=lambda x: abs(x["change_pct"]), reverse=True)
        lines = [f"💰 <b>Große Preisbewegungen (≥{PRICE_CHANGE_THRESHOLD}%)</b>"]
        for m in moves[:10]:
            emoji = "🚀" if m["change_pct"] > 0 else "📉"
            lines.append(
                f"  {emoji} <b>{m['name']}</b> "
                f"${m['old_price']:,.4f} → ${m['new_price']:,.4f} "
                f"({'+' if m['change_pct'] > 0 else ''}{m['change_pct']}%)"
            )
        parts.append("\n".join(lines))

    if changes["volume_spikes"]:
        spikes = sorted(changes["volume_spikes"], key=lambda x: x["ratio"], reverse=True)
        lines = [f"📈 <b>Volume Spikes (≥{VOLUME_SPIKE_THRESHOLD}x)</b>"]
        for v in spikes[:10]:
            lines.append(
                f"  • <b>{v['name']}</b> — {v['ratio']}x Volume"
            )
        parts.append("\n".join(lines))

    if not parts:
        return None

    header = f"🔔 <b>CEX Screener Update</b>\n⏰ {timestamp}\n"
    return header + "\n\n" + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Main Logic
# ---------------------------------------------------------------------------
def run_check():
    """Single check: fetch, compare, notify, save."""
    log.info("Fetching screener data...")
    try:
        new_coins = fetch_screener_data()
    except Exception as e:
        log.error(f"Failed to fetch screener data: {e}")
        notify(f"⚠️ Screener Agent Error: {e}")
        return

    log.info(f"Fetched {len(new_coins)} coins")

    old_snapshot = load_snapshot()

    if old_snapshot is None:
        log.info("No previous snapshot — saving initial baseline.")
        save_snapshot(new_coins)
        notify(
            f"✅ <b>CEX Screener Agent gestartet</b>\n"
            f"Tracking {len(new_coins)} Coins.\n"
            f"Du wirst bei Änderungen benachrichtigt."
        )
        return

    old_coins = old_snapshot["coins"]
    old_time = old_snapshot.get("timestamp", "?")

    changes = compare_snapshots(old_coins, new_coins)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    total_changes = sum(len(v) for v in changes.values())
    log.info(f"Changes detected: {total_changes}")

    if total_changes > 0:
        message = format_changes(changes, now)
        if message:
            notify(message)
    else:
        log.info("No significant changes.")

    save_snapshot(new_coins)


def daemon_loop(interval: int):
    """Run continuously with a sleep interval."""
    log.info(f"Starting daemon mode (interval: {interval}s)")
    while True:
        try:
            run_check()
        except KeyboardInterrupt:
            log.info("Shutting down.")
            break
        except Exception as e:
            log.error(f"Unexpected error: {e}")

        log.info(f"Sleeping {interval}s...")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("Shutting down.")
            break


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    global SCREENER_LIMIT, RANK_SHIFT_THRESHOLD, PRICE_CHANGE_THRESHOLD
    parser = argparse.ArgumentParser(description="TradingView CEX Screener Monitor")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument(
        "--interval", type=int, default=300,
        help="Check interval in seconds (default: 300 = 5 min)"
    )
    parser.add_argument(
        "--limit", type=int, default=SCREENER_LIMIT,
        help=f"Number of coins to track (default: {SCREENER_LIMIT})"
    )
    parser.add_argument(
        "--rank-threshold", type=int, default=RANK_SHIFT_THRESHOLD,
        help=f"Rank shift threshold for notifications (default: {RANK_SHIFT_THRESHOLD})"
    )
    parser.add_argument(
        "--price-threshold", type=float, default=PRICE_CHANGE_THRESHOLD,
        help=f"Price change %% threshold (default: {PRICE_CHANGE_THRESHOLD})"
    )
    args = parser.parse_args()

    SCREENER_LIMIT = args.limit
    RANK_SHIFT_THRESHOLD = args.rank_threshold
    PRICE_CHANGE_THRESHOLD = args.price_threshold

    if args.daemon:
        daemon_loop(args.interval)
    else:
        run_check()


if __name__ == "__main__":
    main()
