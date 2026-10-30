import asyncio
import aiohttp
import os
from datetime import datetime, timedelta
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse

app = FastAPI()

# ==========================================
# CONFIGURATION & HIGH-SPEED MULTI-POS PARAMS
# ==========================================
INITIAL_EQUITY = 50.00           # Starting virtual balance ($50.00)
FIXED_POSITION_USD = 5.00        # Fixed size per trade ($5.00)
MAX_OPEN_POSITIONS = 3           # Increased to 3 simultaneous trades!

# KuCoin trading pairs format (Base-Quote)
TRADING_PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT", "ADA-USDT"]

# Risk Engine Thresholds
STOP_LOSS_PCT = -8.0             # Hard stop at -8%
TRAIL_ACTIVATION_PCT = 8.0       # Activate trailing stop at +8%
TRAIL_DROP_PCT = 2.5             # Sell if price moves 2.5% against peak after activation

# Telegram Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ==========================================
# LOGGER & ACTIVITY FEED
# ==========================================
class CloudDeskLogger:
    def __init__(self):
        self.equity = INITIAL_EQUITY
        self.open_positions = {}     # Dictionary tracking active positions by symbol
        self.price_histories = {pair: [] for pair in TRADING_PAIRS}
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
        for sym, pos in logger.open_positions.items():
            positions_html += f"<b>{pos['side']} {sym}</b> | Entry: ${pos['entry_price']:,.2f} | Peak: ${pos['peak_price']:,.2f}<br>"
    else:
        positions_html = f"<i>No open positions. High-speed scanning {len(TRADING_PAIRS)} pairs (Max 3 Trades)...</i>"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Trading-blue: High-Speed Multi-Pos Desk</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="5">
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
        <h2>⚡ Trading-blue: High-Speed Multi-Pos Desk</h2>
        
        <div class="card">
            <div>Virtual Equity Pool</div>
            <div class="metric">${logger.equity:.2f}</div>
        </div>

        <div class="card">
            <div>Bot Stats</div>
            <div>Scanned Ticks: <b>{logger.scanned_count}</b> | Traded: <b>{logger.traded_count}</b></div>
        </div>

        <div class="card">
            <div>Active Open Positions ({len(logger.open_positions)}/3)</div>
            <div>{positions_html}</div>
        </div>

        <div class="card">
            <div>Live Activity Logs (Auto-refreshes every 5s)</div>
            <ul>{logs_html if logs_html else "<li>Waiting for market data...</li>"}</ul>
        </div>
    </body>
    </html>
    """
    return html_content


# ==========================================
# HIGH-SPEED KUCOIN MARKET WATCHER WORKER
# ==========================================
async def market_watcher_worker():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                live_prices = {}
                for symbol in TRADING_PAIRS:
                    url = f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}"
                    async with session.get(url, timeout=3) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("code") == "200000" and data.get("data"):
                                price_str = data["data"].get("price")
                                if price_str:
                                    live_prices[symbol] = float(price_str)
                                    logger.scanned_count += 1
                    await asyncio.sleep(0.1) # Accelerated scan speed

                if not live_prices:
                    await asyncio.sleep(2)
                    continue

                # --- 1. MANAGEMENT OF OPEN POSITIONS ---
                open_symbols = list(logger.open_positions.keys())
                for symbol in open_symbols:
                    pos = logger.open_positions[symbol]
                    current_price = live_prices.get(symbol, 0)
                    
                    if current_price > 0:
                        entry = pos["entry_price"]
                        side = pos["side"]

                        if side == "LONG":
                            pnl_pct = ((current_price - entry) / entry) * 100
                            if current_price > pos["peak_price"]:
                                pos["peak_price"] = current_price
                            drop_from_peak = ((pos["peak_price"] - current_price) / pos["peak_price"]) * 100
                        else: # SHORT
                            pnl_pct = ((entry - current_price) / entry) * 100
                            if current_price < pos["peak_price"]:
                                pos["peak_price"] = current_price
                            drop_from_peak = ((current_price - pos["peak_price"]) / pos["peak_price"]) * 100

                        if not pos["trailing_active"] and pnl_pct >= TRAIL_ACTIVATION_PCT:
                            pos["trailing_active"] = True
                            await logger.log("RISK", f"${symbol} {side} hit +{pnl_pct:.1f}% · Trailing Stop Active")

                        should_close = False
                        reason = ""
                        if pnl_pct <= STOP_LOSS_PCT:
                            should_close = True
                            reason = "hard stop -8%"
                        elif pos["trailing_active"] and drop_from_peak >= TRAIL_DROP_PCT:
                            should_close = True
                            reason = "trail profit secured"

                        if should_close:
                            if side == "LONG":
                                mult = current_price / entry
                            else:
                                mult = 2.0 - (current_price / entry)

                            net_return = pos["size_usd"] * mult
                            pnl_usd = net_return - pos["size_usd"]
                            pnl_pct_final = (pnl_usd / pos["size_usd"]) * 100
                            logger.equity += pnl_usd

                            logger.open_positions.pop(symbol, None)
                            sign = "+" if pnl_usd >= 0 else ""
                            await logger.log(
                                "RISK",
                                f"CLOSED {side} {symbol} {reason} · {sign}${pnl_usd:.2f} ({sign}{pnl_pct_final:.1f}%) | Equity: ${logger.equity:.2f}",
                                alert_telegram=True,
                            )

                # --- 2. HIGH-SPEED MULTI-POSITION ENTRY SCANNER ---
                if len(logger.open_positions) < MAX_OPEN_POSITIONS:
                    for symbol, current_price in live_prices.items():
                        # Skip if already holding this symbol
                        if symbol in logger.open_positions:
                            continue

                        history = logger.price_histories[symbol]
                        history.append(current_price)
                        if len(history) > 3: # Reduced tick history requirement for faster triggers
                            history.pop(0)

                        if len(history) >= 3:
                            price_start = history[0]
                            price_end = history[-1]
                            momentum_pct = ((price_end - price_start) / price_start) * 100

                            # Lowered momentum sensitivity to 0.02% for faster high-frequency testing
                            if abs(momentum_pct) > 0.02:
                                chosen_side = "LONG" if momentum_pct > 0 else "SHORT"

                                if logger.equity >= FIXED_POSITION_USD and len(logger.open_positions) < MAX_OPEN_POSITIONS:
                                    logger.open_positions[symbol] = {
                                        "symbol": symbol,
                                        "side": chosen_side,
                                        "entry_price": current_price,
                                        "peak_price": current_price,
                                        "size_usd": FIXED_POSITION_USD,
                                        "trailing_active": False,
                                    }
                                    logger.traded_count += 1
                                    await logger.log(
                                        "EXECUTOR",
                                        f"OPENED {chosen_side} {symbol} · size ${FIXED_POSITION_USD:.2f} @ ${current_price:,.2f}",
                                        alert_telegram=True,
                                    )

            except Exception as e:
                await logger.log("WATCHER", f"Scan error: {e}")

            await asyncio.sleep(1.5) # Much faster check loop (every 1.5 seconds)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(market_watcher_worker())
