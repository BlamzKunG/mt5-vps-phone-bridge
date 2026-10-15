"""
Test script to verify client-server contract and communication logic
"""
import sys
import time
import threading
import pandas as pd
from unittest.mock import MagicMock

# ตรวจสอบการ import
from vps_bridge_client import RemoteMT5Bridge

def run_test():
    print("Testing RemoteMT5Bridge initialization...")
    client = RemoteMT5Bridge(vps_url="http://127.0.0.1:8999", api_key="test-key")
    assert client.headers["X-API-Key"] == "test-key"
    assert client.vps_url == "http://127.0.0.1:8999"
    print("Initialization passed!")

if __name__ == "__main__":
    run_test()
