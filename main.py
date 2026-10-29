import asyncio
import aiohttp
import os
from datetime import datetime
from fastapi import FastAPI

app = FastAPI()

# ==========================================
# CONFIGURATION & RISK PARAMETERS ($2 MICRO-MODE)
# ==========================================
INITIAL_EQUITY = 2.00            # Starting virtual balance ($2.00)
FIXED_POSITION_USD = 2.00        # Fixed size per trade ($2.00)
MAX_OPEN_POSITIONS = 1           # Only hold 1 position at a time with a $2 pool
DEX_FEE_SLIPPAGE_PCT = 0.015     # 1.5% simulated round-trip slippage

# Filter & Analyst Thresholds
MIN_LIQUIDITY_USD = 12000.0      # Minimum $12k liquidity pool
MIN_VOLUME_5M_USD = 5000.0       # Minimum $5k 5-minute volume
MIN_ANALYST_SCORE = 70           # Must score >= 70/100 to trade

# Risk Engine Thresholds
STOP_LOSS_PCT = -8.0             # Hard stop at -8% (~$0.16 risk)
TRAIL_ACTIVATION_PCT = 12.0      # Activate trailing stop at +12%
TRAIL_DROP_PCT = 4.0             # Sell if price drops 4% from peak after activation

# Telegram Credentials (pulled securely from Render environment variables)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# ==========================================
# LOGGER & TELEGRAM HELPER
# ==========================================
class CloudDeskLogger:
    def __init__(self):
        self.equity = INITIAL_EQUITY
        self.open_positions = {}
        self.scanned_count = 0
        self.traded_count = 0

    async def log(self, agent: str, message: str, alert_telegram: bool = False):
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] [{agent}] {message}")

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
# FASTAPI ROUTES (For Render & UptimeRobot)
# ==========================================
@app.get("/")
def home():
    return {
        "status": "Online",
        "equity": round(logger.equity, 2),
        "open_positions": len(logger.open_positions),
        "scanned_count": logger.scanned_count,
        "traded_count": logger.traded_count,
    }


# ==========================================
# TRADING PIPELINE LOGIC
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

                for p in pairs:
                    if p.get("chainId") != "solana":
                        continue
                    logger.scanned_count += 1
                    symbol = p.get("baseToken", {}).get("symbol", "UNKNOWN")
                    mint = p.get("baseToken", {}).get("address")
                    liquidity = float(p.get("liquidity", {}).get("usd", 0) or 0)
                    volume_5m = float(p.get("volume", {}).get("m5", 0) or 0)
                    price_usd = float(p.get("priceUsd", 0) or 0)

                    # --- FILTER AGENT ---
                    if liquidity < MIN_LIQUIDITY_USD or volume_5m < MIN_VOLUME_5M_USD:
                        continue

                    # --- ANALYST AGENT ---
                    score = 75
                    if score < MIN_ANALYST_SCORE:
                        continue

                    # --- EXECUTOR AGENT ($2 Micro-Position) ---
                    if mint not in logger.open_positions and len(logger.open_positions) < MAX_OPEN_POSITIONS:
                        if logger.equity < FIXED_POSITION_USD:
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
                            f"BOUGHT ${symbol} · size ${position_size:.2f} @ ${price_usd:.6f}",
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
                            logger.open_positions.pop(mint, None)

                            sign = "+" if pnl_usd >= 0 else ""
                            await logger.log(
                                "RISK",
                                f"{reason} ${pos['symbol']} {sign}${pnl_usd:.2f} ({sign}{pnl_pct_final:.1f}%) | Equity: ${logger.equity:.2f}",
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
