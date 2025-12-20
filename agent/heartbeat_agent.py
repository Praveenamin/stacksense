#!/usr/bin/env python3
"""
StackSense Heartbeat Agent

Lightweight agent script that sends heartbeat signals to the monitoring server
every 30 seconds to indicate the server is online.

Usage:
    python3 heartbeat_agent.py

Configuration:
    Set SERVER_ID and API_URL environment variables, or use config file.
"""

import os
import sys
import time
import json
import requests
from datetime import datetime
from pathlib import Path

# Configuration
CONFIG_FILE = Path.home() / ".stacksense_heartbeat.conf"
DEFAULT_INTERVAL = 30  # seconds
HEARTBEAT_TIMEOUT = 10  # seconds for HTTP request timeout
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


def load_config():
    """Load configuration from environment variables or config file"""
    config = {
        'server_id': os.environ.get('STACKSENSE_SERVER_ID'),
        'api_url': os.environ.get('STACKSENSE_API_URL'),
        'interval': int(os.environ.get('STACKSENSE_INTERVAL', DEFAULT_INTERVAL)),
    }
    
    # Try to load from config file if env vars not set
    if not config['server_id'] or not config['api_url']:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r') as f:
                    file_config = json.load(f)
                    config.update(file_config)
            except Exception as e:
                print(f"Error reading config file: {e}", file=sys.stderr)
    
    return config


def send_heartbeat(server_id, api_url, agent_version=None):
    """Send heartbeat signal to monitoring server"""
    url = f"{api_url.rstrip('/')}/api/heartbeat/{server_id}/"
    
    payload = {}
    if agent_version:
        payload['agent_version'] = agent_version
    
    headers = {
        'Content-Type': 'application/json',
    }
    
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=HEARTBEAT_TIMEOUT
            )
            response.raise_for_status()
            return True, response.json()
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
                continue
            return False, "Request timeout"
        except requests.exceptions.ConnectionError:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
                continue
            return False, "Connection error"
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
                continue
            return False, str(e)
    
    return False, "Max retries exceeded"


def main():
    """Main agent loop"""
    config = load_config()
    
    # Validate configuration
    if not config.get('server_id'):
        print("Error: SERVER_ID not configured. Set STACKSENSE_SERVER_ID environment variable or create config file.", file=sys.stderr)
        sys.exit(1)
    
    if not config.get('api_url'):
        print("Error: API_URL not configured. Set STACKSENSE_API_URL environment variable or create config file.", file=sys.stderr)
        sys.exit(1)
    
    server_id = config['server_id']
    api_url = config['api_url']
    interval = config['interval']
    agent_version = "1.0.0"  # Can be updated as agent evolves
    
    print(f"Heartbeat agent starting...")
    print(f"  Server ID: {server_id}")
    print(f"  API URL: {api_url}")
    print(f"  Interval: {interval} seconds")
    print(f"  Agent Version: {agent_version}")
    print()
    
    consecutive_failures = 0
    max_consecutive_failures = 10
    
    try:
        while True:
            success, result = send_heartbeat(server_id, api_url, agent_version)
            
            if success:
                consecutive_failures = 0
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{timestamp}] Heartbeat sent successfully")
            else:
                consecutive_failures += 1
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{timestamp}] Heartbeat failed: {result}", file=sys.stderr)
                
                if consecutive_failures >= max_consecutive_failures:
                    print(f"Error: {max_consecutive_failures} consecutive failures. Exiting.", file=sys.stderr)
                    sys.exit(1)
            
            # Wait for next interval
            time.sleep(interval)
            
    except KeyboardInterrupt:
        print("\nHeartbeat agent stopped by user")
        sys.exit(0)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

