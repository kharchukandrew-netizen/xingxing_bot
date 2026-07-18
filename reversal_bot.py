#!/usr/bin/env python3
"""
Telegram Bot for Token Reversal Alerts
Tracks local bottoms and alerts when price pumps X% from bottom.
Sends alerts via Pushover with siren sound.

Price sources:
  - Solana -> Jupiter Price API v3 (USD)
  - ETH / BSC / Robinhood / Base -> DexScreener (USD)
All prices in USD.
"""

import os
import json
import time
import threading
import requests
from datetime import datetime

# ============ CONFIGURATION ============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8592673568:AAFxiTjpfKdU2ID3I8FcCSPMPL1RWfb9Hq0")
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "YOUR_PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "YOUR_PUSHOVER_API_TOKEN")

# Only allow your Telegram user ID (set after first /start)
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "")  # Your Telegram user ID

CHECK_INTERVAL = 10  # seconds between price checks
DATA_FILE = "tokens_data.json"

# ============ API ENDPOINTS ============
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "").strip()
JUPITER_PRICE_URL = (
    "https://api.jup.ag/price/v3"
    if JUPITER_API_KEY
    else "https://lite-api.jup.ag/price/v3"
)
JUPITER_TOKEN_BASE = "https://api.jup.ag" if JUPITER_API_KEY else "https://lite-api.jup.ag"
DEXSCREENER_BASE = "https://api.dexscreener.com"

# DexScreener chain ids for EVM addresses. First one that returns a price wins.
EVM_CHAINS = ["ethereum", "bsc", "robinhood", "base"]


def detect_chain(address):
    """Return 'solana' for base58 addresses, 'evm' for 0x addresses, else None."""
    a = address.strip()
    if a.startswith("0x") and len(a) == 42:
        if all(c in "0123456789abcdefABCDEF" for c in a[2:]):
            return "evm"
        return None
    if 32 <= len(a) <= 44:
        allowed = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
        if all(c in allowed for c in a):
            return "solana"
    return None


def fetch_jupiter_price(token_address):
    """Fetch Solana token price (USD) and symbol from Jupiter."""
    headers = {"x-api-key": JUPITER_API_KEY} if JUPITER_API_KEY else {}
    price = None
    symbol = None
    name = None
    # Price
    try:
        url = f"{JUPITER_PRICE_URL}?ids={token_address}"
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            info = data.get(token_address)
            if info and "usdPrice" in info:
                price = float(info["usdPrice"])
    except Exception as e:
        print(f"Jupiter price error: {e}")
    # Symbol / name
    try:
        url = f"{JUPITER_TOKEN_BASE}/tokens/v2/search?query={token_address}"
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for tok in data:
                    if tok.get("id") == token_address or tok.get("address") == token_address:
                        symbol = tok.get("symbol")
                        name = tok.get("name")
                        break
    except Exception as e:
        print(f"Jupiter symbol error: {e}")

    if price is None:
        return None
    return {
        "price": price,
        "name": name or "Unknown",
        "symbol": symbol or "???",
        "liquidity": 0,
        "chain": "solana",
        "price_unit": "USD",
    }


def fetch_dexscreener_price(token_address):
    """Fetch EVM token price (USD) from DexScreener, trying each chain, highest liquidity pair."""
    for chain in EVM_CHAINS:
        url = f"{DEXSCREENER_BASE}/tokens/v1/{chain}/{token_address}"
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                continue
            pairs = resp.json()
        except Exception as e:
            print(f"DexScreener error ({chain}): {e}")
            continue

        if not isinstance(pairs, list) or not pairs:
            continue

        best_price = None
        best_liq = -1
        best_symbol = None
        best_name = None
        for pair in pairs:
            base = pair.get("baseToken") or {}
            if str(base.get("address", "")).lower() != token_address.lower():
                continue
            price_str = pair.get("priceUsd")
            if not price_str:
                continue
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue
            liq = 0.0
            try:
                liq = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
            except (ValueError, TypeError):
                liq = 0.0
            if liq > best_liq:
                best_liq = liq
                best_price = price
                best_symbol = base.get("symbol")
                best_name = base.get("name")

        if best_price is not None:
            return {
                "price": best_price,
                "name": best_name or "Unknown",
                "symbol": best_symbol or "???",
                "liquidity": best_liq if best_liq > 0 else 0,
                "chain": chain,
                "price_unit": "USD",
            }
    return None


def get_token_price(token_address):
    """Unified price fetch: Solana via Jupiter, EVM via DexScreener. Returns USD price."""
    chain_type = detect_chain(token_address)
    if chain_type == "solana":
        return fetch_jupiter_price(token_address)
    elif chain_type == "evm":
        return fetch_dexscreener_price(token_address)
    else:
        # Unknown format - try both
        result = fetch_jupiter_price(token_address)
        if result:
            return result
        return fetch_dexscreener_price(token_address)

# ============ GLOBAL STATE ============
tokens = {}
last_update_id = 0


def save_tokens():
    """Save tokens to file for persistence"""
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(tokens, f, indent=2)
    except Exception as e:
        print(f"Error saving tokens: {e}")


def load_tokens():
    """Load tokens from file"""
    global tokens
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                tokens = json.load(f)
                print(f"Loaded {len(tokens)} tokens from file")
    except Exception as e:
        print(f"Error loading tokens: {e}")
        tokens = {}


def format_usd(price):
    """Format a USD price with adaptive decimals."""
    if price >= 1:
        return f"${price:,.4f}"
    if price >= 0.01:
        return f"${price:.6f}"
    return f"${price:.10f}"


def send_pushover_alert(token_info, ca, percent_gain, local_bottom, price_unit):
    """Send emergency priority notification with SIREN sound"""
    url = "https://api.pushover.net/1/messages.json"
    
    payload = {
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "message": (
            f"🚀 {token_info['symbol']} розвернувся!\n\n"
            f"📈 +{percent_gain:.1f}% від дна\n"
            f"💰 Поточна ціна: {format_usd(token_info['price'])}\n"
            f"📉 Локальне дно: {format_usd(local_bottom)}\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        ),
        "title": f"🔥 REVERSAL: {token_info['symbol']} +{percent_gain:.0f}%",
        "sound": "siren",
        "priority": 2,
        "retry": 30,
        "expire": 3600,
    }
    
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()
        print(f"✅ Pushover alert sent for {token_info['symbol']}")
        return True
    except Exception as e:
        print(f"❌ Error sending Pushover: {e}")
        return False


def send_telegram_message(chat_id, text):
    """Send message via Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")


def get_telegram_updates():
    """Get new messages from Telegram"""
    global last_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 5}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        return data.get("result", [])
    except Exception as e:
        print(f"Error getting updates: {e}")
        return []


def handle_command(chat_id, user_id, text):
    """Process Telegram commands"""
    global tokens
    
    # Check if user is allowed
    if ALLOWED_USER_ID and str(user_id) != str(ALLOWED_USER_ID):
        send_telegram_message(chat_id, "⛔ Access denied. This bot is private.")
        return
    
    parts = text.strip().split()
    command = parts[0].lower()
    
    # /start - Welcome message
    if command == "/start":
        msg = (
            "🤖 <b>Solana Reversal Alert Bot</b>\n\n"
            f"Your Telegram ID: <code>{user_id}</code>\n\n"
            "<b>Commands:</b>\n"
            "/add CA PERCENT - Add token to track\n"
            "/edit CA PERCENT - Change target % for token\n"
            "/list - Show all tracked tokens\n"
            "/remove CA - Remove token\n"
            "/status - Bot status\n\n"
            "<b>Example:</b>\n"
            "<code>/add Cm6fNnMk7NfzStP9CZpsQA2v3jjzbcYGAxdJySmHpump 40</code>\n\n"
            "This will alert when token pumps 40% from its local bottom.\n"
            "Prices are tracked in USD."
        )
        send_telegram_message(chat_id, msg)
    
    # /add CA PERCENT - Add new token
    elif command == "/add":
        if len(parts) < 3:
            send_telegram_message(chat_id, "❌ Usage: /add CA_ADDRESS PERCENT\n\nExample:\n<code>/add Cm6fNnMk...pump 40</code>")
            return
        
        ca = parts[1]
        try:
            target_percent = float(parts[2])
        except:
            send_telegram_message(chat_id, "❌ Invalid percent. Use a number like 40")
            return
        
        # Check if already tracking
        if ca in tokens:
            send_telegram_message(chat_id, f"⚠️ Already tracking this token.\nUse /edit to change target or /remove first.")
            return
        
        # Get current price
        send_telegram_message(chat_id, "🔍 Fetching token info...")
        token_info = get_token_price(ca)
        
        if not token_info:
            send_telegram_message(chat_id, "❌ Token not found. Check the CA (Solana or EVM 0x address).")
            return
        
        # Add to tracking
        tokens[ca] = {
            "target_percent": target_percent,
            "local_bottom": token_info["price"],
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "name": token_info["name"],
            "symbol": token_info["symbol"],
            "chain": token_info.get("chain", "solana"),
            "price_unit": "USD"
        }
        save_tokens()
        
        chain = token_info.get("chain", "solana")
        chain_name = {
            "solana": "Solana",
            "ethereum": "Ethereum",
            "robinhood": "Robinhood",
            "base": "Base",
            "bsc": "BSC",
        }.get(chain, chain.title())
        
        msg = (
            f"✅ <b>Token added!</b>\n\n"
            f"🪙 {token_info['name']} ({token_info['symbol']})\n"
            f"⛓ Chain: {chain_name}\n"
            f"💰 Current price: {format_usd(token_info['price'])}\n"
            f"💧 Liquidity: ${token_info.get('liquidity', 0):,.0f}\n"
            f"🎯 Alert at: +{target_percent}% from bottom\n\n"
            f"Bot will track local bottom and alert on reversal."
        )
        send_telegram_message(chat_id, msg)
    
    # /edit CA PERCENT - Change target percent for existing token
    elif command == "/edit":
        if len(parts) < 3:
            send_telegram_message(chat_id, "❌ Usage: /edit CA_ADDRESS NEW_PERCENT\n\nExample:\n<code>/edit Cm6fNnMk...pump 50</code>")
            return
        
        ca_to_edit = parts[1]
        try:
            new_percent = float(parts[2])
        except:
            send_telegram_message(chat_id, "❌ Invalid percent. Use a number like 50")
            return
        
        # Find matching CA (can be partial)
        found_ca = None
        for ca in tokens:
            if ca.startswith(ca_to_edit) or ca_to_edit in ca:
                found_ca = ca
                break
        
        if found_ca:
            old_percent = tokens[found_ca]["target_percent"]
            tokens[found_ca]["target_percent"] = new_percent
            save_tokens()
            
            symbol = tokens[found_ca].get("symbol", "Unknown")
            msg = (
                f"✅ <b>Target updated!</b>\n\n"
                f"🪙 {symbol}\n"
                f"📉 Old target: +{old_percent}%\n"
                f"🎯 New target: +{new_percent}%"
            )
            send_telegram_message(chat_id, msg)
        else:
            send_telegram_message(chat_id, "❌ Token not found in tracking list.\n\nUse /list to see all tokens.")
    
    # /list - Show all tokens
    elif command == "/list":
        if not tokens:
            send_telegram_message(chat_id, "📋 No tokens being tracked.\n\nUse /add to add tokens.")
            return
        
        msg = "📋 <b>Tracked Tokens:</b>\n\n"
        for ca, data in tokens.items():
            current_info = get_token_price(ca)
            current_price = current_info["price"] if current_info else 0
            bottom = data["local_bottom"]
            chain = data.get("chain", "solana")
            if chain == "solana":
                chain_emoji = "☀️"
            elif chain == "ethereum":
                chain_emoji = "🔷"
            elif chain == "robinhood":
                chain_emoji = "🪶"
            elif chain == "base":
                chain_emoji = "🔵"
            else:
                chain_emoji = "🔶"
            
            if bottom > 0 and current_price > 0:
                percent_from_bottom = ((current_price - bottom) / bottom) * 100
                status = f"+{percent_from_bottom:.1f}%" if percent_from_bottom >= 0 else f"{percent_from_bottom:.1f}%"
            else:
                status = "N/A"
            
            msg += (
                f"{chain_emoji} <b>{data['symbol']}</b>\n"
                f"  📉 Bottom: {format_usd(bottom)}\n"
                f"  💰 Now: {format_usd(current_price)} ({status})\n"
                f"  🎯 Target: +{data['target_percent']}%\n"
                f"  <code>{ca}</code>\n\n"
            )
        
        send_telegram_message(chat_id, msg)
    
    # /remove CA - Remove token
    elif command == "/remove":
        if len(parts) < 2:
            send_telegram_message(chat_id, "❌ Usage: /remove CA_ADDRESS")
            return
        
        ca_to_remove = parts[1]
        
        # Find matching CA (can be partial)
        found_ca = None
        for ca in tokens:
            if ca.startswith(ca_to_remove) or ca_to_remove in ca:
                found_ca = ca
                break
        
        if found_ca:
            symbol = tokens[found_ca].get("symbol", "Unknown")
            del tokens[found_ca]
            save_tokens()
            send_telegram_message(chat_id, f"✅ Removed {symbol} from tracking.")
        else:
            send_telegram_message(chat_id, "❌ Token not found in tracking list.")
    
    # /status - Bot status
    elif command == "/status":
        msg = (
            f"🤖 <b>Bot Status</b>\n\n"
            f"📊 Tracking: {len(tokens)} tokens\n"
            f"⏱ Check interval: {CHECK_INTERVAL}s\n"
            f"💱 Price: USD\n"
            f"✅ Bot is running"
        )
        send_telegram_message(chat_id, msg)
    
    else:
        send_telegram_message(chat_id, "❓ Unknown command. Use /start for help.")


def price_monitor_loop():
    """Background loop to check prices and detect reversals"""
    global tokens
    
    print("🔄 Price monitor started")
    
    while True:
        try:
            tokens_to_remove = []
            
            for ca, data in list(tokens.items()):
                token_info = get_token_price(ca)
                
                if not token_info:
                    continue
                
                current_price = token_info["price"]
                local_bottom = data["local_bottom"]
                target_percent = data["target_percent"]
                
                # Update local bottom if price is lower
                if current_price < local_bottom:
                    tokens[ca]["local_bottom"] = current_price
                    save_tokens()
                    print(f"📉 {data['symbol']}: New bottom {format_usd(current_price)}")
                    continue
                
                # Calculate percent gain from bottom
                if local_bottom > 0:
                    percent_gain = ((current_price - local_bottom) / local_bottom) * 100
                    
                    # Check if target reached
                    if percent_gain >= target_percent:
                        print(f"🚀 {data['symbol']}: +{percent_gain:.1f}% from bottom!")
                        
                        # Send Pushover alert
                        send_pushover_alert(token_info, ca, percent_gain, local_bottom, "USD")
                        
                        # Mark for removal
                        tokens_to_remove.append(ca)
                    else:
                        print(f"📊 {data['symbol']}: {format_usd(current_price)} (+{percent_gain:.1f}% from bottom, target: +{target_percent}%)")
            
            # Remove triggered tokens
            for ca in tokens_to_remove:
                symbol = tokens[ca].get("symbol", "Unknown")
                del tokens[ca]
                save_tokens()
                print(f"🗑 Removed {symbol} after alert")
        
        except Exception as e:
            print(f"Error in monitor loop: {e}")
        
        time.sleep(CHECK_INTERVAL)


def telegram_loop():
    """Background loop to handle Telegram messages"""
    global last_update_id
    
    print("📱 Telegram handler started")
    
    while True:
        try:
            updates = get_telegram_updates()
            
            for update in updates:
                last_update_id = update["update_id"]
                
                if "message" in update and "text" in update["message"]:
                    chat_id = update["message"]["chat"]["id"]
                    user_id = update["message"]["from"]["id"]
                    text = update["message"]["text"]
                    
                    print(f"📨 Message from {user_id}: {text}")
                    handle_command(chat_id, user_id, text)
        
        except Exception as e:
            print(f"Error in Telegram loop: {e}")
        
        time.sleep(1)


def main():
    print("=" * 60)
    print("🤖 Solana Reversal Alert Bot")
    print("=" * 60)
    print(f"Telegram Bot: Active")
    print(f"Pushover: {'Configured' if PUSHOVER_USER_KEY != 'YOUR_PUSHOVER_USER_KEY' else 'NOT SET!'}")
    print(f"Check interval: {CHECK_INTERVAL}s")
    print(f"Price unit: USD")
    print("=" * 60)
    
    # Load saved tokens
    load_tokens()
    
    # Start background threads
    monitor_thread = threading.Thread(target=price_monitor_loop, daemon=True)
    monitor_thread.start()
    
    # Run Telegram loop in main thread
    telegram_loop()


if __name__ == "__main__":
    main()
