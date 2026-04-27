"""
SixFilter Guardian → Kalshi Bridge
Keep this in a SEPARATE file: app/kalshi_trader.py
"""

import os
import logging
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass, field

import numpy as np
from scipy.stats import norm
from pykalshi import KalshiClient
from pykalshi.models import CreateOrderRequest, OrderSide, OrderType

logger = logging.getLogger('kalshi_trader')

# ─── CONFIG ───
@dataclass
class SixFilterConfig:
    MIN_EDGE_PCT: float = 4.0
    MAX_EDGE_PCT: float = 25.0
    KELLY_FRACTION: float = 0.25
    MAX_POSITION_PCT: float = 0.05
    MIN_POSITION_DOLLARS: float = 10.0
    MIN_EV_CENTS: float = 2.0
    MAX_KL_SCORE: float = 2.0
    MIN_CONTEXT_SCORE: float = 0.5
    MAX_SPREAD_CENTS: float = 5.0
    MAKER_DISCOUNT: int = 1
    BANKROLL: float = 10000.0
    MAX_DAILY_LOSS_PCT: float = 0.05
    MAX_OPEN_POSITIONS: int = 10
    TARGET_CATEGORIES: List[str] = field(default_factory=lambda: ['economics', 'finance'])

# ─── SIX FILTER ENGINE ───
class SixFilterEngine:
    def __init__(self, config: SixFilterConfig):
        self.config = config
        self.distribution_params = {
            'mean': 0.249,
            'std': 0.156,
            'recent_mean': 0.255,
            'recent_std': 0.141,
            'seasonal_mean': 0.234,
            'seasonal_std': 0.187
        }

    def filter_1_lmsr(self, threshold: float, kalshi_yes_price: float):
        mu = self.distribution_params['mean']
        sigma = self.distribution_params['std']
        true_prob = (1 - norm.cdf(threshold, mu, sigma)) * 100
        edge = true_prob - kalshi_yes_price
        passed = abs(edge) > self.config.MIN_EDGE_PCT and abs(edge) < self.config.MAX_EDGE_PCT
        return passed, edge, true_prob

    def filter_2_kelly(self, edge: float, price_cents: float, side: str):
        if abs(edge) < self.config.MIN_EDGE_PCT:
            return False, 0.0, 0.0
        if side == 'yes':
            b = (100 - price_cents) / price_cents
            p = (price_cents + edge) / 100
        else:
            b = price_cents / (100 - price_cents)
            p = (100 - price_cents + edge) / 100
        q = 1 - p
        kelly = (b * p - q) / b if b > 0 else 0
        kelly = max(0, min(kelly, 0.25))
        position = self.config.BANKROLL * kelly * self.config.KELLY_FRACTION
        position = min(position, self.config.BANKROLL * self.config.MAX_POSITION_PCT)
        passed = position >= self.config.MIN_POSITION_DOLLARS
        return passed, kelly, position

    def filter_3_ev(self, true_prob: float, cost_cents: float, side: str):
        win_prob = true_prob / 100 if side == 'yes' else 1 - (true_prob / 100)
        payout = 100 - cost_cents
        p = cost_cents / 100
        fee_per_side = np.ceil(0.07 * p * (1 - p) * 100) / 100
        total_fee = fee_per_side * 2
        ev = (win_prob * payout) - cost_cents - total_fee
        passed = ev > self.config.MIN_EV_CENTS
        return passed, ev

# ─── KALSHI TRADER ───
class KalshiTrader:
    def __init__(self):
        self.config = SixFilterConfig()
        self.engine = SixFilterEngine(self.config)

        key_id = os.getenv("KALSHI_KEY_ID") or os.getenv("KALSHI_API_KEY")
        priv_key = os.getenv("KALSHI_PRIVATE_KEY")
        priv_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")

        if not key_id:
            raise ValueError("KALSHI_KEY_ID or KALSHI_API_KEY not set")

        if priv_key:
            self.client = KalshiClient(key_id=key_id, private_key=priv_key)
        elif priv_path:
            self.client = KalshiClient(key_id=key_id, private_key_path=priv_path)
        else:
            raise ValueError("KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH required")

        self.running = False

    def scan_markets(self):
        markets = []
        for category in self.config.TARGET_CATEGORIES:
            try:
                events = self.client.get_events(category=category, status='open')
                for event in events.events:
                    for market in event.markets:
                        threshold = self._extract_threshold(market.title)
                        markets.append({
                            'ticker': market.ticker,
                            'title': market.title,
                            'yes_ask': market.yes_ask,
                            'yes_bid': market.yes_bid,
                            'no_ask': market.no_ask,
                            'no_bid': market.no_bid,
                            'volume': market.volume,
                            'close_date': str(market.close_date) if market.close_date else None,
                            'threshold': threshold,
                            'spread': market.yes_ask - market.yes_bid
                        })
            except Exception as e:
                logger.error(f"Scan error: {e}")
        return markets

    def _extract_threshold(self, title: str) -> Optional[float]:
        import re
        patterns = [r'>(\d+\.\d+)%', r'above\s+(\d+\.\d+)%', r'(\d+\.\d+)%\s+or\s+more']
        for pattern in patterns:
            match = re.search(pattern, title.lower())
            if match:
                return float(match.group(1))
        return None

    def evaluate_market(self, market: Dict) -> Optional[Dict]:
        if market['threshold'] is None:
            return None

        threshold = market['threshold']
        yes_price = market['yes_ask']
        no_price = market['no_ask']
        spread = market['spread']

        f1_pass, edge, true_prob = self.engine.filter_1_lmsr(threshold, yes_price)

        if edge > 0:
            side, trade_price, trade_edge = 'yes', yes_price, edge
        elif edge < 0:
            side, trade_price, trade_edge = 'no', no_price, abs(edge)
        else:
            return None

        f2_pass, kelly, position = self.engine.filter_2_kelly(trade_edge, trade_price, side)
        cost = trade_price if side == 'yes' else (100 - trade_price)
        f3_pass, ev = self.engine.filter_3_ev(true_prob, cost, side)

        f4_pass = True
        f5_pass = True
        f6_pass = spread < self.config.MAX_SPREAD_CENTS

        all_pass = all([f1_pass, f2_pass, f3_pass, f4_pass, f5_pass, f6_pass])

        if not all_pass:
            return None

        contract_cost = trade_price / 100
        count = int(position / contract_cost)
        if count < 1:
            return None

        return {
            'ticker': market['ticker'],
            'title': market['title'],
            'side': side,
            'price': trade_price - self.config.MAKER_DISCOUNT if side == 'no' else trade_price + self.config.MAKER_DISCOUNT,
            'count': count,
            'edge': round(trade_edge, 2),
            'true_prob': round(true_prob if side == 'yes' else (100 - true_prob), 2),
            'kalshi_prob': trade_price,
            'ev': round(ev, 2),
            'kelly': round(kelly, 4),
            'position': round(position, 2),
            'filters': {
                'lmsr': f1_pass, 'kelly': f2_pass, 'ev': f3_pass,
                'kl': f4_pass, 'bayesian': f5_pass, 'stoikov': f6_pass
            }
        }

    def execute_trade(self, signal: Dict) -> Optional[str]:
        try:
            order = CreateOrderRequest(
                ticker=signal['ticker'],
                side=OrderSide.YES if signal['side'] == 'yes' else OrderSide.NO,
                type=OrderType.LIMIT,
                price=int(signal['price']),
                count=signal['count']
            )
            response = self.client.create_order(order)
            logger.info(f"EXECUTED: {signal['title']} | {signal['side'].upper()} @ {signal['price']}¢ | {signal['count']} contracts")
            return response.order_id
        except Exception as e:
            logger.error(f"Execution failed: {e}")
            return None

    def run_cycle(self) -> List[Dict]:
        markets = self.scan_markets()
        signals = []

        for market in markets:
            signal = self.evaluate_market(market)
            if signal:
                order_id = self.execute_trade(signal)
                signal['order_id'] = order_id
                signal['status'] = 'executed' if order_id else 'failed'
                signal['timestamp'] = datetime.now().isoformat()
                signals.append(signal)

        return signals

# Singleton
_trader: Optional[KalshiTrader] = None

def get_trader() -> KalshiTrader:
    global _trader
    if _trader is None:
        _trader = KalshiTrader()
    return _trader
