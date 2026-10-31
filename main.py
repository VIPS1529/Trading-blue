import asyncio
import aiohttp
import os
from datetime import datetime, timedelta
from fastapi import FastAPI, Response, Query
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()

# ==========================================
# CONFIGURATION & 40X LEVERAGE PARAMETERS
# ==========================================
INITIAL_EQUITY = 50.00           # Starting virtual balance ($50.00)
MARGIN_SIZE_USD = 20.00          # Margin per trade ($20.00 collateral)
LEVERAGE = 40                    # 40x Leverage
POSITION_VOLUME_USD = MARGIN_SIZE_USD * LEVERAGE  # $800.00 total volume per trade
MAX_OPEN_POSITIONS = 2           # Max 2 concurrent high-leverage scalps
COOLDOWN_HOURS = 5               # 5-hour standard cooldown for closed symbols

# High-Probability Exception Rule
HIGH_PROB_THRESHOLD = 0.035      # If momentum > 0.035%, bypass cooldown

TRADING_PAIRS = ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT", "ADA-USDT"]

# Ultra-Tight Scalping Risk Engine & Fast-Lock Strategy
STOP_LOSS_PCT = -0.5             # Asset move -0.5% = -20% margin loss hard stop
TRAIL_ACTIVATION_PCT = 0.20      # Faster activation at +0.20% asset move (~8% ROI)
FAST_LOCK_ROI = 5.0              # Fast-Lock: If trade hits +5% ROI instantly, trigger hyper-tight protection!
TRAIL_DROP_PCT = 0.08            # Ultra-tight drop threshold to lock gains immediately

# Telegram Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ==========================================
# LOGGER & STATE MANAGEMENT
# ==========================================
class CloudDeskLogger:
    def __init__(self):
        self.equity = INITIAL_EQUITY
        self.open_positions = {}         # symbol -> position dict
        self.symbol_cooldowns = {}       # symbol -> expiration datetime
        self.latest_prices = {}          # symbol -> current live price
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
# MANUAL CLOSE ROUTE (TRIGGERED VIA UI BUTTON)
# ==========================================
@app.get("/close")
async def manual_close(symbol: str = Query(...)):
    if symbol in logger.open_positions:
        pos = logger.open_positions[symbol]
        current_price = logger.latest_prices.get(symbol, pos["entry_price"])
        entry = pos["entry_price"]
        side = pos["side"]

        if side == "LONG":
            mult = current_price / entry
        else:
            mult = 2.0 - (current_price / entry)

        leveraged_multiplier = 1.0 + ((mult - 1.0) * LEVERAGE)
        net_return = pos["margin_usd"] * leveraged_multiplier
        pnl_usd = net_return - pos["margin_usd"]
        logger.equity += pnl_usd

        logger.open_positions.pop(symbol, None)
        logger.symbol_cooldowns[symbol] = datetime.now() + timedelta(hours=COOLDOWN_HOURS)

        sign = "+" if pnl_usd >= 0 else ""
        await logger.log(
            "MANUAL",
            f"CLOSED 4x {side} {symbol} manually · {sign}${pnl_usd:.2f} USDT | Cooldown: 5h | Equity: ${logger.equity:.2f}",
            alert_telegram=True,
        )

    return RedirectResponse(url="/", status_code=303)


# ==========================================
# VISUAL DASHBOARD ROUTE
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
                asset_pct = ((current_price - entry) / entry) * 100
                mult = current_price / entry
            else: # SHORT
                asset_pct = ((entry - current_price) / entry) * 100
                mult = 2.0 - (current_price / entry)

            leveraged_multiplier = 1.0 + ((mult - 1.0) * LEVERAGE)
            pnl_usd = (pos["margin_usd"] * leveraged_multiplier) - pos["margin_usd"]
            roi_pct = (pnl_usd / pos["margin_usd"]) * 100

            is_profit = pnl_usd >= 0
            color = "#3fb950" if is_profit else "#f85149"
            bg_color = "rgba(63, 185, 80, 0.08)" if is_profit else "rgba(248, 81, 73, 0.08)"
            sign = "+" if is_profit else ""

            positions_html += f"""
            <div style="background: {bg_color}; border: 1px solid {color}; border-left: 5px solid {color}; padding: 12px; margin-bottom: 10px; border-radius: 6px; display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <b>{LEVERAGE}x {side} {sym}</b> (${pos['volume_usd']:,.0f} Vol)<br>
                    Entry: ${entry:,.2f} | Current: ${current_price:,.2f}<br>
                    Margin PnL: <span style="color: {color}; font-weight: bold;">{sign}${pnl_usd:.2f} ({sign}{roi_pct:.1f}% ROI)</span>
                </div>
                <div>
                    <a href="/close?symbol={sym}" style="background: #da3633; color: white; padding: 8px 12px; text-decoration: none; border-radius: 4px; font-weight: bold; font-size: 12px;">❌ Close Now</a>
                </div>
            </div>
            """
    else:
        positions_html = f"<i>No open {LEVERAGE}x scalps. Scanning markets...</i>"

    now = datetime.now()
    active_cooldowns = [sym for sym, exp in logger.symbol_cooldowns.items() if exp > now]
    cooldowns_text = f"<b>Locked Symbols ({len(active_cooldowns)}):</b> " + (", ".join(active_cooldowns) if active_cooldowns else "None")

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Trading-blue: Fast-Lock Scalper Desk</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="refresh" content="2">
        <style>
            body {{ background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 20px; margin: 0; }}
            .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; margin-bottom: 15px; }}
            h2 {{ color: #58a6ff; margin-top: 0; font-size: 20px; }}
            .metric {{ font-size: 24px; font-weight: bold; color: #3fb950; }}
            ul {{ padding-left: 20px; font-family: monospace; font-size: 12px; max-height: 200px; overflow-y: auto; }}
            li {{ margin-bottom: 5px; border-bottom: 1px solid #21262d; padding-bottom: 3px; }}
        </style>
    </head>
    <body>
        <h2>⚡ Trading-blue: Fast-Lock Scalper</h2>
        
        <div class="card">
            <div>Virtual Equity Pool</div>
            <div class="metric">${logger.equity:.2f}</div>
        </div>

        <div class="card">
            <div>Bot Stats</div>
            <div>Scanned Ticks: <b>{logger.scanned_count}</b> | Traded: <b>{logger.traded_count}</b></div>
            <div style="margin-top: 8px; font-size: 13px; color: #8b949e;">{cooldowns_text}</div>
        </div>

        <div class="card">
            <div>Active {LEVERAGE}x Scalps ({len(logger.open_positions)}/{MAX_OPEN_POSITIONS})</div>
            <div style="margin-top: 10px;">{positions_html}</div>
        </div>

        <div class="card">
            <div>Live Scalp Logs (Auto-refreshes every 2s)</div>
            <ul>{logs_html if logs_html else f"<li>Initializing scanner...</li>"}</ul>
        </div>
    </body>
    </html>
    """
    return html_content


# ==========================================
# 40X LEVERAGE FAST-LOCK ENGINE WORKER
# ==========================================
async def market_watcher_worker():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now = datetime.now()
                logger.symbol_cooldowns = {sym: exp for sym, exp in logger.symbol_cooldowns.items() if exp > now}

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
                    asyncio.sleep(0.05)

                if not live_prices:
                    await asyncio.sleep(1)
                    continue

                # --- 1. AUTOMATED RISK MANAGEMENT WITH FAST-LOCK ---
                open_symbols = list(logger.open_positions.keys())
                for symbol in open_symbols:
                    pos = logger.open_positions[symbol]
                    current_price = live_prices.get(symbol, 0)
                    
                    if current_price > 0:
                        entry = pos["entry_price"]
                        side = pos["side"]

                        if side == "LONG":
                            asset_pct = ((current_price - entry) / entry) * 100
                            if current_price > pos["peak_price"]:
                                pos["peak_price"] = current_price
                            drop_from_peak = ((pos["peak_price"] - current_price) / pos["peak_price"]) * 100
                        else: # SHORT
                            asset_pct = ((entry - current_price) / entry) * 100
                            if current_price < pos["peak_price"]:
                                pos["peak_price"] = current_price
                            drop_from_peak = ((current_price - pos["peak_price"]) / pos["peak_price"]) * 100

                        leveraged_pnl_pct = asset_pct * LEVERAGE

                        # Fast-Lock Trigger: If ROI hits +5% quickly, activate hyper-tight trailing stop
                        if not pos["fast_locked"] and leveraged_pnl_pct >= FAST_LOCK_ROI:
                            pos["fast_locked"] = True
                            pos["trailing_active"] = True
                            await logger.log(f"{LEVERAGE}X-FASTLOCK", f"${symbol} {side} hit +{leveraged_pnl_pct:.1f}% ROI · Fast-Lock Armed!")

                        # Standard Trailing Activation Trigger (+10% ROI)
                        if not pos["trailing_active"] and leveraged_pnl_pct >= 10.0:
                            pos["trailing_active"] = True
                            await logger.log(f"{LEVERAGE}X-SCALP", f"${symbol} {side} hit +{leveraged_pnl_pct:.1f}% ROI · Target Lock Active")

                        should_close = False
                        reason = ""
                        if leveraged_pnl_pct <= -15.0:
                            should_close = True
                            reason = "hard stop (-15% ROI)"
                        elif pos["fast_locked"] and drop_from_peak >= TRAIL_DROP_PCT:
                            # Fast-lock exit: secures profit immediately upon slight pullback
                            should_close = True
                            reason = f"Fast-Lock profit secured (+{leveraged_pnl_pct:.1f}% ROI)"
                        elif pos["trailing_active"] and (drop_from_peak * LEVERAGE) >= 2.0:
                            should_close = True
                            reason = "scalp profit secured (>=10% ROI)"

                        if should_close:
                            if side == "LONG":
                                mult = current_price / entry
                            else:
                                mult = 2.0 - (current_price / entry)

                            leveraged_multiplier = 1.0 + ((mult - 1.0) * LEVERAGE)
                            net_return = pos["margin_usd"] * leveraged_multiplier
                            pnl_usd = net_return - pos["margin_usd"]
                            logger.equity += pnl_usd

                            logger.open_positions.pop(symbol, None)
                            logger.symbol_cooldowns[symbol] = datetime.now() + timedelta(hours=COOLDOWN_HOURS)

                            sign = "+" if pnl_usd >= 0 else ""
                            await logger.log(
                                f"{LEVERAGE}X-SCALP",
                                f"CLOSED auto {LEVERAGE}x {side} {symbol} {reason} · {sign}${pnl_usd:.2f} USDT | Cooldown: 5h | Equity: ${logger.equity:.2f}",
                                alert_telegram=True,
                            )

                # --- 2. ENTRY SCANNER WITH HIGH-PROBABILITY BYPASS ---
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
                            abs_momentum = abs(momentum_pct)

                            is_in_cooldown = symbol in logger.symbol_cooldowns
                            is_high_probability = abs_momentum >= HIGH_PROB_THRESHOLD

                            can_trade = False
                            if not is_in_cooldown and abs_momentum > 0.01:
                                can_trade = True
                            elif is_in_cooldown and is_high_probability:
                                can_trade = True

                            if can_trade:
                                chosen_side = "LONG" if momentum_pct > 0 else "SHORT"

                                if logger.equity >= MARGIN_SIZE_USD and len(logger.open_positions) < MAX_OPEN_POSITIONS:
                                    if symbol in logger.symbol_cooldowns:
                                        logger.symbol_cooldowns.pop(symbol, None)

                                    logger.open_positions[symbol] = {
                                        "symbol": symbol,
                                        "side": chosen_side,
                                        "entry_price": current_price,
                                        "peak_price": current_price,
                                        "margin_usd": MARGIN_SIZE_USD,
                                        "volume_usd": POSITION_VOLUME_USD,
                                        "trailing_active": False,
                                        "fast_locked": False, # Fast-lock tracking flag
                                    }
                                    logger.traded_count += 1
                                    await logger.log(
                                        f"{LEVERAGE}X-SCALP",
                                        f"OPENED {LEVERAGE}x {chosen_side} {symbol} (High-Prob Setup) · Margin: ${MARGIN_SIZE_USD:.2f} @ ${current_price:,.2f}",
                                        alert_telegram=True,
                                    )

            except Exception as e:
                await logger.log("WATCHER", f"Scan error: {e}")

            await asyncio.sleep(1.0)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(market_watcher_worker())
