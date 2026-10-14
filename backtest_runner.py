"""
ATM-Only Expiry Hero-Zero Strategy & Backtest Runner
=====================================================
Contains the ATM-Only Expiry Hero-Zero Strategy class and backtests it
against historical 5-minute NIFTY option chain data.
"""

import os
import sys
import csv
import logging
import argparse
from datetime import datetime, time, date, timezone, timedelta
from typing import List, Dict, Any, Optional

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("AtmOnlyExpiryBacktestRunner")

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
    """
    Expiry Hero or Zero Strategy for ATM Strikes ONLY (CE ATM and PE ATM).
    Excludes ITM1 strikes.
    """
    def __init__(
        self,
        symbol: str = "NIFTY",  # "NIFTY" or "SENSEX"
        trigger_time_str: str = "15:15",  # "15:15" or "15:19"
        market_close_time: Optional[time] = None,  # Dynamic broker market close time
        broker_expiry_date: Optional[date] = None,  # Dynamic broker nearest expiry date
        target_entry_price: float = 5.0,  # Target option entry price around ₹5
        min_entry_price: Optional[float] = None,  # Minimum entry price floor (e.g. ₹3.00)
        max_entry_price: Optional[float] = None,  # Maximum entry price cap (e.g. ₹7.00 or ₹5.00)
        price_tolerance: float = 2.0,  # Price window (target_entry_price ± tolerance)
        target_profit: Optional[float] = None,  # No target profit by default (hold till market ends)
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
        
        # Market closing time
        self.market_close_time = market_close_time or time(15, 40, 0)
        
        # Broker expiry date
        self.broker_expiry_date = broker_expiry_date
        
        # Strategy parameters
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
        
        # Risk & Targets - No SL, No Target (Hero or Zero: hold till market ends)
        self.stop_loss = None
        self.target_profit = float(target_profit) if target_profit is not None else None
        self.full_risk_mode = full_risk_mode
        
        # Daily state tracking
        self.is_traded_today = False
        self.active_positions: List[Dict[str, Any]] = []

    def set_market_close_time(self, close_time: time):
        """Updates market closing time."""
        self.market_close_time = close_time

    def set_broker_expiry_date(self, expiry_dt: date):
        """Updates active expiry date."""
        self.broker_expiry_date = expiry_dt

    def is_expiry_day(self, current_date: date, actual_expiry_date: Optional[date] = None) -> bool:
        """Validates if current date is the designated expiry date."""
        target_expiry = actual_expiry_date or self.broker_expiry_date
        if target_expiry is not None:
            return current_date == target_expiry
        return False

    def get_target_strikes(self, spot_price: float) -> List[Dict[str, Any]]:
        """
        Calculates ONLY the 2 ATM target strikes:
        1. CE ATM
        2. PE ATM
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
        option_quotes: Dict[str, Dict[str, Any]],
        actual_expiry_date: Optional[date] = None,
        broker_close_time: Optional[time] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluates strategy entry conditions for ATM Call and ATM Put.
        """
        current_date = current_datetime.date()
        current_time = current_datetime.time()
        close_time = broker_close_time or self.market_close_time or time(15, 40, 0)

        # 1. Expiry Day check
        if not self.is_expiry_day(current_date, actual_expiry_date):
            return None

        # 2. Trigger Time Window check
        if current_time < self.trigger_time or current_time >= close_time:
            return None

        # 3. Daily single trade check
        if self.is_traded_today or len(self.active_positions) > 0:
            return None

        # 4. Target Strikes (ATM ONLY)
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

            # Check entry price condition (min_entry_price <= ltp <= max_entry_price)
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
        current_option_prices: Dict[str, Any],
        broker_close_time: Optional[time] = None,
        candle_quotes: Optional[Dict[str, Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """
        Evaluates exit conditions for active positions:
        1. Target Profit Hit: Exit immediately at target_profit at meet time (if target_profit specified).
        2. Market Close: Auto square-off all positions when market ends (15:40 IST).
        """
        current_time = current_datetime.time()
        close_time = broker_close_time or self.market_close_time or time(15, 40, 0)
        exited_positions = []

        if not self.active_positions:
            return exited_positions

        remaining_positions = []
        is_market_close = current_time >= close_time

        for pos in self.active_positions:
            key = f"{pos['strike']}_{pos['option_type']}"
            quote_data = None
            if candle_quotes:
                quote_data = candle_quotes.get(key)
            if quote_data is None:
                val = current_option_prices.get(key, current_option_prices.get(pos['option_type'], pos['entry_price']))
                if isinstance(val, dict):
                    quote_data = val
                else:
                    quote_data = {"ltp": float(val), "high": float(val), "low": float(val)}

            ltp = float(quote_data.get("ltp", pos['entry_price']) or 0.0)
            high_price = float(quote_data.get("high", ltp) or ltp)

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
                    if high_price >= self.target_profit or ltp >= self.target_profit:
                        is_target_met = True
                else:
                    if ltp >= self.target_profit:
                        is_target_met = True

            meet_time_str = current_datetime.strftime("%Y-%m-%d %H:%M:%S")

            if is_target_met:
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
                logger.info(f"🎯 TARGET REACHED: {pos['symbol']} {pos['strike']} {pos['option_type']} | Entry: ₹{pos['entry_price']:.2f} | Closing Price: ₹{exit_price:.2f} | Meet Time: {meet_time_str} | PnL: +₹{round(pnl, 2):,.2f}")
            elif is_market_close:
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
                logger.info(f"⏰ MARKET CLOSE SQUAREOFF: {pos['symbol']} {pos['strike']} {pos['option_type']} | Entry: ₹{pos['entry_price']:.2f} | Exit: ₹{exit_price:.2f} | Time: {meet_time_str} | PnL: ₹{round(pnl, 2):,.2f}")
            else:
                remaining_positions.append(pos)

        self.active_positions = remaining_positions
        return exited_positions


# --- Backtesting Engine Functions ---

def load_historical_data(csv_file_path: str) -> List[Dict[str, Any]]:
    """Loads historical option chain data from CSV."""
    if not os.path.exists(csv_file_path):
        raise FileNotFoundError(f"CSV file not found at: {csv_file_path}")

    rows = []
    with open(csv_file_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    print(f"✅ Successfully loaded {len(rows)} rows from {os.path.basename(csv_file_path)}")
    return rows


def group_rows_by_date(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Groups historical rows by Date and Timestamp.
    Supports both 5-strike schema (with 'Strike Offset') and 1-strike schema (with 'ATM CE Close (LTP)').
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
            if has_strike_offset:
                offset_map = {r.get("Strike Offset", "").strip(): r for r in candle_rows}
                for r in candle_rows:
                    sp = float(r.get("Strike Price", 0.0))
                    strike_prices[(sp, "CE")] = float(r.get("CE Close (LTP)", 0.0) or 0.0)
                    strike_prices[(sp, "PE")] = float(r.get("PE Close (LTP)", 0.0) or 0.0)
                    strike_prices[(int(sp), "CE")] = float(r.get("CE Close (LTP)", 0.0) or 0.0)
                    strike_prices[(int(sp), "PE")] = float(r.get("PE Close (LTP)", 0.0) or 0.0)

                atm_row = offset_map.get("ATM")
                ce_ltp = float(atm_row.get("CE Close (LTP)", 0.0)) if atm_row else 0.0
                pe_ltp = float(atm_row.get("PE Close (LTP)", 0.0)) if atm_row else 0.0
            else:
                ce_ltp = float(first_row.get("ATM CE Close (LTP)", 0.0) or 0.0)
                pe_ltp = float(first_row.get("ATM PE Close (LTP)", 0.0) or 0.0)
                strike_prices[(atm_strike, "CE")] = ce_ltp
                strike_prices[(atm_strike, "PE")] = pe_ltp
                strike_prices[(int(atm_strike), "CE")] = ce_ltp
                strike_prices[(int(atm_strike), "PE")] = pe_ltp

            quotes = {
                f"{atm_strike}_CE": {"ltp": ce_ltp},
                f"{atm_strike}_PE": {"ltp": pe_ltp},
                f"{int(atm_strike)}_CE": {"ltp": ce_ltp},
                f"{int(atm_strike)}_PE": {"ltp": pe_ltp},
                "CE_ATM": {"ltp": ce_ltp},
                "PE_ATM": {"ltp": pe_ltp},
            }

            structured_by_date[d].append({
                "timestamp_str": ts,
                "datetime": datetime.strptime(ts, "%Y-%m-%d %H:%M:%S") if len(ts) == 19 else datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S"),
                "spot": spot,
                "atm_strike": atm_strike,
                "ce_ltp": ce_ltp,
                "pe_ltp": pe_ltp,
                "quotes": quotes,
                "strike_prices": strike_prices
            })

    return structured_by_date


def get_expiry_days(date_keys: List[str], mode: str = "weekly", symbol: str = "NIFTY") -> List[str]:
    """
    Returns dates designated as expiry days based on symbol and mode:
    - NIFTY weekly expiry: Tuesdays (weekday() == 1)
    - SENSEX weekly expiry: Thursdays (weekday() == 3)
    - 'monthly': Last expiry day of each month
    - 'all': Every trading day in dataset
    """
    sorted_dates = sorted(date_keys)
    if mode == "all":
        return sorted_dates

    # Determine weekday rule based on symbol (NIFTY -> Tuesday / 1, SENSEX -> Thursday / 3)
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


def run_backtest(
    csv_file_path: str,
    symbol: str = "NIFTY",
    trigger_time_str: str = "15:15",
    market_close_time_str: str = "15:40",
    target_entry_price: float = 5.0,
    min_entry_price: Optional[float] = None,
    max_entry_price: Optional[float] = None,
    price_tolerance: float = 2.0,
    target_profit: Optional[float] = None,
    lots: int = 1,
    expiry_mode: str = "weekly",
    strategy_type: str = "atm_only"
) -> Dict[str, Any]:
    """
    Executes backtest over historical dataset using ATM-Only Expiry Hero-Zero Strategy.
    """
    rows = load_historical_data(csv_file_path)
    by_date = group_rows_by_date(rows)
    all_dates = sorted(list(by_date.keys()))
    expiry_dates = get_expiry_days(all_dates, mode=expiry_mode, symbol=symbol)

    close_h, close_m = map(int, market_close_time_str.split(":"))
    market_close_t = time(close_h, close_m, 0)

    # Initialize ATM-Only strategy
    strategy = AtmOnlyExpiryHeroZeroStrategy(
        symbol=symbol,
        trigger_time_str=trigger_time_str,
        market_close_time=market_close_t,
        target_entry_price=target_entry_price,
        min_entry_price=min_entry_price,
        max_entry_price=max_entry_price,
        price_tolerance=price_tolerance,
        target_profit=target_profit,
        lots=lots
    )

    all_trades: List[Dict[str, Any]] = []

    print(f"\n==========================================================================================")
    print(f"🚀 RUNNING BACKTEST: {symbol} ATM-ONLY Expiry Hero-Zero Strategy")
    print(f"==========================================================================================")
    print(f"• Strategy Strategy   : ATM-ONLY (CE ATM & PE ATM ONLY)")
    print(f"• Dataset Total Days : {len(all_dates)} ({all_dates[0]} to {all_dates[-1]})")
    print(f"• Expiry Mode        : {expiry_mode.upper()} ({len(expiry_dates)} expiry days evaluated)")
    print(f"• Trigger Window     : {trigger_time_str} IST onwards")
    print(f"• Market Close Exit  : {market_close_time_str} IST")
    print(f"• Entry Price Window : ₹{strategy.min_entry_price:.2f} to ₹{strategy.max_entry_price:.2f} (Target ~₹{target_entry_price:.2f})")
    print(f"• Lot Size           : {lots} Lot(s) ({lots * IndexConfig.get(symbol)['lot_size']} Qty)")
    print(f"==========================================================================================\n")

    for current_date_str in all_dates:
        is_expiry = current_date_str in expiry_dates
        exp_date_obj = datetime.strptime(current_date_str, "%Y-%m-%d").date()

        # Reset daily strategy state
        strategy.is_traded_today = False
        strategy.active_positions = []
        strategy.set_broker_expiry_date(exp_date_obj if is_expiry else None)

        day_candles = by_date[current_date_str]

        for candle in day_candles:
            dt = candle["datetime"]
            spot = candle["spot"]
            atm_strike = candle["atm_strike"]
            quotes = candle["quotes"]
            strike_prices = candle["strike_prices"]

            # Build complete exit price mapping from all strikes in the candle
            exit_quotes: Dict[str, float] = {}
            for (sp, opt), ltp in strike_prices.items():
                exit_quotes[f"{sp}_{opt}"] = ltp
                exit_quotes[f"{int(sp)}_{opt}"] = ltp
                exit_quotes[f"{float(sp)}_{opt}"] = ltp

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

            if dt.time() >= market_close_t:
                break

            # 2. Evaluate Entry if no active trade today
            if not strategy.active_positions and not strategy.is_traded_today:
                strategy.evaluate_entry(
                    current_datetime=dt,
                    spot_price=spot,
                    option_quotes=quotes,
                    actual_expiry_date=exp_date_obj if is_expiry else None,
                    broker_close_time=market_close_t
                )

    # Calculate statistics
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
            "lots": lots,
            "expiry_mode": expiry_mode,
            "total_days_evaluated": len(all_dates),
            "expiry_days_evaluated": len(expiry_dates)
        }
    }


def calculate_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculates performance statistics from trade log."""
    total_trades = len(trades)
    if total_trades == 0:
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate_pct": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "max_win": 0.0,
            "max_loss": 0.0,
            "avg_trade_pnl": 0.0,
            "max_drawdown": 0.0
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


def export_trades_csv(trades: List[Dict[str, Any]], output_file: str = "backtest_results_atm_only.csv"):
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


def export_trades_excel(trades: List[Dict[str, Any]], output_file: str = "backtest_results_atm_only.xls"):
    """Exports trades to a styled XLS (HTML Spreadsheet) file with green/red colors for WPS Office & Excel."""
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
  .profit-text { color: #28a745; font-weight: bold; }
  .loss-text { color: #dc3545; font-weight: bold; }
  .title { font-size: 16pt; font-weight: bold; color: #1f4e78; margin-bottom: 10px; }
</style>
</head>
<body>
<div class="title">📊 ATM-Only Expiry Strategy Backtest Results</div>
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
    print("📊 BACKTEST PERFORMANCE SUMMARY (ATM-ONLY EXPIRY STRATEGY)")
    print("==========================================================================================")
    print(f"Total Evaluated Days  : {c['total_days_evaluated']} Days")
    print(f"Expiry Days Filtered  : {c['expiry_days_evaluated']} Days ({c['expiry_mode'].upper()})")
    print(f"Trigger Time          : {c['trigger_time']} IST")
    target_prof = c.get('target_profit')
    target_prof_str = f"₹{target_prof:.2f} (Closing Price = Target @ Meet Time)" if target_prof is not None else "None (No Target Price - Full Risk Hero or Zero)"
    print(f"Target Profit Price   : {target_prof_str}")
    print(f"Market Close Exit     : {c.get('market_close_time', '15:40')} IST (Square-off all positions when market ends)")
    print(f"Entry Price Window    : ₹{c.get('min_entry_price', 0):.2f} to ₹{c.get('max_entry_price', 0):.2f} (Target ~₹{c.get('target_entry_price', 5.0):.2f})")
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest ATM-Only Expiry Hero-Zero Strategy on Option Chain CSV")
    default_csv = "NIFTY_5m_csv.csv" if os.path.exists("NIFTY_5m_csv.csv") else "Historical_Option_Chain_NIFTY_5m_2026-08-01_to_2026-09-29 (5).csv"
    parser.add_argument("--csv", type=str, default=default_csv, help="Path to historical option CSV file")
    parser.add_argument("--symbol", type=str, default="NIFTY", help="Symbol name (NIFTY / SENSEX)")
    parser.add_argument("--trigger-time", type=str, default="15:15", help="Entry trigger time IST (e.g. 15:15 or 15:19)")
    parser.add_argument("--market-close", type=str, default="15:40", help="Market close square-off time (e.g. 15:40)")
    parser.add_argument("--target-entry-price", type=float, default=5.0, help="Target option entry price around ₹5 (default: 5.0)")
    parser.add_argument("--price-tolerance", type=float, default=2.0, help="Price window tolerance around target price (default: 2.0 -> ₹3 to ₹7)")
    parser.add_argument("--min-entry-price", type=float, default=None, help="Explicit minimum entry price filter (e.g. 3.0 or 3.5)")
    parser.add_argument("--max-entry-price", type=float, default=None, help="Explicit maximum entry price cap (e.g. 7.0 or 5.0)")
    parser.add_argument("--target-profit", type=float, default=None, help="Target profit price for exit (default: None - hold till market ends)")
    parser.add_argument("--lots", type=int, default=1, help="Number of lots to trade")
    parser.add_argument("--expiry-mode", type=str, default="weekly", choices=["weekly", "monthly", "all"], help="Expiry day selection rule")
    parser.add_argument("--export-csv", type=str, default="backtest_results_atm_only.csv", help="Output file path for trade export")

    args = parser.parse_args()

    results = run_backtest(
        csv_file_path=args.csv,
        symbol=args.symbol,
        trigger_time_str=args.trigger_time,
        market_close_time_str=args.market_close,
        target_entry_price=args.target_entry_price,
        min_entry_price=args.min_entry_price,
        max_entry_price=args.max_entry_price,
        price_tolerance=args.price_tolerance,
        target_profit=args.target_profit,
        lots=args.lots,
        expiry_mode=args.expiry_mode
    )

    print_summary_report(results)
    if results["trades"]:
        export_trades_csv(results["trades"], output_file=args.export_csv)
        xls_path = args.export_csv.replace(".csv", ".xls")
        export_trades_excel(results["trades"], output_file=xls_path)

