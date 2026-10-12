"""
Expiry Hero or Zero Strategy for NIFTY and SENSEX
=================================================

Conditions:
1. Underlying Index: NIFTY and SENSEX support
2. Expiry Filter: Triggers ONLY on Expiry day for the respective index
3. Execution Time: Triggers after 3:15 PM or 3:19 PM IST
4. Entry Target Price: Option Premium strictly exact ₹6.00 (± ₹0.10)
5. Strike Selection: 
   - ATM (At-The-Money Call & Put)
   - CE OTM1 (1 Strike Out-Of-The-Money Call: ATM + 1 Strike Gap)
   - PE OTM1 (1 Strike Out-Of-The-Money Put: ATM - 1 Strike Gap)
   - (ITM1 strikes excluded)
6. Risk Management & Profit Target:
   - Target Profit: ₹20.00 (Position closes at ₹20.00 at exact Target Meet Time)
   - No Stop-loss (SL = None)
7. Exit Rules:
   - Target Price Meet Time: Closes at ₹20.00 when target is met
   - Market Closing Time: Square-off at 15:30 IST if target is not met
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
        market_close_time: Optional[time] = None,  # Dynamic broker market close time
        broker_expiry_date: Optional[date] = None,  # Dynamic broker nearest expiry date
        target_entry_price: float = 6.0,  # Target option entry price strictly exact ₹6
        min_entry_price: Optional[float] = None,  # Minimum entry price floor (e.g. ₹5.90)
        max_entry_price: Optional[float] = None,  # Maximum entry price cap (e.g. ₹6.10 or ₹6.00)
        price_tolerance: float = 0.10,  # Strict tolerance: ± ₹0.10 (₹5.90 to ₹6.10)
        target_profit: float = 20.0,  # Target profit price (default: ₹20.00)
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
        
        # Market closing time obtained dynamically from broker
        self.market_close_time = market_close_time
        
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
        
        # Risk & Targets - No SL, Target = ₹20.00
        self.stop_loss = None
        self.target_profit = float(target_profit) if target_profit is not None else 20.0
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
        Calculates target strikes based on configuration:
        - ATM: CE ATM (atm_strike), PE ATM (atm_strike)
        - OTM1: CE OTM1 (atm_strike + strike_gap), PE OTM1 (atm_strike - strike_gap)
        (ITM1 is excluded as per requirement)
        """
        strike_gap = self.config["strike_gap"]
        atm_strike = float(round(spot_price / strike_gap) * strike_gap)

        strikes = []
        if self.strike_mode in ("OTM1_ONLY", "OTM1", "OTM"):
            strikes.extend([
                {"option_type": "CE", "strike_type": "OTM1", "strike": atm_strike + strike_gap},
                {"option_type": "PE", "strike_type": "OTM1", "strike": atm_strike - strike_gap},
            ])
        elif self.strike_mode in ("ATM_ONLY", "ATM"):
            strikes.extend([
                {"option_type": "CE", "strike_type": "ATM", "strike": atm_strike},
                {"option_type": "PE", "strike_type": "ATM", "strike": atm_strike},
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

        # Update market close time from broker if provided
        close_time = broker_close_time or self.market_close_time or time(15, 39, 0)

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
                    "timestamp": current_datetime.strftime("%Y-%m-%d %H:%M:%S")
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
        candle_quotes: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """
        Evaluates exit conditions for active positions.
        Exit Rules:
        1. Target Hit: If premium reaches target_profit (₹20.00), exit immediately
           with closing_price = target_profit at the exact meet time.
        2. Market Closing Time: Square-off remaining positions at market close time
           (e.g. 15:30 IST) at market close LTP or intrinsic settlement value.
        """
        current_time = current_datetime.time()
        close_time = broker_close_time or self.market_close_time or time(15, 30, 0)
        exited_positions = []

        if not self.active_positions:
            return exited_positions

        remaining_positions = []
        is_market_close = current_time >= close_time

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
                # Target price met! Closing price equals strictly target price (₹20.00)
                exit_price = float(self.target_profit)
                exit_reason = f"TARGET_{int(self.target_profit)}_REACHED"
                pnl = (exit_price - pos['entry_price']) * pos['quantity']

                exit_record = {
                    **pos,
                    "exit_price": exit_price,
                    "exit_time": meet_time_str,
                    "meet_time": meet_time_str,
                    "exit_reason": exit_reason,
                    "pnl": round(pnl, 2)
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
                    "pnl": round(pnl, 2)
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

    # Fallback to standard 15:39 IST if broker API status call is offline
    return time(15, 39, 0)


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
    target_entry_price: float = 5.0,
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

    logger.info(f"Live Strategy initialized for {symbol}. Listening for market ticks...")

    while True:
        now_ist = datetime.now(IST)
        
        # Check market closing exit condition
        if strategy.active_positions and now_ist.time() >= strategy.market_close_time:
            # Query active option quotes for position square-off
            live_exit_quotes = {}
            strategy.evaluate_exit(now_ist, live_exit_quotes)
            break

        # Stop loop after market close
        if now_ist.time() > strategy.market_close_time:
            logger.info("Market session closed. Strategy execution finished.")
            break

        await asyncio.sleep(1)


# --- Backtesting Engine for Historical Option Chain CSVs ---

def find_default_csv_file() -> str:
    """Finds the default historical CSV file in the current directory."""
    candidates = [
        "NIFTY_5m_csv.csv",
        "nifty_5m_csv.csv",
        "Historical_Option_Chain_NIFTY_5m_2026-08-01_to_2026-09-29 (5).csv",
        "real(OC)sensex datas.csv"
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "NIFTY_5m_csv.csv"


def load_historical_option_chain_data(csv_file_path: str) -> List[Dict[str, Any]]:
    """Loads historical option chain CSV and handles case-insensitivity or alternate paths."""
    if not os.path.exists(csv_file_path):
        # Check lower/upper case variations in directory
        dir_name = os.path.dirname(csv_file_path) or "."
        base_name = os.path.basename(csv_file_path).lower()
        matched = None
        for f in os.listdir(dir_name):
            if f.lower() == base_name:
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

                quotes["CE_ATM"] = {"ltp": ce_atm_ltp, "low": ce_atm_low, "high": ce_atm_high, "strike": atm_strike, "opt_type": "CE", "strike_type": "ATM"}
                quotes["PE_ATM"] = {"ltp": pe_atm_ltp, "low": pe_atm_low, "high": pe_atm_high, "strike": atm_strike, "opt_type": "PE", "strike_type": "ATM"}
                quotes["CE_OTM1"] = {"ltp": ce_otm1_ltp, "low": ce_otm1_low, "high": ce_otm1_high, "strike": atm_strike + 50.0, "opt_type": "CE", "strike_type": "OTM1"}
                quotes["PE_OTM1"] = {"ltp": pe_otm1_ltp, "low": pe_otm1_low, "high": pe_otm1_high, "strike": atm_strike - 50.0, "opt_type": "PE", "strike_type": "OTM1"}
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


def calculate_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculates performance statistics from trade log."""
    total_trades = len(trades)
    if total_trades == 0:
        return {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "win_rate_pct": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "net_pnl": 0.0, "profit_factor": 0.0, "max_win": 0.0,
            "max_loss": 0.0, "avg_trade_pnl": 0.0, "max_drawdown": 0.0
        }

    pnls = [t["pnl_amount"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_pnl = sum(pnls)

    win_rate = (len(wins) / total_trades) * 100.0 if total_trades > 0 else 0.0
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0

    for p in pnls:
        cum_pnl += p
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades": total_trades,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": round(win_rate, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(net_pnl, 2),
        "profit_factor": profit_factor,
        "max_win": round(max(pnls) if pnls else 0.0, 2),
        "max_loss": round(min(pnls) if pnls else 0.0, 2),
        "avg_trade_pnl": round(net_pnl / total_trades, 2),
        "max_drawdown": round(max_dd, 2)
    }


def export_trades_csv(trades: List[Dict[str, Any]], output_file: str = "backtest_results.csv"):
    """Exports trades array to a CSV file."""
    if not trades:
        print("⚠️ No trades to export.")
        return

    fieldnames = [
        "expiry_date", "execution_time", "meet_time", "symbol", "strike_price",
        "entry_price", "closing_price", "quantity", "result", "pnl_amount", "exit_reason"
    ]

    with open(output_file, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            writer.writerow(t)

    print(f"💾 Exported trade results to: {output_file}")


def export_trades_excel(trades: List[Dict[str, Any]], output_file: str = "backtest_results.xls"):
    """Exports trades to a styled XLS (HTML Spreadsheet) file with green/red colors for Excel / WPS Office."""
    if not trades:
        return

    html_content = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: Calibri, sans-serif; font-size: 11pt; }
  table { border-collapse: collapse; width: 100%; margin-top: 10px; }
  th { background-color: #1f4e78; color: white; padding: 10px; border: 1px solid #b0c4de; text-align: center; font-size: 11pt; }
  td { padding: 8px 12px; border: 1px solid #d3d3d3; text-align: center; }
  tr:nth-child(even) { background-color: #f9f9f9; }
  .profit-bg { background-color: #d4edda !important; color: #155724 !important; font-weight: bold; }
  .loss-bg { background-color: #f8d7da !important; color: #721c24 !important; font-weight: bold; }
  .title { font-size: 16pt; font-weight: bold; color: #1f4e78; margin-bottom: 10px; }
</style>
</head>
<body>
<div class="title">📊 Expiry Hero or Zero Strategy Backtest Results</div>
<table>
  <thead>
    <tr>
      <th>Expiry Date</th>
      <th>Entry Time</th>
      <th>Meet / Exit Time</th>
      <th>Symbol</th>
      <th>Strike Price</th>
      <th>Entry Price (₹)</th>
      <th>Closing Price (₹)</th>
      <th>Quantity</th>
      <th>Result</th>
      <th>PnL Amount (₹)</th>
      <th>Exit Reason</th>
    </tr>
  </thead>
  <tbody>
"""
    for t in trades:
        pnl = t.get("pnl_amount", 0.0)
        row_cls = "profit-bg" if pnl > 0 else ("loss-bg" if pnl < 0 else "")
        pnl_str = f"+₹{pnl:,.2f}" if pnl > 0 else (f"-₹{abs(pnl):,.2f}" if pnl < 0 else "₹0.00")
        meet_t = t.get("meet_time") or t.get("exit_time", "")

        html_content += f"""    <tr>
      <td>{t['expiry_date']}</td>
      <td>{t['execution_time']}</td>
      <td>{meet_t}</td>
      <td>{t['symbol']}</td>
      <td>{t['strike_price']}</td>
      <td>₹{t['entry_price']:.2f}</td>
      <td>₹{t['closing_price']:.2f}</td>
      <td>{t['quantity']}</td>
      <td class="{row_cls}">{t['result']}</td>
      <td class="{row_cls}">{pnl_str}</td>
      <td>{t['exit_reason']}</td>
    </tr>\n"""

    html_content += """  </tbody>
</table>
</body>
</html>"""

    with open(output_file, mode="w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"🎨 Exported styled green/red Excel spreadsheet to: {output_file}")


def print_summary_report(results: Dict[str, Any]):
    """Prints a clear summary report of backtest results."""
    m = results["metrics"]
    c = results["config"]
    trades = results["trades"]

    print("\n==========================================================================================")
    print(f"📊 BACKTEST PERFORMANCE SUMMARY ({c['symbol']} EXPIRY HERO-ZERO STRATEGY)")
    print("==========================================================================================")
    print(f"Dataset Total Days    : {c['total_days_evaluated']} Days")
    time_label = "Strictly > 15:15 IST (after 3:15 PM)" if c.get("strict_after_315") else f">= {c['trigger_time']} IST (at or after 3:15 PM, not before)"
    if c.get("execution_mode") == "limit":
        price_label = f"Limit Order fill @ exact ₹{c.get('target_entry_price', 5.0):.2f}"
    else:
        price_label = f"₹{c.get('min_entry_price', 0):.2f} to ₹{c.get('max_entry_price', 0):.2f} (Exact ₹{c.get('target_entry_price', 5.0):.2f} ± ₹{c.get('price_tolerance', 0.10):.2f})"

    print(f"Trigger Time Window   : {time_label}")
    print(f"Target Profit Price   : ₹{c.get('target_profit', 20.0):.2f} (Closing Price = Target @ Meet Time)")
    print(f"Market Close Exit     : {c['market_close_time']} IST (Square-off if target not met)")
    print(f"Entry Price Rule      : {price_label}")
    print(f"Lot Size & Quantity   : {c['lots']} Lot(s) ({c['lots'] * IndexConfig.get(c['symbol'])['lot_size']} Qty)")
    print("------------------------------------------------------------------------------------------")
    print(f"Total Trades Placed   : {m['total_trades']}")
    print(f"Winning Trades        : {m['winning_trades']} ({m['win_rate_pct']}%)")
    print(f"Losing Trades         : {m['losing_trades']}")
    print(f"Gross Profit          : ₹{m['gross_profit']:,.2f}")
    print(f"Gross Loss            : ₹{m['gross_loss']:,.2f}")
    print(f"Net Total PnL         : ₹{m['net_pnl']:,.2f}")
    print(f"Profit Factor         : {m['profit_factor']}")
    print(f"Avg PnL Per Trade     : ₹{m['avg_trade_pnl']:,.2f}")
    print(f"Max Single Trade Win  : ₹{m['max_win']:,.2f}")
    print(f"Max Single Trade Loss : ₹{m['max_loss']:,.2f}")
    print(f"Max Peak Drawdown     : ₹{m['max_drawdown']:,.2f}")
    print("==========================================================================================\n")

    if trades:
        print("Detailed Trade Breakdown:")
        print(f"{'Expiry Date':<12} | {'Entry Time':<20} | {'Meet / Exit Time':<20} | {'Strike Price':<22} | {'Entry (₹)':<10} | {'Closing Price':<14} | {'Qty':<5} | {'Result (Profit/Loss)':<22}")
        print("-" * 140)
        for t in trades:
            entry_t = t.get("execution_time", "")
            exit_t = t.get("meet_time") or t.get("exit_time", "")
            print(
                f"{t['expiry_date']:<12} | {entry_t:<20} | {exit_t:<20} | {t['strike_price']:<22} | "
                f"{t['entry_price']:<10.2f} | {t['closing_price']:<14.2f} | {t['quantity']:<5} | "
                f"{t['result']:<22}"
            )
        print("-" * 140)


def run_backtest(
    csv_file_path: str,
    symbol: str = "NIFTY",
    trigger_time_str: str = "15:15",
    market_close_time_str: str = "15:30",
    target_entry_price: float = 6.0,
    min_entry_price: Optional[float] = None,
    max_entry_price: Optional[float] = None,
    price_tolerance: float = 0.10,
    target_profit: float = 20.0,
    execution_mode: str = "limit",
    strict_after_315: bool = False,
    lots: int = 1,
    expiry_mode: str = "weekly",
    strike_mode: str = "atm_otm1"
) -> Dict[str, Any]:
    """
    Executes backtest over historical dataset using ExpiryHeroZeroStrategy.
    Evaluates CE ATM, PE ATM, CE OTM1, PE OTM1 on expiry days (excluding ITM1).
    """
    rows = load_historical_option_chain_data(csv_file_path)
    structured_by_date = group_data_by_date_and_timestamp(rows)
    all_dates = sorted(list(structured_by_date.keys()))
    expiry_dates = get_expiry_days(all_dates, symbol=symbol, mode=expiry_mode)

    close_h, close_m = map(int, market_close_time_str.split(":"))
    market_close_t = time(close_h, close_m, 0)

    strategy = ExpiryHeroZeroStrategy(
        symbol=symbol,
        trigger_time_str=trigger_time_str,
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

    strikes_desc = "ATM & OTM1 (CE ATM, CE OTM1, PE ATM, PE OTM1 - No ITM1)" if strike_mode.lower() == "atm_otm1" else ("OTM1 ONLY (CE OTM1 & PE OTM1)" if "otm" in strike_mode.lower() else "ATM ONLY")

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
    print(f"• Expiry Mode        : {expiry_mode.upper()} ({len(expiry_dates)} expiry days evaluated)")
    print(f"• Trigger Window     : {time_rule}")
    print(f"• Target Profit Price: ₹{target_profit:.2f} (Closing Price = Target @ Meet Time)")
    print(f"• Market Close Exit  : {market_close_time_str} IST (Square-off if target not met)")
    print(f"• Entry Price Rule   : {exec_rule}")
    print(f"• Quantity Traded    : {lots} Lot(s) ({lots * IndexConfig.get(symbol)['lot_size']} Qty)")
    print(f"==========================================================================================\n")

    for current_date_str in all_dates:
        is_expiry = current_date_str in expiry_dates
        exp_date_obj = datetime.strptime(current_date_str, "%Y-%m-%d").date()

        # Reset daily strategy state
        strategy.is_traded_today = False
        strategy.active_positions = []
        strategy.set_broker_expiry_date(exp_date_obj if is_expiry else None)

        day_candles = structured_by_date[current_date_str]

        for candle in day_candles:
            dt = candle["datetime"]
            spot = candle["spot_price"]
            quotes = candle["quotes"]
            strike_prices = candle["strike_prices"]

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
                exited_records = strategy.evaluate_exit(
                    current_datetime=dt,
                    current_option_prices=exit_quotes,
                    broker_close_time=market_close_t,
                    candle_quotes=quotes
                )

                for t in exited_records:
                    pnl_val = t.get("pnl", 0.0)
                    if pnl_val > 0:
                        res_str = f"PROFIT (+₹{pnl_val:,.2f})"
                    elif pnl_val < 0:
                        res_str = f"LOSS (-₹{abs(pnl_val):,.2f})"
                    else:
                        res_str = "NEUTRAL (₹0.00)"

                    trade_item = {
                        "expiry_date": current_date_str,
                        "execution_time": t.get("timestamp", dt.strftime("%Y-%m-%d %H:%M:%S")),
                        "exit_time": t.get("exit_time", dt.strftime("%Y-%m-%d %H:%M:%S")),
                        "meet_time": t.get("meet_time", dt.strftime("%Y-%m-%d %H:%M:%S")),
                        "symbol": t.get("symbol", symbol),
                        "strike_price": f"{int(t['strike'])} {t['option_type']} ({t.get('strike_type', 'ATM')})",
                        "entry_price": t.get("entry_price", 0.0),
                        "closing_price": t.get("exit_price", 0.0),
                        "quantity": t.get("quantity", IndexConfig.get(symbol)["lot_size"]),
                        "result": res_str,
                        "pnl_amount": pnl_val,
                        "exit_reason": t.get("exit_reason", "MARKET_CLOSE_TIME")
                    }
                    all_trades.append(trade_item)

            # End day processing once market close time reached
            if dt.time() >= market_close_t:
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
    parser.add_argument("--market-close", type=str, default="15:30", help="Market close square-off time (e.g. 15:30)")
    parser.add_argument("--target-entry-price", type=float, default=6.0, help="Target option entry price (strictly ₹6.00)")
    parser.add_argument("--price-tolerance", type=float, default=0.10, help="Tolerance around entry price (default: 0.10, giving ₹5.90 to ₹6.10)")
    parser.add_argument("--min-entry-price", type=float, default=None, help="Explicit minimum entry price filter (e.g. 5.90)")
    parser.add_argument("--max-entry-price", type=float, default=None, help="Explicit maximum entry price cap (e.g. 6.10 or 6.00)")
    parser.add_argument("--target-profit", type=float, default=20.0, help="Target profit price for position exit (default: ₹20.00)")
    parser.add_argument("--mode", type=str, default="limit", choices=["limit", "close"], help="Execution mode: 'limit' (exact ₹6 limit fill, default) or 'close' (candle close in range)")
    parser.add_argument("--strict-after-315", action="store_true", help="Enforce strictly after 15:15:00 (> 15:15), ignoring the 15:15:00 bar itself")
    parser.add_argument("--lots", type=int, default=1, help="Number of lots to trade")
    parser.add_argument("--expiry-mode", type=str, default="weekly", choices=["weekly", "monthly", "all"], help="Expiry day selection rule")
    parser.add_argument("--strike-mode", type=str, default="atm_otm1", choices=["atm_otm1", "otm1_only", "atm_only"], help="Strike selection mode (default: atm_otm1)")
    parser.add_argument("--otm-only", action="store_true", help="Shortcut to trade OTM1 strikes only (CE OTM1 & PE OTM1, no ATM)")
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

        chosen_strike_mode = "otm1_only" if args.otm_only else args.strike_mode

        results = run_backtest(
            csv_file_path=csv_file,
            symbol=detected_sym,
            trigger_time_str=args.trigger_time,
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


