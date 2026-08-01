import os
import json
import base64
import hashlib
import requests
from datetime import datetime
from urllib.parse import urljoin

class KalshiClient:
    def __init__(self):
        self.env = os.getenv("KALSHI_ENV", "demo").lower()
        self.base_url = "https://api.elections.kalshi.com/trade-api/v2" if self.env == "live" else "https://demo-api.kalshi.com/trade-api/v2"
        self.key_id = os.getenv("KALSHI_KEY_ID", "")
        
        # Try file path first, then env var
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
        key_env = os.getenv("KALSHI_PRIVATE_KEY", "")
        
        self.private_key = None
        
        if key_path and os.path.exists(key_path):
            with open(key_path, 'r') as f:
                self.private_key = f.read().strip()
        elif key_env and "BEGIN RSA PRIVATE KEY" in key_env:
            self.private_key = key_env.replace("\\n", "\n").strip()
        
        self.session = requests.Session()
    
    def is_configured(self):
        return self.env == "live" and self.private_key is not None and self.key_id
    
    def get_balance(self):
        if not self.is_configured():
            return {"error": "Kalshi not configured", "env": self.env}
        # Placeholder - implement actual Kalshi auth + request
        return {"balance": 0, "env": self.env, "status": "connected"}
    
    def get_config(self):
        return {
            "env": self.env,
            "key_id_set": bool(self.key_id),
            "key_loaded": self.private_key is not None,
            "key_source": "file" if os.getenv("KALSHI_PRIVATE_KEY_PATH") and os.path.exists(os.getenv("KALSHI_PRIVATE_KEY_PATH", "")) else ("env" if self.private_key else "none"),
            "base_url": self.base_url
        }
