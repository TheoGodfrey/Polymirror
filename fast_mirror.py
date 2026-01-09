import os
import time
import requests
import logging
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

# --- CONFIGURATION ---
# Define Side Constants Manually to fix ImportError
BUY = "BUY"
SELL = "SELL"

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger()

# Load Config
load_dotenv()
TARGET_ADDRESS = "0x1ff49fdcb6685c94059b65620f43a683be0ce7a5"  # Your HFT Target
CHAIN_ID = 137
RPC_URL = "https://polygon-rpc.com" # Public RPC

# USDC Contract (Polygon)
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_ABI = '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"}]'

# Init Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
usdc_contract = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)

# Init Polymarket Client
host = "https://clob.polymarket.com"
key = os.getenv("PRIVATE_KEY")
creds = {
    "api_key": os.getenv("API_KEY"),
    "api_secret": os.getenv("API_SECRET"),
    "api_passphrase": os.getenv("API_PASSPHRASE"),
}
client = ClobClient(host, key=key, chain_id=CHAIN_ID, signature_type=1, creds=creds)

def get_target_equity(address):
    """Fetches Target's USDC + Position Value."""
    try:
        # 1. Get USDC Balance (Web3)
        # Note: We use Web3 because the CLOB API cannot fetch private balances of other users
        raw_bal = usdc_contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
        usdc_val = float(raw_bal) / 1e6

        # 2. Get Positions Value (Data API)
        r = requests.get(f"https://data-api.polymarket.com/positions?user={address}")
        if r.status_code != 200:
            return None, {}
        positions = r.json()
        
        pos_val = 0
        parsed_positions = {}
        
        if isinstance(positions, list):
            for p in positions:
                size = float(p.get('size', 0))
                price = float(p.get('avgPrice', 0)) # Use avgPrice for value estimate
                asset_id = p.get('asset_id')
                if not asset_id: continue # Skip invalid
                
                pos_val += size * price
                parsed_positions[asset_id] = size

        total_equity = usdc_val + pos_val
        return total_equity, parsed_positions

    except Exception as e:
        logger.error(f"Error fetching target equity: {e}")
        return None, {}

def get_my_equity():
    """Fetches Our USDC Balance (Cash)."""
    try:
        # We size based on our CASH, not total equity (safer)
        bal_resp = client.get_balance_allowance(asset_type="COLLATERAL")
        cash = float(bal_resp['balance']) / 1e6
        return cash 
    except Exception as e:
        logger.error(f"Error fetching my equity: {e}")
        return 0

def place_mirror_trade(asset_id, amount_to_buy_usd):
    try:
        # Fetch orderbook to get best price
        book = client.get_order_book(asset_id)
        if not book.asks:
            logger.warning(f"No asks for {asset_id}")
            return

        best_ask = float(book.asks[0].price)
        if best_ask <= 0: return

        # Calculate size in shares: USD / Price
        size = amount_to_buy_usd / best_ask
        
        # Round size to 2 decimal places to be safe with API
        size = round(size, 2)

        # Place Order (IOC - Immediate or Cancel to avoid resting orders)
        logger.info(f"🚀 EXECUTING: Buy ${amount_to_buy_usd:.2f} of {asset_id} @ {best_ask} (Size: {size})")
        
        order_args = OrderArgs(
            price=best_ask,
            size=size,
            side=BUY,
            token_id=asset_id, # Library requires 'token_id'
        )
        resp = client.create_order(order_args)
        logger.info(f"✅ Trade sent: {resp}")
        
    except Exception as e:
        logger.error(f"Trade failed: {e}")

def main():
    logger.info("Starting High-Frequency Mirror Bot...")
    logger.info(f"Target: {TARGET_ADDRESS}")
    
    # --- INITIAL SYNC (BASELINE) ---
    # We fetch positions but DO NOT trade. This sets the baseline.
    logger.info("Performing initial sync (setting baseline)...")
    target_equity, known_positions = get_target_equity(TARGET_ADDRESS)
    
    if target_equity is None:
        logger.error("Could not fetch initial target data. Retrying...")
        time.sleep(5)
        return main()

    logger.info(f"Baseline set. Target Equity: ${target_equity:.2f}. Positions: {len(known_positions)}")
    logger.info("Waiting for NEW trades...")

    # --- MAIN LOOP ---
    while True:
        try:
            # 1. Fetch Target State
            new_equity, new_positions = get_target_equity(TARGET_ADDRESS)
            if new_positions is None: 
                time.sleep(1)
                continue

            # 2. Check for Increases (Buys)
            for asset_id, new_size in new_positions.items():
                old_size = known_positions.get(asset_id, 0)
                
                # Threshold to ignore tiny dust changes (e.g. 0.01 shares)
                if new_size > old_size + 0.1: 
                    delta_shares = new_size - old_size
                    
                    # 3. Calculate Our Size
                    # Ratio = My Cash / Target Total Equity
                    my_cash = get_my_equity()
                    ratio = my_cash / new_equity if new_equity > 0 else 0
                    
                    # Cap ratio (max 10% of portfolio per trade)
                    ratio = min(ratio, 0.10) 
                    
                    # They bought 'delta_shares'.
                    # We copy that SIZE * RATIO.
                    my_buy_size_shares = delta_shares * ratio
                    
                    # Fetch price to calculate USD cost for logging/checking
                    book = client.get_order_book(asset_id)
                    if book.asks:
                        price = float(book.asks[0].price)
                        cost_usd = my_buy_size_shares * price
                        
                        logger.info(f"🔔 Target bought ~{delta_shares:.2f} shares. Ratio {ratio:.4f}. We want {my_buy_size_shares:.2f} shares (${cost_usd:.2f})")
                        
                        if cost_usd > 1.0: # Min trade $1
                            place_mirror_trade(asset_id, cost_usd)
                        else:
                            logger.info(f"Trade too small (${cost_usd:.2f}), skipping.")
            
            # Update state
            known_positions = new_positions
            
            # Sleep (Fast polling)
            time.sleep(1.0)
            
        except KeyboardInterrupt:
            logger.info("Stopping...")
            break
        except Exception as e:
            logger.error(f"Loop error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()