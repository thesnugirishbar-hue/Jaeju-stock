import time
from datetime import datetime, date
import pandas as pd
import streamlit as st

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

# =========================
# CONFIG
# =========================
LOC_TRUCK = "Food Truck"
LOC_PREP = "Prep Kitchen"
LOCATIONS = [LOC_TRUCK, LOC_PREP]

ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_FULFILLED = "FULFILLED"
ORDER_STATUS_CANCELLED = "CANCELLED"

PAYMENT_EFTPOS = "EFTPOS"
PAYMENT_CASH = "CASH"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

REQUIRED_TABS = {
    "items": ["id", "name", "unit", "par_level", "price_nzd", "active"],
    "stock": ["item_id", "location", "qty"],
    "movements": ["id", "created_at", "item_id", "location", "delta", "reason", "ref_type", "ref_id"],
    "orders": ["id", "created_at", "from_location", "to_location", "status", "note"],
    "order_lines": ["id", "order_id", "item_id", "qty"],
    "sales": ["id", "created_at", "sale_date", "payment_method", "note"],
    "sale_lines": ["id", "sale_id", "menu_id", "sku", "name", "qty", "unit_price", "line_total"],
    "menu_items": ["id", "sku", "name", "price", "active", "sort_order"],
    "menu_recipes": ["id", "menu_id", "item_id", "qty"],
}

# =========================
# TIME
# =========================
def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def today_str():
    return date.today().isoformat()

# =========================
# SAFE CAST
# =========================
def _safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def _safe_int(x, default=0):
    try:
        return int(float(x))
    except Exception:
        return default

# =========================
# QUOTA BACKOFF HELPERS
# =========================
def _with_backoff(func, *args, **kwargs):
    # light backoff for Sheets 429s
    delay = 0.8
    for attempt in range(6):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            msg = str(e)
            if "429" in msg or "Quota exceeded" in msg or "RESOURCE_EXHAUSTED" in msg:
                time.sleep(delay)
                delay *= 1.7
                continue
            raise
    raise RuntimeError("Google Sheets quota hit too many times (429). Try again in 1–2 mins.")

# =========================
# GOOGLE SHEETS CLIENT
# =========================
@st.cache_resource
def gs_client():
    if "gcp_service_account" not in st.secrets:
        raise ValueError("Missing [gcp_service_account] in Streamlit Secrets.")
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=SCOPES,
    )
    return gspread.authorize(creds)

def sheet_id():
    # You MUST set this in secrets
    if "GSHEET_ID" not in st.secrets:
        raise ValueError("Missing GSHEET_ID in Streamlit Secrets (Manage app → Settings → Secrets).")
    return st.secrets["GSHEET_ID"]

@st.cache_resource
def book():
    # open_by_key is more reliable than open(name)
    return _with_backoff(gs_client().open_by_key, sheet_id())

@st.cache_resource
def ws_map():
    b = book()
    # one metadata fetch cached
    return {w.title: w for w in _with_backoff(b.worksheets)}

def ws(tab: str):
    m = ws_map()
    if tab not in m:
        raise KeyError(f"Missing tab '{tab}' in sheet. Create it or let init_db create it.")
    return m[tab]

def invalidate_ws_cache():
    # call when we create new tabs
    ws_map.clear()

# =========================
# READ CACHE (reduces quota)
# =========================
@st.cache_data(ttl=8)  # short TTL to reduce reads while clicking around
def read_df_cached(tab: str) -> pd.DataFrame:
    w = ws(tab)
    values = _with_backoff(w.get_all_values)
    if not values:
        return pd.DataFrame()
    header = [str(h).strip() for h in values[0]]
    if len(values) == 1:
        return pd.DataFrame(columns=header)
    df = pd.DataFrame(values[1:], columns=header)
    return df

def read_df(tab: str) -> pd.DataFrame:
    return read_df_cached(tab)

def clear_read_cache():
    read_df_cached.clear()

# =========================
# SHEET STRUCTURE / HEADERS
# =========================
def ensure_header(ws_obj, headers):
    current = _with_backoff(ws_obj.row_values, 1)
    current = [str(c).strip() for c in current] if current else []
    want = [str(h).strip() for h in headers]

    if not current:
        _with_backoff(ws_obj.update, "A1", [want])
        return

    # If already matches prefix, do nothing
    if current[: len(want)] == want:
        return

    # If row 1 is not correct, overwrite row 1 only (does not wipe data rows)
    _with_backoff(ws_obj.update, "A1", [want])

def get_or_create_ws(spreadsheet, title: str, rows=3000, cols=40):
    try:
        return _with_backoff(spreadsheet.worksheet, title)
    except Exception:
        # Create new worksheet
        return _with_backoff(spreadsheet.add_worksheet, title=title, rows=str(rows), cols=str(cols))

def init_db():
    b = book()

    # Ensure tabs exist + headers correct
    for tab, headers in REQUIRED_TABS.items():
        w = get_or_create_ws(b, tab, rows=3000, cols=max(40, len(headers) + 10))
        ensure_header(w, headers)

    # refresh worksheet map cache if anything created
    invalidate_ws_cache()
    clear_read_cache()

# =========================
# GENERIC HELPERS
# =========================
def next_id(tab: str) -> int:
    df = read_df(tab)
    if df.empty or "id" not in df.columns:
        return 1
    ids = df["id"].apply(lambda v: _safe_int(v, 0))
    return int(ids.max()) + 1 if len(ids) else 1

def find_row_idx(tab: str, col: str, value: str):
    """
    Returns 1-based sheet row index (including header row).
    Data rows start at 2.
    """
    df = read_df(tab)
    if df.empty or col not in df.columns:
        return None
    col_vals = df[col].astype(str).tolist()
    for i, v in enumerate(col_vals, start=2):
        if str(v) == str(value):
            return i
    return None

def update_cells(tab: str, row_idx: int, updates: dict):
    w = ws(tab)
    header = _with_backoff(w.row_values, 1)
    header = [str(h).strip() for h in header]
    for col, val in updates.items():
        if col not in header:
            raise ValueError(f"Missing column '{col}' in tab '{tab}'")
        c = header.index(col) + 1
        _with_backoff(w.update_cell, row_idx, c, val)

# =========================
# ITEMS + STOCK
# =========================
def ensure_stock_rows_for_item(item_id: int):
    df = read_df("stock")
    existing = set()
    if not df.empty:
        for _, r in df.iterrows():
            existing.add((str(r.get("item_id", "")), str(r.get("location", ""))))

    rows = []
    for loc in LOCATIONS:
        if (str(item_id), loc) not in existing:
            rows.append([item_id, loc, 0])
    if rows:
        _with_backoff(ws("stock").append_rows, rows, value_input_option="USER_ENTERED")
        clear_read_cache()

def get_items_df(active_only=True):
    df = read_df("items")
    if df.empty:
        return pd.DataFrame(columns=REQUIRED_TABS["items"])

    # normalise
    df["id"] = df["id"].apply(lambda v: _safe_int(v, 0))
    df["par_level"] = df["par_level"].apply(lambda v: _safe_float(v, 0.0))
    df["price_nzd"] = df["price_nzd"].apply(lambda v: _safe_float(v, 0.0))
    df["active"] = df["active"].apply(lambda v: 1 if str(v).lower() in ["1", "true", "yes"] else 0)

    if active_only:
        df = df[df["active"] == 1]

    return df.sort_values("name", key=lambda s: s.astype(str).str.lower())

def add_item(name: str, unit: str, par_level: float, price_nzd: float, active: int = 1):
    name = name.strip()
    if not name:
        raise ValueError("Item name cannot be empty.")

    row_idx = find_row_idx("items", "name", name)
    if row_idx is None:
        iid = next_id("items")
        _with_backoff(ws("items").append_row, [iid, name, unit, par_level, price_nzd, int(active)], value_input_option="USER_ENTERED")
        item_id = iid
    else:
        update_cells("items", row_idx, {"unit": unit, "par_level": par_level, "price_nzd": price_nzd, "active": int(active)})
        # read id from that row
        w = ws("items")
        header = _with_backoff(w.row_values, 1)
        item_id = _safe_int(_with_backoff(w.cell, row_idx, header.index("id") + 1).value, 0)

    ensure_stock_rows_for_item(item_id)
    clear_read_cache()

def get_item_id_by_name(name: str):
    row_idx = find_row_idx("items", "name", name)
    if row_idx is None:
        return None
    w = ws("items")
    header = _with_backoff(w.row_values, 1)
    header = [str(h).strip() for h in header]
    active_val = str(_with_backoff(w.cell, row_idx, header.index("active") + 1).value).lower()
    if active_val not in ["1", "true", "yes"]:
        return None
    return _safe_int(_with_backoff(w.cell, row_idx, header.index("id") + 1).value, None)

def adjust_stock(item_id: int, location: str, delta: float, reason: str, ref_type=None, ref_id=None):
    if location not in LOCATIONS:
        raise ValueError("Invalid location.")
    if not reason.strip():
        raise ValueError("Reason is required.")

    ensure_stock_rows_for_item(item_id)
    stock = read_df("stock")

    target_row = None
    current_qty = 0.0
    if not stock.empty:
        for i, r in enumerate(stock.to_dict("records"), start=2):
            if str(r.get("item_id")) == str(item_id) and str(r.get("location")) == str(location):
                target_row = i
                current_qty = _safe_float(r.get("qty"), 0.0)
                break

    new_qty = current_qty + float(delta)

    if target_row is None:
        _with_backoff(ws("stock").append_row, [item_id, location, new_qty], value_input_option="USER_ENTERED")
    else:
        update_cells("stock", target_row, {"qty": new_qty})

    mid = next_id("movements")
    _with_backoff(
        ws("movements").append_row,
        [mid, now_iso(), item_id, location, float(delta), reason, ref_type or "", ref_id or ""],
        value_input_option="USER_ENTERED",
    )
    clear_read_cache()

def get_stock_df():
    stock = read_df("stock")
    items = get_items_df(active_only=True)
    if stock.empty or items.empty:
        return pd.DataFrame(columns=["item_id", "name", "unit", "par_level", "location", "qty"])

    stock["item_id"] = stock["item_id"].apply(lambda v: _safe_int(v, 0))
    stock["qty"] = stock["qty"].apply(lambda v: _safe_float(v, 0.0))

    merged = stock.merge(items[["id", "name", "unit", "par_level"]], left_on="item_id", right_on="id", how="inner")
    return merged[["item_id", "name", "unit", "par_level", "location", "qty"]].sort_values(
        ["name", "location"], key=lambda s: s.astype(str).str.lower()
    )

def get_stock_pivot():
    df = get_stock_df()
    if df.empty:
        return df
    pivot = df.pivot_table(
        index=["item_id", "name", "unit", "par_level"],
        columns="location",
        values="qty",
        aggfunc="sum",
    ).reset_index()
    for loc in LOCATIONS:
        if loc not in pivot.columns:
            pivot[loc] = 0.0
    pivot["Below PAR?"] = (pivot[LOC_PREP] < pivot["par_level"]).map({True: "YES", False: ""})
    return pivot

# =========================
# ORDERS
# =========================
def create_order(note: str = "") -> int:
    oid = next_id("orders")
    _with_backoff(ws("orders").append_row, [oid, now_iso(), LOC_TRUCK, LOC_PREP, ORDER_STATUS_PENDING, note.strip()], value_input_option="USER_ENTERED")
    clear_read_cache()
    return oid

def add_order_line(order_id: int, item_id: int, qty: float):
    if qty <= 0:
        raise ValueError("Qty must be > 0.")
    lid = next_id("order_lines")
    _with_backoff(ws("order_lines").append_row, [lid, int(order_id), int(item_id), float(qty)], value_input_option="USER_ENTERED")
    clear_read_cache()

def get_orders_df(limit=100):
    df = read_df("orders")
    if df.empty:
        return pd.DataFrame(columns=["id", "created_at", "status", "note"])
    df["id"] = df["id"].apply(lambda v: _safe_int(v, 0))
    df = df.sort_values("id", ascending=False).head(limit)
    return df[["id", "created_at", "status", "note"]]

def get_order_lines_df(order_id: int):
    ol = read_df("order_lines")
    items = get_items_df(active_only=True)
    if ol.empty:
        return pd.DataFrame(columns=["name", "unit", "qty", "item_id"])

    ol["order_id"] = ol["order_id"].apply(lambda v: _safe_int(v, 0))
    ol = ol[ol["order_id"] == int(order_id)].copy()
    if ol.empty:
        return pd.DataFrame(columns=["name", "unit", "qty", "item_id"])

    ol["item_id"] = ol["item_id"].apply(lambda v: _safe_int(v, 0))
    ol["qty"] = ol["qty"].apply(lambda v: _safe_float(v, 0.0))

    merged = ol.merge(items[["id", "name", "unit"]], left_on="item_id", right_on="id", how="left")
    merged["name"] = merged["name"].fillna("UNKNOWN ITEM")
    merged["unit"] = merged["unit"].fillna("")
    return merged[["name", "unit", "qty", "item_id"]].sort_values("name", key=lambda s: s.astype(str).str.lower())

def set_order_status(order_id: int, status: str):
    row_idx = find_row_idx("orders", "id", str(order_id))
    if row_idx is None:
        raise ValueError("Order not found.")
    update_cells("orders", row_idx, {"status": status})
    clear_read_cache()

def fulfill_order(order_id: int):
    lines = get_order_lines_df(order_id)
    if lines.empty:
        raise ValueError("Order has no lines.")

    orders = read_df("orders")
    orders["id"] = orders["id"].apply(lambda v: _safe_int(v, 0))
    row = orders.loc[orders["id"] == int(order_id)]
    if row.empty:
        raise ValueError("Order not found.")
    if str(row.iloc[0]["status"]) != ORDER_STATUS_PENDING:
        raise ValueError("Order must be PENDING to fulfill.")

    prep = get_stock_df()
    prep_map = {int(r["item_id"]): float(r["qty"]) for _, r in prep.iterrows() if r["location"] == LOC_PREP}

    for _, line in lines.iterrows():
        item_id = int(line["item_id"])
        qty = float(line["qty"])
        if prep_map.get(item_id, 0.0) < qty:
            raise ValueError(f"Not enough Prep stock for {line['name']} (have {prep_map.get(item_id, 0.0)}, need {qty})")

    for _, line in lines.iterrows():
        item_id = int(line["item_id"])
        qty = float(line["qty"])
        adjust_stock(item_id, LOC_PREP, -qty, "Order fulfillment (Prep → Truck)", ref_type="order", ref_id=order_id)
        adjust_stock(item_id, LOC_TRUCK, +qty, "Order fulfillment (Prep → Truck)", ref_type="order", ref_id=order_id)

    set_order_status(order_id, ORDER_STATUS_FULFILLED)

def get_movements_df(limit=300):
    mv = read_df("movements")
    items = read_df("items")
    if mv.empty:
        return pd.DataFrame(columns=["created_at", "name", "location", "delta", "reason", "ref_type", "ref_id"])
    mv["id"] = mv["id"].apply(lambda v: _safe_int(v, 0))
    mv = mv.sort_values("id", ascending=False).head(limit)
    mv["item_id"] = mv["item_id"].apply(lambda v: _safe_int(v, 0))

    if not items.empty and "id" in items.columns and "name" in items.columns:
        items["id"] = items["id"].apply(lambda v: _safe_int(v, 0))
        merged = mv.merge(items[["id", "name"]], left_on="item_id", right_on="id", how="left")
        merged["name"] = merged["name"].fillna("UNKNOWN ITEM")
    else:
        merged = mv.copy()
        merged["name"] = "UNKNOWN ITEM"

    return merged[["created_at", "name", "location", "delta", "reason", "ref_type", "ref_id"]]

# =========================
# MENU
# =========================
def seed_menu_if_empty():
    df = read_df("menu_items")
    if not df.empty:
        return
    starters = [
        ("JUST_CHICKEN", "Just Chicken", 20.00, 1, 10),
        ("SMALL_CHIPS", "Small Chicken on Chips", 22.00, 1, 20),
        ("LARGE_CHIPS", "Large Chicken on Chips", 26.00, 1, 30),
        ("BURGER", "Chicken Burger", 20.00, 1, 40),
        ("CAULI", "Korean Cauli", 18.00, 1, 50),
        ("CHIPS", "Chips", 8.00, 1, 60),
    ]
    w = ws("menu_items")
    for sku, name, price, active, sort_order in starters:
        mid = next_id("menu_items")
        _with_backoff(w.append_row, [mid, sku, name, price, active, sort_order], value_input_option="USER_ENTERED")
    clear_read_cache()

def get_menu_items(active_only=True):
    df = read_df("menu_items")
    if df.empty:
        return pd.DataFrame(columns=REQUIRED_TABS["menu_items"])
    df["id"] = df["id"].apply(lambda v: _safe_int(v, 0))
    df["price"] = df["price"].apply(lambda v: _safe_float(v, 0.0))
    df["active"] = df["active"].apply(lambda v: 1 if str(v).lower() in ["1", "true", "yes"] else 0)
    df["sort_order"] = df["sort_order"].apply(lambda v: _safe_int(v, 0))
    if active_only:
        df = df[df["active"] == 1]
    return df.sort_values(["sort_order", "name"], key=lambda s: s.astype(str).str.lower())

def upsert_menu_items(df: pd.DataFrame):
    w = ws("menu_items")
    for _, r in df.iterrows():
        sku = str(r.get("sku", "")).strip()
        name = str(r.get("name", "")).strip()
        if not sku or not name:
            continue
        price = float(r.get("price", 0.0))
        active = 1 if bool(r.get("active", True)) else 0
        sort_order = int(r.get("sort_order", 0))
        rid = r.get("id", None)

        if pd.isna(rid):
            mid = next_id("menu_items")
            _with_backoff(w.append_row, [mid, sku, name, price, active, sort_order], value_input_option="USER_ENTERED")
        else:
            row_idx = find_row_idx("menu_items", "id", str(int(rid)))
            if row_idx is None:
                _with_backoff(w.append_row, [int(rid), sku, name, price, active, sort_order], value_input_option="USER_ENTERED")
            else:
                update_cells("menu_items", row_idx, {"sku": sku, "name": name, "price": price, "active": active, "sort_order": sort_order})

    clear_read_cache()

def get_menu_recipe(menu_id: int):
    mr = read_df("menu_recipes")
    items = get_items_df(active_only=True)
    if mr.empty:
        return pd.DataFrame(columns=["id", "menu_id", "item_id", "item_name", "qty"])

    # Ensure required cols exist even if sheet got messed up
    for c in ["id", "menu_id", "item_id", "qty"]:
        if c not in mr.columns:
            mr[c] = ""

    mr["menu_id"] = mr["menu_id"].apply(lambda v: _safe_int(v, 0))
    mr = mr[mr["menu_id"] == int(menu_id)].copy()
    if mr.empty:
        return pd.DataFrame(columns=["id", "menu_id", "item_id", "item_name", "qty"])

    mr["item_id"] = mr["item_id"].apply(lambda v: _safe_int(v, 0))
    mr["qty"] = mr["qty"].apply(lambda v: _safe_float(v, 0.0))
    mr = mr[mr["qty"] > 0]

    merged = mr.merge(items[["id", "name"]], left_on="item_id", right_on="id", how="left")
    merged = merged.rename(columns={"name": "item_name"})
    merged["item_name"] = merged["item_name"].fillna("UNKNOWN ITEM")

    out = merged[["id_x", "menu_id", "item_id", "item_name", "qty"]].copy()
    out = out.rename(columns={"id_x": "id"})
    return out.sort_values("item_name", key=lambda s: s.astype(str).str.lower())

def upsert_menu_recipe(menu_id: int, df: pd.DataFrame):
    # “soft wipe” existing menu_id rows by setting qty=0 (keeps history, avoids deletes)
    existing = read_df("menu_recipes")
    if not existing.empty and "menu_id" in existing.columns:
        existing["menu_id"] = existing["menu_id"].apply(lambda v: _safe_int(v, 0))
        w = ws("menu_recipes")
        for i, r in enumerate(existing.to_dict("records"), start=2):
            if _safe_int(r.get("menu_id"), 0) == int(menu_id):
                update_cells("menu_recipes", i, {"qty": 0})

    w = ws("menu_recipes")
    for _, r in df.iterrows():
        try:
            item_id = int(r["item_id"])
            qty = float(r["qty"])
        except Exception:
            continue
        if item_id <= 0 or qty <= 0:
            continue
        rid = next_id("menu_recipes")
        _with_backoff(w.append_row, [rid, int(menu_id), int(item_id), float(qty)], value_input_option="USER_ENTERED")

    clear_read_cache()

def get_recipe_map(menu_id: int):
    df = read_df("menu_recipes")
    if df.empty:
        return {}
    if "menu_id" not in df.columns:
        return {}
    df["menu_id"] = df["menu_id"].apply(lambda v: _safe_int(v, 0))
    df = df[df["menu_id"] == int(menu_id)].copy()
    if df.empty:
        return {}
    if "item_id" not in df.columns or "qty" not in df.columns:
        return {}
    df["item_id"] = df["item_id"].apply(lambda v: _safe_int(v, 0))
    df["qty"] = df["qty"].apply(lambda v: _safe_float(v, 0.0))
    df = df[df["qty"] > 0]
    return {int(r["item_id"]): float(r["qty"]) for _, r in df.iterrows()}

# =========================
# SALES / POS
# =========================
def create_sale(payment_method: str, note: str = "") -> int:
    sid = next_id("sales")
    _with_backoff(ws("sales").append_row, [sid, now_iso(), today_str(), payment_method, note.strip()], value_input_option="USER_ENTERED")
    clear_read_cache()
    return sid

def add_sale_line(sale_id: int, menu_id: int, sku: str, name: str, qty: float, unit_price: float):
    line_total = float(qty) * float(unit_price)
    lid = next_id("sale_lines")
    _with_backoff(ws("sale_lines").append_row, [lid, int(sale_id), int(menu_id), sku, name, float(qty), float(unit_price), float(line_total)], value_input_option="USER_ENTERED")
    clear_read_cache()

def get_today_sales_summary():
    sales = read_df("sales")
    lines = read_df("sale_lines")
    if sales.empty or lines.empty:
        return (
            pd.DataFrame(columns=["sale_date", "payment_method", "total"]),
            pd.DataFrame(columns=["sku", "name", "qty", "total"]),
        )

    for c in ["id", "sale_date", "payment_method"]:
        if c not in sales.columns:
            sales[c] = ""
    for c in ["sale_id", "qty", "line_total", "sku", "name"]:
        if c not in lines.columns:
            lines[c] = ""

    sales["id"] = sales["id"].apply(lambda v: _safe_int(v, 0))
    sales["sale_date"] = sales["sale_date"].astype(str)

    lines["sale_id"] = lines["sale_id"].apply(lambda v: _safe_int(v, 0))
    lines["qty"] = lines["qty"].apply(lambda v: _safe_float(v, 0.0))
    lines["line_total"] = lines["line_total"].apply(lambda v: _safe_float(v, 0.0))

    merged = lines.merge(sales[["id", "sale_date", "payment_method"]], left_on="sale_id", right_on="id", how="left")
    merged = merged[merged["sale_date"] == today_str()].copy()

    if merged.empty:
        return (
            pd.DataFrame(columns=["sale_date", "payment_method", "total"]),
            pd.DataFrame(columns=["sku", "name", "qty", "total"]),
        )

    pay = (
        merged.groupby(["sale_date", "payment_method"], as_index=False)["line_total"]
        .sum()
        .rename(columns={"line_total": "total"})
    )
    item = (
        merged.groupby(["sku", "name"], as_index=False)
        .agg(qty=("qty", "sum"), total=("line_total", "sum"))
        .sort_values("total", ascending=False)
    )
    return pay, item

def record_pos_sale(menu_row: pd.Series, qty: float, payment_method: str, note: str = ""):
    if qty <= 0:
        raise ValueError("Qty must be > 0.")

    menu_id = int(menu_row["id"])
    sku = str(menu_row["sku"])
    name = str(menu_row["name"])
    price = float(menu_row["price"])

    recipe = get_recipe_map(menu_id)
    if not recipe:
        raise ValueError("This menu item has no recipe yet. Add it in Menu Admin.")

    sale_id = create_sale(payment_method=payment_method, note=note)
    add_sale_line(sale_id, menu_id, sku, name, float(qty), price)

    for item_id, per_unit in recipe.items():
        total_used = float(per_unit) * float(qty)
        adjust_stock(
            item_id=item_id,
            location=LOC_TRUCK,
            delta=-total_used,
            reason=f"POS sale: {name} x{qty}",
            ref_type="sale",
            ref_id=sale_id,
        )
    return sale_id

# =========================
# EVENT MODE
# =========================
def forecast_from_revenue(revenue_nzd: float, mix: dict, menu_df: pd.DataFrame):
    revenue_nzd = float(revenue_nzd)
    qty_rows = []

    for mid, share in mix.items():
        row = menu_df.loc[menu_df["id"] == mid]
        if row.empty:
            continue
        price = float(row.iloc[0]["price"])
        if price <= 0:
            continue
        qty_est = (revenue_nzd * float(share)) / price
        qty_rows.append((mid, qty_est))

    ing_totals = {}
    for mid, qty_est in qty_rows:
        recipe = get_recipe_map(int(mid))
        for item_id, per_unit in recipe.items():
            ing_totals[item_id] = ing_totals.get(item_id, 0.0) + (float(per_unit) * float(qty_est))

    return qty_rows, ing_totals

# =========================
# ORDER DRAFT STATE
# =========================
def ensure_order_lines_state():
    if "order_lines" not in st.session_state:
        st.session_state["order_lines"] = []

def add_to_order_draft(item_name: str, qty: float):
    ensure_order_lines_state()
    if qty <= 0:
        return
    for line in st.session_state["order_lines"]:
        if line["Item"] == item_name:
            line["Qty"] = float(line["Qty"]) + float(qty)
            return
    st.session_state["order_lines"].append({"Item": item_name, "Qty": float(qty)})

def set_order_draft_from_name_totals(name_totals: dict):
    ensure_order_lines_state()
    st.session_state["order_lines"] = [{"Item": k, "Qty": float(v)} for k, v in name_totals.items() if float(v) > 0]

# =========================
# UI
# =========================
st.set_page_config(page_title="JAEJU Ops (Sheets)", page_icon="🍗", layout="wide")

with st.sidebar:
    st.markdown("### 🔧 Sheets connection")
    st.write("Service account:", st.secrets.get("gcp_service_account", {}).get("client_email", "(missing)"))
    st.write("GSHEET_ID:", st.secrets.get("GSHEET_ID", "(missing)"))
    if st.button("Refresh sheet cache"):
        clear_read_cache()
        ws_map.clear()
        st.toast("Cache cleared")
        st.rerun()

# init Sheets DB + seed
init_db()
seed_menu_if_empty()

st.title("JAEJU Stock + POS + Events (Google Sheets)")

mobile_mode = st.toggle("Mobile mode", value=True)
PAGES = ["POS", "Event Mode", "Orders", "Dashboard", "Adjust Stock", "Menu Admin", "Items", "Movements"]

if mobile_mode:
    page = st.selectbox("Go to", PAGES)
    tabs = None
else:
    tabs = st.tabs(PAGES)
    page = None

def _container(name):
    if mobile_mode:
        return st.container()
    return tabs[PAGES.index(name)]

# -------- POS --------
if (mobile_mode and page == "POS") or (not mobile_mode):
    with _container("POS"):
        st.subheader("POS (one-tap buttons)")

        menu = get_menu_items(active_only=True)
        if menu.empty:
            st.warning("No active menu items. Go to Menu Admin.")
        else:
            pay = st.segmented_control("Payment", [PAYMENT_EFTPOS, PAYMENT_CASH], default=PAYMENT_EFTPOS)
            qty_mode = st.toggle("Qty mode (sell more than 1)", value=False)
            qty = 1.0
            if qty_mode:
                qty = st.number_input("Qty", min_value=1.0, value=1.0, step=1.0)

            cols = st.columns(2)
            for i, (_, r) in enumerate(menu.iterrows()):
                with cols[i % 2]:
                    label = f"{r['name']}\n${float(r['price']):.0f}"
                    if st.button(label, use_container_width=True):
                        try:
                            sale_id = record_pos_sale(r, float(qty), pay, "")
                            st.toast(f"Sold: {r['name']} (#{sale_id})")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

        st.divider()
        st.subheader("Today totals")
        pay_summary, item_summary = get_today_sales_summary()

        total_today = float(pay_summary["total"].sum()) if not pay_summary.empty else 0.0
        eftpos_today = float(pay_summary.loc[pay_summary["payment_method"] == PAYMENT_EFTPOS, "total"].sum()) if not pay_summary.empty else 0.0
        cash_today = float(pay_summary.loc[pay_summary["payment_method"] == PAYMENT_CASH, "total"].sum()) if not pay_summary.empty else 0.0

        c1, c2, c3 = st.columns(3)
      c1.metric("Total", f"${total_today:,.2f}")
        c2.metric("E
