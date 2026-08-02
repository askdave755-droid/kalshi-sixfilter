import os
import base64
import json
import time
import uuid
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

class KalshiClient:
    def __init__(self):
        self.env = os.getenv("KALSHI_ENV", "demo").lower()
        self.base_url = "https://api.elections.kalshi.com" if self.env == "live" else "https://demo-api.kalshi.com"
        self.api_prefix = "/trade-api/v2"
        self.key_id = os.getenv("KALSHI_KEY_ID", "")
        
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
        self.private_key = None
        
        if key_path and os.path.exists(key_path):
            with open(key_path, "rb") as f:
                self.private_key = serialization.load_pem_private_key(
                    f.read(), password=None, backend=default_backend()
                )
        else:
            key_env = os.getenv("KALSHI_PRIVATE_KEY", "")
            if key_env and "BEGIN" in key_env:
                clean_key = key_env.replace("\\n", "\n").strip().encode('utf-8')
                self.private_key = serialization.load_pem_private_key(
                    clean_key, password=None, backend=default_backend()
                )
        
        self.session = requests.Session()
    
    def is_configured(self):
        return self.env == "live" and self.private_key is not None and bool(self.key_id)
    
    def _sign(self, message: str) -> str:
        signature = self.private_key.sign(
            message.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')
    
    def _headers(self, method: str, path: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        msg_string = timestamp + method + path
        sig = self._sign(msg_string)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    
    def _url(self, path: str) -> str:
        return f"{self.base_url}{self.api_prefix}{path}"
    
    def get_config(self):
        return {
            "env": self.env,
            "key_id_set": bool(self.key_id),
            "key_loaded": self.private_key is not None,
            "base_url": self.base_url
        }
    
    def get_balance(self):
        if not self.is_configured():
            return {"error": "Kalshi not configured", "env": self.env}
        
        path = "/portfolio/balance"
        full_path = f"{self.api_prefix}{path}"
        url = self._url(path)
        headers = self._headers("GET", full_path)
        
        try:
            response = self.session.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            else:
                return {"error": f"Kalshi API error {response.status_code}", "detail": response.text, "env": self.env}
        except Exception as e:
            return {"error": str(e), "env": self.env}
    
    def get_markets(self, limit=100):
        if not self.is_configured():
            return {"error": "Kalshi not configured"}
        
        path = f"/markets?limit={limit}"
        sign_path = f"{self.api_prefix}/markets"
        url = self._url(path)
        headers = self._headers("GET", sign_path)
        
        try:
            response = self.session.get(url, headers=headers, timeout=10)
            return response.json() if response.status_code == 200 else {"error": response.text, "status": response.status_code}
        except Exception as e:
            return {"error": str(e)}
    
    def place_order(self, ticker: str, side: str, count: str, price: str, client_order_id: str = None):
        if not self.is_configured():
            return {"error": "Kalshi not configured"}
        
        path = "/portfolio/events/orders"
        full_path = f"{self.api_prefix}{path}"
        url = self._url(path)
        
        if client_order_id is None:
            client_order_id = str(uuid.uuid4())
        
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side,
            "count": count,
            "price": price,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": False,
            "reduce_only": False,
            "subaccount": 0,
            "exchange_index": 0
        }
        
        body_json = json.dumps(body)
        headers = self._headers("POST", full_path)
        
        try:
            response = self.session.post(url, headers=headers, data=body_json, timeout=10)
            return response.json() if response.status_code in (200, 201) else {"error": response.text, "status": response.status_code}
        except Exception as e:
            return {"error": str(e)}
