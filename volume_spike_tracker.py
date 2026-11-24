#!/usr/bin/env python3
"""
CoinGecko Volume Spike & Top Gainers Tracker
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Set, Tuple

# Default configuration
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
PRO_BASE_URL = "https://pro-api.coingecko.com/api/v3"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_COINS_FILE = os.path.join(BASE_DIR, "seen_alerts.json")
ALERTS_CSV_FILE = os.path.join(BASE_DIR, "alerts_history.csv")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def fetch_json(url: str, api_key: str = None, is_pro: bool = False) -> any:
    """Fetch JSON data from CoinGecko API using standard library."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "CoinGeckoVolumeTracker/1.0"
    }
    if api_key:
        header_key = "x-cg-pro-api-key" if is_pro else "x-cg-demo-api-key"
        headers[header_key] = api_key

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            print("[!] Rate limited by CoinGecko API (HTTP 429). Waiting before retry...", file=sys.stderr)
        else:
            print(f"[!] HTTP Error {e.code} fetching {url}: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[!] Error fetching {url}: {e}", file=sys.stderr)
        return None


def get_top_gainers_via_pro_endpoint(api_key: str, is_pro: bool) -> List[Dict]:
    """
    Fetch top gainers from CoinGecko's dedicated /coins/top_gainers_losers endpoint
    (Requires Demo/Pro API key depending on plan tier).
    """
    base = PRO_BASE_URL if is_pro else COINGECKO_BASE_URL
    params = urllib.parse.urlencode({
        "vs_currency": "usd",
        "duration": "24h",
        "top_coins": "1000"
    })
    url = f"{base}/coins/top_gainers_losers?{params}"
    data = fetch_json(url, api_key=api_key, is_pro=is_pro)
    if data and "top_gainers" in data:
        return data["top_gainers"]
    return []


def get_markets_data(api_key: str = None, is_pro: bool = False, pages: int = 4) -> List[Dict]:
    """
    Fetch market data (price, 24h volume, market cap, 24h price change) from the
    public /coins/markets endpoint for the Top 1,000 coins by Market Cap (matching
    CoinGecko's Top Gainers page pool).
    """
    base = PRO_BASE_URL if is_pro else COINGECKO_BASE_URL
    all_coins = []

    for page in range(1, pages + 1):
        params = urllib.parse.urlencode({
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
            "sparkline": "false",
            "price_change_percentage": "1h,24h"
        })
        url = f"{base}/coins/markets?{params}"
        data = fetch_json(url, api_key=api_key, is_pro=is_pro)
        if not data or not isinstance(data, list):
            break
        all_coins.extend(data)
        if page < pages:
            time.sleep(2.0)  # Respect public rate limits across 4 pages

    return all_coins


def notify_mac(title: str, message: str):
    """Send a native macOS desktop notification."""
    if sys.platform != "darwin":
        return
    try:
        script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def send_webhook(webhook_url: str, content: str):
    """Send alert to a Discord or Slack compatible webhook."""
    if not webhook_url:
        return
    payload = json.dumps({"content": content, "text": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "CoinGeckoVolumeTracker/1.0"},
        method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[!] Failed to send webhook notification: {e}", file=sys.stderr)


def get_telegram_chat_ids(bot_token: str) -> List[str]:
    """Auto-detect all Telegram chat_ids that have messaged the bot."""
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    data = fetch_json(url)
    found = []
    if data and data.get("ok") and data.get("result"):
        for update in data["result"]:
            msg = update.get("message") or update.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                cid = str(chat["id"])
                if cid not in found:
                    found.append(cid)
    return found


def get_telegram_chat_id(bot_token: str) -> str:
    """Return comma-separated list of all detected chat_ids."""
    ids = get_telegram_chat_ids(bot_token)
    return ",".join(ids) if ids else None


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    """Send a formatted message via Telegram Bot API to one or more comma-separated chat_ids."""
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chat_ids = [c.strip() for c in str(chat_id).split(",") if c.strip()]
    any_ok = False
    for cid in chat_ids:
        payload = json.dumps({
            "chat_id": cid,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "CoinGeckoVolumeTracker/1.0"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    any_ok = True
        except Exception as e:
            print(f"[!] Failed to send Telegram notification to {cid}: {e}", file=sys.stderr)
    return any_ok


def load_config() -> Dict[str, str]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(cfg: Dict[str, str]):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[!] Could not save config: {e}", file=sys.stderr)


def load_seen_coins() -> Dict[str, float]:
    if os.path.exists(SEEN_COINS_FILE):
        try:
            with open(SEEN_COINS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_seen_coins(seen: Dict[str, float]):
    try:
        with open(SEEN_COINS_FILE, "w") as f:
            json.dump(seen, f, indent=2)
    except Exception as e:
        print(f"[!] Could not save seen alerts: {e}", file=sys.stderr)


def log_alerts_to_spreadsheet(new_alerts: List[Dict], sheet_webhook_url: str = None):
    """
    Append newly alerted coins to alerts_history.csv (for Excel / Google Sheets IMPORTDATA)
    and optionally push them directly to a Google Sheets Apps Script Web App URL.
    """
    if not new_alerts:
        return

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    headers = [
        "Date / Time (UTC)",
        "Top Gainer Rank",
        "Crypto",
        "Symbol",
        "Price (USD)",
        "1h Change (%)",
        "24h Change (%)",
        "24h Volume (USD)",
        "Market Cap (USD)",
        "Vol / MCap (%)",
        "CoinGecko URL",
    ]

    file_exists = os.path.exists(ALERTS_CSV_FILE) and os.path.getsize(ALERTS_CSV_FILE) > 0
    rows_to_add = []
    for c in new_alerts:
        c1h_val = f"{c['change_1h']:+.2f}%" if c.get("change_1h") is not None else "N/A"
        c24h_val = f"{c['change_24h']:+.2f}%"
        vol_mcap_pct = f"{c['vol_mcap_ratio'] * 100:.1f}%" if c.get("market_cap", 0) > 0 else "N/A"
        row = [
            now_str,
            f"#{c['gainer_rank']}",
            f"{c['name']} ({c['symbol']})",
            c["symbol"],
            c["price"],
            c1h_val,
            c24h_val,
            round(c["volume_24h"], 2),
            round(c["market_cap"], 2),
            vol_mcap_pct,
            c["url"],
        ]
        rows_to_add.append(row)

    try:
        with open(ALERTS_CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(headers)
            writer.writerows(rows_to_add)
        print(f"[+] Logged {len(rows_to_add)} alert(s) to {ALERTS_CSV_FILE}")
    except Exception as e:
        print(f"[!] Failed to write to {ALERTS_CSV_FILE}: {e}", file=sys.stderr)

    if sheet_webhook_url:
        payload = json.dumps({"headers": headers, "rows": rows_to_add}).encode("utf-8")
        req = urllib.request.Request(
            sheet_webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "CoinGeckoVolumeTracker/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if 200 <= resp.status < 400:
                    print(f"[+] Appended {len(rows_to_add)} row(s) to Google Sheet via webhook.")
        except Exception as e:
            print(f"[!] Failed to append to Google Sheet webhook: {e}", file=sys.stderr)


def analyze_coins(
    coins: List[Dict],
    top_gainers_limit: int = 30,
    min_price_gain: float = 10.0,
    min_vol_mcap_ratio: float = 0.10,
    min_volume_usd: float = 1_000_000,
) -> List[Dict]:
    """
    Filter and rank coins that are both:
    1. In the Top Gainers list (sorted by 24h % price increase, matching CoinGecko's Top Gainers criteria of Volume > $50k)
    2. Experiencing a volume spike (high 24h volume relative to market cap and above minimum USD volume)
    """
    # CoinGecko's Top Gainers page only includes coins with 24h volume > $50,000
    eligible_for_gainers = [
        c for c in coins
        if (c.get("total_volume") or 0) >= 50_000
        and (c.get("price_change_percentage_24h") is not None)
    ]

    # Sort by 24h price change descending to identify Top Gainers
    sorted_by_gain = sorted(
        eligible_for_gainers,
        key=lambda x: x.get("price_change_percentage_24h", 0),
        reverse=True
    )

    top_gainers = sorted_by_gain[:top_gainers_limit]
    flagged = []

    for rank, coin in enumerate(top_gainers, start=1):
        price_change_24h = coin.get("price_change_percentage_24h") or 0.0
        price_change_1h = coin.get("price_change_percentage_1h_in_currency")
        volume = coin.get("total_volume") or 0.0
        mcap = coin.get("market_cap") or 0.0

        # Volume-to-Market-Cap ratio is a strong indicator of an unusual volume spike
        vol_mcap_ratio = (volume / mcap) if mcap > 0 else 0.0

        if (
            price_change_24h >= min_price_gain
            and volume >= min_volume_usd
            and (vol_mcap_ratio >= min_vol_mcap_ratio or mcap == 0)
        ):
            flagged.append({
                "gainer_rank": rank,
                "id": coin.get("id"),
                "symbol": (coin.get("symbol") or "").upper(),
                "name": coin.get("name"),
                "price": coin.get("current_price"),
                "change_1h": price_change_1h,
                "change_24h": price_change_24h,
                "volume_24h": volume,
                "market_cap": mcap,
                "vol_mcap_ratio": vol_mcap_ratio,
                "url": f"https://www.coingecko.com/en/coins/{coin.get('id')}"
            })

    return flagged


def format_currency(val: float) -> str:
    if val is None:
        return "N/A"
    if val >= 1_000_000_000:
        return f"${val / 1_000_000_000:.2f}B"
    if val >= 1_000_000:
        return f"${val / 1_000_000:.2f}M"
    if val >= 1:
        return f"${val:,.2f}"
    return f"${val:.6f}"


def run_check(args, seen_coins: Dict[str, any]) -> Tuple[List[Dict], Dict[str, any]]:
    newly_added_chat_ids = []
    if args.telegram_token:
        existing_ids = [c.strip() for c in str(args.telegram_chat_id or "").split(",") if c.strip()]
        saved_ids = seen_coins.get("_chat_ids", []) if isinstance(seen_coins.get("_chat_ids"), list) else []
        for sid in saved_ids:
            if str(sid) not in existing_ids:
                existing_ids.append(str(sid))

        detected_ids = get_telegram_chat_ids(args.telegram_token)
        for did in detected_ids:
            if did not in existing_ids:
                existing_ids.append(did)
                newly_added_chat_ids.append(did)
                print(f"[+] Connected new Telegram Chat ID: {did}")
                send_telegram(
                    args.telegram_token,
                    did,
                    "✅ <b>CoinGecko Volume Spike Bot Connected!</b>\nMonitoring Top Gainers for volume spikes..."
                )

        if existing_ids:
            args.telegram_chat_id = ",".join(existing_ids)
            seen_coins["_chat_ids"] = existing_ids
            cfg = load_config()
            if cfg.get("telegram_chat_id") != args.telegram_chat_id:
                cfg["telegram_chat_id"] = args.telegram_chat_id
                save_config(cfg)

    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Scanning CoinGecko for Top Gainers with Volume Spikes...")

    coins = get_markets_data(api_key=args.api_key, is_pro=args.pro, pages=args.pages)
    if not coins:
        print("[!] No market data returned.")
        return [], seen_coins

    alerts = analyze_coins(
        coins,
        top_gainers_limit=args.top_n,
        min_price_gain=args.min_gain,
        min_vol_mcap_ratio=args.min_vol_ratio,
        min_volume_usd=args.min_volume
    )

    if not alerts:
        print("No coins currently match all Top Gainer + Volume Spike thresholds.")
        return [], seen_coins

    # Print formatted table
    header = f"{'Rank':<5} {'Symbol':<8} {'Name':<20} {'Price':<12} {'1h %':<8} {'24h %':<9} {'24h Vol':<10} {'MCap':<10} {'Vol/MCap':<9}"
    print("-" * len(header))
    print(header)
    print("-" * len(header))

    new_alerts = []
    now_ts = time.time()

    for c in alerts:
        c1h = f"{c['change_1h']:+.1f}%" if c['change_1h'] is not None else "N/A"
        c24h = f"{c['change_24h']:+.1f}%"
        ratio_str = f"{c['vol_mcap_ratio'] * 100:.0f}%" if c['market_cap'] > 0 else "N/A"

        print(
            f"#{c['gainer_rank']:<4} "
            f"{c['symbol']:<8} "
            f"{(c['name'] or '')[:19]:<20} "
            f"{format_currency(c['price']):<12} "
            f"{c1h:<8} "
            f"{c24h:<9} "
            f"{format_currency(c['volume_24h']):<10} "
            f"{format_currency(c['market_cap']):<10} "
            f"{ratio_str:<9}"
        )

        # Deduplicate alerts within cooldown window (default 6 hours)
        last_alerted = seen_coins.get(c["id"], 0)
        if now_ts - last_alerted > args.cooldown * 3600:
            new_alerts.append(c)
            if not args.telegram_token or args.telegram_chat_id:
                seen_coins[c["id"]] = now_ts

    print("-" * len(header))

    if new_alerts:
        log_alerts_to_spreadsheet(new_alerts, getattr(args, "sheet_webhook", None))
        for c in new_alerts:
            msg = (
                f"🚀 {c['symbol']} ({c['name']}) is #{c['gainer_rank']} on Top Gainers! "
                f"24h: +{c['change_24h']:.1f}% | Vol: {format_currency(c['volume_24h'])} "
                f"(Vol/MCap: {c['vol_mcap_ratio']*100:.0f}%)"
            )
            if args.desktop_notify:
                notify_mac("CoinGecko Volume Spike Alert", msg)
            if args.webhook:
                send_webhook(args.webhook, f"{msg}\n{c['url']}")
            if args.telegram_token and args.telegram_chat_id:
                c1h_str = f"{c['change_1h']:+.1f}%" if c['change_1h'] is not None else "N/A"
                tg_html = (
                    f"🚀 <b>{c['symbol']} ({c['name']})</b> — #{c['gainer_rank']} Top Gainer\n"
                    f"💰 <b>Price:</b> {format_currency(c['price'])}\n"
                    f"📈 <b>24h Change:</b> +{c['change_24h']:.1f}% (1h: {c1h_str})\n"
                    f"📊 <b>24h Volume:</b> {format_currency(c['volume_24h'])}\n"
                    f"🏦 <b>Market Cap:</b> {format_currency(c['market_cap'])} (Vol/MCap: {c['vol_mcap_ratio']*100:.0f}%)\n"
                    f"🔗 <a href=\"{c['url']}\">View on CoinGecko</a>"
                )
                send_telegram(args.telegram_token, args.telegram_chat_id, tg_html)
    else:
        if alerts and not os.path.exists(ALERTS_CSV_FILE):
            # Seed initial CSV with currently active alerts if CSV doesn't exist yet
            log_alerts_to_spreadsheet(alerts, getattr(args, "sheet_webhook", None))
        if newly_added_chat_ids and alerts and args.telegram_token:
            new_targets = ",".join(newly_added_chat_ids)
            for c in alerts:
                c1h_str = f"{c['change_1h']:+.1f}%" if c['change_1h'] is not None else "N/A"
                tg_html = (
                    f"🚀 <b>{c['symbol']} ({c['name']})</b> — #{c['gainer_rank']} Top Gainer\n"
                    f"💰 <b>Price:</b> {format_currency(c['price'])}\n"
                    f"📈 <b>24h Change:</b> +{c['change_24h']:.1f}% (1h: {c1h_str})\n"
                    f"📊 <b>24h Volume:</b> {format_currency(c['volume_24h'])}\n"
                    f"🏦 <b>Market Cap:</b> {format_currency(c['market_cap'])} (Vol/MCap: {c['vol_mcap_ratio']*100:.0f}%)\n"
                    f"🔗 <a href=\"{c['url']}\">View on CoinGecko</a>"
                )
                send_telegram(args.telegram_token, new_targets, tg_html)

    save_seen_coins(seen_coins)
    return alerts, seen_coins


def main():
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description="Monitor CoinGecko for Top Gainers experiencing volume spikes."
    )
    parser.add_argument("--min-gain", type=float, default=10.0, help="Minimum 24h price gain percentage (default: 10.0)")
    parser.add_argument("--min-vol-ratio", type=float, default=0.10, help="Minimum 24h Volume / Market Cap ratio (default: 0.10 = 10%%)")
    parser.add_argument("--min-volume", type=float, default=1_000_000, help="Minimum 24h USD trading volume (default: $1,000,000)")
    parser.add_argument("--top-n", type=int, default=30, help="Check coins within the top N gainers (default: 30)")
    parser.add_argument("--pages", type=int, default=4, help="Number of 250-coin pages to fetch by market cap (default: 4 = top 1000 coins)")
    parser.add_argument("--interval", type=int, default=0, help="Poll interval in seconds. Set > 0 (e.g. 300) for continuous monitoring.")
    parser.add_argument("--cooldown", type=float, default=6.0, help="Hours before re-alerting on the same coin (default: 6)")
    parser.add_argument("--webhook", type=str, default=os.environ.get("CG_WEBHOOK_URL") or cfg.get("webhook"), help="Discord/Slack webhook URL for notifications")
    parser.add_argument("--sheet-webhook", type=str, default=os.environ.get("GOOGLE_SHEET_WEBHOOK_URL") or cfg.get("sheet_webhook"), help="Google Sheets Apps Script Web App URL for logging alerts")
    parser.add_argument("--telegram-token", type=str, default=os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_token"), help="Telegram Bot Token from @BotFather")
    parser.add_argument("--telegram-chat-id", type=str, default=os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id"), help="Telegram Chat ID (auto-detected if omitted after messaging your bot)")
    parser.add_argument("--test-telegram", action="store_true", help="Send a test alert to your Telegram bot and exit")
    parser.add_argument("--api-key", type=str, default=os.environ.get("CG_API_KEY") or cfg.get("api_key"), help="Optional CoinGecko Demo/Pro API key")
    parser.add_argument("--pro", action="store_true", help="Use Pro API URL with --api-key")
    parser.add_argument("--desktop-notify", action="store_true", help="Enable macOS desktop notifications (disabled by default)")
    parser.add_argument("--no-desktop-notify", action="store_true", help="Disable macOS desktop notifications (default)")

    args = parser.parse_args()

    # Auto-detect Telegram Chat ID if token is provided without chat_id
    if args.telegram_token and not args.telegram_chat_id:
        print("Detecting your Telegram Chat ID from recent messages sent to your bot...")
        detected_id = get_telegram_chat_id(args.telegram_token)
        if detected_id:
            args.telegram_chat_id = detected_id
            print(f"[+] Found Telegram Chat ID: {detected_id}")
        else:
            print("[!] Could not auto-detect your Chat ID. Please open your bot in Telegram, press 'Start' (or send it 'hello'), and re-run!")

    # Save credentials to config.json so the user doesn't have to re-type them next time
    updated_cfg = False
    for key in ("telegram_token", "telegram_chat_id", "webhook", "sheet_webhook", "api_key"):
        val = getattr(args, key, None)
        if val and cfg.get(key) != val:
            cfg[key] = val
            updated_cfg = True
    if updated_cfg:
        save_config(cfg)
        print(f"[+] Saved your settings to {CONFIG_FILE}")

    if args.test_telegram:
        if not args.telegram_token or not args.telegram_chat_id:
            print("[!] Missing --telegram-token or --telegram-chat-id.")
            sys.exit(1)
        ok = send_telegram(
            args.telegram_token,
            args.telegram_chat_id,
            "✅ <b>CoinGecko Volume Spike Bot Connected!</b>\nYou will receive alerts here whenever a Top Gainer spikes in price and volume."
        )
        if ok:
            print("[+] Test message sent to your Telegram successfully!")
        else:
            print("[!] Failed to send test message.")
        return

    seen_coins = load_seen_coins()

    if args.interval > 0:
        print(f"Starting continuous monitoring every {args.interval} seconds. Press Ctrl+C to stop.")
        try:
            while True:
                run_check(args, seen_coins)
                elapsed = 0
                while elapsed < args.interval:
                    sleep_step = 5 if (args.telegram_token and not args.telegram_chat_id) else min(30, args.interval - elapsed)
                    time.sleep(sleep_step)
                    elapsed += sleep_step
                    if args.telegram_token and not args.telegram_chat_id:
                        detected_id = get_telegram_chat_id(args.telegram_token)
                        if detected_id:
                            # Break early to immediately link and send initial alerts
                            break
        except KeyboardInterrupt:
            print("\nMonitoring stopped.")
    else:
        run_check(args, seen_coins)


if __name__ == "__main__":
    main()
