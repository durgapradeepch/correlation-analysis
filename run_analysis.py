#!/usr/bin/env python3
"""
Complete analysis pipeline: run engine and convert insights for dashboard.
"""

import subprocess
import sys
import argparse
import os


def run_command(cmd, description):
    """Run a command and handle errors."""
    print(f"\n🔄 {description}...")
    print(f"Running: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"✅ {description} completed successfully")
        if result.stdout:
            print(result.stdout)
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {description} failed with exit code {e.returncode}")
        if e.stdout:
            print("STDOUT:", e.stdout)
        if e.stderr:
            print("STDERR:", e.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description='Run complete analysis pipeline')
    parser.add_argument('--input', default='alerts.json', help='Input alerts file/directory')
    parser.add_argument('--skip-engine', action='store_true', help='Skip engine run, only convert existing insights')
    parser.add_argument('--window', type=int, default=900, help='Analysis window in seconds')
    parser.add_argument('--dedup-ttl', type=int, default=180, help='Deduplication TTL in seconds (increased for better aggregation)')
    parser.add_argument('--episode-gap', type=int, default=600, help='Episode gap in seconds (increased to merge bursts)')
    parser.add_argument('--max-lag', type=int, default=90, help='Max lag in seconds')
    parser.add_argument('--min-support', type=int, default=2, help='Minimum support for correlations (lowered for more correlations)')
    parser.add_argument('--graph', help='Optional dependency graph JSON file')
    
    args = parser.parse_args()
    
    print("🚀 Starting Alert Analysis Pipeline")
    print("=" * 50)
    
    # Step 1: Run the engine (unless skipped)
    if not args.skip_engine:
        engine_cmd = [
            'python3', 'engine.py',
            '--input', args.input,
            '--out', 'public/vl_insights.jsonl',
            '--window', str(args.window),
            '--hop', '1',
            '--dedup-ttl', str(args.dedup_ttl),
            '--episode-gap', str(args.episode_gap),
            '--max-lag', str(args.max_lag),
            '--min-support', str(args.min_support)
        ]
        
        if args.graph:
            engine_cmd.extend(['--graph', args.graph])
        
        if not run_command(engine_cmd, "Alert Analysis Engine"):
            print("❌ Pipeline failed at engine step")
            sys.exit(1)
    else:
        print("⏭️  Skipping engine run (using existing insights)")
    
    # Step 2: Convert insights for dashboard
    convert_cmd = [
        'python3', 'convert_insights.py',
        '--input', 'public/vl_insights.jsonl',
        '--output', 'dashboard/public/insights.json'
    ]
    
    if not run_command(convert_cmd, "Insights Conversion"):
        print("❌ Pipeline failed at conversion step")
        sys.exit(1)
    
    # Step 3: Check if dashboard is running
    print("\n🔍 Checking dashboard status...")
    
    try:
        # Check if port 3000 is in use (dashboard running)
        result = subprocess.run(['lsof', '-i', ':3000'], capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ Dashboard appears to be running on http://localhost:3000")
            print("🔄 Refresh your browser to see the updated insights")
        else:
            print("ℹ️  Dashboard not running. Start it with:")
            print("   cd dashboard && npm start")
    except FileNotFoundError:
        print("ℹ️  Could not check dashboard status (lsof not available)")
        print("   If dashboard is running, refresh your browser to see updates")
    
    print("\n🎉 Analysis pipeline completed successfully!")
    print("=" * 50)
    print("📊 Results:")
    print(f"   • Engine output: public/vl_insights.jsonl")
    print(f"   • Dashboard data: dashboard/public/insights.json")
    print(f"   • Dashboard URL: http://localhost:3000")


if __name__ == '__main__':
    main()
