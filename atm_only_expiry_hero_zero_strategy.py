"""
ATM-Only Expiry Hero or Zero Strategy for NIFTY and SENSEX
===========================================================

Conditions:
1. Underlying Index: NIFTY and SENSEX support
2. Expiry Filter: Triggers ONLY on Expiry day for the respective index
3. Execution Time: Triggers after 3:15 PM or 3:19 PM IST
4. Entry Target Price: Option Premium around ₹5
5. Strike Selection: 
   - ATM (At-The-Money Call and Put ONLY - No ITM1 strikes)
6. Risk Management:
   - Full Risk (Hero or Zero)
   - No Stop-loss (SL = None)
   - No Target (Target = None)
7. Exit Rule: Market Closing Time (3:40 PM IST auto square-off, Real market timing: 9:15 to 3:40)
"""

import os
import sys
import logging
from datetime import datetime, time, date, timezone, timedelta
from typing import Dict, Any, Optional, List

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("AtmOnlyExpiryHeroZeroStrategy")

IST = timezone(timedelta(hours=5, minutes=30))


class IndexConfig:
    """Index specifications for NIFTY and SENSEX"""
    CONFIGS = {
        "NIFTY": {
            "strike_gap": 50,
            "lot_size": 65,  # 1 Lot = 65 Qty
            "exchange": "NSE"
        },
        "SENSEX": {
            "strike_gap": 100,
            "lot_size": 20,  # 1 Lot = 20 Qty
            "exchange": "BSE"
        }
    }

    @classmethod
    def get(cls, symbol: str) -> Dict[str, Any]:
        sym = symbol.upper()
        if sym not in cls.CONFIGS:
            raise ValueError(f"Unsupported index: {symbol}. Supported indices: {list(cls.CONFIGS.keys())}")
        return cls.CONFIGS[sym]


class AtmOnlyExpiryHeroZeroStrategy:
    def __init__(
        self,
        symbol: str = "NIFTY",  # "NIFTY" or "SENSEX"
        trigger_time_str: str = "15:15",  # "15:15" or "15:19"
        market_open_time: Optional[time] = None,  # Real market open time (default: 09:15 IST)
        market_close_time: Optional[time] = None,  # Dynamic broker market close time (default: 15:40 IST)
        broker_expiry_date: Optional[date] = None,  # Dynamic broker nearest expiry date
        target_entry_price: float = 5.0,  # Target option entry price around ₹5
        min_entry_price: Optional[float] = None,  # Minimum entry price floor (e.g. ₹3.00)
        max_entry_price: Optional[float] = None,  # Maximum entry price cap (e.g. ₹7.00 or ₹5.00)
        price_tolerance: float = 2.0,  # Price window (target_entry_price ± tolerance)
        ce_strike_mode: str = "ATM",
        pe_strike_mode: str = "ATM",
        lots: int = 1,
        full_risk_mode: bool = True
    ):
        self.symbol = symbol.upper()
        self.config = IndexConfig.get(self.symbol)
        
        # Time configuration
        if isinstance(trigger_time_str, str):
            h, m = map(int, trigger_time_str.split(":"))
            self.trigger_time = time(h, m, 0)
        else:
            self.trigger_time = trigger_time_str
        
        # Real market timing: 9:15 to 3:40 IST
        self.market_open_time = market_open_time or time(9, 15, 0)
        self.market_close_time = market_close_time or time(15, 40, 0)
        
        # Expiry date obtained dynamically from broker API / DB
        self.broker_expiry_date = broker_expiry_date
        
        # Strategy parameters - Entry price window around target (e.g. ₹5)
        self.target_entry_price = float(target_entry_price)
        self.price_tolerance = float(price_tolerance)

        # Determine lower bound (min_entry_price)
        if min_entry_price is not None:
            self.min_entry_price = float(min_entry_price)
        elif max_entry_price is not None and max_entry_price <= self.target_entry_price:
            self.min_entry_price = max(1.0, max_entry_price - self.price_tolerance)
        else:
            self.min_entry_price = max(1.0, self.target_entry_price - self.price_tolerance)

        # Determine upper bound (max_entry_price)
        if max_entry_price is not None:
            self.max_entry_price = float(max_entry_price)
        else:
            self.max_entry_price = self.target_entry_price + self.price_tolerance

        self.ce_strike_mode = ce_strike_mode.upper()
        self.pe_strike_mode = pe_strike_mode.upper()
        self.lots = lots
        self.quantity = lots * self.config["lot_size"]
        
        # Risk & Targets - No SL, No Target as per condition
        self.stop_loss = None
        self.target_profit = None
        self.full_risk_mode = full_risk_mode
        
        # State tracking
        self.is_traded_today = False
        self.active_positions: List[Dict[str, Any]] = []

    def set_market_close_time(self, close_time: time):
        """Sets or updates the market closing time received dynamically from broker API."""
        self.market_close_time = close_time

    def set_broker_expiry_date(self, expiry_dt: date):
        """Sets or updates the active expiry date received dynamically from broker API."""
        self.broker_expiry_date = expiry_dt

    def is_expiry_day(self, current_date: date, actual_expiry_date: Optional[date] = None) -> bool:
        """
        Validates if current date is the expiry date fetched directly from the broker API.
        Does NOT rely on hardcoded weekdays.
        """
        target_expiry = actual_expiry_date or self.broker_expiry_date
        if target_expiry is not None:
            return current_date == target_expiry
        
        # If broker expiry is not set yet, return False to prevent unauthorized execution
        return False

    def get_target_strikes(self, spot_price: float) -> List[Dict[str, Any]]:
        """
        Calculates ONLY the 2 ATM target strikes (CE ATM and PE ATM).
        No ITM1 strikes included.
        """
        strike_gap = self.config["strike_gap"]
        atm_strike = float(round(spot_price / strike_gap) * strike_gap)

        return [
            {"option_type": "CE", "strike_type": "ATM", "strike": atm_strike},
            {"option_type": "PE", "strike_type": "ATM", "strike": atm_strike},
        ]

    def evaluate_entry(
        self,
        current_datetime: datetime,
        spot_price: float,
        option_quotes: Dict[str, Dict[str, Any]],  # Quotes dictionary for CE and PE ATM strikes
        actual_expiry_date: Optional[date] = None,
        broker_close_time: Optional[time] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluates strategy conditions for trade entry across 2 ATM strikes (CE ATM, PE ATM).
        
        Conditions checked:
        1. Expiry day only
        2. Time is at or after trigger time (e.g. 15:15 IST onwards)
        3. Scans bar-by-bar after 15:15 until option premium reaches target (<= ₹5 or max_entry_price)
        4. Triggers ONCE per day / strike. No re-entries if price hits target again later.
        """
        current_date = current_datetime.date()
        current_time = current_datetime.time()

        # Update market close time from broker if provided
        close_time = broker_close_time or self.market_close_time or time(15, 40, 0)

        # 1. Check Expiry Day
        if not self.is_expiry_day(current_date, actual_expiry_date):
            logger.debug(f"[{self.symbol}] Date {current_date} is NOT an expiry day. Skipping entry.")
            return None

        # 2. Check Trigger Time Window (e.g. after 3:15 PM IST but before market close time)
        if current_time < self.trigger_time or current_time >= close_time:
            logger.debug(f"[{self.symbol}] Current time {current_time} outside trigger window ({self.trigger_time} - {close_time}).")
            return None

        # 3. Check if trade already executed today (Enforce single entry rule per day)
        if self.is_traded_today or len(self.active_positions) > 0:
            logger.debug(f"[{self.symbol}] Already traded today. Skipping duplicate re-entry.")
            return None

        # 4. Determine ATM Target Strikes ONLY
        target_strikes = self.get_target_strikes(spot_price)

        logger.info(
            f"[{self.symbol} ATM-ONLY HERO-ZERO] Bar: {current_datetime.strftime('%H:%M:%S')} | Spot: {spot_price} | "
            f"Scanning ATM Strikes: CE ATM ({target_strikes[0]['strike']}), PE ATM ({target_strikes[1]['strike']})"
        )

        orders_to_place = []

        for item in target_strikes:
            opt_type = item["option_type"]
            strike = item["strike"]
            strike_type = item["strike_type"]

            # Flexible quote lookup by keys
            key_exact = f"{strike}_{opt_type}"
            key_type = f"{opt_type}_{strike_type}"
            
            quote = (
                option_quotes.get(key_exact) or 
                option_quotes.get(key_type) or 
                option_quotes.get(opt_type, {}).get(strike) or 
                option_quotes.get(opt_type, {})
            )
            
            ltp = quote.get("ltp") if isinstance(quote, dict) else None

            if ltp is None:
                continue

            # Price condition matching (min_entry_price <= ltp <= max_entry_price)
            is_price_matched = self.min_entry_price <= ltp <= self.max_entry_price

            if is_price_matched:
                orders_to_place.append({
                    "symbol": self.symbol,
                    "option_type": opt_type,
                    "strike_type": strike_type,
                    "strike": strike,
                    "entry_price": ltp,
                    "target_price": self.target_entry_price,
                    "quantity": self.quantity,
                    "lots": self.lots,
                    "risk": "FULL_RISK (Hero or Zero)",
                    "stop_loss": self.stop_loss,
                    "target": self.target_profit,
                    "timestamp": current_datetime.strftime("%Y-%m-%d %H:%M:%S")
                })
                logger.info(
                    f"🟢 ENTRY SIGNAL TRIGGERED AT {current_datetime.strftime('%H:%M:%S')}: Buy {self.symbol} {strike} {opt_type} @ ₹{ltp} "
                    f"(Target Window: ₹{self.min_entry_price:.2f} - ₹{self.max_entry_price:.2f})"
                )

        if orders_to_place:
            self.is_traded_today = True  # Lock entry to prevent duplicate re-entries later
            for order in orders_to_place:
                self.active_positions.append(order)

            return {
                "status": "ENTRY_TRIGGERED",
                "orders": orders_to_place
            }

        return None

    def evaluate_exit(
        self,
        current_datetime: datetime,
        current_option_prices: Dict[str, float],
        broker_close_time: Optional[time] = None
    ) -> List[Dict[str, Any]]:
        """
        Evaluates exit conditions for active positions.
        Exit Rule: Market Closing Time obtained dynamically from Broker.
        No Stop Loss or Target exit since risk is full (Hero or Zero).
        """
        current_time = current_datetime.time()
        close_time = broker_close_time or self.market_close_time or time(15, 40, 0)
        exited_positions = []

        if not self.active_positions:
            return exited_positions

        # Exit triggered ONLY at market closing time from broker
        if current_time >= close_time:
            logger.info(f"⏰ BROKER MARKET CLOSE TIME REACHED ({close_time}). Squaring off all active positions.")
            
            remaining_positions = []
            for pos in self.active_positions:
                key = f"{pos['strike']}_{pos['option_type']}"
                exit_price = current_option_prices.get(key, current_option_prices.get(pos['option_type'], pos['entry_price']))
                pnl = (exit_price - pos['entry_price']) * pos['quantity']
                
                exit_record = {
                    **pos,
                    "exit_price": exit_price,
                    "exit_time": current_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                    "exit_reason": "MARKET_CLOSE_TIME",
                    "pnl": round(pnl, 2)
                }
                exited_positions.append(exit_record)
                logger.info(f"🔴 EXITED POSITION: {pos['symbol']} {pos['strike']} {pos['option_type']} | Entry: ₹{pos['entry_price']} | Exit: ₹{exit_price} | PnL: ₹{round(pnl, 2)}")

            self.active_positions = remaining_positions

        return exited_positions
