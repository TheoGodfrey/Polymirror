#!/usr/bin/env python3
"""
Polymarket Comprehensive Market Scanner
========================================
Scans ALL active markets individually to find whale activity.
Uses PER-MARKET statistical analysis - a whale is detected relative to
that specific market's normal trading pattern, not a global threshold.

Key Features:
- Detects outliers using Z-score relative to each market
- Automatically adjusts for market size/liquidity
- A $500 trade can be a whale in a small market
- A $50k trade might be normal in a huge market

Usage:
    python comprehensive_scan.py                    # Scan ALL markets (default)
    python comprehensive_scan.py --fast             # Scan top 200 markets (faster)
    python comprehensive_scan.py --z-score 2.0      # More sensitive
    python comprehensive_scan.py --z-score 3.5      # Less sensitive (only extreme outliers)
    python comprehensive_scan.py --hours 12         # Last 12 hours only
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
import time
import json
from pathlib import Path
import argparse

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

class ComprehensiveScanner:
    def __init__(self, lookback_hours=24, z_threshold=2.5, min_absolute=100, percentile_threshold=90):
        self.lookback_hours = lookback_hours
        self.z_threshold = z_threshold  # Z-score threshold for outliers
        self.min_absolute = min_absolute  # Absolute minimum to avoid spam ($100+)
        self.percentile_threshold = percentile_threshold  # Top X% of trades in market
        self.session = requests.Session()
        self.all_whales = {}
        self.market_stats = {}
        
        # Cutoff timestamp
        self.cutoff_time = int((datetime.now() - timedelta(hours=lookback_hours)).timestamp())
    
    def get_markets(self):
        """Get ALL active markets with pagination"""
        url = f"{GAMMA_API}/markets"
        all_markets = []
        offset = 0
        batch_size = 100  # Fetch in batches of 100
        
        print("   Fetching all markets (this may take a minute)...")
        
        while True:
            params = {
                'active': 'true',
                'closed': 'false',
                'limit': batch_size,
                'offset': offset,
                'order': 'volume24hr',
                'ascending': 'false'
            }
            
            try:
                response = self.session.get(url, params=params, timeout=30)
                response.raise_for_status()
                markets = response.json()
                
                if not markets:
                    break
                
                all_markets.extend(markets)
                offset += batch_size
                
                if offset % 500 == 0:
                    print(f"   Fetched {offset} markets so far...")
                
                # If we got less than batch_size, we're done
                if len(markets) < batch_size:
                    break
                
                time.sleep(0.2)  # Rate limiting between batches
                
            except Exception as e:
                print(f"   Error at offset {offset}: {e}")
                break
        
        return all_markets
    
    def get_market_trades(self, condition_id, limit=500):
        """Get trades for a specific market"""
        url = f"{DATA_API}/trades"
        params = {
            'market': condition_id,
            'limit': limit,
            'takerOnly': 'false'
        }
        try:
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            return []
    
    def scan_all_markets(self, max_markets=None):
        """Scan all markets for whale trades using per-market statistics"""
        print("="*70)
        print("COMPREHENSIVE POLYMARKET WHALE SCAN")
        print(f"Lookback: {self.lookback_hours}h | Z-Score: >{self.z_threshold} | "
              f"Percentile: >{self.percentile_threshold}%")
        print("="*70)
        
        # Phase 1: Get all markets
        print("\n📊 Phase 1: Fetching ALL active markets...")
        markets = self.get_markets()
        print(f"   ✓ Found {len(markets)} total active markets")
        
        # Filter to markets with decent volume
        active_markets = [m for m in markets if float(m.get('volume24hr', 0) or 0) > 50]
        print(f"   {len(active_markets)} markets with >$50 daily volume")
        
        # Optionally limit number of markets
        if max_markets and len(active_markets) > max_markets:
            print(f"   Limiting to top {max_markets} markets by volume")
            active_markets = active_markets[:max_markets]
        
        # Phase 2: Scan each market with per-market statistics
        print(f"\n🔍 Phase 2: Scanning {len(active_markets)} markets...")
        print(f"   Detecting outliers RELATIVE to each market's baseline")
        print(f"   (Takes ~{len(active_markets)*0.3/60:.1f} minutes)")
        
        whale_trades = []
        market_summaries = []
        
        for i, market in enumerate(active_markets, 1):
            condition_id = market.get('conditionId', '')
            title = market.get('question', '')[:50]
            volume_24h = float(market.get('volume24hr', 0) or 0)
            
            if i % 20 == 0 or i == 1:
                print(f"   Progress: {i}/{len(active_markets)} markets...")
            
            # Get trades for this market
            trades = self.get_market_trades(condition_id, limit=500)
            
            # Filter to timeframe
            recent_trades = [t for t in trades 
                           if t.get('timestamp', 0) >= self.cutoff_time]
            
            if not recent_trades or len(recent_trades) < 5:
                time.sleep(0.1)
                continue
            
            # Calculate THIS MARKET's statistics
            trade_values = []
            for trade in recent_trades:
                size = trade.get('size', 0)
                price = trade.get('price', 0)
                value = size * price
                if value >= self.min_absolute:  # Only consider real trades
                    trade_values.append(value)
            
            if not trade_values or len(trade_values) < 5:
                time.sleep(0.1)
                continue
            
            # Per-market statistics
            mean = np.mean(trade_values)
            std = np.std(trade_values)
            median = np.median(trade_values)
            p_threshold = np.percentile(trade_values, self.percentile_threshold)
            
            # Find outliers in THIS market
            market_whales = []
            for trade in recent_trades:
                size = trade.get('size', 0)
                price = trade.get('price', 0)
                value = size * price
                
                if value < self.min_absolute:
                    continue
                
                # Calculate Z-score relative to THIS market
                z_score = (value - mean) / std if std > 0 else 0
                percentile_rank = sum(1 for v in trade_values if v <= value) / len(trade_values) * 100
                
                # Detect outlier
                is_outlier = False
                reason = []
                
                if z_score >= self.z_threshold:
                    is_outlier = True
                    reason.append(f"Z={z_score:.1f}")
                
                if value >= p_threshold:
                    is_outlier = True
                    reason.append(f"Top{100-self.percentile_threshold}%")
                
                # Market impact
                market_pct = (value / volume_24h * 100) if volume_24h > 0 else 0
                if market_pct >= 5:
                    is_outlier = True
                    reason.append(f"{market_pct:.1f}% of daily vol")
                
                if not is_outlier:
                    continue
                
                # This is a whale trade!
                trade['_value'] = value
                trade['_z_score'] = z_score
                trade['_percentile'] = percentile_rank
                trade['_market_pct'] = market_pct
                trade['_market_title'] = title
                trade['_market_slug'] = market.get('slug', '')
                trade['_volume_24h'] = volume_24h
                trade['_market_mean'] = mean
                trade['_market_median'] = median
                trade['_detection_reason'] = ', '.join(reason)
                
                market_whales.append(trade)
                whale_trades.append(trade)
                
                # Track whale wallet
                addr = trade.get('proxyWallet', '')
                if addr not in self.all_whales:
                    self.all_whales[addr] = {
                        'name': trade.get('name', '') or trade.get('pseudonym', ''),
                        'total_volume': 0,
                        'num_trades': 0,
                        'largest_trade': 0,
                        'markets_traded': set()
                    }
                
                self.all_whales[addr]['total_volume'] += value
                self.all_whales[addr]['num_trades'] += 1
                self.all_whales[addr]['largest_trade'] = max(
                    self.all_whales[addr]['largest_trade'], value
                )
                self.all_whales[addr]['markets_traded'].add(condition_id)
            
            # Market summary
            if trade_values:
                market_summaries.append({
                    'market': title,
                    'condition_id': condition_id,
                    'volume_24h': volume_24h,
                    'num_trades': len(trade_values),
                    'total_traded': sum(trade_values),
                    'mean_trade': mean,
                    'median_trade': median,
                    'max_trade': max(trade_values),
                    'num_whales': len(market_whales),
                    'whale_volume': sum(t['_value'] for t in market_whales)
                })
            
            time.sleep(0.25)
        
        print(f"   ✓ Scan complete!")
        
        # Phase 3: Calculate global statistics
        print("\n📐 Phase 3: Analyzing results...")
        
        if not whale_trades:
            print("   No whale trades found in this timeframe!")
            return None
        
        all_values = [t['_value'] for t in whale_trades]
        
        global_stats = {
            'num_whale_trades': len(whale_trades),
            'total_whale_volume': sum(all_values),
            'mean_whale_trade': np.mean(all_values),
            'median_whale_trade': np.median(all_values),
            'largest_whale_trade': max(all_values),
            'num_markets_with_whales': len(set(t.get('conditionId', '') for t in whale_trades)),
            'num_unique_whales': len(self.all_whales),
            'avg_z_score': np.mean([t['_z_score'] for t in whale_trades]),
            'avg_market_pct': np.mean([t['_market_pct'] for t in whale_trades if t['_market_pct'] > 0])
        }
        
        # Sort by value
        whale_trades.sort(key=lambda x: x['_value'], reverse=True)
        
        return {
            'whale_trades': whale_trades,
            'global_stats': global_stats,
            'market_summaries': market_summaries,
            'whales': self.all_whales
        }
    
    def print_report(self, results):
        """Print comprehensive report"""
        if not results:
            return
        
        whale_trades = results['whale_trades']
        stats = results['global_stats']
        market_summaries = results['market_summaries']
        whales = results['whales']
        
        print("\n" + "="*70)
        print("SUMMARY STATISTICS")
        print("="*70)
        print(f"Time Period: Last {self.lookback_hours} hours")
        print(f"Detection Method: Per-market Z-score >{self.z_threshold} OR Top {100-self.percentile_threshold}%")
        print(f"")
        print(f"Whale Trades Found: {stats['num_whale_trades']}")
        print(f"Total Whale Volume: ${stats['total_whale_volume']:,.0f}")
        print(f"Markets with Whales: {stats['num_markets_with_whales']}")
        print(f"Unique Whale Wallets: {stats['num_unique_whales']}")
        print(f"")
        print(f"Mean Whale Trade: ${stats['mean_whale_trade']:,.0f}")
        print(f"Median Whale Trade: ${stats['median_whale_trade']:,.0f}")
        print(f"Largest Trade: ${stats['largest_whale_trade']:,.0f}")
        print(f"Avg Z-Score: {stats['avg_z_score']:.2f}")
        print(f"Avg Market Impact: {stats['avg_market_pct']:.2f}%")
        
        # Top whale trades
        print("\n" + "="*70)
        print("TOP 20 WHALE TRADES")
        print("="*70)
        
        for i, trade in enumerate(whale_trades[:20], 1):
            value = trade['_value']
            title = trade['_market_title'][:45]
            trader = trade.get('name', '') or trade.get('pseudonym', '') or trade.get('proxyWallet', '')[:10]
            side = trade.get('side', '')
            outcome = trade.get('outcome', '')
            price = trade.get('price', 0)
            z_score = trade['_z_score']
            market_pct = trade['_market_pct']
            market_mean = trade['_market_mean']
            market_median = trade['_market_median']
            detection_reason = trade['_detection_reason']
            timestamp = trade.get('timestamp', 0)
            
            # Severity based on Z-score and market impact
            if z_score >= 4 or market_pct >= 20:
                emoji = "🚨"
                severity = "CRITICAL"
            elif z_score >= 3 or market_pct >= 10:
                emoji = "⚠️"
                severity = "HIGH"
            else:
                emoji = "🐋"
                severity = "WHALE"
            
            time_str = datetime.fromtimestamp(timestamp).strftime('%m/%d %H:%M') if timestamp else '?'
            
            print(f"\n{i}. {emoji} {severity}: ${value:,.0f} @ {time_str}")
            print(f"   Market: {title}")
            print(f"   Trader: {trader}")
            print(f"   Action: {side} {outcome} @ {price:.3f}")
            print(f"   Detection: {detection_reason}")
            print(f"   Market Context: Mean=${market_mean:,.0f}, Median=${market_median:,.0f}")
            print(f"   Outlier Factor: {value/market_mean:.1f}x mean trade size")
            print(f"   → https://polymarket.com/event/{trade.get('_market_slug', '')}")
        
        # Most active markets
        print("\n" + "="*70)
        print("MARKETS WITH MOST WHALE ACTIVITY")
        print("="*70)
        
        market_summaries.sort(key=lambda x: x['num_whales'], reverse=True)
        for i, m in enumerate(market_summaries[:10], 1):
            whale_pct = (m['whale_volume'] / m['total_traded'] * 100) if m['total_traded'] > 0 else 0
            print(f"\n{i}. {m['market']}")
            print(f"   Whale Trades: {m['num_whales']} | Normal Trades: {m['num_trades'] - m['num_whales']}")
            print(f"   24h Volume: ${m['volume_24h']:,.0f} | Max Trade: ${m['max_trade']:,.0f}")
            print(f"   Mean Trade: ${m['mean_trade']:,.0f} | Median: ${m['median_trade']:,.0f}")
            print(f"   {whale_pct:.1f}% of volume from whale trades")
        
        # Top whales
        print("\n" + "="*70)
        print("TOP WHALE WALLETS")
        print("="*70)
        
        sorted_whales = sorted(whales.items(), 
                              key=lambda x: x[1]['total_volume'], 
                              reverse=True)
        
        for i, (addr, info) in enumerate(sorted_whales[:15], 1):
            name = info['name'] or addr[:12]
            print(f"\n{i}. {name}")
            print(f"   Volume: ${info['total_volume']:,.0f} | Trades: {info['num_trades']}")
            print(f"   Largest: ${info['largest_trade']:,.0f} | Markets: {len(info['markets_traded'])}")
            print(f"   → https://polymarket.com/profile/{addr}")
        
        # Time distribution
        print("\n" + "="*70)
        print("ACTIVITY BY HOUR")
        print("="*70)
        
        hourly_volume = defaultdict(float)
        hourly_count = defaultdict(int)
        
        for trade in whale_trades:
            ts = trade.get('timestamp', 0)
            if ts:
                hour = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:00')
                hourly_volume[hour] += trade['_value']
                hourly_count[hour] += 1
        
        for hour in sorted(hourly_volume.keys())[-12:]:  # Last 12 hours with activity
            vol = hourly_volume[hour]
            count = hourly_count[hour]
            bar = '█' * int(vol / 10000)
            print(f"{hour}: ${vol:>10,.0f} ({count:>3} trades) {bar}")
    
    def save_results(self, results):
        """Save to files"""
        if not results:
            return
        
        output_dir = Path("./whale_alerts")
        output_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Save whale trades
        df_trades = pd.DataFrame([{
            'timestamp': datetime.fromtimestamp(t.get('timestamp', 0)).isoformat() if t.get('timestamp') else '',
            'value': t['_value'],
            'market': t['_market_title'],
            'trader': t.get('name', '') or t.get('pseudonym', ''),
            'trader_address': t.get('proxyWallet', ''),
            'side': t.get('side', ''),
            'outcome': t.get('outcome', ''),
            'price': t.get('price', 0),
            'z_score': t['_z_score'],
            'percentile': t['_percentile'],
            'market_pct': t['_market_pct'],
            'detection_reason': t['_detection_reason'],
            'market_mean': t['_market_mean'],
            'market_median': t['_market_median'],
            'outlier_factor': t['_value'] / t['_market_mean'] if t['_market_mean'] > 0 else 0,
            'market_url': f"https://polymarket.com/event/{t.get('_market_slug', '')}"
        } for t in results['whale_trades']])
        
        trades_file = output_dir / f"comprehensive_scan_{timestamp}.csv"
        df_trades.to_csv(trades_file, index=False)
        
        # Save whale summary
        whale_data = []
        for addr, info in results['whales'].items():
            whale_data.append({
                'address': addr,
                'name': info['name'],
                'total_volume': info['total_volume'],
                'num_trades': info['num_trades'],
                'largest_trade': info['largest_trade'],
                'num_markets': len(info['markets_traded']),
                'url': f"https://polymarket.com/profile/{addr}"
            })
        
        df_whales = pd.DataFrame(whale_data)
        df_whales = df_whales.sort_values('total_volume', ascending=False)
        whales_file = output_dir / f"whales_{timestamp}.csv"
        df_whales.to_csv(whales_file, index=False)
        
        # Save market summary
        if results['market_summaries']:
            df_markets = pd.DataFrame([{
                'market': m['market'],
                'volume_24h': m['volume_24h'],
                'num_trades': m['num_trades'],
                'num_whales': m['num_whales'],
                'whale_volume': m['whale_volume'],
                'whale_pct': (m['whale_volume'] / m['total_traded'] * 100) if m['total_traded'] > 0 else 0,
                'mean_trade': m['mean_trade'],
                'median_trade': m['median_trade'],
                'max_trade': m['max_trade'],
                'total_traded': m['total_traded']
            } for m in results['market_summaries']])
            df_markets = df_markets.sort_values('num_whales', ascending=False)
            markets_file = output_dir / f"markets_{timestamp}.csv"
            df_markets.to_csv(markets_file, index=False)
        
        print(f"\n💾 Results saved:")
        print(f"   Trades: {trades_file}")
        print(f"   Whales: {whales_file}")
        if results['market_summaries']:
            print(f"   Markets: {markets_file}")

def main():
    parser = argparse.ArgumentParser(
        description='Comprehensive Polymarket whale scanner - detects outliers relative to each market',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python comprehensive_scan.py                          # Scan all markets, default settings
  python comprehensive_scan.py --fast                   # Quick scan top 200 markets
  python comprehensive_scan.py --z-score 2.0            # More sensitive (more alerts)
  python comprehensive_scan.py --z-score 3.0            # Less sensitive (fewer alerts)
  python comprehensive_scan.py --hours 12 --percentile 95  # Last 12h, top 5% only
        """
    )
    parser.add_argument('--hours', type=int, default=24,
                       help='Lookback hours (default: 24)')
    parser.add_argument('--z-score', type=float, default=2.5,
                       help='Z-score threshold for outliers (default: 2.5, lower=more sensitive)')
    parser.add_argument('--percentile', type=int, default=90,
                       help='Percentile threshold (default: 90, means top 10%% of trades)')
    parser.add_argument('--min-absolute', type=float, default=100,
                       help='Absolute minimum trade size to consider (default: $100)')
    parser.add_argument('--max-markets', type=int, default=None,
                       help='Limit number of markets to scan (default: all markets)')
    parser.add_argument('--fast', action='store_true',
                       help='Fast mode: scan only top 200 markets by volume')
    
    args = parser.parse_args()
    
    # Fast mode overrides max_markets
    max_markets = 200 if args.fast else args.max_markets
    
    scanner = ComprehensiveScanner(
        lookback_hours=args.hours,
        z_threshold=args.z_score,
        min_absolute=args.min_absolute,
        percentile_threshold=args.percentile
    )
    
    start_time = time.time()
    
    results = scanner.scan_all_markets(max_markets=max_markets)
    
    if results:
        scanner.print_report(results)
        scanner.save_results(results)
    
    elapsed = time.time() - start_time
    print(f"\n⏱️  Scan completed in {elapsed/60:.1f} minutes")
    print("✓ Done!")

if __name__ == "__main__":
    main()