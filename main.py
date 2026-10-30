import asyncio
import aiohttp
import os
from datetime import datetime, timedelta
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse

app = FastAPI()

# ==========================================
# CONFIGURATION & SCALPING PARAMETERS
# ==========================================
INITIAL_EQUITY = 50.00           # Starting virtual balance ($50.00)
FIXED_POSITION_USD = 20.00       # Safer scalp size ($20.00 per trade)
MAX_OPEN_POSITIONS = 2           # Max 2 simultaneous scalps ($40 total exposure)

TRADING_PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT", "ADA-USDT"]

# Scalping Risk Engine Thresholds (Tighter & Faster)
STOP_LOSS_PCT = -2.5             # Tight hard stop at -2.5%
TRAIL_ACTIVATION_PCT = 1.5       # Quick activation at +1.5%
TRAIL_DROP_PCT = 0.8             # Secure profit if price drops 0.8% from peak

# Telegram Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ==========================================
# LOGGER & STATE MANAGEMENT
# ==========================================
class CloudDeskLogger:
    def __init__(self):
        self.equity = INITIAL_EQUITY
        self.open_positions = {}     # symbol -> position dict
        self.latest_prices = {}      # symbol -> current live price
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
# VISUAL DASHBOARD ROUTE (SCALPER ROI UI)
# ==========================================
@app.get("/", response_class=HTMLResponse)
@app.head("/")
def dashboard():
    logs_html = "".join([f"<li>{log}</li>" for log in logger.activity_logs])
    positions_html = ""
    
    if logger.open_positions:
        for sym, pos in logger.open_positions.items():
            current_price = logger.latest_prices.get(sym, pos["entry_price"])
            entry = pos["entry_price"]
            side = pos["side"]

            if side == "LONG":
                pnl_pct = ((current_price - entry) / entry) * 100
                mult = current_price / entry
            else: # SHORT
                pnl_pct = ((entry - current_price) / entry) * 100
                mult = 2.0 - (current_price / entry)

            pnl_usd = (pos["size_usd"] * mult) - pos["size_usd"]
            color = "#3fb950" if pnl_usd >= 0 else "#f85149"
            sign = "+" if pnl_usd >= 0 else ""

            positions_html += f"""
            <div style="background: #21262d; border-left: 4px solid {color}; padding: 10px; margin-bottom: 8px; border-radius: 4px;">
                <b>SCALP {side} {sym}</b><br>
                Entry: ${entry:,.2f} | Current: ${current_price:,.2f}<br>
                Floating PnL: <span style="color: {color}; font-weight: bold;">{sign}${pnl_usd:.2f} ({sign}{pnl_pct:.2f}%)</span>
            </div>
            """
    else:
        positions_html = "<i>No open scalps. Scanning micro-momentum across KuCoin...</i>"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Trading-blue: Scalper Desk</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="2">
        <style>
            body {{ background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 20px; margin: 0; }}
            .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; margin-bottom: 15px; }}
            h2 {{ color: #58a6ff; margin-top: 0; font-size: 20px; }}
            .metric {{ font-size: 24px; font-weight: bold; color: #3fb950; }}
            ul {{ padding-left: 20px; font-family: monospace; font-size: 12px; max-height: 220px; overflow-y: auto; }}
            li {{ margin-bottom: 5px; border-bottom: 1px solid #21262d; padding-bottom: 3px; }}
        </style>
    </head>
    <body>
        <h2>⚡ Trading-blue: High-Frequency Scalper ($20 Size)</h2>
        
        <div class="card">
            <div>Virtual Equity Pool</div>
            <div class="metric">${logger.equity:.2f}</div>
        </div>

        <div class="card">
            <div>Bot Stats</div>
            <div>Scanned Ticks: <b>{logger.scanned_count}</b> | Scalps Traded: <b>{logger.traded_count}</b></div>
        </div>

        <div class="card">
            <div>Active Scalps ({len(logger.open_positions)}/{MAX_OPEN_POSITIONS})</div>
            <div style="margin-top: 10px;">{positions_html}</div>
        </div>

        <div class="card">
            <div>Live Scalp Logs (Auto-refreshes every 2s)</div>
            <ul>{logs_html if logs_html else "<li>Initializing scalp scanner...</li>"}</ul>
        </div>
    </body>
    </html>
    """
    return html_content


# ==========================================
# HIGH-FREQUENCY SCALPER ENGINE WORKER
# ==========================================
async def market_watcher_worker():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                live_prices = {}
                for symbol in TRADING_PAIRS:
                    url = f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}"
                    async with session.get(url, timeout=2.5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get("code") == "200000" and data.get("data"):
                                price_str = data["data"].get("price")
                                if price_str:
                                    price = float(price_str)
                                    live_prices[symbol] = price
                                    logger.latest_prices[symbol] = price
                                    logger.scanned_count += 1
                    await asyncio.sleep(0.05) # Hyper-fast polling

                if not live_prices:
                    await asyncio.sleep(1)
                    continue

                # --- 1. MANAGEMENT OF ACTIVE SCALPS ---
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
                            await logger.log("SCALP", f"${symbol} {side} hit +{pnl_pct:.1f}% · Target Lock Active")

                        should_close = False
                        reason = ""
                        if pnl_pct <= STOP_LOSS_PCT:
                            should_close = True
                            reason = "hard stop -2.5%"
                        elif pos["trailing_active"] and drop_from_peak >= TRAIL_DROP_PCT:
                            should_close = True
                            reason = "scalp profit secured"

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
                                "SCALP",
                                f"CLOSED {side} {symbol} {reason} · {sign}${pnl_usd:.2f} ({sign}{pnl_pct_final:.2f}%) | Equity: ${logger.equity:.2f}",
                                alert_telegram=True,
                            )

                # --- 2. LIGHTNING-FAST SCALP ENTRY SCANNER ---
                if len(logger.open_positions) < MAX_OPEN_POSITIONS:
                    for symbol, current_price in live_prices.items():
                        if symbol in logger.open_positions:
                            continue

                        history = logger.price_histories[symbol]
                        history.append(current_price)
                        if len(history) > 3:
                            history.pop(0)

                        if len(history) >= 3:
                            price_start = history[0]
                            price_end = history[-1]
                            momentum_pct = ((price_end - price_start) / price_start) * 100

                            # Micro-scalp sensitivity (0.015% quick trigger)
                            if abs(momentum_pct) > 0.015:
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
                                        "SCALP",
                                        f"OPENED {chosen_side} {symbol} · size ${FIXED_POSITION_USD:.2f} @ ${current_price:,.2f}",
                                        alert_telegram=True,
                                    )

            except Exception as e:
                await logger.log("WATCHER", f"Scan error: {e}")

            await asyncio.sleep(1.0) # 1-second ultra-fast scalping loop


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(market_watcher_worker())
