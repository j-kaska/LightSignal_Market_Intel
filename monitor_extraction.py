#!/usr/bin/env python3
"""Monitor article extraction progress."""
import time
import re
from pathlib import Path

log_file = Path('data/articles/logs/2026-07-15_10-02_articles.log')

print("Monitoring extraction progress... (updating every 30s)\n")
start_time = time.time()

while True:
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Extract latest progress
        matches = re.findall(r'\[  (\d+)/118\]', content)
        if matches:
            latest = int(matches[-1])
            elapsed = int(time.time() - start_time)
            print(f"[{elapsed//60}m {elapsed%60}s] Article {latest}/118 in progress...")
        
        # Check for completion
        if 'Stage \'Extract\' complete in' in content:
            print("\n✓ EXTRACTION COMPLETE")
            # Extract final stats
            lines = content.split('\n')
            for i, line in enumerate(lines[-20:]):
                if 'complete in' in line.lower() or 'progress saved' in line.lower():
                    print(f"  {line.strip()}")
            break
        
        # Check for pipeline completion
        if 'Pipeline complete' in content or 'Article pipeline complete' in content:
            print("\n✓ FULL PIPELINE COMPLETE")
            break
        
        # Check for fatal errors
        if '[ERROR]' in content[max(0, len(content)-2000):]:
            error_lines = [l for l in content.split('\n') if '[ERROR]' in l]
            if error_lines:
                print(f"\n✗ Fatal error: {error_lines[-1]}")
                break
        
        time.sleep(30)
    except KeyboardInterrupt:
        print("\nMonitoring stopped by user.")
        break
    except Exception as e:
        print(f"Monitor error: {e}")
        time.sleep(30)
