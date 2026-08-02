import os
import base64
import json
import time
import requests

class KalshiClient:
    def __init__(self):
        self.env = os.getenv("KALSHI_ENV", "demo").lower()
        self.base_url = "https://api.elections.kalshi.com" if self.env == "live" else "https://demo-api.kalshi.com"
        self.api_prefix = "/trade-api/v2"
        self.key_id = os.getenv("KALSHI_KEY_ID", "")
        
        # Load private key
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
        
        if self.private_key is None:
            key_env = os.getenv("KALSHI_PRIVATE_KEY", "")
            if key_env and "BEGIN" in key_env:
                self.private_key = key_env.replace("\\n", "\n").strip()
                self.key_source = "env"
        
        self.session = requests.Session()
    
    def is_configured(self):
        return self.env == "live" and self.private_key is not None and bool(self.key_id)
    
    def _sign_request(self, method, path, body=""):
        """RSA-SHA256 sign the request for Kalshi auth"""
        timestamp = str(int(time.time() * 1000))
        # Kalshi signature: timestamp + method + path (path includes /trade-api/v2)
        message = timestamp + method + path + body
        
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            
            private_key = serialization.load_pem_private_key(
                self.private_key.encode('utf-8'),
                password=None
            )
            signature = private_key.sign(
                message.encode('utf-8'),
                padding.PKCS1v15(),
                hashes.SHA256()
            )
            signature_b64 = base64.b64encode(signature).decode('utf-8')
            return timestamp, signature_b64
        except Exception as e:
            print(f"Signing error: {e}")
            return timestamp, ""
    
    def _headers(self, method, path, body=""):
        timestamp, signature = self._sign_request(method, path, body)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    
    def _url(self, path):
        """Build full URL correctly — no urljoin bugs"""
        return f"{self.base_url}{self.api_prefix}{path}"
    
    def get_config(self):
        return {
            "env": self.env,
            "key_id_set": bool(self.key_id),
            "key_loaded": self.private_key is not None,
            "key_source": self.key_source,
            "base_url": self.base_url,
            "api_prefix": self.api_prefix
        }
    
    def get_balance(self):
        if not self.is_configured():
            return {"error": "Kalshi not configured", "env": self.env}
        
        path = "/portfolio/balance"
        full_path = f"{self.api_prefix}{path}"  # /trade-api/v2/portfolio/balance
        url = self._url(path)  # https://api.elections.kalshi.com/trade-api/v2/portfolio/balance
        headers = self._headers("GET", full_path)
        
        try:
            response = self.session.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            else:
                return {
                    "error": f"Kalshi API error {response.status_code}",
                    "detail": response.text,
                    "env": self.env,
                    "url": url,
                    "signed_path": full_path
                }
        except Exception as e:
            return {"error": str(e), "env": self.env}
    
    def get_markets(self, limit=100):
        if not self.is_configured():
            return {"error": "Kalshi not configured"}
        
        path = f"/markets?limit={limit}"
        full_path = f"{self.api_prefix}{path}"
        url = self._url(path)
        headers = self._headers("GET", full_path)
        
        try:
            response = self.session.get(url, headers=headers, timeout=10)
            return response.json() if response.status_code == 200 else {"error": response.text, "status": response.status_code}
        except Exception as e:
            return {"error": str(e)}
    
    def place_order(self, market_id, side, count, price=None):
        if not self.is_configured():
            return {"error": "Kalshi not configured"}
        
        path = "/portfolio/orders"
        full_path = f"{self.api_prefix}{path}"
        url = self._url(path)
        
        body = {
            "market_id": market_id,
            "side": side,
            "count": count
        }
        if price is not None:
            body["price"] = price
        
        body_json = json.dumps(body)
        headers = self._headers("POST", full_path, body_json)
        
        try:
            response = self.session.post(url, headers=headers, data=body_json, timeout=10)
            return response.json() if response.status_code == 200 else {"error": response.text, "status": response.status_code}
        except Exception as e:
            return {"error": str(e)}
