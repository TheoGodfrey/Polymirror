import os
import requests
import json
from dotenv import load_dotenv

load_dotenv()

TARGET = os.getenv("TARGET_ADDRESS")
URL = "https://data-api.polymarket.com/positions"

print(f"Checking positions for: {TARGET}")

try:
    resp = requests.get(URL, params={"user": TARGET})
    print(f"Status Code: {resp.status_code}")
    
    if resp.ok:
        data = resp.json()
        print(f"Raw Response Type: {type(data)}")
        if isinstance(data, list):
            print(f"Number of items found: {len(data)}")
            if len(data) > 0:
                print("First item sample:")
                print(json.dumps(data[0], indent=2))
                
                # Check why they might be filtered
                print("\n--- Checking Filter Logic ---")
                filtered_count = 0
                for p in data:
                    size = float(p.get("size", 0))
                    if size < 0.1: # This is the threshold in the new script
                        filtered_count += 1
                print(f"Positions smaller than 0.1: {filtered_count}")
        else:
            print("Response is not a list!")
            print(data)
    else:
        print("Response not OK")
        print(resp.text)

except Exception as e:
    print(f"Error: {e}")