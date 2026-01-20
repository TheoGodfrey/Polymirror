#!/usr/bin/env python3
"""
Polymarket Trader Scanner (Pipeline Step 1)
-------------------------------------------
✅ UPDATED: Fetches via Pagination (Offset) to get thousands of candidates.
✅ UPDATED: Top N set to 1000.
✅ UPDATED: Progress bars to track the long scan.
"""

import requests
import json
import time
import pandas as pd
import sys
from datetime import datetime, timedelta

# --- Configuration ---
OUTPUT_FILE = "whitelist.json"
TOP_N_TO_SELECT = 1000          # User requested 1000
SCAN_DEPTH = 3000               # How many rows to fetch from leaderboard (3000 to find 1000 good ones)
BATCH_SIZE = 100                # API limit per call

# Risk Settings
SLIPPAGE_SETTING = 0.01          
MAX_POSITION_USDC = 50          

# API Endpoints
LEADERBOARD_BASE = "https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit={}&offset={}"
ACTIVITY_API = "https://data-api.polymarket.com/activity?user={}&limit=10"

# --- Thresholds ---
MIN_VOLUME = 2000               # Lowered to find more traders in lower ranks
MIN_ROI = 0.05                  # Keep strict: We don't want losers, even if we want volume
                                # Note: If you want MORE people, lower this to 0.02 (2%)

def fetch_all_leaderboard():
    print(f"🔍 Scanning Global Leaderboard (Depth: {SCAN_DEPTH})...")
    all_data = []
    
    # Pagination Loop
    for offset in range(0, SCAN_DEPTH, BATCH_SIZE):
        url = LEADERBOARD_BASE.format(BATCH_SIZE, offset)
        try:
            sys.stdout.write(f"\r   Fetching ranks {offset} - {offset+BATCH_SIZE}...")
            sys.stdout.flush()
            
            resp = requests.get(url, timeout=10)
            if not resp.ok: break
            
            data = resp.json()
            if not data: break # End of list
            
            all_data.extend(data)
            time.sleep(0.1) # Be nice to API
            
        except Exception as e:
            print(f"\n❌ Error at offset {offset}: {e}")
            break
            
    print(f"\n✅ Fetch Complete. Total raw candidates: {len(all_data)}")
    
    df = pd.DataFrame(all_data)
    if df.empty: return pd.DataFrame()

    # Normalize Columns
    column_map = {'pnl': 'profit', 'vol': 'volume', 'proxyWallet': 'address', 'userName': 'name'}
    df.rename(columns=column_map, inplace=True)

    # Cleanup
    df['profit'] = pd.to_numeric(df['profit'], errors='coerce').fillna(0)
    df['volume'] = pd.to_numeric(df['volume'], errors='coerce').fillna(0)
    
    # ROI Calculation
    df['roi'] = df.apply(lambda row: row['profit'] / row['volume'] if row['volume'] > 0 else 0, axis=1)
    
    return df

def analyze_trader(address):
    """
    Checks if trader is active (Last 7 days).
    """
    try:
        resp = requests.get(ACTIVITY_API.format(address), timeout=5)
        if resp.status_code == 429: # Rate limit
            time.sleep(2)
            return False, "Rate Limit"
            
        if not resp.ok: return False, "API Error"
        
        activities = resp.json()
        if not activities: return False, "No Activity"
        
        # Timestamp check
        last_ts = int(activities[0]['timestamp'])
        if last_ts > 1000000000000: last_ts /= 1000 # Handle MS timestamps
        
        last_date = datetime.fromtimestamp(last_ts)
        if datetime.now() - last_date > timedelta(days=7):
            return False, "Inactive"
            
        return True, "Active"

    except Exception as e:
        return False, f"Error"

def generate_whitelist():
    df = fetch_all_leaderboard()
    if df.empty: return

    # 1. Broad Filter (ROI & Volume)
    print(f"📉 Filtering candidates (Min Vol: ${MIN_VOLUME}, Min ROI: {MIN_ROI*100}%)...")
    candidates = df[
        (df['volume'] >= MIN_VOLUME) & 
        (df['roi'] >= MIN_ROI)
    ].copy()
    
    print(f"🕵️ Analyzing {len(candidates)} candidates for activity...")
    
    valid_traders = []
    
    # Iterate candidates
    total_scanned = 0
    for index, row in candidates.iterrows():
        total_scanned += 1
        addr = row['address']
        name = row.get('name', '') 
        if not name: name = addr[:8]
        
        # Activity Check
        is_valid, status = analyze_trader(addr)
        
        if is_valid:
            # Score = Profit * ROI
            score = row['profit'] * row['roi']
            
            valid_traders.append({
                "name": name,
                "address": addr,
                "roi": row['roi'],
                "profit": row['profit'],
                "score": score
            })
            
            # Print progress every 10 finds
            if len(valid_traders) % 10 == 0:
                sys.stdout.write(f"\r   ✅ Found {len(valid_traders)} sharps so far (Scanned {total_scanned})...")
                sys.stdout.flush()

        if len(valid_traders) >= TOP_N_TO_SELECT:
            break
        
        # Rate Limiting is crucial for 1000 checks
        time.sleep(0.05) 
    
    print(f"\n📊 Sorting top {TOP_N_TO_SELECT}...")
    valid_traders.sort(key=lambda x: x['score'], reverse=True)
    
    # 2. Write JSON
    output_data = {
        "global_settings": {
            "max_slippage": SLIPPAGE_SETTING,
            "max_position_usdc": MAX_POSITION_USDC,
            "min_trade_usdc": 5.0,
            "default_sizing_mode": "fixed",
            "default_fixed_amount": 10
        },
        "whitelist": []
    }
    
    for t in valid_traders:
        entry = {
            "name": f"{str(t['name'])} (ROI {t['roi']:.0%})",
            "address": t['address'],
            "sizing_mode": "fixed",
            "fixed_amount": 20, 
            "max_slippage": SLIPPAGE_SETTING
        }
        output_data['whitelist'].append(entry)
        
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(output_data, f, indent=2)
        
    print(f"\n🎉 Success! Wrote {len(valid_traders)} sharps to {OUTPUT_FILE}")

if __name__ == "__main__":
    generate_whitelist()