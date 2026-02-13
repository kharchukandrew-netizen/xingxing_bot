#!/usr/bin/env python3
"""
Telegram Bot for Solana Token Reversal Alerts
Tracks local bottoms and alerts when price pumps X% from bottom.
Sends alerts via Pushover with siren sound.
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

# ============ GLOBAL STATE ============
# Format: {
#   "CA_ADDRESS": {
#       "target_percent": 40,
#       "local_bottom": 0.001,
#       "added_at": "2024-01-01 12:00:00",
#       "name": "TokenName",
#       "symbol": "TKN"
#   }
# }
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


def get_token_price(token_address):
    """Fetch current token price from DexScreener API"""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get("pairs") and len(data["pairs"]) > 0:
            pair = data["pairs"][0]
            price_usd = float(pair.get("priceUsd", 0))
            token_name = pair.get("baseToken", {}).get("name", "Unknown")
            token_symbol = pair.get("baseToken", {}).get("symbol", "???")
            return {
                "price": price_usd,
                "name": token_name,
                "symbol": token_symbol
            }
        return None
    except Exception as e:
        print(f"Error fetching price: {e}")
        return None


def send_pushover_alert(token_info, ca, percent_gain, local_bottom):
    """Send emergency priority notification with SIREN sound"""
    url = "https://api.pushover.net/1/messages.json"
    
    payload = {
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "message": (
            f"🚀 {token_info['symbol']} розвернувся!\n\n"
            f"📈 +{percent_gain:.1f}% від дна\n"
            f"💰 Поточна ціна: ${token_info['price']:.6f}\n"
            f"📉 Локальне дно: ${local_bottom:.6f}\n"
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
            "/list - Show all tracked tokens\n"
            "/remove CA - Remove token\n"
            "/status - Bot status\n\n"
            "<b>Example:</b>\n"
            "<code>/add Cm6fNnMk7NfzStP9CZpsQA2v3jjzbcYGAxdJySmHpump 40</code>\n\n"
            "This will alert when token pumps 40% from its local bottom."
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
            send_telegram_message(chat_id, f"⚠️ Already tracking this token.\nUse /remove {ca[:10]}... first.")
            return
        
        # Get current price
        send_telegram_message(chat_id, "🔍 Fetching token info...")
        token_info = get_token_price(ca)
        
        if not token_info:
            send_telegram_message(chat_id, "❌ Token not found on DexScreener. Check the CA.")
            return
        
        # Add to tracking
        tokens[ca] = {
            "target_percent": target_percent,
            "local_bottom": token_info["price"],
            "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "name": token_info["name"],
            "symbol": token_info["symbol"]
        }
        save_tokens()
        
        msg = (
            f"✅ <b>Token added!</b>\n\n"
            f"🪙 {token_info['name']} ({token_info['symbol']})\n"
            f"💰 Current price: ${token_info['price']:.6f}\n"
            f"🎯 Alert at: +{target_percent}% from bottom\n\n"
            f"Bot will track local bottom and alert on reversal."
        )
        send_telegram_message(chat_id, msg)
    
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
            
            if bottom > 0 and current_price > 0:
                percent_from_bottom = ((current_price - bottom) / bottom) * 100
                status = f"+{percent_from_bottom:.1f}%" if percent_from_bottom >= 0 else f"{percent_from_bottom:.1f}%"
            else:
                status = "N/A"
            
            msg += (
                f"<b>{data['symbol']}</b>\n"
                f"  📉 Bottom: ${bottom:.6f}\n"
                f"  💰 Now: ${current_price:.6f} ({status})\n"
                f"  🎯 Target: +{data['target_percent']}%\n"
                f"  <code>{ca[:20]}...</code>\n\n"
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
                    print(f"📉 {data['symbol']}: New bottom ${current_price:.6f}")
                    continue
                
                # Calculate percent gain from bottom
                if local_bottom > 0:
                    percent_gain = ((current_price - local_bottom) / local_bottom) * 100
                    
                    # Check if target reached
                    if percent_gain >= target_percent:
                        print(f"🚀 {data['symbol']}: +{percent_gain:.1f}% from bottom!")
                        
                        # Send Pushover alert
                        send_pushover_alert(token_info, ca, percent_gain, local_bottom)
                        
                        # Mark for removal
                        tokens_to_remove.append(ca)
                    else:
                        print(f"📊 {data['symbol']}: ${current_price:.6f} (+{percent_gain:.1f}% from bottom, target: +{target_percent}%)")
            
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
