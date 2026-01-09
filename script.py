import requests
target = '0x1ff49fdcb6685c94059b65620f43a683be0ce7a5'

# Check positions
r = requests.get(f'https://data-api.polymarket.com/positions?user={target}')
positions = r.json()
print(f'Positions count: {len(positions)}')

total = 0
for p in positions[:10]:
    size = float(p.get('size', 0))
    price = float(p.get('price', 0))
    value = size * price
    total += value
    print(f'  {p.get(\"title\", \"?\")[:40]}: size={size:.2f}, price={price:.2f}, value=${value:.2f}')

print(f'...')
print(f'Estimated total (from positions): ${total:.2f}')

# Check value endpoint
r2 = requests.get(f'https://data-api.polymarket.com/value?user={target}')
print(f'Value endpoint: {r2.json()}')