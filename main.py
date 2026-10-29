import asyncio
import aiohttp
import os
from datetime import datetime, timedelta
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse

app = FastAPI()

# ==========================================
# CONFIGURATION & $50 BANKROLL PARAMETERS
# ==========================================
INITIAL_EQUITY = 50.00           # Starting virtual balance ($50.00)
FIXED_POSITION_USD = 5.00        # Fixed size per trade ($5.00 per shot)
MAX_OPEN_POSITIONS = 1           # Only hold 1 position at a time
DEX_FEE_SLIPPAGE_PCT = 0.015     # 1.5% simulated round-trip slippage

# --- SMART FEEDBACK FILTER THRESHOLDS ---
MIN_LIQUIDITY_USD = 50000.0      # $50k+ liquidity floor
MIN_VOLUME_5M_USD = 12000.0      # $12k+ 5-minute volume
MIN_BUY_SELL_RATIO = 1.2         # 20% more buyers than sellers
BLACKLIST_COOLDOWN_HOURS = 4     # Only blacklist losers for 4 hours

# Risk Engine Thresholds
STOP_LOSS_PCT = -8.0             # Hard stop at -8%
TRAIL_ACTIVATION_PCT = 12.0      # Activate trailing stop at +12%
TRAIL_DROP_PCT = 4.0             # Sell if price drops 4% from peak after activation

# Telegram Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ==========================================
# LOGGER & ACTIVITY FEED
# ==========================================
class CloudDeskLogger:
    def __init__(self):
        self.equity = INITIAL_EQUITY
        self.open_positions = {}
        self.loser_blacklist = {}    # mint -> expiration datetime (only for losses)
        self.scanned_count = 0
        self.traded_count = 0
        self.activity_logs = []

    async def log(self, agent: str, message: str, alert_telegram: bool = False):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] [{agent}] {message}"
        print(log_entry)
        
        self.activity_logs.insert(0, log_entry)
        if len(self.activity_logs) > 30:
            self.activity_logs.pop()

        if alert_telegram and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            await self.send_telegram(f"*{agent}*: {message}")

    async def send_telegram(self, text: str):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=payload, timeout=5)
        except Exception:
            pass


logger = CloudDeskLogger()


# ==========================================
# VISUAL DASHBOARD ROUTE (HTML / HEAD)
# ==========================================
@app.get("/", response_class=HTMLResponse)
@app.head("/")
def dashboard():
    logs_html = "".join([f"<li>{log}</li>" for log in logger.activity_logs])
    positions_html = ""
    if logger.open_positions:
        for mint, pos in logger.open_positions.items():
            positions_html += f"<b>{pos['symbol']}</b> | Entry: ${pos['entry_price']:.6f} | Peak: ${pos['peak_price']:.6f}<br>"
    else:
        positions_html = "<i>No open positions. Scanning $50k+ pools ($50 Bankroll Mode)...</i>"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Trading-blue Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="10">
        <style>
            body {{ background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 20px; margin: 0; }}
            .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; margin-bottom: 15px; }}
            h2 {{ color: #58a6ff; margin-top: 0; font-size: 20px; }}
            .metric {{ font-size: 24px; font-weight: bold; color: #3fb950; }}
            ul {{ padding-left: 20px; font-family: monospace; font-size: 13px; max-height: 250px; overflow-y: auto; }}
            li {{ margin-bottom: 5px; border-bottom: 1px solid #21262d; padding-bottom: 3px; }}
        </style>
    </head>
    <body>
        <h2>⚡ Trading-blue: $50 Bankroll Sniper Desk</h2>
        
        <div class="card">
            <div>Virtual Equity Pool</div>
            <div class="metric">${logger.equity:.2f}</div>
        </div>

        <div class="card">
            <div>Bot Stats</div>
            <div>Scanned: <b>{logger.scanned_count}</b> | Traded: <b>{logger.traded_count}</b></div>
        </div>

        <div class="card">
            <div>Active Open Position</div>
            <div>{positions_html}</div>
        </div>

        <div class="card">
            <div>Live Activity Logs (Auto-refreshes every 10s)</div>
            <ul>{logs_html if logs_html else "<li>Waiting for first market scan...</li>"}</ul>
        </div>
    </body>
    </html>
    """
    return html_content


# ==========================================
# TRADING PIPELINE LOGIC ($50 POOL + $5 POSITIONS)
# ==========================================
async def scout_and_process_tokens(session: aiohttp.ClientSession):
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    mint_addresses = []

    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                items = await resp.json()
                for item in items:
                    if item.get("chainId") == "solana":
                        addr = item.get("tokenAddress")
                        if addr:
                            mint_addresses.append(addr)
    except Exception as e:
        await logger.log("SCOUT", f"Feed error: {e}")

    if not mint_addresses:
        return

    batch = list(dict.fromkeys(mint_addresses))[:10]
    pair_url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(batch)}"

    try:
        async with session.get(pair_url, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs") or []

                now = datetime.now()
                logger.loser_blacklist = {m: exp for m, exp in logger.loser_blacklist.items() if exp > now}

                for p in pairs:
                    if p.get("chainId") != "solana":
                        continue
                    logger.scanned_count += 1
                    symbol = p.get("baseToken", {}).get("symbol", "UNKNOWN")
                    mint = p.get("baseToken", {}).get("address")
                    
                    if mint in logger.loser_blacklist:
                        continue

                    liquidity = float(p.get("liquidity", {}).get("usd", 0) or 0)
                    volume_5m = float(p.get("volume", {}).get("m5", 0) or 0)
                    price_usd = float(p.get("priceUsd", 0) or 0)

                    # --- FILTERS ---
                    if liquidity < MIN_LIQUIDITY_USD:
                        continue
                    if volume_5m < MIN_VOLUME_5M_USD:
                        continue

                    txns_m5 = p.get("txns", {}).get("m5", {})
                    buys_5m = int(txns_m5.get("buys", 0))
                    sells_5m = int(txns_m5.get("sells", 0))
                    buy_sell_ratio = (buys_5m / max(sells_5m, 1))
                    
                    if buys_5m < sells_5m or buy_sell_ratio < MIN_BUY_SELL_RATIO:
                        continue

                    # --- EXECUTOR AGENT ($5.00 Risk per Trade) ---
                    if mint not in logger.open_positions and len(logger.open_positions) < MAX_OPEN_POSITIONS:
                        if logger.equity < FIXED_POSITION_USD:
                            await logger.log("EXECUTOR", "Equity too low for position size.", alert_telegram=False)
                            continue

                        position_size = FIXED_POSITION_USD
                        effective_entry = position_size * (1 - (DEX_FEE_SLIPPAGE_PCT / 2))

                        logger.open_positions[mint] = {
                            "symbol": symbol,
                            "entry_price": price_usd,
                            "peak_price": price_usd,
                            "size_usd": position_size,
                            "effective_usd": effective_entry,
                            "trailing_active": False,
                        }
                        logger.traded_count += 1
                        await logger.log(
                            "EXECUTOR",
                            f"BOUGHT ${symbol} (Liq: ${liquidity:,.0f}) · size ${position_size:.2f} @ ${price_usd:.6f}",
                            alert_telegram=True,
                        )

                    await asyncio.sleep(0.5)
    except Exception as e:
        await logger.log("PIPELINE", f"Error: {e}")


async def monitor_risk_loop(session: aiohttp.ClientSession):
    while True:
        await asyncio.sleep(5)
        open_mints = list(logger.open_positions.keys())
        if not open_mints:
            continue

        url = f"https://api.dexscreener.com/latest/dex/tokens/{','.join(open_mints)}"
        try:
            async with session.get(url, timeout=8) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pairs = data.get("pairs") or []
                    live_prices = {
                        p.get("baseToken", {}).get("address"): float(p.get("priceUsd", 0) or 0)
                        for p in pairs
                        if p.get("chainId") == "solana"
                    }

                    for mint in open_mints:
                        pos = logger.open_positions.get(mint)
                        curr_price = live_prices.get(mint, 0)
                        if not pos or curr_price <= 0:
                            continue

                        entry = pos["entry_price"]
                        pnl_pct = ((curr_price - entry) / entry) * 100

                        if curr_price > pos["peak_price"]:
                            pos["peak_price"] = curr_price

                        if not pos["trailing_active"] and pnl_pct >= TRAIL_ACTIVATION_PCT:
                            pos["trailing_active"] = True
                            await logger.log("RISK", f"${pos['symbol']} hit +{pnl_pct:.1f}% · Trailing Stop Active")

                        should_sell = False
                        reason = ""
                        if pnl_pct <= STOP_LOSS_PCT:
                            should_sell = True
                            reason = "hard stop -8%"
                        elif pos["trailing_active"]:
                            drop_from_peak = ((pos["peak_price"] - curr_price) / pos["peak_price"]) * 100
                            if drop_from_peak >= TRAIL_DROP_PCT:
                                should_sell = True
                                reason = "trail + sold"

                        if should_sell:
                            mult = curr_price / entry
                            net_return = pos["effective_usd"] * mult * (1 - (DEX_FEE_SLIPPAGE_PCT / 2))
                            pnl_usd = net_return - pos["size_usd"]
                            pnl_pct_final = (pnl_usd / pos["size_usd"]) * 100
                            logger.equity += pnl_usd
                            
                            # Smart feedback blacklist on loss, keep open on win
                            if pnl_usd < 0:
                                logger.loser_blacklist[mint] = datetime.now() + timedelta(hours=BLACKLIST_COOLDOWN_HOURS)
                                reason_tag = f"{reason} (Blacklisted for 4h)"
                            else:
                                reason_tag = f"{reason} (Profitable - Kept Watchlist Active)"

                            logger.open_positions.pop(mint, None)

                            sign = "+" if pnl_usd >= 0 else ""
                            await logger.log(
                                "RISK",
                                f"{reason_tag} ${pos['symbol']} {sign}${pnl_usd:.2f} ({sign}{pnl_pct_final:.1f}%) | Equity: ${logger.equity:.2f}",
                                alert_telegram=True,
                            )
        except Exception:
            pass


# ==========================================
# BACKGROUND WORKER BOOTSTRAP
# ==========================================
async def background_trading_worker():
    await asyncio.sleep(3)
    async with aiohttp.ClientSession() as session:
        await asyncio.gather(
            monitor_risk_loop(session),
            scanner_loop(session)
        )


async def scanner_loop(session: aiohttp.ClientSession):
    while True:
        await scout_and_process_tokens(session)
        await asyncio.sleep(25)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_trading_worker())
