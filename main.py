import os
import requests
import json
import asyncio
from dotenv import load_dotenv
from flask import Flask
from threading import Thread
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from solana.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from solana.transaction import Transaction
from solana.rpc.types import TxOpts
from base64 import b64decode
from datetime import datetime

# === WALIDACJA .env ===
load_dotenv()

def validate_env():
    required = ["TELEGRAM_TOKEN", "BIRDEYE_API_KEY", "PRIVATE_KEY", "DEFAULT_THRESHOLD", "BUY_AMOUNT_SOL"]
    for key in required:
        if not os.getenv(key):
            raise EnvironmentError(f"Brakuje zmiennej środowiskowej: {key}")
    try:
        json.loads(os.getenv("PRIVATE_KEY"))
    except:
        raise ValueError("PRIVATE_KEY musi być poprawnym JSON array")

validate_env()

# === KONFIGURACJA ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")
PRIVATE_KEY = json.loads(os.getenv("PRIVATE_KEY"))
MONITORED_WALLET = os.getenv("MONITORED_WALLET", "FiWe3vBZv32jv6GQeacwoBmHT88vbznjDRAgSgDwK1aa")
MARKETCAP_THRESHOLD = int(os.getenv("DEFAULT_THRESHOLD"))
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL"))

keypair = Keypair.from_secret_key(bytes(PRIVATE_KEY))
last_seen_token = None
watched_token = None
watch_expiration = None
app = None

# === KEEPALIVE ===
keepalive_app = Flask(__name__)

@keepalive_app.route("/")
def home():
    return "Bot działa!", 200

def run_keepalive():
    keepalive_app.run(host="0.0.0.0", port=8080)

# === KOMENDY TELEGRAM ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot aktywny. Komendy:\n/check\n/set_threshold <liczba>\n/set_amount <sol>\n/threshold\n/amount")

async def set_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MARKETCAP_THRESHOLD
    try:
        value = int(context.args[0])
        MARKETCAP_THRESHOLD = value
        await update.message.reply_text(f"Ustawiono próg marketcap: {value:,} USD")
    except:
        await update.message.reply_text("Użycie: /set_threshold 300000")

async def get_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Aktualny próg marketcap: {MARKETCAP_THRESHOLD:,} USD")

async def set_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BUY_AMOUNT_SOL
    try:
        value = float(context.args[0])
        BUY_AMOUNT_SOL = value
        await update.message.reply_text(f"Ustawiono kwotę zakupu: {value} SOL")
    except:
        await update.message.reply_text("Użycie: /set_amount 0.05")

async def get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Aktualna kwota zakupu: {BUY_AMOUNT_SOL} SOL")

# === LOGI I ALERTY ===
def save_to_history(text):
    with open("history.log", "a") as f:
        f.write(f"[{datetime.now()}] {text}\n")

async def send_telegram(message):
    if app and app.bot and app.chat_data:
        for chat_id in app.chat_data:
            await app.bot.send_message(chat_id=chat_id, text=message)

# === SPRAWDZANIE WALLETU ===
async def check_wallet(update: Update = None, context: ContextTypes.DEFAULT_TYPE = None, silent=False):
    global last_seen_token, watched_token, watch_expiration
    url = f"https://public-api.birdeye.so/public/wallet/token-list?wallet={MONITORED_WALLET}"
    headers = {"X-API-KEY": BIRDEYE_API_KEY}
    data = requests.get(url, headers=headers).json()
    tokens = data.get("data", [])
    if not tokens:
        if update: await update.message.reply_text("Brak danych z Birdeye.")
        return

    token = tokens[0]
    token_address = token["tokenAddress"]
    symbol = token["tokenSymbol"]

    if token_address == last_seen_token:
        if update and not silent:
            await update.message.reply_text("Brak nowych tokenów.")
        return

    last_seen_token = token_address
    watched_token = {"address": token_address, "symbol": symbol}
    watch_expiration = datetime.now().timestamp() + 1800  # 30 minut
    if update:
        await update.message.reply_text(f"Nowy token {symbol}. Czekam na spadek marketcap...")
    save_to_history(f"Obserwujemy token {symbol} ({token_address})")

# === TRYB "WATCH" ===
async def watch_mode():
    global watched_token, watch_expiration
    while True:
        if watched_token:
            now = datetime.now().timestamp()
            if now > watch_expiration:
                msg = f"Timeout: Porzucamy {watched_token['symbol']}"
                save_to_history(msg)
                await send_telegram(msg)
                watched_token = None
                continue

            try:
                headers = {"X-API-KEY": BIRDEYE_API_KEY}
                url = f"https://public-api.birdeye.so/public/token/basic-info?address={watched_token['address']}"
                mc_data = requests.get(url, headers=headers).json()
                marketcap = mc_data["data"].get("marketCap", 0)

                if marketcap <= MARKETCAP_THRESHOLD:
                    msg = f"{watched_token['symbol']} spadł do ${marketcap:,.0f} — KUPUJEMY {BUY_AMOUNT_SOL} SOL!"
                    save_to_history(msg)
                    await send_telegram(msg)
                    result = await buy_token(watched_token['address'])
                    await send_telegram(f"Hash transakcji: {result}")
                    save_to_history(f"TX: {result}")
                    watched_token = None
            except Exception as e:
                save_to_history(f"Błąd: {e}")
        await asyncio.sleep(1)

# === KUPNO TOKENA ===
async def buy_token(mint_address: str):
    jupiter_url = "https://quote-api.jup.ag/v6/swap"
    params = {
        "inputMint": "So11111111111111111111111111111111111111112",
        "outputMint": mint_address,
        "amount": int(BUY_AMOUNT_SOL * 10**9),
        "slippageBps": 100,
        "userPublicKey": str(keypair.public_key),
        "wrapUnwrapSOL": True,
        "feeBps": 0,
    }

    quote = requests.get(jupiter_url, params=params).json()
    swap_tx_base64 = quote["swapTransaction"]
    client = AsyncClient("https://api.mainnet-beta.solana.com")
    raw_tx = b64decode(swap_tx_base64)
    tx = Transaction.deserialize(raw_tx)
    tx.recent_blockhash = (await client.get_recent_blockhash())['result']['value']['blockhash']
    tx.sign(keypair)
    send_result = await client.send_raw_transaction(tx.serialize(), opts=TxOpts(skip_confirmation=False))
    await client.close()
    return send_result["result"]

# === START APLIKACJI ===
async def main():
    global app
    Thread(target=run_keepalive).start()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check_wallet))
    app.add_handler(CommandHandler("set_threshold", set_threshold))
    app.add_handler(CommandHandler("threshold", get_threshold))
    app.add_handler(CommandHandler("set_amount", set_amount))
    app.add_handler(CommandHandler("amount", get_amount))
    asyncio.create_task(watch_mode())
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
