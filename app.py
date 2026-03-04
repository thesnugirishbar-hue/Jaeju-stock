from datetime import datetime, date
import time
import pandas as pd
import streamlit as st

import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials

# ---------------------------
# CONFIG
# ---------------------------
APP_TITLE = "JAEJU Stock + POS + Events (Sheets DB)"
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

# Required tabs + headers in your Google Sheet
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


# ---------------------------
# HELPERS
# ---------------------------
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_str():
    return date.today().isoformat()


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


def _truthy(v) -> int:
    return 1 if str(v).strip().lower() in {"1", "true", "yes", "y"} else 0


# ---------------------------
# GOOGLE SHEETS CONNECTION
# ---------------------------
@st.cache_resource
def gs_client():
    if "gcp_service_account" not in st.secrets:
        raise ValueError("Missing [gcp_service_account] in Streamlit Secrets (Manage app → Secrets).")
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=SCOPES,
    )
    return gspread.authorize(creds)


def book():
    gsid = st.secrets.get("GSHEET_ID", "")
    if not gsid:
        raise ValueError("Missing GSHEET_ID in Streamlit Secrets (Manage app → Secrets).")
    return gs_client().open_by_key(gsid)


def get_or_create_ws(spreadsheet, title: str, rows: int = 3000, cols: int = 30):
    try:
        return spreadsheet.worksheet(title)
    except WorksheetNotFound:
        # create sheet
        return spreadsheet.add_worksheet(title=title, rows=str(rows), cols=str(cols))


def ensure_header(ws_obj, headers: list[str]):
    """
    Ensures the first row contains exactly these headers (in order).
    - If the sheet is empty -> writes headers.
    - If it has headers but missing some -> appends missing headers to the right.
    - Never wipes existing data.
    """
    current = ws_obj.row_values(1)
    if not current:
        ws_obj.append_row(headers, value_input_option="USER_ENTERED")
        return

    # If sheet already has some headers, extend with missing ones
    missing = [h for h in headers if h not in current]
    if missing:
        new_header = current + missing
        ws_obj.update("1:1", [new_header])


def ws(tab: str):
    return book().worksheet(tab)


def read_df(tab: str) -> pd.DataFrame:
    w = ws(tab)
    values = w.get_all_values()
    if not values:
        return pd.DataFrame()
    header = values[0]
    if len(values) == 1:
        return pd.DataFrame(columns=header)
    return pd.DataFrame(values[1:], columns=header)


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
    header = w.row_values(1)
    for col, val in updates.items():
        if col not in header:
            raise ValueError(f"Missing column '{col}' in tab '{tab}'. Fix headers.")
        c = header.index(col) + 1
        w.update_cell(row_idx, c, val)


# ---------------------------
# INIT DB (TABS + HEADERS)
# ---------------------------
def init_db():
    """
    Ensures required tabs exist + headers exist. Never wipes existing data.
    """
    b = book()
    for tab, headers in REQUIRED_TABS.items():
        w = get_or_create_ws(b, tab, rows=3000, cols=max(30, len(headers) + 5))
        ensure_header(w, headers)


def permission_test_ui():
    with st.expander("🔧 Google Sheets connection test", expanded=False):
        st.write("Service account:", st.secrets.get("gcp_service_account", {}).get("client_email", "(missing)"))
        st.write("Spreadsheet ID:", st.secrets.get("GSHEET_ID", "(missing)"))
        try:
            b = book()
            st.success(f"Connected ✅ Sheet title: {b.title}")
            st.write("Tabs found:", [w.title for w in b.worksheets()])
        except Exception as e:
            st.error(f"Connection failed: {e}")


# ---------------------------
# ITEMS + STOCK
# ---------------------------
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
        ws("stock").append_rows(rows, value_input_option="USER_ENTERED")


def get_items_df(active_only=True):
    df = read_df("items")
    if df.empty:
        return pd.DataFrame(columns=REQUIRED_TABS["items"])
    df["id"] = df["id"].apply(lambda v: _safe_int(v, 0))
    df["par_level"] = df["par_level"].apply(lambda v: _safe_float(v, 0.0))
    df["price_nzd"] = df["price_nzd"].apply(lambda v: _safe_float(v, 0.0))
    df["active"] = df["active"].apply(_truthy)
    if active_only:
        df = df[df["active"] == 1]
    return df.sort_values("name", key=lambda s: s.astype(str).str.lower())


def add_item(name: str, unit: str, par_level: float, price_nzd: float, active: int = 1):
    name = (name or "").strip()
    unit = (unit or "").strip() or "unit"
    if not name:
        raise ValueError("Item name cannot be empty.")

    row_idx = find_row_idx("items", "name", name)
    if row_idx is None:
        iid = next_id("items")
        ws("items").append_row(
            [iid, name, unit, float(par_level), float(price_nzd), int(active)],
            value_input_option="USER_ENTERED",
        )
        item_id = iid
    else:
        update_cells("items", row_idx, {
            "unit": unit,
            "par_level": float(par_level),
            "price_nzd": float(price_nzd),
            "active": int(active),
        })
        w = ws("items")
        header = w.row_values(1)
        item_id = _safe_int(w.cell(row_idx, header.index("id") + 1).value, 0)

    ensure_stock_rows_for_item(item_id)


def get_item_id_by_name(name: str):
    row_idx = find_row_idx("items", "name", name)
    if row_idx is None:
        return None
    w = ws("items")
    header = w.row_values(1)
    active_val = _truthy(w.cell(row_idx, header.index("active") + 1).value)
    if active_val != 1:
        return None
    return _safe_int(w.cell(row_idx, header.index("id") + 1).value, None)


def adjust_stock(item_id: int, location: str, delta: float, reason: str, ref_type=None, ref_id=None):
    if location not in LOCATIONS:
        raise ValueError("Invalid location.")
    if not (reason or "").strip():
        raise ValueError("Reason is required.")

    ensure_stock_rows_for_item(item_id)
    stock = read_df("stock")

    target_row = None
    current_qty = 0.0
    for i, r in enumerate(stock.to_dict("records"), start=2):
        if str(r.get("item_id")) == str(item_id) and str(r.get("location")) == str(location):
            target_row = i
            current_qty = _safe_float(r.get("qty"), 0.0)
            break

    new_qty = current_qty + float(delta)
    if target_row is None:
        ws("stock").append_row([item_id, location, new_qty], value_input_option="USER_ENTERED")
    else:
        update_cells("stock", target_row, {"qty": new_qty})

    mid = next_id("movements")
    ws("movements").append_row(
        [mid, now_iso(), item_id, location, float(delta), reason, ref_type or "", ref_id or ""],
        value_input_option="USER_ENTERED",
    )


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


# ---------------------------
# ORDERS
# ---------------------------
def create_order(note: str = "") -> int:
    oid = next_id("orders")
    ws("orders").append_row([oid, now_iso(), LOC_TRUCK, LOC_PREP, ORDER_STATUS_PENDING, note.strip()],
                            value_input_option="USER_ENTERED")
    return oid


def add_order_line(order_id: int, item_id: int, qty: float):
    if qty <= 0:
        raise ValueError("Qty must be > 0.")
    lid = next_id("order_lines")
    ws("order_lines").append_row([lid, int(order_id), int(item_id), float(qty)], value_input_option="USER_ENTERED")


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
    return merged[["name", "unit", "qty", "item_id"]].sort_values("name", key=lambda s: s.astype(str).str.lower())


def set_order_status(order_id: int, status: str):
    row_idx = find_row_idx("orders", "id", str(order_id))
    if row_idx is None:
        raise ValueError("Order not found.")
    update_cells("orders", row_idx, {"status": status})


def fulfill_order(order_id: int):
    lines = get_order_lines_df(order_id)
    if lines.empty:
        raise ValueError("Order has no lines.")

    orders = read_df("orders")
    if orders.empty:
        raise ValueError("Order not found.")
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
    items = get_items_df(active_only=False)
    if mv.empty:
        return pd.DataFrame(columns=["created_at", "name", "location", "delta", "reason", "ref_type", "ref_id"])
    mv["id"] = mv["id"].apply(lambda v: _safe_int(v, 0))
    mv = mv.sort_values("id", ascending=False).head(limit)
    mv["item_id"] = mv["item_id"].apply(lambda v: _safe_int(v, 0))
    merged = mv.merge(items[["id", "name"]], left_on="item_id", right_on="id", how="left")
    return merged[["created_at", "name", "location", "delta", "reason", "ref_type", "ref_id"]]


# ---------------------------
# MENU + RECIPES
# ---------------------------
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
        w.append_row([mid, sku, name, price, active, sort_order], value_input_option="USER_ENTERED")


def get_menu_items(active_only=True):
    df = read_df("menu_items")
    if df.empty:
        return pd.DataFrame(columns=REQUIRED_TABS["menu_items"])
    df["id"] = df["id"].apply(lambda v: _safe_int(v, 0))
    df["price"] = df["price"].apply(lambda v: _safe_float(v, 0.0))
    df["active"] = df["active"].apply(_truthy)
    df["sort_order"] = df["sort_order"].apply(lambda v: _safe_int(v, 0))
    if active_only:
        df = df[df["active"] == 1]
    return df.sort_values(["sort_order", "name"], key=lambda s: s.astype(str).str.lower())


def upsert_menu_items(df: pd.DataFrame):
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
            ws("menu_items").append_row([mid, sku, name, price, active, sort_order], value_input_option="USER_ENTERED")
        else:
            row_idx = find_row_idx("menu_items", "id", str(int(rid)))
            if row_idx is None:
                ws("menu_items").append_row([int(rid), sku, name, price, active, sort_order], value_input_option="USER_ENTERED")
            else:
                update_cells("menu_items", row_idx, {"sku": sku, "name": name, "price": price, "active": active, "sort_order": sort_order})


def get_menu_recipe(menu_id: int):
    mr = read_df("menu_recipes")
    items = get_items_df(active_only=True)
    if mr.empty:
        return pd.DataFrame(columns=["item_id", "qty"])
    mr["menu_id"] = mr["menu_id"].apply(lambda v: _safe_int(v, 0))
    mr = mr[mr["menu_id"] == int(menu_id)].copy()
    if mr.empty:
        return pd.DataFrame(columns=["item_id", "qty"])
    mr["item_id"] = mr["item_id"].apply(lambda v: _safe_int(v, 0))
    mr["qty"] = mr["qty"].apply(lambda v: _safe_float(v, 0.0))
    mr = mr[mr["qty"] > 0]

    merged = mr.merge(items[["id", "name"]], left_on="item_id", right_on="id", how="left")
    merged = merged.rename(columns={"name": "item_name"})
    return merged[["item_id", "item_name", "qty"]].sort_values("item_name", key=lambda s: s.astype(str).str.lower())


def upsert_menu_recipe(menu_id: int, df: pd.DataFrame):
    # Soft wipe: set existing rows for menu_id qty=0 (keeps history but disables)
    existing = read_df("menu_recipes")
    if not existing.empty:
        existing["menu_id"] = existing["menu_id"].apply(lambda v: _safe_int(v, 0))
        for i, r in enumerate(existing.to_dict("records"), start=2):
            if _safe_int(r.get("menu_id"), 0) == int(menu_id):
                update_cells("menu_recipes", i, {"qty": 0})

    for _, r in df.iterrows():
        try:
            item_id = int(r["item_id"])
            qty = float(r["qty"])
        except Exception:
            continue
        if item_id <= 0 or qty <= 0:
            continue
        rid = next_id("menu_recipes")
        ws("menu_recipes").append_row([rid, int(menu_id), int(item_id), float(qty)], value_input_option="USER_ENTERED")


def get_recipe_map(menu_id: int):
    df = read_df("menu_recipes")
    if df.empty:
        return {}
    df["menu_id"] = df["menu_id"].apply(lambda v: _safe_int(v, 0))
    df = df[df["menu_id"] == int(menu_id)].copy()
    if df.empty:
        return {}
    df["item_id"] = df["item_id"].apply(lambda v: _safe_int(v, 0))
    df["qty"] = df["qty"].apply(lambda v: _safe_float(v, 0.0))
    df = df[df["qty"] > 0]
    return {int(r["item_id"]): float(r["qty"]) for _, r in df.iterrows()}


# ---------------------------
# SALES / POS
# ---------------------------
def create_sale(payment_method: str, note: str = "") -> int:
    sid = next_id("sales")
    ws("sales").append_row([sid, now_iso(), today_str(), payment_method, note.strip()], value_input_option="USER_ENTERED")
    return sid


def add_sale_line(sale_id: int, menu_id: int, sku: str, name: str, qty: float, unit_price: float):
    line_total = float(qty) * float(unit_price)
    lid = next_id("sale_lines")
    ws("sale_lines").append_row(
        [lid, int(sale_id), int(menu_id), sku, name, float(qty), float(unit_price), float(line_total)],
        value_input_option="USER_ENTERED",
    )


def get_today_sales_summary():
    sales = read_df("sales")
    lines = read_df("sale_lines")
    if sales.empty or lines.empty:
        return (
            pd.DataFrame(columns=["sale_date", "payment_method", "total"]),
            pd.DataFrame(columns=["sku", "name", "qty", "total"]),
        )

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


# ---------------------------
# EVENT MODE
# ---------------------------
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


# ---------------------------
# Orders draft state
# ---------------------------
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


# ---------------------------
# APP UI
# ---------------------------
st.set_page_config(page_title=APP_TITLE, page_icon="🍗", layout="wide")
st.title(APP_TITLE)

# Connection test always visible
permission_test_ui()

# Init Sheets DB + seed menu
init_db()
seed_menu_if_empty()

mobile_mode = st.toggle("Mobile mode", value=True)
PAGES = ["POS", "Event Mode", "Orders", "Dashboard", "Adjust Stock", "Menu Admin", "Items", "Movements"]

if mobile_mode:
    page = st.selectbox("Go to", PAGES)
    tabs = None
else:
    tabs = st.tabs(PAGES)
    page = None


# -------- POS --------
if (mobile_mode and page == "POS") or (not mobile_mode):
    container = st.container() if mobile_mode else tabs[PAGES.index("POS")]
    with container:
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
        c2.metric("EFTPOS", f"${eftpos_today:,.2f}")
        c3.metric("Cash", f"${cash_today:,.2f}")

        if not item_summary.empty:
            st.dataframe(item_summary, use_container_width=True, hide_index=True)
        else:
            st.info("No sales recorded today yet.")


# -------- Event Mode --------
if (mobile_mode and page == "Event Mode") or (not mobile_mode):
    container = st.container() if mobile_mode else tabs[PAGES.index("Event Mode")]
    with container:
        st.subheader("Event Mode (Revenue → Ingredients → Draft Order)")

        menu = get_menu_items(active_only=True)
        if menu.empty:
            st.warning("No active menu items. Add them in Menu Admin.")
        else:
            event_name = st.text_input("Event name", placeholder="Electric Ave Day 1")
            revenue = st.number_input("Target revenue (NZD)", min_value=0.0, value=10000.0, step=500.0)
            buffer_pct = st.number_input("Safety buffer (%)", min_value=0.0, value=10.0, step=1.0)

            st.markdown("### Menu mix")
            mix_raw = {}
            total = 0.0
            for _, r in menu.iterrows():
                mid = int(r["id"])
                default = int(100 / max(len(menu), 1))
                val = st.slider(f"{r['name']} (%)", 0, 100, default)
                mix_raw[mid] = float(val)
                total += float(val)

            if total <= 0:
                st.warning("Set at least one menu share above 0%.")
            else:
                mix = {k: v / total for k, v in mix_raw.items() if v > 0}
                qty_rows, ing_totals = forecast_from_revenue(revenue, mix, menu)
                ing_totals = {k: v * (1.0 + buffer_pct / 100.0) for k, v in ing_totals.items()}

                items = get_items_df(active_only=True)
                id_to_name = {int(r["id"]): str(r["name"]) for _, r in items.iterrows()}

                name_totals = {}
                for item_id, qty_total in ing_totals.items():
                    if item_id in id_to_name:
                        name_totals[id_to_name[item_id]] = float(qty_total)

                st.markdown("### Estimated qty sold")
                qty_view = []
                for mid, qty_est in qty_rows:
                    row = menu.loc[menu["id"] == mid].iloc[0]
                    qty_view.append({"Menu item": row["name"], "Qty (est)": round(float(qty_est), 1), "Price": float(row["price"])})
                st.dataframe(pd.DataFrame(qty_view), use_container_width=True, hide_index=True)

                st.markdown("### Load sheet (ingredients)")
                load_df = pd.DataFrame(
                    [{"Item": k, "Qty": round(float(v), 2)} for k, v in name_totals.items() if float(v) > 0],
                    columns=["Item", "Qty"],
                ).sort_values("Item")
                st.dataframe(load_df, use_container_width=True, hide_index=True)

                st.download_button(
                    "Download load sheet (CSV)",
                    data=load_df.to_csv(index=False).encode("utf-8"),
                    file_name=f"load_sheet_{(event_name or 'event').replace(' ', '_')}.csv",
                    mime="text/csv",
                )

                if st.button("Send to Orders draft", type="primary"):
                    set_order_draft_from_name_totals(name_totals)
                    st.success("Draft created. Go to Orders tab and press Create order.")


# -------- Orders --------
if (mobile_mode and page == "Orders") or (not mobile_mode):
    container = st.container() if mobile_mode else tabs[PAGES.index("Orders")]
    with container:
        st.subheader("Truck → Prep Kitchen Orders (mobile friendly)")

        items = get_items_df(active_only=True)
        if items.empty:
            st.info("Add items first.")
        else:
            item_names = list(items["name"].tolist())
            ensure_order_lines_state()

            note = st.text_input("Order note (optional)", placeholder="Friday top-up / Event name")

            st.markdown("### Add to order")
            c1, c2, c3 = st.columns([2, 1, 1])
            pick_item = c1.selectbox("Item", item_names, key="order_pick_item")
            pick_qty = c2.number_input("Qty", min_value=0.0, value=1.0, step=0.5, key="order_pick_qty")

            if c3.button("Add", type="primary"):
                if pick_qty <= 0:
                    st.warning("Qty must be > 0")
                else:
                    add_to_order_draft(pick_item, float(pick_qty))
                    st.success("Added.")
                    st.rerun()

            st.divider()
            st.markdown("### Draft order")

            if not st.session_state["order_lines"]:
                st.info("No items in the draft yet.")
            else:
                df = pd.DataFrame(st.session_state["order_lines"])
                edited = st.data_editor(
                    df,
                    num_rows="dynamic",
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Item": st.column_config.SelectboxColumn("Item", options=item_names),
                        "Qty": st.column_config.NumberColumn("Qty", min_value=0.0, step=0.5),
                    },
                    key="order_lines_editor",
                )
                edited["Item"] = edited["Item"].fillna("").astype(str)
                edited["Qty"] = edited["Qty"].fillna(0).astype(float)
                edited = edited[(edited["Item"].str.strip() != "") & (edited["Qty"] > 0)]
                st.session_state["order_lines"] = edited.to_dict(orient="records")

                c1, c2 = st.columns([1, 2])
                if c1.button("Clear draft"):
                    st.session_state["order_lines"] = []
                    st.rerun()

                if c2.button("Create order", type="primary"):
                    try:
                        if not st.session_state["order_lines"]:
                            raise ValueError("Add at least one item to the draft.")
                        order_id = create_order(note=note)
                        for line in st.session_state["order_lines"]:
                            item_id = get_item_id_by_name(line["Item"])
                            if not item_id:
                                raise ValueError(f"Item not found/active: {line['Item']}")
                            add_order_line(order_id, int(item_id), float(line["Qty"]))
                        st.success(f"Order #{order_id} created (PENDING).")
                        st.session_state["order_lines"] = []
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

        st.divider()
        st.subheader("Manage orders")
        orders = get_orders_df()
        if orders.empty:
            st.info("No orders yet.")
        else:
            st.dataframe(orders, use_container_width=True, hide_index=True)
            order_id = st.number_input("Order ID", min_value=1, step=1, value=int(orders.iloc[0]["id"]))
            lines = get_order_lines_df(int(order_id))
            if not lines.empty:
                st.dataframe(lines[["name", "unit", "qty"]], use_container_width=True, hide_index=True)

            c1, c2 = st.columns(2)
            if c1.button("Fulfill (Prep → Truck)", type="primary"):
                try:
                    fulfill_order(int(order_id))
                    st.success("Fulfilled. Stock moved Prep → Truck.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            if c2.button("Cancel order"):
                try:
                    set_order_status(int(order_id), ORDER_STATUS_CANCELLED)
                    st.success("Cancelled.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


# -------- Dashboard --------
if (mobile_mode and page == "Dashboard") or (not mobile_mode):
    container = st.container() if mobile_mode else tabs[PAGES.index("Dashboard")]
    with container:
        st.subheader("Stock snapshot")
        pivot = get_stock_pivot()
        if pivot.empty:
            st.info("No items yet. Add items in Items tab.")
        else:
            st.dataframe(
                pivot[["name", "unit", "par_level", LOC_TRUCK, LOC_PREP, "Below PAR?"]],
                use_container_width=True,
                hide_index=True,
            )


# -------- Adjust Stock --------
if (mobile_mode and page == "Adjust Stock") or (not mobile_mode):
    container = st.container() if mobile_mode else tabs[PAGES.index("Adjust Stock")]
    with container:
        st.subheader("Adjust stock (counts, wastage, deliveries)")
        items = get_items_df(active_only=True)
        if items.empty:
            st.info("Add items first.")
        else:
            item_map = dict(zip(items["name"], items["id"]))

            item_name = st.selectbox("Item", list(item_map.keys()))
            location = st.selectbox("Location", LOCATIONS)
            delta = st.number_input("Delta (+ add / - remove)", value=0.0, step=0.5)
            reason = st.text_input("Reason", placeholder="delivery / wastage / recount")

            if st.button("Apply adjustment", type="primary"):
                try:
                    adjust_stock(int(item_map[item_name]), location, float(delta),
                                 reason=reason.strip() or "Manual adjustment",
                                 ref_type="manual", ref_id=None)
                    st.success("Stock updated.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


# -------- Menu Admin --------
if (mobile_mode and page == "Menu Admin") or (not mobile_mode):
    container = st.container() if mobile_mode else tabs[PAGES.index("Menu Admin")]
    with container:
        st.subheader("Menu Admin (edit menu + recipes)")
        st.info("Tip: Do this on a laptop if possible. Mobile works, but it’s slower.")

        menu_df = get_menu_items(active_only=False)
        st.markdown("### Menu items")
        edited_menu = st.data_editor(
            menu_df,
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            column_config={
                "id": st.column_config.NumberColumn("id", disabled=True),
                "sku": st.column_config.TextColumn("sku"),
                "name": st.column_config.TextColumn("name"),
                "price": st.column_config.NumberColumn("price", min_value=0.0, step=0.5),
                "active": st.column_config.CheckboxColumn("active"),
                "sort_order": st.column_config.NumberColumn("sort_order", step=10),
            },
            key="menu_editor",
        )
        if st.button("Save menu items", type="primary"):
            try:
                upsert_menu_items(edited_menu)
                st.success("Menu saved.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

        st.divider()
        st.markdown("### Edit recipe (ingredients per 1 sale)")

        menu_all = get_menu_items(active_only=False)
        if menu_all.empty:
            st.warning("No menu items yet.")
        else:
            pick = st.selectbox("Choose menu item", menu_all["name"].tolist())
            menu_id = int(menu_all.loc[menu_all["name"] == pick].iloc[0]["id"])

            items = get_items_df(active_only=True)
            if items.empty:
                st.warning("Add ingredient items in Items tab first.")
            else:
                id_to_name = {int(r["id"]): str(r["name"]) for _, r in items.iterrows()}
                options = list(id_to_name.keys())

                recipe_df = get_menu_recipe(menu_id)
                if recipe_df.empty:
                    edit_df = pd.DataFrame([{"item_id": int(items.iloc[0]["id"]), "qty": 0.0}], columns=["item_id", "qty"])
                else:
                    edit_df = recipe_df[["item_id", "qty"]].copy()

                edited_recipe = st.data_editor(
                    edit_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "item_id": st.column_config.SelectboxColumn(
                            "Ingredient item",
                            options=options,
                            format_func=lambda x: id_to_name.get(int(x), str(x)),
                        ),
                        "qty": st.column_config.NumberColumn("Qty per sale", step=0.01),
                    },
                    key="recipe_editor",
                )
                if st.button("Save recipe", type="primary"):
                    try:
                        upsert_menu_recipe(menu_id, edited_recipe)
                        st.success("Recipe saved.")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))


# -------- Items --------
if (mobile_mode and page == "Items") or (not mobile_mode):
    container = st.container() if mobile_mode else tabs[PAGES.index("Items")]
    with container:
        st.subheader("Items")

        with st.expander("Add / Update item", expanded=True):
            name = st.text_input("Item name", placeholder="Chicken thigh diced")
            unit = st.text_input("Unit", placeholder="kg / pcs / L")
            par = st.number_input("PAR level (Prep)", min_value=0.0, value=0.0, step=0.5)
            price = st.number_input("Price NZD (optional)", min_value=0.0, value=0.0, step=0.1)
            active = st.checkbox("Active", value=True)

            if st.button("Save item", type="primary"):
                try:
                    add_item(name=name, unit=unit.strip() or "unit", par_level=float(par), price_nzd=float(price), active=1 if active else 0)
                    st.success("Saved.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        df = get_items_df(active_only=False)
        if not df.empty:
            st.dataframe(df, use_container_width=True, hide_index=True)


# -------- Movements --------
if (mobile_mode and page == "Movements") or (not mobile_mode):
    container = st.container() if mobile_mode else tabs[PAGES.index("Movements")]
    with container:
        st.subheader("Movements log (audit trail)")
        mv = get_movements_df()
        if mv.empty:
            st.info("No movements yet.")
        else:
            st.dataframe(mv, use_container_width=True, hide_index=True)
