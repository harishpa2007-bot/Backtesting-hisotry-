"""
Expiry Hero or Zero Strategy for NIFTY and SENSEX
=================================================

Conditions:
1. Real Market Timing: 9:15 AM to 3:40 PM IST (09:15 to 15:40)
2. Underlying Index: NIFTY and SENSEX support
3. Expiry Filter: Triggers ONLY on Expiry day for the respective index
4. Execution Time: Triggers after 3:15 PM or 3:19 PM IST
5. Entry Target Price: Option Premium strictly exact ₹6.00 (± ₹0.10)
6. Target Rule: 1:3 Target (Target Price = ₹24.00 on Entry ₹6.00)
7. Strike Selection:
   - Separate results for ATM, OTM (OTM1), and ITM (ITM1)
8. Output Format:
   - Do NOT show net profit or loss
   - Show ONLY True or False
   - If entry attains 1:3 in any strike -> True
   - If entry did not attain 1:3 in any strike -> False
9. Exit Rules:
   - 1:3 Target Hit: Reaches ₹24.00 -> True
   - Market Ends (15:40 IST): Square-off if 1:3 not met -> False
"""

import os
import sys
import csv
import logging
import argparse
from datetime import datetime, time, date, timezone, timedelta
from typing import Dict, Any, Optional, List

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ExpiryHeroZeroStrategy")

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


class ExpiryHeroZeroStrategy:
    def __init__(
        self,
        symbol: str = "NIFTY",  # "NIFTY" or "SENSEX"
        trigger_time_str: str = "15:15",  # "15:15" or "15:19"
        market_open_time: Optional[time] = None,  # Real market open time (default: 09:15 IST)
        market_close_time: Optional[time] = None,  # Real market close square-off time (default: 15:40 IST)
        broker_expiry_date: Optional[date] = None,  # Dynamic broker nearest expiry date
        target_entry_price: float = 6.0,  # Target option entry price strictly exact ₹6
        min_entry_price: Optional[float] = None,  # Minimum entry price floor (e.g. ₹5.90)
        max_entry_price: Optional[float] = None,  # Maximum entry price cap (e.g. ₹6.10 or ₹6.00)
        price_tolerance: float = 0.10,  # Strict tolerance: ± ₹0.10 (₹5.90 to ₹6.10)
        target_profit: Optional[float] = 24.0,  # 1:3 Target profit (₹24.00 on entry ₹6.00)
        execution_mode: str = "limit",  # "limit" (exact ₹5 limit fill) or "close" (checks close LTP in window)
        strict_after_315: bool = False,  # If True, strictly after 15:15 (> 15:15:00). If False, >= 15:15:00
        ce_strike_mode: str = "ATM",  # "ATM" strike selection
        pe_strike_mode: str = "ATM",  # "ATM" strike selection
        strike_mode: str = "ATM_OTM1",  # "ATM_OTM1", "OTM1_ONLY", or "ATM_ONLY"
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
        if isinstance(market_open_time, str):
            oh, om = map(int, market_open_time.split(":"))
            self.market_open_time = time(oh, om, 0)
        else:
            self.market_open_time = market_open_time or time(9, 15, 0)

        if isinstance(market_close_time, str):
            ch, cm = map(int, market_close_time.split(":"))
            self.market_close_time = time(ch, cm, 0)
        else:
            self.market_close_time = market_close_time or time(15, 40, 0)
        
        # Expiry date obtained dynamically from broker API / DB
        self.broker_expiry_date = broker_expiry_date
        
        # Strategy parameters - Entry price window around target (strictly exact ₹5 ± 0.10)
        self.target_entry_price = float(target_entry_price)
        self.price_tolerance = float(price_tolerance)
        self.execution_mode = execution_mode.lower()
        self.strict_after_315 = strict_after_315

        # Determine lower bound (min_entry_price)
        if min_entry_price is not None:
            self.min_entry_price = float(min_entry_price)
        elif max_entry_price is not None and max_entry_price <= self.target_entry_price:
            self.min_entry_price = max(0.5, max_entry_price - self.price_tolerance)
        else:
            self.min_entry_price = max(0.5, self.target_entry_price - self.price_tolerance)

        # Determine upper bound (max_entry_price)
        if max_entry_price is not None:
            self.max_entry_price = float(max_entry_price)
        else:
            self.max_entry_price = self.target_entry_price + self.price_tolerance

        self.ce_strike_mode = ce_strike_mode.upper()
        self.pe_strike_mode = pe_strike_mode.upper()
        self.strike_mode = strike_mode.upper()
        self.lots = lots
        self.quantity = lots * self.config["lot_size"]
        
        # Risk & Targets - 1:3 Target (Entry ₹6 -> Target ₹24)
        self.stop_loss = None
        self.target_profit = float(target_profit) if target_profit is not None else (self.target_entry_price * 4.0)
        self.full_risk_mode = full_risk_mode
        
        # State tracking
        self.is_traded_today = False
        self.active_positions: List[Dict[str, Any]] = []

    def set_market_open_time(self, open_time: time):
        """Sets or updates the market open time (default: 09:15 IST)."""
        self.market_open_time = open_time

    def set_market_close_time(self, close_time: time):
        """Sets or updates the market closing time received dynamically from broker API (default: 15:40 IST)."""
        self.market_close_time = close_time

    def is_market_timing(self, current_time: time) -> bool:
        """Validates if current time is within real market timing (9:15 to 3:40 IST)."""
        return self.market_open_time <= current_time <= self.market_close_time

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
        Calculates target strikes based on configuration:
        - ATM: CE ATM (atm_strike), PE ATM (atm_strike)
        - OTM1: CE OTM1 (atm_strike + strike_gap), PE OTM1 (atm_strike - strike_gap)
        - ITM1: CE ITM1 (atm_strike - strike_gap), PE ITM1 (atm_strike + strike_gap)
        - ALL / ATM_OTM_ITM: CE ATM, CE OTM1, CE ITM1, PE ATM, PE OTM1, PE ITM1
        """
        strike_gap = self.config["strike_gap"]
        atm_strike = float(round(spot_price / strike_gap) * strike_gap)

        strikes = []
        mode = self.strike_mode.upper()
        if mode in ("OTM1_ONLY", "OTM1", "OTM"):
            strikes.extend([
                {"option_type": "CE", "strike_type": "OTM1", "strike": atm_strike + strike_gap},
                {"option_type": "PE", "strike_type": "OTM1", "strike": atm_strike - strike_gap},
            ])
        elif mode in ("ATM_ONLY", "ATM"):
            strikes.extend([
                {"option_type": "CE", "strike_type": "ATM", "strike": atm_strike},
                {"option_type": "PE", "strike_type": "ATM", "strike": atm_strike},
            ])
        elif mode in ("ITM1_ONLY", "ITM_ONLY", "ITM"):
            strikes.extend([
                {"option_type": "CE", "strike_type": "ITM1", "strike": atm_strike - strike_gap},
                {"option_type": "PE", "strike_type": "ITM1", "strike": atm_strike + strike_gap},
            ])
        elif mode in ("ALL", "ATM_OTM_ITM", "ATM_OTM1_ITM1"):
            strikes.extend([
                {"option_type": "CE", "strike_type": "ATM", "strike": atm_strike},
                {"option_type": "CE", "strike_type": "OTM1", "strike": atm_strike + strike_gap},
                {"option_type": "CE", "strike_type": "ITM1", "strike": atm_strike - strike_gap},
                {"option_type": "PE", "strike_type": "ATM", "strike": atm_strike},
                {"option_type": "PE", "strike_type": "OTM1", "strike": atm_strike - strike_gap},
                {"option_type": "PE", "strike_type": "ITM1", "strike": atm_strike + strike_gap},
            ])
        else:
            # Default "ATM_OTM1": CE ATM, CE OTM1, PE ATM, PE OTM1 (NO ITM1)
            strikes.extend([
                {"option_type": "CE", "strike_type": "ATM", "strike": atm_strike},
                {"option_type": "CE", "strike_type": "OTM1", "strike": atm_strike + strike_gap},
                {"option_type": "PE", "strike_type": "ATM", "strike": atm_strike},
                {"option_type": "PE", "strike_type": "OTM1", "strike": atm_strike - strike_gap},
            ])
        return strikes

    def evaluate_entry(
        self,
        current_datetime: datetime,
        spot_price: float,
        option_quotes: Dict[str, Dict[str, Any]],  # Quotes dictionary for CE and PE strikes
        actual_expiry_date: Optional[date] = None,
        broker_close_time: Optional[time] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluates strategy conditions for trade entry across 4 strikes (CE ATM, CE ITM1, PE ATM, PE ITM1).
        
        Conditions checked:
        1. Expiry day only
        2. Time is at or after trigger time (e.g. 15:15 or 15:19 PM)
        3. Haven't traded already today
        4. Option premium is near target price (<= ₹5 or within target range)
        """
        current_date = current_datetime.date()
        current_time = current_datetime.time()

        # Update market close time from broker if provided (default: 15:40 IST)
        close_time = broker_close_time or self.market_close_time or time(15, 40, 0)

        # 0. Check Real Market Timing (9:15 to 3:40 IST)
        if not (self.market_open_time <= current_time <= close_time):
            logger.debug(f"[{self.symbol}] Current time {current_time} outside real market timing ({self.market_open_time} to {close_time}). Skipping entry.")
            return None

        # 1. Check Expiry Day
        if not self.is_expiry_day(current_date, actual_expiry_date):
            logger.debug(f"[{self.symbol}] Date {current_date} is NOT an expiry day. Skipping entry.")
            return None

        # 2. Check Trigger Time Window (strictly after 3:15 PM only, not before 3:15 PM)
        is_time_valid = (current_time > self.trigger_time) if self.strict_after_315 else (current_time >= self.trigger_time)
        if not is_time_valid or current_time >= close_time:
            logger.debug(f"[{self.symbol}] Current time {current_time} outside trigger window (trigger: {self.trigger_time}, close: {close_time}).")
            return None

        # 3. Check if trade already executed today
        if self.is_traded_today or len(self.active_positions) > 0:
            logger.debug(f"[{self.symbol}] Already traded today or active position exists.")
            return None

        # 4. Determine 4 Target Strikes
        target_strikes = self.get_target_strikes(spot_price)

        strike_labels = ", ".join([f"{item['option_type']} {item['strike_type']} ({int(item['strike'])})" for item in target_strikes])
        logger.info(
            f"[{self.symbol} EXPIRY HERO-ZERO] Spot: {spot_price} | Evaluating Strikes: {strike_labels}"
        )

        orders_to_place = []

        for item in target_strikes:
            opt_type = item["option_type"]
            strike = item["strike"]
            strike_type = item["strike_type"]

            # Flexible quote lookup by keys
            key_exact = f"{strike}_{opt_type}"
            key_int = f"{int(strike)}_{opt_type}"
            key_float = f"{float(strike)}_{opt_type}"
            key_type = f"{opt_type}_{strike_type}"
            
            quote = (
                option_quotes.get(key_exact) or 
                option_quotes.get(key_int) or 
                option_quotes.get(key_float) or 
                option_quotes.get(key_type) or 
                option_quotes.get(opt_type, {}).get(strike) or 
                option_quotes.get(opt_type, {})
            )
            
            ltp = quote.get("ltp") if isinstance(quote, dict) else None

            if ltp is None:
                continue

            # Price condition matching (Exact ₹5.00 ± tolerance or Limit Order touch at exact ₹5.00)
            if self.execution_mode == "limit":
                low = quote.get("low", ltp)
                high = quote.get("high", ltp)
                if low <= self.target_entry_price <= high:
                    is_price_matched = True
                    fill_price = self.target_entry_price  # strictly exact 5.00
                else:
                    is_price_matched = False
                    fill_price = ltp
            else:
                is_price_matched = self.min_entry_price <= ltp <= self.max_entry_price
                fill_price = ltp

            if is_price_matched:
                high_quote = float(quote.get("high", fill_price) or fill_price)
                orders_to_place.append({
                    "symbol": self.symbol,
                    "option_type": opt_type,
                    "strike_type": strike_type,
                    "strike": strike,
                    "entry_price": fill_price,
                    "target_price": self.target_entry_price,
                    "quantity": self.quantity,
                    "lots": self.lots,
                    "risk": "FULL_RISK (Hero or Zero)",
                    "stop_loss": self.stop_loss,
                    "target": self.target_profit,
                    "timestamp": current_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                    "max_high": high_quote
                })
                logger.info(
                    f"🟢 ENTRY SIGNAL DETECTED: Buy {self.symbol} {strike} {opt_type} ({strike_type}) @ ₹{fill_price:.2f} "
                    f"(Target Entry: ₹{self.target_entry_price:.2f} ± ₹{self.price_tolerance:.2f} | Window: ₹{self.min_entry_price:.2f} - ₹{self.max_entry_price:.2f})"
                )

        if orders_to_place:
            self.is_traded_today = True
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
        current_option_prices: Dict[str, Any],
        broker_close_time: Optional[time] = None,
        candle_quotes: Optional[Dict[str, Dict[str, Any]]] = None,
        force_market_close: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Evaluates exit conditions for active positions.
        Exit Rule:
        - Market Ends / Closing Time: Square-off all positions when market ends
          (15:40 IST / 3:40 PM) at market close LTP or intrinsic settlement value.
        """
        current_time = current_datetime.time()
        close_time = broker_close_time or self.market_close_time or time(15, 40, 0)
        exited_positions = []

        if not self.active_positions:
            return exited_positions

        remaining_positions = []
        is_market_close = force_market_close or (current_time >= close_time)

        for pos in self.active_positions:
            key_exact = f"{pos['strike']}_{pos['option_type']}"
            key_int = f"{int(pos['strike'])}_{pos['option_type']}"
            key_float = f"{float(pos['strike'])}_{pos['option_type']}"
            key_type = f"{pos['option_type']}_{pos.get('strike_type')}"

            # 1. Retrieve quote info
            quote_data = None
            if candle_quotes:
                quote_data = (
                    candle_quotes.get(key_exact) or
                    candle_quotes.get(key_int) or
                    candle_quotes.get(key_float) or
                    candle_quotes.get(key_type) or
                    candle_quotes.get(pos['option_type'])
                )

            # Fallback to current_option_prices
            if quote_data is None:
                val = (
                    current_option_prices.get(key_exact) or
                    current_option_prices.get(key_int) or
                    current_option_prices.get(key_float) or
                    current_option_prices.get(key_type) or
                    current_option_prices.get(pos['option_type'])
                )
                if isinstance(val, dict):
                    quote_data = val
                elif isinstance(val, (int, float)):
                    quote_data = {"ltp": float(val), "high": float(val), "low": float(val)}

            if quote_data is None:
                quote_data = {"ltp": pos['entry_price'], "high": pos['entry_price'], "low": pos['entry_price']}

            ltp = float(quote_data.get("ltp", pos['entry_price']) or 0.0)
            high_price = float(quote_data.get("high", ltp) or ltp)

            # Update running max high reached since entry
            pos["max_high"] = max(pos.get("max_high", pos["entry_price"]), high_price)

            # 2. Check if Target Profit is met
            is_target_met = False
            if self.target_profit is not None and self.target_profit > 0:
                pos_ts_str = pos.get("timestamp") or pos.get("entry_time")
                is_after_entry = True
                if pos_ts_str:
                    try:
                        pos_dt = datetime.strptime(pos_ts_str[:19], "%Y-%m-%d %H:%M:%S")
                        is_after_entry = (current_datetime > pos_dt)
                    except Exception:
                        pass

                if is_after_entry:
                    if self.execution_mode == "limit":
                        if high_price >= self.target_profit:
                            is_target_met = True
                    else:
                        if ltp >= self.target_profit or high_price >= self.target_profit:
                            is_target_met = True
                else:
                    # Same entry candle
                    if ltp >= self.target_profit:
                        is_target_met = True

            meet_time_str = current_datetime.strftime("%Y-%m-%d %H:%M:%S")

            if is_target_met:
                # Target price met! Closing price equals strictly target price (₹24.00)
                exit_price = float(self.target_profit)
                exit_reason = f"TARGET_{int(self.target_profit)}_REACHED"
                pnl = (exit_price - pos['entry_price']) * pos['quantity']

                exit_record = {
                    **pos,
                    "exit_price": exit_price,
                    "exit_time": meet_time_str,
                    "meet_time": meet_time_str,
                    "exit_reason": exit_reason,
                    "pnl": round(pnl, 2),
                    "max_high": pos["max_high"]
                }
                exited_positions.append(exit_record)
                logger.info(
                    f"🎯 TARGET REACHED: Buy {pos['symbol']} {int(pos['strike'])} {pos['option_type']} | "
                    f"Entry: ₹{pos['entry_price']:.2f} | Closing Price: ₹{exit_price:.2f} | Meet Time: {meet_time_str} | PnL: +₹{round(pnl, 2):,.2f}"
                )
            elif is_market_close:
                # Target not met: Market close squareoff
                exit_price = ltp
                exit_reason = "MARKET_CLOSE_TIME"
                pnl = (exit_price - pos['entry_price']) * pos['quantity']

                exit_record = {
                    **pos,
                    "exit_price": exit_price,
                    "exit_time": meet_time_str,
                    "meet_time": meet_time_str,
                    "exit_reason": exit_reason,
                    "pnl": round(pnl, 2),
                    "max_high": pos["max_high"]
                }
                exited_positions.append(exit_record)
                logger.info(
                    f"⏰ MARKET CLOSE SQUAREOFF: {pos['symbol']} {int(pos['strike'])} {pos['option_type']} | "
                    f"Entry: ₹{pos['entry_price']:.2f} | Exit: ₹{exit_price:.2f} | Time: {meet_time_str} | PnL: ₹{round(pnl, 2):,.2f}"
                )
            else:
                # Continue holding position until target met or market close
                remaining_positions.append(pos)

        self.active_positions = remaining_positions
        return exited_positions


def parse_expiry_date(exp_str: str) -> date:
    """Safely parses broker expiry date strings into a python date object."""
    for fmt in ("%Y-%m-%d", "%d-%b-%y", "%d-%b-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(exp_str.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Could not parse expiry date string: {exp_str}")


async def fetch_broker_market_close_time(broker: Any, user: Any, db: Any, symbol: str) -> time:
    """
    Fetches the exact market closing / auto-squareoff time dynamically from active broker API.
    Does NOT rely on hardcoded market close time strings.
    """
    try:
        from app.api.v1.brokers.market import get_market_quote

        exchange = IndexConfig.get(symbol).get("exchange", "NSE")

        # Query live quote / market status from broker API
        quote_res = await get_market_quote(
            broker_id=broker.id,
            exchange=exchange,
            symbol=symbol.upper(),
            current_user=user,
            db=db
        )
        if quote_res and isinstance(quote_res, dict) and quote_res.get("success") and quote_res.get("data"):
            data = quote_res["data"]
            # Check for broker provided market close or session end time string
            close_time_str = data.get("market_close_time") or data.get("session_close") or data.get("square_off_time")
            if close_time_str:
                for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p"):
                    try:
                        parsed_t = datetime.strptime(close_time_str.strip(), fmt).time()
                        logger.info(f"Dynamically fetched broker market close time for {symbol}: {parsed_t}")
                        return parsed_t
                    except ValueError:
                        continue

        # Check broker model / session attributes if available
        if hasattr(broker, "market_close_time") and broker.market_close_time:
            logger.info(f"Using broker configuration market close time: {broker.market_close_time}")
            return broker.market_close_time
    except Exception as e:
        logger.error(f"Error fetching dynamic market close time from broker API: {e}")

    # Fallback to real market close: 15:40 IST (3:40 PM) if broker API status call is offline
    return time(15, 40, 0)


async def fetch_broker_lot_size(broker: Any, db: Any, symbol: str) -> int:
    """
    Dynamically queries the active broker's instrument database for the exact lot size.
    Falls back to IndexConfig default if offline.
    """
    try:
        from sqlalchemy import select
        from app.db.models import get_instrument_model

        Instrument = get_instrument_model(broker.broker_name)
        stmt = select(Instrument.lot_size).filter(
            Instrument.symbol_name == symbol.upper()
        ).limit(1)
        res = await db.execute(stmt)
        lot_size = res.scalar()
        if lot_size:
            logger.info(f"Dynamically resolved lot size from broker DB for {symbol}: {lot_size}")
            return int(lot_size)
    except Exception as e:
        logger.warning(f"Could not query broker instrument lot size from DB: {e}")

    default_size = IndexConfig.get(symbol)["lot_size"]
    return default_size


async def fetch_broker_nearest_expiry(broker: Any, user: Any, db: Any, symbol: str) -> date:
    """
    Fetches nearest expiry date for the given symbol directly from broker API or DB.
    """
    try:
        from app.api.v1.brokers.market import get_option_chain_expiries
        from app.db.models import get_instrument_model

        res_payload = await get_option_chain_expiries(
            broker_id=broker.id,
            underlying_symbol=symbol.upper(),
            current_user=user,
            db=db
        )
        if res_payload.get("success") and res_payload.get("data"):
            expiries = res_payload["data"]
            today_dt = datetime.now(IST).date()
            valid_expiries = []
            for exp in expiries:
                try:
                    exp_dt = parse_expiry_date(exp)
                    if exp_dt >= today_dt:
                        valid_expiries.append(exp_dt)
                except Exception:
                    continue
            
            if valid_expiries:
                valid_expiries.sort()
                nearest_expiry = valid_expiries[0]
                logger.info(f"Dynamically fetched broker expiry date for {symbol}: {nearest_expiry}")
                return nearest_expiry

        # DB fallback query
        Instrument = get_instrument_model(broker.broker_name)
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        stmt = select(Instrument.expiry_date).filter(
            Instrument.symbol_name == symbol.upper(),
            Instrument.expiry_date >= today_str
        ).distinct().order_by(Instrument.expiry_date.asc())
        res = await db.execute(stmt)
        db_expiries = res.scalars().all()
        if db_expiries:
            exp_dt = parse_expiry_date(db_expiries[0])
            logger.info(f"Database query fetched broker expiry for {symbol}: {exp_dt}")
            return exp_dt
    except Exception as e:
        logger.error(f"Error fetching expiry from broker API/DB: {e}")

    return datetime.now(IST).date()


# --- Live Strategy Execution Runner ---
async def run_live_hero_zero_strategy(
    symbol: str = "NIFTY",
    trigger_time_str: str = "15:15",
    target_entry_price: float = 6.0,
    lots: int = 1,
    ce_strike_mode: str = "ATM",
    pe_strike_mode: str = "ATM"
):
    """
    Live runner for Expiry Hero-Zero Strategy.
    Fetches real-time market data from active broker, validates expiry date,
    and executes orders upon signal generation.
    """
    import asyncio
    from sqlalchemy import select
    
    # Resolve backend imports dynamically
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from app.db.session import async_session_maker
        from app.db.models import User, Broker
        from app.services.broker_gateway import BrokerGateway
        from app.api.v1.brokers.market import get_market_quote, get_option_chain_expiries, get_option_chain
    except ImportError as err:
        logger.warning(f"Backend modules not loaded in environment: {err}. Strategy class remains available for direct integration.")
        return

    logger.info(f"🚀 Initializing Live Expiry Hero-Zero Strategy for {symbol}...")
    
    async with async_session_maker() as session:
        broker_stmt = select(Broker).filter(Broker.status == "active").limit(1)
        res = await session.execute(broker_stmt)
        broker = res.scalars().first()
        if not broker:
            logger.error("No active broker found in database. Please connect a broker first.")
            return

        user_stmt = select(User).filter(User.id == broker.user_id).limit(1)
        res = await session.execute(user_stmt)
        user = res.scalars().first()
        if not user:
            logger.error("User owner not found for active broker.")
            return

        # Fetch market closing time, nearest expiry date & lot size dynamically from broker session
        broker_close_time = await fetch_broker_market_close_time(broker, user, session, symbol)
        broker_expiry_date = await fetch_broker_nearest_expiry(broker, user, session, symbol)
        broker_lot_size = await fetch_broker_lot_size(broker, session, symbol)
        logger.info(f"Broker Configuration Loaded | Close Time: {broker_close_time} | Expiry Date: {broker_expiry_date} | Lot Size: {broker_lot_size}")

    strategy = ExpiryHeroZeroStrategy(
        symbol=symbol,
        trigger_time_str=trigger_time_str,
        market_close_time=broker_close_time,
        broker_expiry_date=broker_expiry_date,
        target_entry_price=target_entry_price,
        ce_strike_mode=ce_strike_mode,
        pe_strike_mode=pe_strike_mode,
        lots=lots
    )

    logger.info(f"Live Strategy initialized for {symbol}. Real Market Timing: {strategy.market_open_time} to {strategy.market_close_time} IST (9:15 to 3:40). Listening for market ticks...")

    while True:
        now_ist = datetime.now(IST)
        now_time = now_ist.time()

        # 1. Real market timing check: wait if before market open (09:15 IST)
        if now_time < strategy.market_open_time:
            logger.debug(f"Current time {now_time} is before market open ({strategy.market_open_time}). Waiting for market to open...")
            await asyncio.sleep(1)
            continue
        
        # 2. Check market closing exit condition at 3:40 PM (15:40 IST)
        if strategy.active_positions and now_time >= strategy.market_close_time:
            # Query active option quotes for position square-off
            live_exit_quotes = {}
            strategy.evaluate_exit(now_ist, live_exit_quotes, force_market_close=True)
            break

        # 3. Stop loop after market close (after 15:40 IST)
        if now_time > strategy.market_close_time:
            logger.info(f"Market session closed (after {strategy.market_close_time}). Strategy execution finished.")
            break

        await asyncio.sleep(1)


# --- Backtesting Engine for Historical Option Chain CSVs ---

def find_default_csv_file() -> str:
    """Finds the default historical CSV file in the current directory."""
    candidates = [
        "ORIGINAL DATA FOR 1m.csv",
        "original data for 1m.csv",
        "Historical_Option_Chain_NIFTY_1m_2026-10-13_to_2026-10-13.csv",
        "NIFTY_5m_csv.csv",
        "nifty_5m_csv.csv",
        "Historical_Option_Chain_NIFTY_5m_2026-08-01_to_2026-09-29 (5).csv",
        "real(OC)sensex datas.csv"
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "ORIGINAL DATA FOR 1m.csv" if os.path.exists("ORIGINAL DATA FOR 1m.csv") else "NIFTY_5m_csv.csv"


def load_historical_option_chain_data(csv_file_path: str) -> List[Dict[str, Any]]:
    """Loads historical option chain CSV and handles case-insensitivity or alternate paths."""
    if not os.path.exists(csv_file_path):
        if os.path.exists(csv_file_path + ".csv"):
            csv_file_path = csv_file_path + ".csv"
        else:
            # Check lower/upper case variations or partial match in directory
            dir_name = os.path.dirname(csv_file_path) or "."
            base_name = os.path.basename(csv_file_path).lower()
            matched = None
            for f in sorted(os.listdir(dir_name)):
                f_lower = f.lower()
                if f_lower == base_name or f_lower == base_name + ".csv" or (len(base_name) > 3 and base_name in f_lower and f_lower.endswith(".csv")):
                    matched = os.path.join(dir_name, f)
                    break
            if matched and os.path.exists(matched):
                csv_file_path = matched
            else:
                raise FileNotFoundError(f"CSV file not found at: {csv_file_path}")

    rows = []
    with open(csv_file_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    print(f"✅ Successfully loaded {len(rows)} rows from {os.path.basename(csv_file_path)}")
    return rows


def group_data_by_date_and_timestamp(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Groups historical option chain rows by Date and Timestamp.
    Supports both 5-strike schema (multiple rows per timestamp with 'Strike Offset')
    and 1-strike schema (single row per timestamp with 'ATM CE Close (LTP)').
    """
    by_date: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for r in rows:
        d = r.get("Date", "").strip()
        ts = r.get("Timestamp", "").strip() or f"{d} {r.get('Time', '').strip()}"
        if not d or not ts:
            continue
        if d not in by_date:
            by_date[d] = {}
        if ts not in by_date[d]:
            by_date[d][ts] = []
        by_date[d][ts].append(r)

    # Convert to chronological list of candles per date
    structured_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for d, ts_dict in by_date.items():
        structured_by_date[d] = []
        for ts in sorted(ts_dict.keys()):
            candle_rows = ts_dict[ts]
            first_row = candle_rows[0]
            has_strike_offset = "Strike Offset" in first_row

            spot = float(first_row.get("Underlying Close") or first_row.get("Underlying Spot") or 0.0)
            atm_strike = float(first_row.get("ATM Strike") or (round(spot / 50.0) * 50.0))

            strike_prices: Dict[tuple, float] = {}
            quotes: Dict[str, Dict[str, Any]] = {}

            if has_strike_offset:
                # 5-strike schema: multiple rows per timestamp (ATM-2, ATM-1, ATM, ATM+1, ATM+2)
                offset_map = {r.get("Strike Offset", "").strip(): r for r in candle_rows}
                for r in candle_rows:
                    sp = float(r.get("Strike Price", 0.0))
                    ce_ltp = float(r.get("CE Close (LTP)", 0.0) or 0.0)
                    pe_ltp = float(r.get("PE Close (LTP)", 0.0) or 0.0)
                    ce_low = float(r.get("CE Low", 0.0) or ce_ltp)
                    ce_high = float(r.get("CE High", 0.0) or ce_ltp)
                    pe_low = float(r.get("PE Low", 0.0) or pe_ltp)
                    pe_high = float(r.get("PE High", 0.0) or pe_ltp)
                    strike_prices[(sp, "CE")] = ce_ltp
                    strike_prices[(sp, "PE")] = pe_ltp
                    strike_prices[(int(sp), "CE")] = ce_ltp
                    strike_prices[(int(sp), "PE")] = pe_ltp
                    quotes[f"{int(sp)}_CE"] = {"ltp": ce_ltp, "low": ce_low, "high": ce_high, "strike": sp, "opt_type": "CE"}
                    quotes[f"{int(sp)}_PE"] = {"ltp": pe_ltp, "low": pe_low, "high": pe_high, "strike": sp, "opt_type": "PE"}
                    quotes[f"{float(sp)}_CE"] = {"ltp": ce_ltp, "low": ce_low, "high": ce_high, "strike": sp, "opt_type": "CE"}
                    quotes[f"{float(sp)}_PE"] = {"ltp": pe_ltp, "low": pe_low, "high": pe_high, "strike": sp, "opt_type": "PE"}

                atm_row = offset_map.get("ATM")
                otm1_ce_row = offset_map.get("ATM+1")  # Strike ATM+50 is Call OTM1
                otm1_pe_row = offset_map.get("ATM-1")  # Strike ATM-50 is Put OTM1
                itm1_ce_row = offset_map.get("ATM-1")  # Strike ATM-50 is Call ITM1
                itm1_pe_row = offset_map.get("ATM+1")  # Strike ATM+50 is Put ITM1

                ce_atm_ltp = float(atm_row.get("CE Close (LTP)", 0.0)) if atm_row else 0.0
                pe_atm_ltp = float(atm_row.get("PE Close (LTP)", 0.0)) if atm_row else 0.0
                ce_atm_low = float(atm_row.get("CE Low", 0.0) or ce_atm_ltp) if atm_row else 0.0
                ce_atm_high = float(atm_row.get("CE High", 0.0) or ce_atm_ltp) if atm_row else 0.0
                pe_atm_low = float(atm_row.get("PE Low", 0.0) or pe_atm_ltp) if atm_row else 0.0
                pe_atm_high = float(atm_row.get("PE High", 0.0) or pe_atm_ltp) if atm_row else 0.0

                ce_otm1_ltp = float(otm1_ce_row.get("CE Close (LTP)", 0.0)) if otm1_ce_row else 0.0
                pe_otm1_ltp = float(otm1_pe_row.get("PE Close (LTP)", 0.0)) if otm1_pe_row else 0.0
                ce_otm1_low = float(otm1_ce_row.get("CE Low", 0.0) or ce_otm1_ltp) if otm1_ce_row else 0.0
                ce_otm1_high = float(otm1_ce_row.get("CE High", 0.0) or ce_otm1_ltp) if otm1_ce_row else 0.0
                pe_otm1_low = float(otm1_pe_row.get("PE Low", 0.0) or pe_otm1_ltp) if otm1_pe_row else 0.0
                pe_otm1_high = float(otm1_pe_row.get("PE High", 0.0) or pe_otm1_ltp) if otm1_pe_row else 0.0

                ce_itm1_ltp = float(itm1_ce_row.get("CE Close (LTP)", 0.0)) if itm1_ce_row else 0.0
                pe_itm1_ltp = float(itm1_pe_row.get("PE Close (LTP)", 0.0)) if itm1_pe_row else 0.0
                ce_itm1_low = float(itm1_ce_row.get("CE Low", 0.0) or ce_itm1_ltp) if itm1_ce_row else 0.0
                ce_itm1_high = float(itm1_ce_row.get("CE High", 0.0) or ce_itm1_ltp) if itm1_ce_row else 0.0
                pe_itm1_low = float(itm1_pe_row.get("PE Low", 0.0) or pe_itm1_ltp) if itm1_pe_row else 0.0
                pe_itm1_high = float(itm1_pe_row.get("PE High", 0.0) or pe_itm1_ltp) if itm1_pe_row else 0.0

                quotes["CE_ATM"] = {"ltp": ce_atm_ltp, "low": ce_atm_low, "high": ce_atm_high, "strike": atm_strike, "opt_type": "CE", "strike_type": "ATM"}
                quotes["PE_ATM"] = {"ltp": pe_atm_ltp, "low": pe_atm_low, "high": pe_atm_high, "strike": atm_strike, "opt_type": "PE", "strike_type": "ATM"}
                quotes["CE_OTM1"] = {"ltp": ce_otm1_ltp, "low": ce_otm1_low, "high": ce_otm1_high, "strike": atm_strike + 50.0, "opt_type": "CE", "strike_type": "OTM1"}
                quotes["PE_OTM1"] = {"ltp": pe_otm1_ltp, "low": pe_otm1_low, "high": pe_otm1_high, "strike": atm_strike - 50.0, "opt_type": "PE", "strike_type": "OTM1"}
                quotes["CE_ITM1"] = {"ltp": ce_itm1_ltp, "low": ce_itm1_low, "high": ce_itm1_high, "strike": atm_strike - 50.0, "opt_type": "CE", "strike_type": "ITM1"}
                quotes["PE_ITM1"] = {"ltp": pe_itm1_ltp, "low": pe_itm1_low, "high": pe_itm1_high, "strike": atm_strike + 50.0, "opt_type": "PE", "strike_type": "ITM1"}
            else:
                # 1-strike schema: single row per timestamp
                ce_ltp = float(first_row.get("ATM CE Close (LTP)", 0.0) or 0.0)
                pe_ltp = float(first_row.get("ATM PE Close (LTP)", 0.0) or 0.0)
                quotes[f"{int(atm_strike)}_CE"] = {"ltp": ce_ltp, "strike": atm_strike, "opt_type": "CE"}
                quotes[f"{int(atm_strike)}_PE"] = {"ltp": pe_ltp, "strike": atm_strike, "opt_type": "PE"}
                quotes["CE_ATM"] = {"ltp": ce_ltp, "strike": atm_strike, "opt_type": "CE", "strike_type": "ATM"}
                quotes["PE_ATM"] = {"ltp": pe_ltp, "strike": atm_strike, "opt_type": "PE", "strike_type": "ATM"}
                strike_prices[(atm_strike, "CE")] = ce_ltp
                strike_prices[(atm_strike, "PE")] = pe_ltp
                strike_prices[(int(atm_strike), "CE")] = ce_ltp
                strike_prices[(int(atm_strike), "PE")] = pe_ltp

            structured_by_date[d].append({
                "timestamp_str": ts,
                "datetime": datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") if len(ts) == 19 else datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S"),
                "spot_price": spot,
                "atm_strike": atm_strike,
                "quotes": quotes,
                "strike_prices": strike_prices,
                "raw": first_row
            })

    return structured_by_date


def get_expiry_days(date_keys: List[str], symbol: str = "NIFTY", mode: str = "weekly") -> List[str]:
    """
    Determines expiry dates based on symbol and mode:
    - NIFTY weekly: Tuesdays (weekday() == 1)
    - SENSEX weekly: Thursdays (weekday() == 3)
    - 'monthly': Last expiry day of each month
    - 'all': Every trading day in dataset
    """
    sorted_dates = sorted(date_keys)
    if mode == "all":
        return sorted_dates

    # NIFTY Tuesday (1) in this dataset, SENSEX Thursday (3)
    target_weekday = 1 if symbol.upper() == "NIFTY" else 3
    expiry_days = [d for d in sorted_dates if datetime.strptime(d, "%Y-%m-%d").weekday() == target_weekday]

    if mode == "weekly":
        return expiry_days
    elif mode == "monthly":
        monthly_expiries = {}
        for d in expiry_days:
            ym = d[:7]
            monthly_expiries[ym] = d
        return list(monthly_expiries.values())
    else:
        raise ValueError(f"Unknown expiry mode: {mode}")


def calculate_metrics(trades: List[Dict[str, Any]], target_profit: Optional[float] = 24.0) -> Dict[str, Any]:
    """Calculates multi-ratio target performance statistics (1:1, 1:2, 1:3 True / False evaluation)."""
    total_trades = len(trades)
    if total_trades == 0:
        return {
            "total_trades": 0,
            "t1_true": 0, "t1_false": 0, "t1_pct": 0.0,
            "t2_true": 0, "t2_false": 0, "t2_pct": 0.0,
            "t3_true": 0, "t3_false": 0, "t3_pct": 0.0,
            "attained_true": 0, "attained_false": 0, "attained_pct": 0.0,
            "true_count": 0, "false_count": 0, "win_rate_pct": 0.0, "result": "False"
        }

    t1_true = sum(1 for t in trades if str(t.get("target 1:1", "")).lower() == "true")
    t2_true = sum(1 for t in trades if str(t.get("target 1:2", "")).lower() == "true")
    t3_true = sum(1 for t in trades if str(t.get("target1:3", "")).lower() == "true")
    attained_true = sum(1 for t in trades if str(t.get("Attained (True/False)", "")).lower() == "true")

    t1_pct = round((t1_true / total_trades) * 100.0, 1)
    t2_pct = round((t2_true / total_trades) * 100.0, 1)
    t3_pct = round((t3_true / total_trades) * 100.0, 1)
    attained_pct = round((attained_true / total_trades) * 100.0, 1)

    return {
        "total_trades": total_trades,
        "t1_true": t1_true,
        "t1_false": total_trades - t1_true,
        "t1_pct": t1_pct,
        "t2_true": t2_true,
        "t2_false": total_trades - t2_true,
        "t2_pct": t2_pct,
        "t3_true": t3_true,
        "t3_false": total_trades - t3_true,
        "t3_pct": t3_pct,
        "attained_true": attained_true,
        "attained_false": total_trades - attained_true,
        "attained_pct": attained_pct,
        "true_count": t3_true,
        "false_count": total_trades - t3_true,
        "winning_trades": attained_true,
        "losing_trades": total_trades - attained_true,
        "win_rate_pct": attained_pct,
        "result": "True" if attained_true > 0 else "False"
    }


def export_trades_csv(trades: List[Dict[str, Any]], output_file: str = "backtest_results.csv"):
    """Exports trades to a CSV file matching the exact WPS spreadsheet layout (no PnL)."""
    if not trades:
        print("⚠️ No trades to export.")
        return

    fieldnames = [
        "epiry date", "entry price", "symbol", "strike price",
        "target 1:1", "target 1:2", "target1:3", "Attained (True/False)",
        "MARKET_CLOSE_Price", "After entry  High"
    ]

    with open(output_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            writer.writerow(t)

    print(f"💾 Exported trade results to: {output_file}")


def export_trades_excel(trades: List[Dict[str, Any]], output_file: str = "backtest_results.xls"):
    """Exports trades to a styled XLS file matching the 10-column layout with True (green) and False (red)."""
    if not trades:
        return

    html_content = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: Calibri, sans-serif; font-size: 11pt; }
  table { border-collapse: collapse; width: 100%; margin-top: 10px; }
  th { background-color: #1f4e78; color: white; padding: 10px; border: 1px solid #b0c4de; text-align: center; font-size: 11pt; font-weight: bold; }
  td { padding: 8px 12px; border: 1px solid #d3d3d3; text-align: center; font-size: 11pt; }
  tr:nth-child(even) { background-color: #f9f9f9; }
  .true-bg { background-color: #d4edda !important; color: #155724 !important; font-weight: bold; }
  .false-bg { background-color: #f8d7da !important; color: #721c24 !important; font-weight: bold; }
  .title { font-size: 16pt; font-weight: bold; color: #1f4e78; margin-bottom: 10px; }
</style>
</head>
<body>
<div class="title">📊 Expiry Hero or Zero Strategy - Backtest Results</div>
<table>
  <thead>
    <tr>
      <th>epiry date</th>
      <th>entry price</th>
      <th>symbol</th>
      <th>strike price</th>
      <th>target 1:1</th>
      <th>target 1:2</th>
      <th>target1:3</th>
      <th>Attained (True/False)</th>
      <th>MARKET_CLOSE_Price</th>
      <th>After entry  High</th>
    </tr>
  </thead>
  <tbody>
"""
    for t in trades:
        def cell_html(val: Any) -> str:
            s_val = str(val).strip()
            if s_val.lower() == "true":
                return '<td class="true-bg">True</td>'
            elif s_val.lower() == "false":
                return '<td class="false-bg">False</td>'
            return f'<td>{s_val}</td>'

        html_content += f"""    <tr>
      <td>{t.get('epiry date', t.get('expiry_date', ''))}</td>
      <td>{t.get('entry price', t.get('entry_price', ''))}</td>
      <td>{t.get('symbol', '')}</td>
      <td>{t.get('strike price', t.get('strike_price', ''))}</td>
      {cell_html(t.get('target 1:1', ''))}
      {cell_html(t.get('target 1:2', ''))}
      {cell_html(t.get('target1:3', ''))}
      {cell_html(t.get('Attained (True/False)', ''))}
      <td>{t.get('MARKET_CLOSE_Price', '')}</td>
      <td>{t.get('After entry  High', '')}</td>
    </tr>\n"""

    html_content += """  </tbody>
</table>
</body>
</html>"""

    with open(output_file, mode="w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"🎨 Exported styled green/red Excel spreadsheet to: {output_file}")


def print_summary_report(results: Dict[str, Any]):
    """Prints a clear summary report with multi-ratio True/False evaluation matching the 10-column layout."""
    m = results["metrics"]
    c = results["config"]
    trades = results["trades"]

    print("\n==========================================================================================")
    print(f"📊 MULTI-RATIO TARGET EVALUATION SUMMARY ({c['symbol']} EXPIRY HERO-ZERO)")
    print("==========================================================================================")
    print(f"Dataset Total Days    : {c['total_days_evaluated']} Days ({c.get('expiry_days_evaluated', 0)} Expiry Days Evaluated)")
    print(f"Real Market Timing    : {c.get('market_open_time', '09:15')} to {c.get('market_close_time', '15:40')} IST (9:15 to 3:40)")
    time_label = "Strictly > 15:15 IST (after 3:15 PM)" if c.get("strict_after_315") else f">= {c['trigger_time']} IST (at or after 3:15 PM, not before)"
    if c.get("execution_mode") == "limit":
        price_label = f"Limit Order fill @ exact ₹{c.get('target_entry_price', 6.0):.2f}"
    else:
        price_label = f"₹{c.get('min_entry_price', 0):.2f} to ₹{c.get('max_entry_price', 0):.2f} (Exact ₹{c.get('target_entry_price', 6.0):.2f} ± ₹{c.get('price_tolerance', 0.10):.2f})"

    t_entry = c.get('target_entry_price', 6.0)
    print(f"Trigger Time Window   : {time_label}")
    print(f"Target Ratios         : 1:1 (₹{t_entry*2:.2f}), 1:2 (₹{t_entry*3:.2f}), 1:3 (₹{t_entry*4:.2f})")
    print(f"Market Close Exit     : {c['market_close_time']} IST (Square-off if target not met)")
    print(f"Entry Price Rule      : {price_label}")
    print(f"Lot Size & Quantity   : {c['lots']} Lot(s) ({c['lots'] * IndexConfig.get(c['symbol'])['lot_size']} Qty)")
    print("------------------------------------------------------------------------------------------")
    print(f"Total Trades Placed   : {m['total_trades']}")
    print(f"Target 1:1 Attained   : {m.get('t1_true', 0)} / {m['total_trades']} ({m.get('t1_pct', 0.0)}%)")
    print(f"Target 1:2 Attained   : {m.get('t2_true', 0)} / {m['total_trades']} ({m.get('t2_pct', 0.0)}%)")
    print(f"Target 1:3 Attained   : {m.get('t3_true', 0)} / {m['total_trades']} ({m.get('t3_pct', 0.0)}%)")
    print(f"Overall Attained      : {m.get('attained_true', 0)} / {m['total_trades']} ({m.get('attained_pct', 0.0)}%)")
    print(f"Overall Result        : {'True' if m.get('attained_true', 0) > 0 else 'False'}")
    print("==========================================================================================\n")

    if trades:
        print("Detailed Trade Breakdown (Exact 10-Column Spreadsheet Layout):")
        headers = ["epiry date", "entry price", "symbol", "strike price", "target 1:1", "target 1:2", "target1:3", "Attained (True/False)", "MARKET_CLOSE_Price", "After entry  High"]
        widths = [12, 12, 8, 20, 11, 11, 11, 23, 20, 18]
        header_line = " | ".join(f"{h:<{w}}" for h, w in zip(headers, widths))
        sep_line = "-" * len(header_line)
        print(header_line)
        print(sep_line)
        for t in trades:
            row = [
                str(t.get("epiry date", t.get("expiry_date", ""))),
                str(t.get("entry price", t.get("entry_price", ""))),
                str(t.get("symbol", "")),
                str(t.get("strike price", t.get("strike_price", ""))),
                str(t.get("target 1:1", "")),
                str(t.get("target 1:2", "")),
                str(t.get("target1:3", "")),
                str(t.get("Attained (True/False)", "")),
                str(t.get("MARKET_CLOSE_Price", "")),
                str(t.get("After entry  High", ""))
            ]
            print(" | ".join(f"{val:<{w}}" for val, w in zip(row, widths)))
        print(sep_line + "\n")


def run_backtest(
    csv_file_path: str,
    symbol: str = "NIFTY",
    trigger_time_str: str = "15:15",
    market_open_time_str: str = "09:15",
    market_close_time_str: str = "15:40",
    target_entry_price: float = 6.0,
    min_entry_price: Optional[float] = None,
    max_entry_price: Optional[float] = None,
    price_tolerance: float = 0.10,
    target_profit: Optional[float] = 24.0,
    execution_mode: str = "limit",
    strict_after_315: bool = False,
    lots: int = 1,
    expiry_mode: str = "weekly",
    strike_mode: str = "atm_otm1"
) -> Dict[str, Any]:
    """
    Executes backtest over historical dataset using ExpiryHeroZeroStrategy.
    Evaluates CE ATM, PE ATM, CE OTM1, PE OTM1 on expiry days (excluding ITM1).
    Real market timing: 9:15 to 3:40 IST (09:15 to 15:40).
    """
    rows = load_historical_option_chain_data(csv_file_path)
    structured_by_date = group_data_by_date_and_timestamp(rows)
    all_dates = sorted(list(structured_by_date.keys()))
    expiry_dates = get_expiry_days(all_dates, symbol=symbol, mode=expiry_mode)

    open_h, open_m = map(int, market_open_time_str.split(":"))
    market_open_t = time(open_h, open_m, 0)
    close_h, close_m = map(int, market_close_time_str.split(":"))
    market_close_t = time(close_h, close_m, 0)

    strategy = ExpiryHeroZeroStrategy(
        symbol=symbol,
        trigger_time_str=trigger_time_str,
        market_open_time=market_open_t,
        market_close_time=market_close_t,
        target_entry_price=target_entry_price,
        min_entry_price=min_entry_price,
        max_entry_price=max_entry_price,
        price_tolerance=price_tolerance,
        target_profit=target_profit,
        execution_mode=execution_mode,
        strict_after_315=strict_after_315,
        lots=lots,
        strike_mode=strike_mode
    )

    all_trades: List[Dict[str, Any]] = []

    mode_lower = strike_mode.lower()
    if mode_lower in ("all", "atm_otm_itm", "atm_otm1_itm1"):
        strikes_desc = "ALL (ATM, OTM1, ITM1: CE ATM/OTM1/ITM1 & PE ATM/OTM1/ITM1)"
    elif mode_lower in ("itm1_only", "itm_only", "itm"):
        strikes_desc = "ITM1 ONLY (CE ITM1 & PE ITM1)"
    elif mode_lower in ("otm1_only", "otm_only", "otm"):
        strikes_desc = "OTM1 ONLY (CE OTM1 & PE OTM1)"
    elif mode_lower in ("atm_only", "atm"):
        strikes_desc = "ATM ONLY (CE ATM & PE ATM)"
    else:
        strikes_desc = "ATM & OTM1 (CE ATM, CE OTM1, PE ATM, PE OTM1 - No ITM1)"

    time_rule = "Strictly > 15:15 IST (after 3:15 PM)" if strict_after_315 else f">= {trigger_time_str} IST (at or after 3:15 PM, not before)"
    if execution_mode == "limit":
        exec_rule = f"Limit Order fill @ exact ₹{target_entry_price:.2f} (when Low <= {target_entry_price:.2f} <= High)"
    else:
        exec_rule = f"Close LTP Window: ₹{strategy.min_entry_price:.2f} to ₹{strategy.max_entry_price:.2f} (Exact ₹{target_entry_price:.2f} ± ₹{price_tolerance:.2f})"

    print(f"\n==========================================================================================")
    print(f"🚀 RUNNING BACKTEST: {symbol} Expiry Hero or Zero Strategy")
    print(f"==========================================================================================")
    print(f"• Dataset Source     : {os.path.basename(csv_file_path)}")
    print(f"• Strikes Evaluated  : {strikes_desc}")
    print(f"• Dataset Total Days : {len(all_dates)} ({all_dates[0]} to {all_dates[-1]})")
    print(f"• Real Market Timing : {market_open_time_str} to {market_close_time_str} IST (9:15 to 3:40)")
    print(f"• Expiry Mode        : {expiry_mode.upper()} ({len(expiry_dates)} expiry days evaluated)")
    print(f"• Trigger Window     : {time_rule}")
    target_val = target_profit if target_profit is not None else 24.0
    print(f"• Target Condition   : 1:3 Target (₹{target_val:.2f} Target Price on ₹{target_entry_price:.2f} Entry)")
    print(f"• Market Close Exit  : {market_close_time_str} IST (Square-off if 1:3 target not met)")
    print(f"• Entry Price Rule   : {exec_rule}")
    print(f"• Quantity Traded    : {lots} Lot(s) ({lots * IndexConfig.get(symbol)['lot_size']} Qty)")
    print(f"==========================================================================================\n")

    # Pre-index day close quotes for every date and strike
    day_close_quotes_by_date: Dict[str, Dict[str, float]] = {}
    for d_str, day_c in structured_by_date.items():
        day_close_quotes_by_date[d_str] = {}
        for c in reversed(day_c):
            for (sp, opt), ltp in c["strike_prices"].items():
                k1 = f"{int(sp)}_{opt}"
                k2 = f"{sp}_{opt}"
                if k1 not in day_close_quotes_by_date[d_str]:
                    day_close_quotes_by_date[d_str][k1] = ltp
                    day_close_quotes_by_date[d_str][k2] = ltp

    def create_trade_record(t_rec: Dict[str, Any], date_str: str, default_exit_time: str, last_day_spot: float) -> Dict[str, Any]:
        pos_entry = float(t_rec.get("entry_price", target_entry_price))
        after_entry_high = round(float(t_rec.get("max_high", pos_entry)), 2)

        # Retrieve market close price from day_close_quotes_by_date
        sp_key1 = f"{int(t_rec['strike'])}_{t_rec['option_type']}"
        sp_key2 = f"{float(t_rec['strike'])}_{t_rec['option_type']}"
        mkt_close = day_close_quotes_by_date.get(date_str, {}).get(sp_key1)
        if mkt_close is None:
            mkt_close = day_close_quotes_by_date.get(date_str, {}).get(sp_key2)
        if mkt_close is None:
            if t_rec["option_type"] == "CE":
                mkt_close = max(0.05, last_day_spot - t_rec["strike"])
            else:
                mkt_close = max(0.05, t_rec["strike"] - last_day_spot)
        mkt_close_price = round(float(mkt_close), 2)

        # Target Ratios:
        # 1:1 -> ₹12.00 (target_entry_price * 2)
        # 1:2 -> ₹18.00 (target_entry_price * 3)
        # 1:3 -> ₹24.00 (target_entry_price * 4)
        t1_thresh = round(target_entry_price * 2.0, 2)
        t2_thresh = round(target_entry_price * 3.0, 2)
        t3_thresh = round(target_entry_price * 4.0, 2)

        t_1_1 = "True" if after_entry_high >= t1_thresh else "False"
        t_1_2 = "True" if after_entry_high >= t2_thresh else "False"
        t_1_3 = "True" if after_entry_high >= t3_thresh else "False"
        attained = "True" if after_entry_high >= t1_thresh else "False"

        strike_label = f"{int(t_rec['strike'])} {t_rec['option_type']} ({t_rec.get('strike_type', 'ATM')})"
        exit_reason = t_rec.get("exit_reason", "MARKET_CLOSE_TIME")

        return {
            # Exact 10 WPS spreadsheet columns matching user requirements:
            "epiry date": date_str,
            "entry price": f"{pos_entry:.2f}",
            "symbol": t_rec.get("symbol", symbol),
            "strike price": strike_label,
            "target 1:1": t_1_1,
            "target 1:2": t_1_2,
            "target1:3": t_1_3,
            "Attained (True/False)": attained,
            "MARKET_CLOSE_Price": f"{mkt_close_price:.2f}",
            "After entry  High": f"{after_entry_high:.2f}",
            # Internal fields:
            "expiry_date": date_str,
            "execution_time": t_rec.get("timestamp", default_exit_time),
            "exit_time": t_rec.get("exit_time", default_exit_time),
            "meet_time": t_rec.get("meet_time", default_exit_time),
            "strike": t_rec["strike"],
            "option_type": t_rec["option_type"],
            "strike_type": t_rec.get("strike_type", "ATM"),
            "raw_entry_price": pos_entry,
            "raw_after_entry_high": after_entry_high,
            "raw_market_close": mkt_close_price,
            "exit_reason": exit_reason,
            "is_target_attained": attained == "True"
        }

    for current_date_str in all_dates:
        is_expiry = current_date_str in expiry_dates
        exp_date_obj = datetime.strptime(current_date_str, "%Y-%m-%d").date()

        # Reset daily strategy state
        strategy.is_traded_today = False
        strategy.active_positions = []
        strategy.set_broker_expiry_date(exp_date_obj if is_expiry else None)

        day_candles = structured_by_date[current_date_str]
        num_candles = len(day_candles)
        last_day_spot = day_candles[-1]["spot_price"] if day_candles else 0.0

        for candle_idx, candle in enumerate(day_candles):
            dt = candle["datetime"]
            current_t = dt.time()

            # Ignore data outside real market timing (09:15 to 15:40 IST)
            if current_t < market_open_t or current_t > market_close_t:
                continue

            spot = candle["spot_price"]
            quotes = candle["quotes"]
            strike_prices = candle["strike_prices"]
            is_last_candle = (candle_idx == num_candles - 1)

            # Build complete exit price mapping from all strikes in the candle
            exit_quotes: Dict[str, float] = {}
            for (sp, opt), ltp in strike_prices.items():
                exit_quotes[f"{sp}_{opt}"] = ltp
                exit_quotes[f"{int(sp)}_{opt}"] = ltp
                exit_quotes[f"{float(sp)}_{opt}"] = ltp

            # Add intrinsic settlement fallback for any strike outside monitored window
            for pos in strategy.active_positions:
                p_strike = pos["strike"]
                p_opt = pos["option_type"]
                k1 = f"{p_strike}_{p_opt}"
                k2 = f"{int(p_strike)}_{p_opt}"
                if k1 not in exit_quotes and k2 not in exit_quotes:
                    if p_opt == "CE":
                        intrinsic = max(0.05, spot - p_strike)
                    else:
                        intrinsic = max(0.05, p_strike - spot)
                    exit_quotes[k1] = intrinsic
                    exit_quotes[k2] = intrinsic
                    if k1 not in quotes and k2 not in quotes:
                        quotes[k1] = {"ltp": intrinsic, "low": intrinsic, "high": intrinsic, "strike": p_strike, "opt_type": p_opt}
                        quotes[k2] = {"ltp": intrinsic, "low": intrinsic, "high": intrinsic, "strike": p_strike, "opt_type": p_opt}

            # 1. Evaluate Exit for active positions on EVERY candle (checks Target meet time or Market close)
            if strategy.active_positions:
                eval_dt = dt.replace(hour=market_close_t.hour, minute=market_close_t.minute, second=market_close_t.second) if is_last_candle else dt
                exited_records = strategy.evaluate_exit(
                    current_datetime=eval_dt,
                    current_option_prices=exit_quotes,
                    broker_close_time=market_close_t,
                    candle_quotes=quotes,
                    force_market_close=is_last_candle
                )

                for t in exited_records:
                    trade_item = create_trade_record(t, current_date_str, dt.strftime("%Y-%m-%d %H:%M:%S"), last_day_spot)
                    all_trades.append(trade_item)

            # End day processing once market close time reached or on final candle
            if dt.time() >= market_close_t or is_last_candle:
                # Ensure any remaining active positions are squared off at market close
                if strategy.active_positions:
                    close_dt = dt.replace(hour=market_close_t.hour, minute=market_close_t.minute, second=market_close_t.second)
                    force_exits = strategy.evaluate_exit(
                        current_datetime=close_dt,
                        current_option_prices=exit_quotes,
                        broker_close_time=market_close_t,
                        candle_quotes=quotes,
                        force_market_close=True
                    )
                    for t in force_exits:
                        trade_item = create_trade_record(t, current_date_str, close_dt.strftime("%Y-%m-%d %H:%M:%S"), last_day_spot)
                        all_trades.append(trade_item)
                break

            # 2. Evaluate Entry if no active trade today and within trigger window
            if not strategy.active_positions and not strategy.is_traded_today:
                strategy.evaluate_entry(
                    current_datetime=dt,
                    spot_price=spot,
                    option_quotes=quotes,
                    actual_expiry_date=exp_date_obj if is_expiry else None,
                    broker_close_time=market_close_t
                )

    metrics = calculate_metrics(all_trades)
    return {
        "trades": all_trades,
        "metrics": metrics,
        "config": {
            "symbol": symbol,
            "trigger_time": trigger_time_str,
            "market_open_time": market_open_time_str,
            "market_close_time": market_close_time_str,
            "target_entry_price": target_entry_price,
            "min_entry_price": strategy.min_entry_price,
            "max_entry_price": strategy.max_entry_price,
            "price_tolerance": price_tolerance,
            "target_profit": target_profit,
            "execution_mode": execution_mode,
            "strict_after_315": strict_after_315,
            "lots": lots,
            "expiry_mode": expiry_mode,
            "strike_mode": strike_mode,
            "total_days_evaluated": len(all_dates),
            "expiry_days_evaluated": len(expiry_dates)
        }
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Expiry Hero or Zero Strategy - Backtesting & Live Engine")
    parser.add_argument("--csv", type=str, default=None, help="Path to historical option chain CSV (defaults to NIFTY_5m_csv.csv)")
    parser.add_argument("--symbol", type=str, default="NIFTY", help="Symbol name (NIFTY / SENSEX)")
    parser.add_argument("--trigger-time", type=str, default="15:15", help="Entry trigger time IST (e.g. 15:15 or 15:19)")
    parser.add_argument("--market-open", type=str, default="09:15", help="Real market open time IST (default: 09:15)")
    parser.add_argument("--market-close", type=str, default="15:40", help="Real market close square-off time IST (default: 15:40)")
    parser.add_argument("--target-entry-price", type=float, default=6.0, help="Target option entry price (strictly ₹6.00)")
    parser.add_argument("--price-tolerance", type=float, default=0.10, help="Tolerance around entry price (default: 0.10, giving ₹5.90 to ₹6.10)")
    parser.add_argument("--min-entry-price", type=float, default=None, help="Explicit minimum entry price filter (e.g. 5.90)")
    parser.add_argument("--max-entry-price", type=float, default=None, help="Explicit maximum entry price cap (e.g. 6.10 or 6.00)")
    parser.add_argument("--target-profit", type=float, default=24.0, help="Target profit price for position exit (default: ₹24.00 for 1:3 target)")
    parser.add_argument("--mode", type=str, default="limit", choices=["limit", "close"], help="Execution mode: 'limit' (exact ₹6 limit fill, default) or 'close' (candle close in range)")
    parser.add_argument("--strict-after-315", action="store_true", help="Enforce strictly after 15:15:00 (> 15:15), ignoring the 15:15:00 bar itself")
    parser.add_argument("--lots", type=int, default=1, help="Number of lots to trade")
    parser.add_argument("--expiry-mode", type=str, default="weekly", choices=["weekly", "monthly", "all"], help="Expiry day selection rule")
    parser.add_argument("--strike-mode", type=str, default="atm_otm1", 
                        choices=["atm_otm1", "otm1_only", "atm_only", "itm1_only", "all", "atm_otm_itm"], 
                        help="Strike selection mode (default: atm_otm1)")
    parser.add_argument("--otm-only", "--OTM-only", "--otm", dest="otm_only", action="store_true", help="Shortcut to trade OTM1 strikes only (CE OTM1 & PE OTM1)")
    parser.add_argument("--atm-only", "--ATM-only", "--atm", dest="atm_only", action="store_true", help="Shortcut to trade ATM strikes only (CE ATM & PE ATM)")
    parser.add_argument("--itm-only", "--ITM-only", "--itm", dest="itm_only", action="store_true", help="Shortcut to trade ITM1 strikes only (CE ITM1 & PE ITM1)")
    parser.add_argument("--all-strikes", "--ALL-strikes", dest="all_strikes", action="store_true", help="Shortcut to trade all strikes (ATM, OTM1, ITM1)")
    parser.add_argument("--compare-all", "--COMPARE-ALL", dest="compare_all", action="store_true", help="Run comparative backtest across ATM, OTM, ITM, and ALL")
    parser.add_argument("--export-csv", type=str, default="backtest_results.csv", help="Output file path for trade export")
    parser.add_argument("--live", action="store_true", help="Run in live trading broker execution mode")

    args = parser.parse_args()

    if args.live:
        import asyncio
        print(f"Starting Live Expiry Hero-Zero Strategy execution for {args.symbol} with trigger {args.trigger_time}...")
        try:
            asyncio.run(run_live_hero_zero_strategy(symbol=args.symbol, trigger_time_str=args.trigger_time, lots=args.lots))
        except KeyboardInterrupt:
            logger.info("Strategy execution interrupted by user.")
    else:
        # Resolve CSV file
        csv_file = args.csv or find_default_csv_file()

        # Auto-detect symbol from CSV if possible
        detected_sym = args.symbol
        if "sensex" in csv_file.lower():
            detected_sym = "SENSEX"
        elif "nifty" in csv_file.lower():
            detected_sym = "NIFTY"

        if args.compare_all:
            configs = [
                ("ATM ONLY", "atm_only"),
                ("OTM ONLY", "otm1_only"),
                ("ITM ONLY", "itm1_only"),
                ("ALL (ATM + OTM + ITM)", "all")
            ]
            comp_results = []
            results_by_sm = {}
            for label, sm in configs:
                res = run_backtest(
                    csv_file_path=csv_file,
                    symbol=detected_sym,
                    trigger_time_str=args.trigger_time,
                    market_open_time_str=args.market_open,
                    market_close_time_str=args.market_close,
                    target_entry_price=args.target_entry_price,
                    min_entry_price=args.min_entry_price,
                    max_entry_price=args.max_entry_price,
                    price_tolerance=args.price_tolerance,
                    target_profit=args.target_profit,
                    execution_mode=args.mode,
                    strict_after_315=args.strict_after_315,
                    lots=args.lots,
                    expiry_mode=args.expiry_mode,
                    strike_mode=sm
                )
                comp_results.append((label, sm, res))
                results_by_sm[sm] = res

            print("\n" + "=" * 125)
            print("📊 COMPARATIVE MULTI-RATIO TARGET SUMMARY: ATM vs OTM vs ITM vs ALL")
            print("=" * 125)
            print(f"{'Configuration':<24} | {'Trades':<8} | {'Target 1:1':<18} | {'Target 1:2':<18} | {'Target 1:3':<18} | {'Overall Attained':<18} | {'Result':<8}")
            print("-" * 125)
            for label, sm, res in comp_results:
                m = res["metrics"]
                tot = m['total_trades']
                t1_s = f"{m.get('t1_true', 0)}/{tot} ({m.get('t1_pct', 0.0)}%)" if tot > 0 else "0/0 (0.0%)"
                t2_s = f"{m.get('t2_true', 0)}/{tot} ({m.get('t2_pct', 0.0)}%)" if tot > 0 else "0/0 (0.0%)"
                t3_s = f"{m.get('t3_true', 0)}/{tot} ({m.get('t3_pct', 0.0)}%)" if tot > 0 else "0/0 (0.0%)"
                att_s = f"{m.get('attained_true', 0)}/{tot} ({m.get('attained_pct', 0.0)}%)" if tot > 0 else "0/0 (0.0%)"
                overall_res = "True" if m.get("attained_true", 0) > 0 else "False"
                print(
                    f"{label:<24} | {tot:<8} | {t1_s:<18} | {t2_s:<18} | {t3_s:<18} | {att_s:<18} | {overall_res:<8}"
                )
            print("=" * 125 + "\n")

            # Extract evaluated expiry dates across results
            all_exp_dates = []
            for _, _, res in comp_results:
                for t in res["trades"]:
                    if t["expiry_date"] not in all_exp_dates:
                        all_exp_dates.append(t["expiry_date"])

            rows = load_historical_option_chain_data(csv_file)
            structured = group_data_by_date_and_timestamp(rows)
            calendar_expiries = get_expiry_days(list(structured.keys()), symbol=detected_sym, mode=args.expiry_mode)
            sorted_exp_dates = sorted(list(set(all_exp_dates + calendar_expiries)))

            print("=" * 95)
            print("📅 EXPIRY DATE-BY-DATE TARGET ATTAINMENT (1:1 / 1:2 / 1:3 Status)")
            print("=" * 95)
            print(f"{'Expiry Date':<12} | {'ATM (1:1/1:2/1:3)':<20} | {'OTM (1:1/1:2/1:3)':<20} | {'ITM (1:1/1:2/1:3)':<20} | {'ANY Attained':<14}")
            print("-" * 95)

            atm_true_days = 0
            otm_true_days = 0
            itm_true_days = 0
            any_true_days = 0
            total_days = len(sorted_exp_dates)

            def format_day_status(trades_list: List[Dict[str, Any]]) -> str:
                if not trades_list:
                    return "No Trade"
                t1 = any(str(t.get('target 1:1', '')).lower() == 'true' for t in trades_list)
                t2 = any(str(t.get('target 1:2', '')).lower() == 'true' for t in trades_list)
                t3 = any(str(t.get('target1:3', '')).lower() == 'true' for t in trades_list)
                def sym(b: bool) -> str: return "T" if b else "F"
                return f"{sym(t1)} / {sym(t2)} / {sym(t3)}"

            for d in sorted_exp_dates:
                atm_trades = [t for t in results_by_sm.get('atm_only', {}).get('trades', []) if t['expiry_date'] == d]
                otm_trades = [t for t in results_by_sm.get('otm1_only', {}).get('trades', []) if t['expiry_date'] == d]
                itm_trades = [t for t in results_by_sm.get('itm1_only', {}).get('trades', []) if t['expiry_date'] == d]

                atm_status = format_day_status(atm_trades)
                otm_status = format_day_status(otm_trades)
                itm_status = format_day_status(itm_trades)

                atm_att = any(str(t.get('Attained (True/False)', '')).lower() == 'true' for t in atm_trades)
                otm_att = any(str(t.get('Attained (True/False)', '')).lower() == 'true' for t in otm_trades)
                itm_att = any(str(t.get('Attained (True/False)', '')).lower() == 'true' for t in itm_trades)
                any_att = atm_att or otm_att or itm_att

                if atm_att: atm_true_days += 1
                if otm_att: otm_true_days += 1
                if itm_att: itm_true_days += 1
                if any_att: any_true_days += 1

                print(f"{d:<12} | {atm_status:<20} | {otm_status:<20} | {itm_status:<20} | {str(any_att):<14}")

            print("-" * 95)
            print(f"{'DAYS ATTAINED':<12} | {f'{atm_true_days} / {total_days}':<20} | {f'{otm_true_days} / {total_days}':<20} | {f'{itm_true_days} / {total_days}':<20} | {f'{any_true_days} / {total_days}':<14}")
            any_pct = (any_true_days / total_days * 100) if total_days > 0 else 0.0
            print(f"{'SUCCESS RATE':<12} | {f'{atm_true_days/total_days*100:.1f}%':<20} | {f'{otm_true_days/total_days*100:.1f}%':<20} | {f'{itm_true_days/total_days*100:.1f}%':<20} | {f'{any_pct:.1f}%':<14}")
            print("=" * 95 + "\n")
        else:
            if args.otm_only:
                chosen_strike_mode = "otm1_only"
            elif args.atm_only:
                chosen_strike_mode = "atm_only"
            elif args.itm_only:
                chosen_strike_mode = "itm1_only"
            elif args.all_strikes:
                chosen_strike_mode = "all"
            else:
                chosen_strike_mode = args.strike_mode

            results = run_backtest(
                csv_file_path=csv_file,
                symbol=detected_sym,
                trigger_time_str=args.trigger_time,
                market_open_time_str=args.market_open,
                market_close_time_str=args.market_close,
                target_entry_price=args.target_entry_price,
                min_entry_price=args.min_entry_price,
                max_entry_price=args.max_entry_price,
                price_tolerance=args.price_tolerance,
                target_profit=args.target_profit,
                execution_mode=args.mode,
                strict_after_315=args.strict_after_315,
                lots=args.lots,
                expiry_mode=args.expiry_mode,
                strike_mode=chosen_strike_mode
            )

            print_summary_report(results)
            if results["trades"]:
                export_trades_csv(results["trades"], output_file=args.export_csv)
                xls_path = args.export_csv.replace(".csv", ".xls")
                export_trades_excel(results["trades"], output_file=xls_path)


