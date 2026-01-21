#!/usr/bin/env python3
"""
Clean whitelist.json - removes per-trader overrides so global caps apply.
Usage: python clean_whitelist.py whitelist.json
"""

import json
import sys

def clean_whitelist(filepath):
    with open(filepath, 'r') as f:
        data = json.load(f)
    
    # Set reasonable global settings
    data['global_settings'] = {
        "sizing_mode": "fixed",
        "fixed_amount": 1,        # £1 per trade
        "max_position_usdc": 5,   # $5 max per market  
        "max_slippage": 0.035
    }
    
    # Remove per-trader overrides (keep only address and name)
    cleaned = []
    for trader in data.get('whitelist', []):
        cleaned.append({
            "address": trader['address'],
            "name": trader.get('name', '')
        })
    
    data['whitelist'] = cleaned
    
    # Save backup
    backup_path = filepath.replace('.json', '_backup.json')
    with open(backup_path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"✅ Backup saved to {backup_path}")
    
    # Save cleaned version
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"✅ Cleaned {len(cleaned)} traders in {filepath}")
    print(f"   Global settings: $1/trade, $5 max position")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python clean_whitelist.py whitelist.json")
        sys.exit(1)
    clean_whitelist(sys.argv[1])