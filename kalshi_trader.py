import os

class KalshiClient:
    def __init__(self):
        self.env = os.getenv("KALSHI_ENV", "demo").lower()
        self.key_id = os.getenv("KALSHI_KEY_ID", "")
        self.base_url = "https://api.elections.kalshi.com/trade-api/v2" if self.env == "live" else "https://demo-api.kalshi.com/trade-api/v2"
        
        # Try file path first
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
        self.private_key = None
        self.key_source = "none"
        
        if key_path and os.path.exists(key_path):
            try:
                with open(key_path, 'r') as f:
                    self.private_key = f.read().strip()
                    self.key_source = "file"
            except Exception:
                pass
        
        # Fallback to env var
        if self.private_key is None:
            key_env = os.getenv("KALSHI_PRIVATE_KEY", "")
            if key_env and "BEGIN" in key_env:
                self.private_key = key_env.replace("\\n", "\n").strip()
                self.key_source = "env"
    
    def is_configured(self):
        return self.env == "live" and self.private_key is not None and bool(self.key_id)
    
    def get_config(self):
        return {
            "env": self.env,
            "key_id_set": bool(self.key_id),
            "key_loaded": self.private_key is not None,
            "key_source": self.key_source,
            "base_url": self.base_url
        }
    
    def get_balance(self):
        if not self.is_configured():
            return {"error": "Kalshi not configured", "env": self.env}
        # TODO: Add actual Kalshi API call here
        return {"balance": 0, "env": self.env, "status": "key_loaded"}
