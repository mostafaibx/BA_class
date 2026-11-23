#!/usr/bin/env python3
"""
generate_data.py
================
Generates a *raw*, source-system-style dummy dataset for a fictional
B2B/B2C distribution company, following the order-to-invoice process:

    Order intake (API/ERP) -> validation -> inventory check & reservation
    -> warehouse picking (WMS) -> packing & QC (with rework loop)
    -> carrier handoff & delivery -> invoice & ledger posting

The output deliberately is NOT a star schema. Each table mimics an export
from one operational system (ERP, WMS, carrier feed, finance) so that the
dimensional model can be designed and built on top of it (e.g. in Power BI).

Outputs (folder ./raw):
    * one CSV per table (UTF-8 with BOM, comma separated, '.' decimal)
    * ba_raw.sqlite  - the same tables in a single SQLite database

Run:
    python generate_data.py

See README.md (how the script works) and DATA_DICTIONARY.md (the tables).
"""
from __future__ import annotations

import heapq
import math
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# =============================================================================
# 1. CONFIGURATION  - change these values to reshape the dataset
# =============================================================================
SEED = 42                                   # same seed => identical dataset
START_DATE = date(2025, 11, 1)              # first order date (inclusive)
END_DATE = date(2026, 10, 31)               # last order date (inclusive)
EXTRACT_TS = datetime(2026, 11, 1, 0, 0)    # moment the systems were "exported"

TARGET_ORDERS = 25_000                      # expected number of orders
N_CUSTOMERS = 400
N_PRODUCTS = 80                             # must be divisible by #categories
N_PACK_STATIONS = 2

OUTPUT_DIR = Path(__file__).resolve().parent / "raw"
SQLITE_FILE = "ba_raw.sqlite"

# --- Demand shape --------------------------------------------------------------
HOUR_WEIGHTS = np.array([0.15, 0.10, 0.08, 0.08, 0.10, 0.20,    # 00-05
                         0.45, 0.80, 1.20, 1.45, 1.50, 1.40,    # 06-11
                         1.20, 1.25, 1.35, 1.30, 1.15, 1.00,    # 12-17
                         0.90, 0.85, 0.80, 0.65, 0.45, 0.25])   # 18-23
WEEKDAY_WEIGHTS = np.array([1.15, 1.10, 1.05, 1.00, 0.95, 0.55, 0.40])  # Mon..Sun
MONTH_WEIGHTS = np.array([0.85, 0.90, 1.00, 1.00, 1.00, 0.95,           # Jan..Jun
                          0.90, 0.90, 1.00, 1.05, 1.30, 1.40])          # Jul..Dec
ANNUAL_GROWTH = 0.08                        # +8 % volume from start to end

# --- Order intake / validation -----------------------------------------------------
P_VALIDATION_FAIL = {"GOOD": 0.012, "WATCH": 0.07, "HOLD": 0.65}
P_MANUAL_REVIEW = {"GOOD": 0.02, "WATCH": 0.40, "HOLD": 1.00}   # credit desk review
P_EXPRESS = 0.12
PROMISE_CUTOFF = time(14, 0)                # orders before cutoff ship same day
PROMISE_SHIP_BY = time(19, 0)

# --- Inventory -----------------------------------------------------------------------
SAFETY_FACTOR_RANGE = (1.1, 1.6)            # reorder point = demand * lead * factor
P_UNRELIABLE_SUPPLIER = 0.15                # SKUs with volatile lead times

# --- Warehouse ----------------------------------------------------------------------
WAREHOUSE_DAYS = {0, 1, 2, 3, 4, 5}         # Mon-Sat
WAREHOUSE_OPEN, WAREHOUSE_CLOSE = time(6, 0), time(22, 0)
SHIFTS = {"EARLY": (time(6, 0), time(14, 0)), "LATE": (time(14, 0), time(22, 0))}
P_PICKER_ABSENT = 0.06
WAVE_MINUTES = 15                           # WMS releases pick waves every 15 min
BULK_UNITS_THRESHOLD = 40                   # orders with >= units go to BULK queue
P_QC_DETECTS_PICK_ERROR = 0.92
P_PACKAGING_DEFECT = 0.015
P_RECHECK_FAIL = 0.08

# --- Delivery ------------------------------------------------------------------------
P_DELIVERY_EXCEPTION = 0.002                # lost / damaged in transit

# --- Finance -------------------------------------------------------------------------
VAT_RATE_DOMESTIC = 0.19                    # DE customers; EU B2B = reverse charge 0 %
P_INVOICE_MANUAL_DELAY = 0.02
P_LEDGER_POST_ERROR = 0.01
LEDGER_BATCH_TIME = time(23, 30)
PICKER_HOURLY_RATE = {"Permanent": 21.50, "Temporary": 24.00}
PACKER_HOURLY_RATE = 19.00
MONTHLY_RENT = 18_500.00
MONTHLY_UTILITIES = 3_200.00

# --- Raw-data imperfections ("light" mess) -------------------------------------------
INJECT_DIRTY_DATA = True
DIRTY = {
    "duplicate_order_rate": 0.004,          # API re-sends -> duplicate order rows
    "channel_variant_rate": 0.03,           # 'web', 'Web ', ...
    "carrier_name_variant_rate": 0.05,      # 'swiftparcel', 'SWIFTPARCEL ', ...
    "missing_city_rate": 0.02,              # NULL city in customer master
    "product_name_whitespace_rate": 0.05,   # trailing blanks in product names
}

# =============================================================================
# 2. REFERENCE DATA
# =============================================================================
COUNTRIES = {
    "DE": dict(share=0.55, cities=["Berlin", "Hamburg", "Munich", "Cologne", "Frankfurt",
                                   "Stuttgart", "Leipzig"], suffixes=["GmbH", "AG", "KG"]),
    "NL": dict(share=0.15, cities=["Amsterdam", "Rotterdam", "Utrecht", "Eindhoven"],
               suffixes=["B.V."]),
    "AT": dict(share=0.12, cities=["Vienna", "Graz", "Linz"], suffixes=["GmbH"]),
    "FR": dict(share=0.10, cities=["Paris", "Lyon", "Lille", "Strasbourg"],
               suffixes=["SARL", "SAS"]),
    "BE": dict(share=0.08, cities=["Brussels", "Antwerp", "Ghent"], suffixes=["BV", "SA"]),
}
NAME_PREFIX = ["Nord", "Alpen", "Rhein", "Euro", "Blue", "Prime", "Urban", "Delta", "Smart",
               "Green", "Metro", "Sun", "Kraft", "Linden", "Hansa", "Atlas", "Nova", "Vista",
               "Polar", "Orbit"]
NAME_CORE = ["Trade", "Supply", "Retail", "Home", "Tech", "Market", "Store", "Goods",
             "Office", "Living", "Sports", "Line"]

SEGMENTS = {
    # share of customers, discount, relative order frequency, extra lines (Poisson mean),
    # mean units per line, credit limit range, channel mix
    "Key Account": dict(share=0.06, discount=0.12, weight=25, lines=2.0, qty=8,
                        credit=(50_000, 200_000), channels={"EDI": 0.8, "WEB": 0.2}),
    "Wholesale": dict(share=0.14, discount=0.08, weight=6, lines=1.5, qty=6,
                      credit=(20_000, 80_000),
                      channels={"EDI": 0.5, "WEB": 0.4, "MOBILE_APP": 0.1}),
    "Retail": dict(share=0.40, discount=0.04, weight=2, lines=1.0, qty=3,
                   credit=(5_000, 25_000),
                   channels={"WEB": 0.5, "MARKETPLACE": 0.3, "EDI": 0.1, "MOBILE_APP": 0.1}),
    "SME": dict(share=0.40, discount=0.00, weight=1, lines=0.6, qty=2,
                credit=(1_000, 10_000),
                channels={"WEB": 0.6, "MARKETPLACE": 0.25, "MOBILE_APP": 0.15}),
}

CATEGORIES = {
    "Electronics": dict(zone="A", price=(19, 349), kg=(0.1, 2.5),
                        items=["Wireless Headphones", "Bluetooth Speaker", "USB-C Hub",
                               "Power Bank", "Smart Plug", "HD Webcam", "Mechanical Keyboard",
                               "Wireless Mouse", "Monitor Arm", "Charging Cable"]),
    "Home & Kitchen": dict(zone="B", price=(9, 129), kg=(0.3, 4.0),
                           items=["Coffee Grinder", "Glass Storage Set", "Chef Knife",
                                  "Table Lamp", "Cotton Towel Set", "Non-Stick Pan",
                                  "Water Filter Jug", "Spice Rack"]),
    "Office Supplies": dict(zone="C", price=(2, 59), kg=(0.05, 3.0),
                            items=["A4 Paper Box", "Gel Pen Pack", "Desk Organizer",
                                   "Ring Binder", "Sticky Notes", "Label Printer Tape",
                                   "Stapler", "Whiteboard Markers"]),
    "Sports & Outdoor": dict(zone="D", price=(12, 199), kg=(0.2, 6.0),
                             items=["Yoga Mat", "Water Bottle", "Resistance Bands",
                                    "Hiking Backpack", "Camping Lantern", "Dumbbell Set",
                                    "Cycling Gloves", "Running Belt"]),
    "Toys & Games": dict(zone="E", price=(6, 79), kg=(0.1, 2.0),
                         items=["Building Blocks", "Puzzle 1000pc", "Board Game", "Plush Bear",
                                "RC Car", "Card Game", "Craft Kit", "Wooden Train"]),
}
VARIANTS = ["Classic", "Pro", "Lite", "Plus", "Mini", "Max", "Eco", "X2"]
PRICE_CHANGE_DATE = date(2026, 4, 1)
PRICE_CHANGE = {"Electronics": 0.04, "Home & Kitchen": 0.05}   # list-price increases

PICKERS = [
    # name, shift, employment type, hire date, termination date
    ("Lukas Becker", "EARLY", "Permanent", date(2019, 3, 1), None),
    ("Anna Kowalski", "EARLY", "Permanent", date(2020, 6, 15), None),
    ("Mehmet Yilmaz", "EARLY", "Permanent", date(2018, 9, 1), None),
    ("Julia Wagner", "EARLY", "Permanent", date(2022, 2, 1), None),
    ("Sara Haddad", "LATE", "Permanent", date(2021, 1, 11), None),
    ("Jonas Fischer", "LATE", "Permanent", date(2017, 7, 1), None),
    ("Fatima Demir", "LATE", "Permanent", date(2022, 10, 3), None),
    ("Tobias Schulz", "LATE", "Permanent", date(2024, 4, 2), None),
    ("Kevin Krüger", "EARLY", "Temporary", date(2025, 11, 3), date(2026, 1, 16)),
    ("Marta Weber", "LATE", "Temporary", date(2025, 11, 3), date(2026, 1, 16)),
]
PROBLEM_PICKER = 7      # index of a permanent picker with an elevated error rate

CARRIERS = {
    "SWP": dict(name="SwiftParcel", pickups=[time(10), time(14), time(18), time(21)],
                sat=[time(12), time(18)], transit=1, p_delay=0.08, base_cost=4.90, per_kg=0.60),
    "EFR": dict(name="EuroFreight Logistics", pickups=[time(13), time(20)], sat=[time(13)],
                transit=2, p_delay=0.12, base_cost=6.50, per_kg=0.35),
    "PNW": dict(name="ParcelNow Express", pickups=[time(9), time(12), time(15), time(18), time(21)],
                sat=[time(11), time(17)], transit=1, p_delay=0.05, base_cost=7.90, per_kg=0.70),
    "TLX": dict(name="TransLogix", pickups=[time(11), time(16), time(20, 30)], sat=[time(14)],
                transit=1, p_delay=0.20, base_cost=3.90, per_kg=0.50),
}
CARRIER_MIX_STANDARD = {"SWP": 0.45, "TLX": 0.35, "EFR": 0.10, "PNW": 0.10}
CARRIER_MIX_EXPRESS = {"PNW": 0.80, "SWP": 0.20}
CARRIER_MIX_BULK = {"EFR": 0.70, "SWP": 0.30}

# =============================================================================
# 3. HELPERS
# =============================================================================
rng = np.random.default_rng(SEED)


def lognorm(median: float, sigma: float = 0.5) -> float:
    """Right-skewed positive value with the given median (durations, waits)."""
    return median * math.exp(sigma * rng.standard_normal())


def choose(weights: dict):
    """Pick one key of a {value: weight} dict."""
    keys = list(weights)
    p = np.array(list(weights.values()), dtype=float)
    return keys[rng.choice(len(keys), p=p / p.sum())]


def mins(m: float) -> timedelta:
    return timedelta(minutes=float(m))


def at(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def next_midnight(t: datetime) -> datetime:
    return at(t.date() + timedelta(days=1), time(0))


def next_window(t: datetime, days: set, start: time, end: time) -> datetime:
    """Earliest moment >= t that lies inside the daily window [start, end) on `days`."""
    for _ in range(14):
        d = t.date()
        if d.weekday() in days:
            if t < at(d, start):
                return at(d, start)
            if t < at(d, end):
                return t
        t = next_midnight(t)
    return t


def next_open(t: datetime) -> datetime:
    return next_window(t, WAREHOUSE_DAYS, WAREHOUSE_OPEN, WAREHOUSE_CLOSE)


def next_office(t: datetime) -> datetime:
    return next_window(t, {0, 1, 2, 3, 4}, time(8), time(18))


def ceil_to_wave(t: datetime) -> datetime:
    base = t.replace(second=0, microsecond=0)
    extra = (-base.minute) % WAVE_MINUTES
    if extra == 0 and t > base:
        extra = WAVE_MINUTES
    return base + mins(extra)


def add_business_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def next_business_day(d: date) -> date:
    return add_business_days(d, 1)


def hex_id(n_bytes: int = 16) -> str:
    return rng.bytes(n_bytes).hex()


def cut(ts):
    """Events after the extract moment do not exist yet in the source systems."""
    return ts if ts is not None and ts <= EXTRACT_TS else None


# =============================================================================
# 4. MASTER DATA
# =============================================================================
def build_customers():
    seg_names = list(SEGMENTS)
    seg_p = np.array([SEGMENTS[s]["share"] for s in seg_names])
    segs = rng.choice(seg_names, N_CUSTOMERS, p=seg_p / seg_p.sum())
    used, rows, hidden = set(), [], []
    history_start = date(2019, 1, 1)
    for i in range(N_CUSTOMERS):
        seg = str(segs[i])
        cfg = SEGMENTS[seg]
        cc = choose({k: v["share"] for k, v in COUNTRIES.items()})
        city = str(rng.choice(COUNTRIES[cc]["cities"]))
        while True:
            name = f"{rng.choice(NAME_PREFIX)} {rng.choice(NAME_CORE)}"
            if rng.random() < 0.4:
                name += f" {city}"
            name += f" {rng.choice(COUNTRIES[cc]['suffixes'])}"
            if name not in used:
                used.add(name)
                break
        if rng.random() < 0.12:     # onboarded during the data period
            created = START_DATE + timedelta(days=int(rng.integers(0, 330)))
        else:
            created = history_start + timedelta(
                days=int(rng.integers(0, (START_DATE - history_start).days)))
        created_at = at(created, time(int(rng.integers(8, 18)), int(rng.integers(0, 60))))

        # credit status history (a few customers change status during the year)
        status = choose({"GOOD": 0.86, "WATCH": 0.10, "HOLD": 0.04})
        history = [(date.min, status)]
        modified_at = created_at
        if rng.random() < 0.08:
            change = max(created, START_DATE) + timedelta(days=int(rng.integers(20, 300)))
            new_status = choose({s: 1 for s in ["GOOD", "WATCH", "HOLD"] if s != status})
            history.append((change, new_status))
            modified_at = at(change, time(int(rng.integers(8, 18)), int(rng.integers(0, 60))))

        weight = cfg["weight"] * lognorm(1.0, 0.6) * (0.4 if status == "HOLD" else 1.0)
        rows.append(dict(
            customer_id=f"C{i + 1:05d}", customer_name=name, segment=seg, country_code=cc,
            city=city, credit_limit_eur=float(round(rng.uniform(*cfg["credit"]), -2)),
            credit_status=history[-1][1], created_at=created_at, last_modified_at=modified_at))
        hidden.append(dict(weight=weight, history=history, created=created, segment=seg,
                           country=cc))
    return pd.DataFrame(rows), hidden


def credit_status_on(cust_hidden: dict, d: date) -> str:
    status = cust_hidden["history"][0][1]
    for eff, st in cust_hidden["history"]:
        if d >= eff:
            status = st
    return status


def build_products():
    per_cat = N_PRODUCTS // len(CATEGORIES)
    rows, hidden, price_hist = [], [], []
    n = 0
    popularity = 1.0 / np.arange(1, N_PRODUCTS + 1) ** 0.9     # Zipf-like popularity
    rng.shuffle(popularity)
    for cat, cfg in CATEGORIES.items():
        combos = [(it, v) for it in cfg["items"] for v in VARIANTS]
        picks = rng.choice(len(combos), per_cat, replace=False)
        for k in picks:
            item, variant = combos[k]
            price0 = round(float(np.exp(rng.uniform(*np.log(cfg["price"])))), 2)
            cost = round(price0 * rng.uniform(0.45, 0.70), 2)
            sku = f"SKU-{10001 + n}"
            bay = f"{cfg['zone']}-{int(rng.integers(1, 13)):02d}-{int(rng.integers(1, 5))}"
            inc = PRICE_CHANGE.get(cat)
            price_now = round(price0 * (1 + inc), 2) if inc else price0
            rows.append(dict(sku=sku, product_name=f"{item} {variant}", category=cat,
                             list_price_eur=price_now, unit_cost_eur=cost,
                             weight_kg=round(rng.uniform(*cfg["kg"]), 2), storage_bay=bay,
                             is_active="Y", created_at=at(date(2024, 1, 2), time(9))))
            if inc:
                price_hist.append(dict(sku=sku, list_price_eur=price0,
                                       valid_from=date(2024, 1, 1),
                                       valid_to=PRICE_CHANGE_DATE - timedelta(days=1)))
                price_hist.append(dict(sku=sku, list_price_eur=price_now,
                                       valid_from=PRICE_CHANGE_DATE, valid_to=None))
            else:
                price_hist.append(dict(sku=sku, list_price_eur=price0,
                                       valid_from=date(2024, 1, 1), valid_to=None))
            unreliable = rng.random() < P_UNRELIABLE_SUPPLIER
            hidden.append(dict(sku=sku, price0=price0, price_now=price_now,
                               popularity=popularity[n], category=cat,
                               kg=rows[-1]["weight_kg"], bay=bay,
                               lead_median=float(rng.uniform(3, 9)),
                               lead_sigma=0.6 if unreliable else 0.2,
                               safety=1.0 if unreliable else rng.uniform(*SAFETY_FACTOR_RANGE)))
            n += 1
    return pd.DataFrame(rows), hidden, pd.DataFrame(price_hist)


def build_pickers():
    rows, hidden = [], []
    for i, (name, shift, emp, hired, term) in enumerate(PICKERS):
        rows.append(dict(picker_id=f"PK{i + 1:03d}", picker_name=name, shift=shift,
                         employment_type=emp, hire_date=hired, termination_date=term))
        if emp == "Temporary":
            err, speed = 0.030, 1.20
        else:
            err, speed = lognorm(0.010, 0.3), lognorm(1.0, 0.12)
        if i == PROBLEM_PICKER:
            err = 0.045
        hidden.append(dict(error_rate=err, speed=speed, shift=shift, hired=hired, term=term,
                           emp=emp))
    absent = set()
    d = START_DATE
    while d <= END_DATE + timedelta(days=14):
        for i, h in enumerate(hidden):
            employed = h["hired"] <= d and (h["term"] is None or d <= h["term"])
            if not employed or rng.random() < P_PICKER_ABSENT:
                absent.add((i, d))
        d += timedelta(days=1)
    return pd.DataFrame(rows), hidden, absent


def build_carriers():
    return pd.DataFrame([
        dict(carrier_code=c, carrier_name=v["name"],
             pickup_times_weekday=", ".join(t.strftime("%H:%M") for t in v["pickups"]),
             pickup_times_saturday=", ".join(t.strftime("%H:%M") for t in v["sat"]) or None,
             contracted_transit_days=v["transit"], base_rate_eur=v["base_cost"],
             rate_per_kg_eur=v["per_kg"])
        for c, v in CARRIERS.items()])


# =============================================================================
# 5. ORDER INTAKE
# =============================================================================
def generate_arrivals() -> list[datetime]:
    """Non-homogeneous Poisson arrivals: hour-of-day x weekday x month x growth."""
    days = pd.date_range(START_DATE, END_DATE, freq="D")
    trend = 1 + ANNUAL_GROWTH * np.arange(len(days)) / len(days)
    day_w = WEEKDAY_WEIGHTS[days.day_of_week] * MONTH_WEIGHTS[days.month - 1] * trend
    intensity = day_w[:, None] * HOUR_WEIGHTS[None, :]
    counts = rng.poisson(intensity * TARGET_ORDERS / intensity.sum())
    flat = np.repeat(np.arange(counts.size), counts.ravel())
    secs = (flat % 24) * 3600 + rng.integers(0, 3600, flat.size)
    stamps = days.values[flat // 24] + secs.astype("timedelta64[s]")
    return sorted(pd.to_datetime(stamps).to_pydatetime())


def generate_orders(cust_hidden, prod_hidden):
    arrivals = generate_arrivals()
    n = len(arrivals)
    w = np.array([c["weight"] for c in cust_hidden])
    w = w / w.sum()
    created = np.array([np.datetime64(c["created"]) for c in cust_hidden])
    odates = np.array([np.datetime64(a.date()) for a in arrivals])
    cust = rng.choice(N_CUSTOMERS, n, p=w)
    for _ in range(100):                      # customers can't order before onboarding
        bad = created[cust] > odates
        if not bad.any():
            break
        cust[bad] = rng.choice(N_CUSTOMERS, bad.sum(), p=w)

    pop = np.array([p["popularity"] for p in prod_hidden])
    pop = pop / pop.sum()
    orders, lines = [], []
    for k, (received, ci) in enumerate(zip(arrivals, cust)):
        ch = cust_hidden[ci]
        cfg = SEGMENTS[ch["segment"]]
        n_lines = min(10, 1 + rng.poisson(cfg["lines"]))
        skus = rng.choice(N_PRODUCTS, n_lines, replace=False, p=pop)
        o = dict(order_id=f"SO{k + 1:07d}", cust_idx=int(ci), received_ts=received,
                 channel=choose(cfg["channels"]),
                 priority="EXPRESS" if rng.random() < P_EXPRESS else "STANDARD",
                 lines=[])
        for ln_no, si in enumerate(skus, start=1):
            ph = prod_hidden[si]
            price = ph["price_now"] if received.date() >= PRICE_CHANGE_DATE else ph["price0"]
            qty = int(min(60, rng.geometric(1 / cfg["qty"])))
            line = dict(order_id=o["order_id"], line_no=ln_no, sku_idx=int(si), qty=qty,
                        unit_price=price, discount=cfg["discount"])
            o["lines"].append(line)
            lines.append(line)
        orders.append(o)
    return orders, lines


def validate_orders(orders, cust_hidden):
    reasons = {"INVALID_ADDRESS": 0.40, "MISSING_PO_NUMBER": 0.25,
               "CREDIT_LIMIT_EXCEEDED": 0.20, "PRICE_MISMATCH": 0.15}
    for o in orders:
        ch = cust_hidden[o["cust_idx"]]
        rcv = o["received_ts"]
        status = credit_status_on(ch, rcv.date())
        o["credit_status"] = status
        if rng.random() < P_MANUAL_REVIEW[status]:
            o["validated_ts"] = next_office(rcv + mins(lognorm(5, 0.5))) + mins(lognorm(40, 0.6))
        else:
            o["validated_ts"] = rcv + timedelta(seconds=lognorm(45, 0.5))
        failed = rng.random() < P_VALIDATION_FAIL[status]
        o["validation_result"] = "FAILED" if failed else "PASSED"
        o["validation_reason"] = (("CREDIT_HOLD" if status == "HOLD" else choose(reasons))
                                  if failed else None)
        o["notified_ts"] = o["validated_ts"] + mins(rng.uniform(1, 10)) if failed else None
        # dispatch promise (ERP rule: before 14:00 on a weekday -> ship same day)
        d = rcv.date()
        base = d if (d.weekday() < 5 and rcv.time() < PROMISE_CUTOFF) else next_business_day(d)
        o["promise_base"] = base
        o["promised_ship_by_ts"] = at(base, PROMISE_SHIP_BY)


# =============================================================================
# 6. INVENTORY (ERP): reservation, backorders, replenishment, daily snapshots
# =============================================================================
def simulate_inventory(orders, lines, prod_hidden):
    n_days = (END_DATE - START_DATE).days + 1
    demand = np.zeros(N_PRODUCTS)
    for ln in lines:
        demand[ln["sku_idx"]] += ln["qty"]
    demand = np.maximum(demand / n_days, 0.2)

    rop = [math.ceil(demand[i] * p["lead_median"] * p["safety"]) for i, p in enumerate(prod_hidden)]
    roq = [math.ceil(demand[i] * rng.uniform(10, 25)) for i in range(N_PRODUCTS)]
    available = [rop[i] + roq[i] for i in range(N_PRODUCTS)]
    on_order = [0] * N_PRODUCTS
    incoming: list = []                      # heap of (arrival_ts, sku_idx, qty)
    reservations, backorders, snapshots = [], [], []

    def receive_until(t):
        while incoming and incoming[0][0] <= t:
            _, si, q = heapq.heappop(incoming)
            available[si] += q
            on_order[si] -= q

    def maybe_reorder(si, t):
        if on_order[si] == 0 and available[si] <= rop[si]:
            ph = prod_hidden[si]
            lead = max(1, round(lognorm(ph["lead_median"], ph["lead_sigma"])))
            arrive_d = t.date() + timedelta(days=lead)
            if arrive_d.weekday() == 6:
                arrive_d += timedelta(days=1)
            heapq.heappush(incoming, (at(arrive_d, time(8)), si, roq[si]))
            on_order[si] += roq[si]

    def close_day(d):
        receive_until(at(d, time(23, 59, 59)))
        for si in range(N_PRODUCTS):
            snapshots.append(dict(snapshot_date=d, sku_idx=si, available_qty=available[si],
                                  on_order_qty=on_order[si], reorder_point=rop[si]))

    cur = START_DATE
    for o in sorted((o for o in orders if o["validation_result"] == "PASSED"),
                    key=lambda o: o["validated_ts"]):
        t = o["validated_ts"]
        while cur < t.date() and cur <= END_DATE:
            close_day(cur)
            cur += timedelta(days=1)
        receive_until(t)
        short = [ln for ln in o["lines"] if ln["qty"] > available[ln["sku_idx"]]]
        if short:
            o["outcome"] = "BACKORDERED"
            o["backorder_ts"] = t + timedelta(seconds=rng.uniform(20, 120))
            for ln in short:
                si = ln["sku_idx"]
                nxt = min((a for a, s, _ in incoming if s == si), default=None)
                backorders.append(dict(
                    order_id=o["order_id"], line_no=ln["line_no"], sku_idx=si,
                    ordered_qty=ln["qty"], available_qty=available[si],
                    backorder_qty=ln["qty"] - available[si],
                    backorder_reason="OUT_OF_STOCK" if available[si] == 0 else "INSUFFICIENT_STOCK",
                    expected_restock_date=nxt.date() if nxt else None,
                    backorder_ts=o["backorder_ts"],
                    notice_sent_ts=o["backorder_ts"] + mins(rng.uniform(1, 15))))
                maybe_reorder(si, t)
        else:
            o["outcome"] = "RESERVED"
            o["reserved_ts"] = t + timedelta(seconds=rng.uniform(5, 90))
            for ln in o["lines"]:
                si = ln["sku_idx"]
                reservations.append(dict(
                    order_id=o["order_id"], line_no=ln["line_no"], sku_idx=si,
                    available_qty_before=available[si], reserved_qty=ln["qty"],
                    reservation_ts=o["reserved_ts"]))
                available[si] -= ln["qty"]
                maybe_reorder(si, t)
    while cur <= END_DATE:
        close_day(cur)
        cur += timedelta(days=1)
    return reservations, backorders, snapshots


# =============================================================================
# 7. WAREHOUSE (WMS), DISPATCH (carrier) and FINANCE (invoice/ledger)
# =============================================================================
def simulate_fulfilment(orders, prod_hidden, cust_hidden, pick_hidden, absent):
    n_pickers = len(pick_hidden)
    picker_free = [datetime.min] * n_pickers
    station_free = [datetime.min] * N_PACK_STATIONS
    pick_rows, qc_rows, ship_rows, inv_rows = [], [], [], []

    def picker_ready(p, t):
        s, e = SHIFTS[pick_hidden[p]["shift"]]
        for _ in range(14):
            d = t.date()
            if d.weekday() in WAREHOUSE_DAYS and (p, d) not in absent:
                if t < at(d, s):
                    return at(d, s)
                if t < at(d, e) - mins(10):
                    return t
            t = next_midnight(t)
        return t

    for o in sorted((o for o in orders if o.get("outcome") == "RESERVED"),
                    key=lambda o: o["reserved_ts"]):
        units = sum(ln["qty"] for ln in o["lines"])
        queue = ("EXPRESS" if o["priority"] == "EXPRESS"
                 else "BULK" if units >= BULK_UNITS_THRESHOLD else "STANDARD")
        released = next_open(ceil_to_wave(next_open(o["reserved_ts"])))

        # --- assign the picker who can start first (random tie-break) -------------------
        cands = [(picker_ready(p, max(released, picker_free[p])), rng.random(), p)
                 for p in range(n_pickers)]
        assigned, _, p = min(cands)
        ph = pick_hidden[p]

        # --- pick each line ---------------------------------------------------------------
        t = assigned + mins(rng.uniform(0.5, 3))
        errors = []
        for ln in o["lines"]:
            si = ln["sku_idx"]
            start = t
            t = t + mins((lognorm(2.2, 0.4) + 0.25 * ln["qty"]) * ph["speed"])
            picked_sku, picked_qty = si, ln["qty"]
            err_p = ph["error_rate"] * (1.4 if start.hour >= 20 else 1.0)
            if rng.random() < err_p:
                kind = choose({"QTY_SHORT": 0.45, "QTY_OVER": 0.20, "WRONG_ITEM": 0.35})
                errors.append(kind)
                if kind == "QTY_SHORT":
                    picked_qty = max(0, ln["qty"] - int(rng.integers(1, 3)))
                elif kind == "QTY_OVER":
                    picked_qty = ln["qty"] + 1
                else:
                    same_cat = [j for j, x in enumerate(prod_hidden)
                                if x["category"] == prod_hidden[si]["category"] and j != si]
                    picked_sku = int(rng.choice(same_cat))
            pick_rows.append(dict(order_id=o["order_id"], line_no=ln["line_no"],
                                  queue_name=queue, picker_idx=p, released_ts=released,
                                  assigned_ts=assigned, pick_start_ts=start, pick_end_ts=t,
                                  storage_bay=prod_hidden[si]["bay"], ordered_sku_idx=si,
                                  picked_sku_idx=picked_sku, ordered_qty=ln["qty"],
                                  picked_qty=picked_qty))
        pick_end = t
        picker_free[p] = pick_end + mins(rng.uniform(1, 3))

        # --- pack & QC with rework loop -----------------------------------------------------
        s = min(range(N_PACK_STATIONS), key=lambda i: station_free[i])
        qc_start = next_open(max(pick_end + mins(rng.uniform(2, 6)), station_free[s]))
        detected = bool(errors) and rng.random() < P_QC_DETECTS_PICK_ERROR
        pack_defect = rng.random() < P_PACKAGING_DEFECT
        attempt = 1
        while True:
            qc_end = qc_start + mins(lognorm(5 + 0.7 * len(o["lines"]) + 0.04 * units, 0.35))
            if attempt == 1:
                failed = detected or pack_defect
                defect = (errors[0] if detected else
                          choose({"PACKAGING_DAMAGE": 0.7, "LABEL_MISMATCH": 0.3})
                          if failed else None)
            else:
                failed = attempt < 3 and rng.random() < P_RECHECK_FAIL
                defect = choose({"PACKAGING_DAMAGE": 0.6, "LABEL_MISMATCH": 0.4}) if failed else None
            rework = round(lognorm(18 if defect == "WRONG_ITEM" else 10, 0.5), 1) if failed else None
            qc_rows.append(dict(order_id=o["order_id"], attempt_no=attempt,
                                pack_station=f"PACK-{s + 1:02d}", qc_start_ts=qc_start,
                                qc_end_ts=qc_end, qc_result="FAIL" if failed else "PASS",
                                defect_type=defect, rework_minutes=rework))
            if not failed:
                break
            qc_start = qc_end + mins(rework)
            attempt += 1
        station_free[s] = qc_end

        # --- dispatch: label, carrier pickup, delivery --------------------------------------
        ch = cust_hidden[o["cust_idx"]]
        intl = ch["country"] != "DE"
        carrier = choose(CARRIER_MIX_EXPRESS if queue == "EXPRESS" else
                         CARRIER_MIX_BULK if queue == "BULK" else CARRIER_MIX_STANDARD)
        cv = CARRIERS[carrier]
        label_ts = qc_end + mins(rng.uniform(1, 4))
        pickup = None
        for k in range(10):
            d = label_ts.date() + timedelta(days=k)
            slots = cv["pickups"] if d.weekday() < 5 else cv["sat"] if d.weekday() == 5 else []
            pickup = next((at(d, tt) for tt in slots if at(d, tt) >= label_ts + mins(15)), None)
            if pickup:
                break
        handoff = pickup + mins(rng.uniform(0, 25))
        transit = cv["transit"] + (1 if intl else 0)
        if rng.random() < cv["p_delay"]:
            transit += int(rng.choice([1, 1, 1, 2, 3]))
        delivered = at(add_business_days(handoff.date(), transit), time(8, 30)) + mins(rng.uniform(0, 630))
        exception = rng.random() < P_DELIVERY_EXCEPTION
        promised = add_business_days(o["promise_base"], (1 if o["priority"] == "EXPRESS" else 2) + (1 if intl else 0))
        kg = sum(ln["qty"] * prod_hidden[ln["sku_idx"]]["kg"] for ln in o["lines"]) + 0.3
        pkgs = max(1, math.ceil(units / 12))

        if label_ts <= EXTRACT_TS:
            h = cut(handoff)
            dlv = None if exception else cut(delivered)
            status = ("LABEL_CREATED" if h is None else "EXCEPTION" if exception
                      else "DELIVERED" if dlv else "IN_TRANSIT")
            ship_rows.append(dict(order_id=o["order_id"], carrier_name=cv["name"],
                                  service_level="EXPRESS" if queue == "EXPRESS" else "STANDARD",
                                  tracking_number=f"{carrier}{rng.integers(10**9, 10**10)}",
                                  label_created_ts=label_ts, handoff_ts=h,
                                  promised_delivery_date=promised, delivered_ts=dlv,
                                  delivery_status=status, package_count=pkgs,
                                  weight_kg=round(kg, 2),
                                  shipping_cost_eur=round(cv["base_cost"] * pkgs + cv["per_kg"] * kg, 2)))
        o["handoff_ts"] = cut(handoff)
        o["delivered"] = cut(delivered) is not None and not exception and o["handoff_ts"] is not None

        # --- invoice & ledger posting -------------------------------------------------------
        if o["handoff_ts"] is None:
            continue
        inv_ts = handoff + mins(lognorm(20, 0.6))
        if rng.random() < P_INVOICE_MANUAL_DELAY:
            inv_ts = next_office(handoff + timedelta(days=int(rng.integers(1, 4)))) + mins(rng.uniform(0, 300))
        if inv_ts > EXTRACT_TS:
            continue
        batch_d = inv_ts.date() if inv_ts.time() < LEDGER_BATCH_TIME else inv_ts.date() + timedelta(days=1)
        posted = at(batch_d, LEDGER_BATCH_TIME) + mins(rng.uniform(0, 20))
        if rng.random() < P_LEDGER_POST_ERROR:
            post_status, posted = "ERROR", None
        elif posted > EXTRACT_TS:
            post_status, posted = "PENDING", None
        else:
            post_status = "POSTED"
        net = round(sum(ln["qty"] * ln["unit_price"] * (1 - ln["discount"]) for ln in o["lines"]), 2)
        vat = 0.0 if intl else VAT_RATE_DOMESTIC
        inv_rows.append(dict(order_id=o["order_id"], customer_idx=o["cust_idx"],
                             invoice_ts=inv_ts, net_amount_eur=net, vat_rate=vat,
                             vat_amount_eur=round(net * vat, 2),
                             gross_amount_eur=round(net * (1 + vat), 2), currency="EUR",
                             posting_status=post_status, ledger_posted_ts=posted))
    return pick_rows, qc_rows, ship_rows, inv_rows


# =============================================================================
# 8. COST RECORDS (finance)
# =============================================================================
def build_costs(pick_hidden, absent, shipments: pd.DataFrame, pick_tasks: pd.DataFrame):
    rows = []
    ship = shipments.dropna(subset=["handoff_ts"]).copy()
    ship["d"] = pd.to_datetime(ship["handoff_ts"]).dt.date
    ship_by_day = ship.groupby("d").agg(n=("order_id", "size"), freight=("shipping_cost_eur", "sum"))
    lines_by_day = pick_tasks.dropna(subset=["pick_end_ts"]).assign(
        d=lambda x: pd.to_datetime(x["pick_end_ts"]).dt.date).groupby("d").size()
    d = START_DATE
    while d <= END_DATE:
        if d.day == 1:
            rows.append((d, "WAREHOUSE", "RENT", MONTHLY_RENT, "Monthly warehouse rent"))
            rows.append((d, "WAREHOUSE", "UTILITIES", round(MONTHLY_UTILITIES * rng.uniform(0.85, 1.25), 2),
                         "Monthly utilities (energy, water)"))
        if d.weekday() in WAREHOUSE_DAYS:
            picking = sum(8 * PICKER_HOURLY_RATE[h["emp"]] for i, h in enumerate(pick_hidden)
                          if (i, d) not in absent)
            rows.append((d, "PICKING", "LABOUR", round(picking, 2), "Picker shift wages"))
            rows.append((d, "PACKING_QC", "LABOUR", round(N_PACK_STATIONS * 16 * PACKER_HOURLY_RATE, 2),
                         "Packing & QC station wages"))
        if d in ship_by_day.index:
            n, freight = ship_by_day.loc[d, "n"], ship_by_day.loc[d, "freight"]
            mat = n * rng.uniform(1.1, 1.6) + lines_by_day.get(d, 0) * 0.25
            rows.append((d, "DISPATCH", "PACKAGING_MATERIAL", round(mat, 2), "Boxes, filler, labels"))
            rows.append((d, "DISPATCH", "FREIGHT", round(freight, 2), "Carrier freight accrual"))
        d += timedelta(days=1)
    df = pd.DataFrame(rows, columns=["posting_date", "cost_center", "cost_type", "amount_eur", "description"])
    df.insert(0, "cost_entry_id", [f"CE{i + 1:06d}" for i in range(len(df))])
    return df


# =============================================================================
# 9. ASSEMBLE TABLES
# =============================================================================
def assemble(orders, lines, customers, cust_hidden, products, prod_hidden, pickers,
             reservations, backorders, snapshots, pick_rows, qc_rows, ship_rows, inv_rows):
    sku = lambda i: prod_hidden[i]["sku"]
    cid = customers["customer_id"].tolist()
    pid = pickers["picker_id"].tolist()

    # --- orders (ERP header) ---------------------------------------------------------------
    o_rows = []
    for o in orders:
        v = cut(o["validated_ts"])
        if v is None:
            status = "RECEIVED"
        elif o["validation_result"] == "FAILED":
            status = "REJECTED"
        elif o.get("outcome") == "BACKORDERED":
            status = "BACKORDERED"
        elif o.get("handoff_ts") is None:
            status = "IN_FULFILMENT"
        else:
            status = "DELIVERED" if o.get("delivered") else "SHIPPED"
        o_rows.append(dict(
            order_id=o["order_id"], api_message_id=hex_id(),
            customer_id=cid[o["cust_idx"]], channel=o["channel"], priority=o["priority"],
            received_ts=o["received_ts"],
            ingested_ts=o["received_ts"] + timedelta(seconds=rng.uniform(1, 6)),
            validated_ts=v, validation_result=o["validation_result"] if v else None,
            validation_reason=o["validation_reason"] if v else None,
            credit_status_at_order=o["credit_status"],
            customer_notified_ts=cut(o["notified_ts"]),
            promised_ship_by_ts=o["promised_ship_by_ts"] if status != "REJECTED" else None,
            order_status=status))
    t_orders = pd.DataFrame(o_rows)

    t_lines = pd.DataFrame([dict(order_id=ln["order_id"], line_no=ln["line_no"],
                                 sku=sku(ln["sku_idx"]), ordered_qty=ln["qty"],
                                 unit_price_eur=ln["unit_price"],
                                 discount_pct=round(ln["discount"] * 100, 1)) for ln in lines])

    # --- inventory -------------------------------------------------------------------------
    t_res = pd.DataFrame(reservations)
    t_res = t_res[t_res["reservation_ts"] <= EXTRACT_TS].sort_values("reservation_ts")
    t_res.insert(0, "reservation_id", [f"RS{i + 1:07d}" for i in range(len(t_res))])
    t_res["sku"] = t_res.pop("sku_idx").map(sku)
    t_res = t_res[["reservation_id", "order_id", "line_no", "sku", "available_qty_before",
                   "reserved_qty", "reservation_ts"]]

    t_bo = pd.DataFrame(backorders)
    t_bo = t_bo[t_bo["backorder_ts"] <= EXTRACT_TS].sort_values("backorder_ts")
    t_bo.insert(0, "backorder_id", [f"BO{i + 1:06d}" for i in range(len(t_bo))])
    t_bo["sku"] = t_bo.pop("sku_idx").map(sku)
    t_bo["notice_sent_ts"] = t_bo["notice_sent_ts"].where(t_bo["notice_sent_ts"] <= EXTRACT_TS)
    t_bo = t_bo[["backorder_id", "order_id", "line_no", "sku", "ordered_qty", "available_qty",
                 "backorder_qty", "backorder_reason", "expected_restock_date", "backorder_ts",
                 "notice_sent_ts"]]

    # --- WMS pick tasks (with open tasks at extract time) ----------------------------------
    t_pick = pd.DataFrame(pick_rows)
    t_pick = t_pick[t_pick["released_ts"] <= EXTRACT_TS].sort_values(["released_ts", "order_id", "line_no"])
    queued = t_pick["assigned_ts"] > EXTRACT_TS
    running = ~queued & (t_pick["pick_end_ts"] > EXTRACT_TS)
    t_pick["task_status"] = np.where(queued, "QUEUED", np.where(running, "IN_PROGRESS", "COMPLETED"))
    for c in ["assigned_ts", "pick_start_ts"]:
        t_pick[c] = t_pick[c].where(t_pick[c] <= EXTRACT_TS)
    t_pick["pick_end_ts"] = t_pick["pick_end_ts"].where(~queued & ~running)
    t_pick["picker_id"] = t_pick.pop("picker_idx").map(lambda i: pid[i]).where(~queued)
    t_pick["ordered_sku"] = t_pick.pop("ordered_sku_idx").map(sku)
    t_pick["picked_sku"] = t_pick.pop("picked_sku_idx").map(sku).where(~queued & ~running)
    t_pick["picked_qty"] = t_pick["picked_qty"].where(~queued & ~running).astype("Int64")
    t_pick.insert(0, "pick_task_id", [f"PT{i + 1:07d}" for i in range(len(t_pick))])
    t_pick = t_pick[["pick_task_id", "order_id", "line_no", "queue_name", "picker_id",
                     "released_ts", "assigned_ts", "pick_start_ts", "pick_end_ts",
                     "storage_bay", "ordered_sku", "picked_sku", "ordered_qty", "picked_qty",
                     "task_status"]]

    t_qc = pd.DataFrame(qc_rows)
    t_qc = t_qc[t_qc["qc_start_ts"] <= EXTRACT_TS].sort_values("qc_start_ts")
    open_qc = t_qc["qc_end_ts"] > EXTRACT_TS
    for c in ["qc_end_ts", "qc_result", "defect_type", "rework_minutes"]:
        t_qc[c] = t_qc[c].where(~open_qc)
    t_qc.insert(0, "qc_check_id", [f"QC{i + 1:07d}" for i in range(len(t_qc))])

    t_ship = pd.DataFrame(ship_rows).sort_values("label_created_ts")
    t_ship.insert(0, "shipment_id", [f"SH{i + 1:07d}" for i in range(len(t_ship))])

    t_inv = pd.DataFrame(inv_rows).sort_values("invoice_ts")
    years = pd.to_datetime(t_inv["invoice_ts"]).dt.year
    t_inv.insert(0, "invoice_id", [f"INV{y}-{n + 1:06d}" for y, n in
                                   zip(years, t_inv.groupby(years).cumcount())])
    t_inv["customer_id"] = t_inv.pop("customer_idx").map(lambda i: cid[i])
    t_inv = t_inv[["invoice_id", "order_id", "customer_id", "invoice_ts", "net_amount_eur",
                   "vat_rate", "vat_amount_eur", "gross_amount_eur", "currency",
                   "posting_status", "ledger_posted_ts"]]

    # --- daily inventory snapshot (on hand = available + reserved but not yet picked) ------
    snap = pd.DataFrame(snapshots)
    snap["sku"] = snap.pop("sku_idx").map(sku)
    res_ev = t_res[["sku", "reservation_ts", "order_id", "line_no", "reserved_qty"]].merge(
        t_pick[["order_id", "line_no", "pick_end_ts"]], on=["order_id", "line_no"], how="left")
    plus = res_ev.assign(d=pd.to_datetime(res_ev["reservation_ts"]).dt.date, q=res_ev["reserved_qty"])
    minus = res_ev.dropna(subset=["pick_end_ts"]).assign(
        d=lambda x: pd.to_datetime(x["pick_end_ts"]).dt.date, q=lambda x: -x["reserved_qty"])
    ev = pd.concat([plus, minus])[["sku", "d", "q"]].groupby(["sku", "d"])["q"].sum()
    snap = snap.merge(ev.rename("net").reset_index().rename(columns={"d": "snapshot_date"}),
                      on=["sku", "snapshot_date"], how="left")
    snap["net"] = snap["net"].fillna(0)
    snap = snap.sort_values(["sku", "snapshot_date"])
    snap["reserved_qty"] = snap.groupby("sku")["net"].cumsum().astype(int)
    snap["on_hand_qty"] = snap["available_qty"] + snap["reserved_qty"]
    bo_keys = set(zip(t_bo["sku"], pd.to_datetime(t_bo["backorder_ts"]).dt.date))
    snap["stockout_flag"] = np.where(
        (snap["available_qty"].to_numpy() <= 0)
        | np.array([k in bo_keys for k in zip(snap["sku"], snap["snapshot_date"])]),
        "Y", "N")
    t_snap = snap[["snapshot_date", "sku", "on_hand_qty", "reserved_qty", "available_qty",
                   "on_order_qty", "reorder_point", "stockout_flag"]].sort_values(["snapshot_date", "sku"])

    return dict(orders=t_orders, order_lines=t_lines, inventory_reservations=t_res,
                backorders=t_bo, inventory_snapshots=t_snap, pick_tasks=t_pick,
                qc_checks=t_qc, shipments=t_ship, invoices=t_inv)


# =============================================================================
# 10. CHECKS, KPIs, DIRTY DATA, OUTPUT
# =============================================================================
def run_checks(t: dict):
    """Fail loudly if the simulation produced impossible data (run BEFORE dirtying)."""
    o, pk, qc, sh, inv = t["orders"], t["pick_tasks"], t["qc_checks"], t["shipments"], t["invoices"]
    assert o["order_id"].is_unique
    assert set(t["order_lines"]["order_id"]) <= set(o["order_id"])
    assert set(pk["picker_id"].dropna()) <= set(t["pickers"]["picker_id"])
    assert set(t["order_lines"]["sku"]) <= set(t["products"]["sku"])
    assert set(inv["customer_id"]) <= set(t["customers"]["customer_id"])
    assert (o["validated_ts"].dropna() >= o.loc[o["validated_ts"].notna(), "received_ts"]).all()
    assert (pk["assigned_ts"].dropna() >= pk.loc[pk["assigned_ts"].notna(), "released_ts"]).all()
    done = pk.dropna(subset=["pick_end_ts"])
    assert (done["pick_end_ts"] > done["pick_start_ts"]).all()
    last_pick = done.groupby("order_id")["pick_end_ts"].max()
    first_qc = qc.groupby("order_id")["qc_start_ts"].min()
    assert (first_qc > last_pick.reindex(first_qc.index)).all()
    last_qc = qc.groupby("order_id")["qc_end_ts"].max()
    assert (sh.set_index("order_id")["label_created_ts"] > last_qc.reindex(sh["order_id"])).all()
    s = sh.dropna(subset=["handoff_ts"])
    assert (s["handoff_ts"] > s["label_created_ts"]).all()
    d = sh.dropna(subset=["delivered_ts"])
    assert (d["delivered_ts"] > d["handoff_ts"]).all()
    m = inv.merge(sh[["order_id", "handoff_ts"]], on="order_id")
    assert (m["invoice_ts"] > m["handoff_ts"]).all()
    assert (t["inventory_snapshots"]["available_qty"] >= 0).all()
    print("✓ integrity checks passed")


def print_kpis(t: dict):
    o, pk, qc, sh = t["orders"], t["pick_tasks"], t["qc_checks"], t["shipments"]
    n = len(o)
    m = sh.dropna(subset=["handoff_ts"]).merge(o[["order_id", "received_ts"]], on="order_id")
    cyc = (m["handoff_ts"] - m["received_ts"]).dt.total_seconds() / 3600
    dl = sh.dropna(subset=["delivered_ts"])
    on_time = (dl["delivered_ts"].dt.date <= dl["promised_delivery_date"]).mean()
    done = pk[pk["task_status"] == "COMPLETED"]
    correct = np.where(done["picked_sku"] == done["ordered_sku"],
                       np.minimum(done["picked_qty"], done["ordered_qty"]), 0)
    first = qc[qc["attempt_no"] == 1].dropna(subset=["qc_result"])
    hc = t["handling_costs"]
    cost = hc["amount_eur"].sum()
    handling = hc.loc[hc["cost_type"] != "FREIGHT", "amount_eur"].sum()
    print("\nBaseline KPIs (clean data)")
    print(f"  orders received ............ {n:,}")
    print(f"  order lines ................ {len(t['order_lines']):,}")
    print(f"  validation failure rate .... {(o['order_status'] == 'REJECTED').mean():.1%}")
    print(f"  backorder rate ............. {(o['order_status'] == 'BACKORDERED').mean():.1%}")
    print(f"  order-to-handoff (hours) ... mean {cyc.mean():.1f} | median {cyc.median():.1f} | p90 {cyc.quantile(.9):.1f}")
    print(f"  on-time delivery ........... {on_time:.1%}")
    print(f"  picking accuracy (units) ... {correct.sum() / done['picked_qty'].sum():.2%}")
    print(f"  QC first-pass failure ...... {(first['qc_result'] == 'FAIL').mean():.1%}")
    print(f"  cost per fulfilled order ... EUR {handling / len(m):.2f} handling | EUR {cost / len(m):.2f} incl. freight")


def make_dirty(t: dict):
    """Inject light, realistic source-system imperfections (documented in DATA_DICTIONARY.md)."""
    o = t["orders"]
    dup = o.sample(frac=DIRTY["duplicate_order_rate"], random_state=SEED).copy()
    dup["api_message_id"] = [hex_id() for _ in range(len(dup))]
    dup["ingested_ts"] = dup["ingested_ts"] + pd.to_timedelta(rng.integers(30, 600, len(dup)), unit="s")
    o = pd.concat([o, dup]).sort_values(["received_ts", "ingested_ts"]).reset_index(drop=True)
    mask = rng.random(len(o)) < DIRTY["channel_variant_rate"]
    o.loc[mask, "channel"] = [rng.choice([c.lower(), c.title(), c + " "]) for c in o.loc[mask, "channel"]]
    t["orders"] = o

    sh = t["shipments"]
    mask = rng.random(len(sh)) < DIRTY["carrier_name_variant_rate"]
    sh.loc[mask, "carrier_name"] = [rng.choice([c.upper(), c.lower(), c + " ", " " + c])
                                    for c in sh.loc[mask, "carrier_name"]]

    cu = t["customers"]
    cu.loc[rng.random(len(cu)) < DIRTY["missing_city_rate"], "city"] = None
    pr = t["products"]
    mask = rng.random(len(pr)) < DIRTY["product_name_whitespace_rate"]
    pr.loc[mask, "product_name"] = pr.loc[mask, "product_name"] + "  "


def write_outputs(t: dict):
    OUTPUT_DIR.mkdir(exist_ok=True)
    db_path = OUTPUT_DIR / SQLITE_FILE
    db_path.unlink(missing_ok=True)
    with sqlite3.connect(db_path) as con:
        for name, df in t.items():
            out = df.copy()
            for c in out.columns:
                if pd.api.types.is_datetime64_any_dtype(out[c]):
                    out[c] = out[c].dt.strftime("%Y-%m-%d %H:%M:%S")
                elif out[c].dtype == object and out[c].map(lambda v: isinstance(v, date)).any():
                    out[c] = out[c].map(lambda v: v.isoformat() if isinstance(v, date) else None)
            out.to_csv(OUTPUT_DIR / f"{name}.csv", index=False, encoding="utf-8-sig")
            out.to_sql(name, con, index=False)
            print(f"  {name + '.csv':<30} {len(out):>8,} rows")
    print(f"  {SQLITE_FILE:<30} {len(t)} tables")


# =============================================================================
# 11. MAIN
# =============================================================================
def main():
    print("Building master data ...")
    customers, cust_hidden = build_customers()
    products, prod_hidden, price_hist = build_products()
    pickers, pick_hidden, absent = build_pickers()
    carriers = build_carriers()

    print("Simulating order intake & validation ...")
    orders, lines = generate_orders(cust_hidden, prod_hidden)
    validate_orders(orders, cust_hidden)

    print("Simulating inventory ...")
    reservations, backorders, snapshots = simulate_inventory(orders, lines, prod_hidden)

    print("Simulating warehouse, dispatch and invoicing ...")
    pick_rows, qc_rows, ship_rows, inv_rows = simulate_fulfilment(
        orders, prod_hidden, cust_hidden, pick_hidden, absent)

    tables = assemble(orders, lines, customers, cust_hidden, products, prod_hidden, pickers,
                      reservations, backorders, snapshots, pick_rows, qc_rows, ship_rows, inv_rows)
    for name in ["orders", "pick_tasks", "qc_checks", "shipments", "invoices",
                 "inventory_reservations", "backorders"]:
        for c in tables[name].columns:
            if c.endswith("_ts"):
                tables[name][c] = pd.to_datetime(tables[name][c])
    tables["handling_costs"] = build_costs(pick_hidden, absent, tables["shipments"], tables["pick_tasks"])
    tables.update(customers=customers, products=products, product_price_history=price_hist,
                  pickers=pickers, carriers=carriers)

    run_checks(tables)
    print_kpis(tables)
    if INJECT_DIRTY_DATA:
        make_dirty(tables)

    print(f"\nWriting output to {OUTPUT_DIR} ...")
    order = ["customers", "products", "product_price_history", "pickers", "carriers", "orders",
             "order_lines", "inventory_reservations", "backorders", "inventory_snapshots",
             "pick_tasks", "qc_checks", "shipments", "invoices", "handling_costs"]
    write_outputs({k: tables[k] for k in order})
    print("Done.")


if __name__ == "__main__":
    main()
