import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
import pandas as pd
import streamlit as st

# ----------------------------
# CONFIG: POS menu + recipes
# IMPORTANT: ingredient names must exactly match your Items tab names.
# ----------------------------
MENU = [
    {
        "sku": "KFC",
        "name": "Korean Fried Chicken",
        "price": 20.00,
        "recipe": {
            "Chicken thigh diced": 0.18,  # kg per portion
            "Flour": 0.03,                # kg
            "Cornstarch": 0.02,           # kg
            "Frying oil": 0.01,           # L (rough)
            "KFC sauce": 0.03,            # kg/L
        },
    },
    {
        "sku": "KFC_CHIPS",
        "name": "KFC on Chips",
        "price": 22.00,
        "recipe": {
            "Chicken thigh diced": 0.18,
            "Flour": 0.03,
            "Cornstarch": 0.02,
            "Frying oil": 0.01,
            "KFC sauce": 0.03,
            "Chips": 0.30,                # kg
        },
    },
    {
        "sku": "BURGER",
        "name": "Chicken Burger",
        "price": 20.00,
        "recipe": {
            "Chicken thigh diced": 0.18,
            "Buns": 1,
            "Slaw mix": 0.08,             # kg
            "Burger sauce": 0.02,
            "Frying oil": 0.01,
            "Flour": 0.03,
            "Cornstarch": 0.02,
        },
    },
    {
        "sku": "CAULI",
        "name": "Korean Cauli",
        "price": 18.00,
        "recipe": {
            "Cauli": 0.35,                # kg
            "Flour": 0.02,
            "Cornstarch": 0.02,
            "Frying oil": 0.01,
            "KFC sauce": 0.02,
        },
    },
    {
        "sku": "CHIPS",
        "name": "Chips",
        "price": 8.00,
        "recipe": {
            "Chips": 0.30,
            "Frying oil": 0.005,
        },
    },
]

DB_PATH = "jaeju_stock.db"

LOC_TRUCK = "Food Truck"
LOC_PREP = "Prep Kitchen"
LOCATIONS = [LOC_TRUCK, LOC_PREP]

ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_FULFILLED = "FULFILLED"
ORDER_STATUS_CANCELLED = "CANCELLED"

PAYMENT_EFTPOS = "EFTPOS"
PAYMENT_CASH = "CASH"

# ----------------------------
# DB helpers
# ----------------------------
@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def today_str():
    return date.today().isoformat()


def init_db():
    with get_conn() as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            unit TEXT NOT NULL,
            par_level REAL DEFAULT 0,
            price_nzd REAL DEFAULT 0,
            active INTEGER DEFAULT 1
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            item_id INTEGER NOT NULL,
            location TEXT NOT NULL,
            qty REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (item_id, location),
            FOREIGN KEY (item_id) REFERENCES items(id)
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            from_location TEXT NOT NULL,
            to_location TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS order_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            qty REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (item_id) REFERENCES items(id)
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            item_id INTEGER NOT NULL,
            location TEXT NOT NULL,
            delta REAL NOT NULL,
            reason TEXT NOT NULL,
            ref_type TEXT,
            ref_id INTEGER,
            FOREIGN KEY (item_id) REFERENCES items(id)
        );
        """)

        # POS tables
        cur.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            sale_date TEXT NOT NULL,
            payment_method TEXT NOT NULL,
            note TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sale_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            name TEXT NOT NULL,
            qty REAL NOT NULL,
            unit_price REAL NOT NULL,
            line_total REAL NOT NULL,
            FOREIGN KEY (sale_id) REFERENCES sales(id)
        );
        """)

        conn.commit()


def get_items_df(active_only=True):
    with get_conn() as conn:
        q = "SELECT id, name, unit, par_level, price_nzd, active FROM items"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY name COLLATE NOCASE"
        return pd.read_sql_query(q, conn)


def ensure_stock_rows_for_item(item_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        for loc in LOCATIONS:
            cur.execute("""
                INSERT OR IGNORE INTO stock(item_id, location, qty)
                VALUES (?, ?, 0)
            """, (item_id, loc))
        conn.commit()


# Upsert: if name exists, update it instead of UNIQUE error
def add_item(name: str, unit: str, par_level: float, price_nzd: float, active: int = 1):
    name = name.strip()
    if not name:
        raise ValueError("Item name cannot be empty.")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM items WHERE name = ?", (name,))
        row = cur.fetchone()

        if row:
            item_id = int(row["id"])
            cur.execute("""
                UPDATE items
                SET unit = ?, par_level = ?, price_nzd = ?, active = ?
                WHERE id = ?
            """, (unit, par_level, price_nzd, active, item_id))
            conn.commit()
        else:
            cur.execute("""
                INSERT INTO items(name, unit, par_level, price_nzd, active)
                VALUES (?, ?, ?, ?, ?)
            """, (name, unit, par_level, price_nzd, active))
            item_id = cur.lastrowid
            conn.commit()

    ensure_stock_rows_for_item(item_id)


def get_stock_df():
    with get_conn() as conn:
        q = """
        SELECT i.id AS item_id, i.name, i.unit, i.par_level, i.price_nzd,
               s.location, s.qty
        FROM stock s
        JOIN items i ON i.id = s.item_id
        WHERE i.active = 1
        ORDER BY i.name COLLATE NOCASE, s.location
        """
        return pd.read_sql_query(q, conn)


def get_stock_pivot():
    df = get_stock_df()
    if df.empty:
        return df
    pivot = df.pivot_table(
        index=["item_id", "name", "unit", "par_level", "price_nzd"],
        columns="location",
        values="qty",
        aggfunc="sum"
    ).reset_index()
    for loc in LOCATIONS:
        if loc not in pivot.columns:
            pivot[loc] = 0.0
    pivot["Below PAR?"] = (pivot[LOC_PREP] < pivot["par_level"]).map({True: "YES", False: ""})
    return pivot


def adjust_stock(item_id: int, location: str, delta: float, reason: str, ref_type=None, ref_id=None):
    if location not in LOCATIONS:
        raise ValueError("Invalid location.")
    if reason.strip() == "":
        raise ValueError("Reason is required.")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO stock(item_id, location, qty) VALUES (?, ?, 0)",
                    (item_id, location))
        cur.execute("UPDATE stock SET qty = qty + ? WHERE item_id = ? AND location = ?",
                    (delta, item_id, location))
        cur.execute("""
            INSERT INTO movements(created_at, item_id, location, delta, reason, ref_type, ref_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (now_iso(), item_id, location, delta, reason, ref_type, ref_id))
        conn.commit()


def get_item_id_by_name(name: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM items WHERE name = ? AND active = 1", (name,))
        row = cur.fetchone()
        return int(row["id"]) if row else None


def create_order(note: str = "") -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO orders(created_at, from_location, to_location, status, note)
            VALUES (?, ?, ?, ?, ?)
        """, (now_iso(), LOC_TRUCK, LOC_PREP, ORDER_STATUS_PENDING, note.strip()))
        order_id = cur.lastrowid
        conn.commit()
    return order_id


def add_order_line(order_id: int, item_id: int, qty: float):
    if qty <= 0:
        raise ValueError("Qty must be > 0.")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO order_lines(order_id, item_id, qty) VALUES (?, ?, ?)",
                    (order_id, item_id, qty))
        conn.commit()


def get_orders_df(limit=100):
    with get_conn() as conn:
        q = "SELECT id, created_at, status, note FROM orders ORDER BY id DESC LIMIT ?"
        return pd.read_sql_query(q, conn, params=(limit,))


def get_order_lines_df(order_id: int):
    with get_conn() as conn:
        q = """
        SELECT i.name, i.unit, ol.qty, ol.item_id
        FROM order_lines ol
        JOIN items i ON i.id = ol.item_id
        WHERE ol.order_id = ?
        ORDER BY i.name COLLATE NOCASE
        """
        return pd.read_sql_query(q, conn, params=(order_id,))


def set_order_status(order_id: int, status: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        conn.commit()


def fulfill_order(order_id: int):
    lines = get_order_lines_df(order_id)
    if lines.empty:
        raise ValueError("Order has no lines.")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Order not found.")
        if row["status"] != ORDER_STATUS_PENDING:
            raise ValueError(f"Order must be PENDING to fulfill (currently {row['status']}).")

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
    with get_conn() as conn:
        q = """
        SELECT m.created_at, i.name, m.location, m.delta, m.reason, m.ref_type, m.ref_id
        FROM movements m
        JOIN items i ON i.id = m.item_id
        ORDER BY m.id DESC
        LIMIT ?
        """
        return pd.read_sql_query(q, conn, params=(limit,))


# ----------------------------
# POS
# ----------------------------
def create_sale(payment_method: str, note: str = "") -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO sales(created_at, sale_date, payment_method, note)
            VALUES (?, ?, ?, ?)
        """, (now_iso(), today_str(), payment_method, note.strip()))
        sale_id = cur.lastrowid
        conn.commit()
    return sale_id


def add_sale_line(sale_id: int, sku: str, name: str, qty: float, unit_price: float):
    line_total = float(qty) * float(unit_price)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO sale_lines(sale_id, sku, name, qty, unit_price, line_total)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (sale_id, sku, name, qty, unit_price, line_total))
        conn.commit()


def get_today_sales_summary():
    with get_conn() as conn:
        pay = pd.read_sql_query("""
            SELECT sale_date, payment_method, SUM(line_total) AS total
            FROM sales s
            JOIN sale_lines sl ON sl.sale_id = s.id
            WHERE sale_date = ?
            GROUP BY sale_date, payment_method
        """, conn, params=(today_str(),))

        lines = pd.read_sql_query("""
            SELECT sl.sku, sl.name, SUM(sl.qty) AS qty, SUM(sl.line_total) AS total
            FROM sales s
            JOIN sale_lines sl ON sl.sale_id = s.id
            WHERE s.sale_date = ?
            GROUP BY sl.sku, sl.name
            ORDER BY total DESC
        """, conn, params=(today_str(),))

    return pay, lines


def record_pos_sale(menu_item: dict, qty: float, payment_method: str, note: str = ""):
    if qty <= 0:
        raise ValueError("Qty must be > 0.")

    missing = []
    ingredient_item_ids = {}
    for ing_name in menu_item["recipe"].keys():
        item_id = get_item_id_by_name(ing_name)
        if not item_id:
            missing.append(ing_name)
        else:
            ingredient_item_ids[ing_name] = item_id
    if missing:
        raise ValueError("Missing ingredient items in Items tab: " + ", ".join(missing))

    sale_id = create_sale(payment_method=payment_method, note=note)
    add_sale_line(
        sale_id=sale_id,
        sku=menu_item["sku"],
        name=menu_item["name"],
        qty=float(qty),
        unit_price=float(menu_item["price"]),
    )

    for ing_name, per_portion in menu_item["recipe"].items():
        item_id = ingredient_item_ids[ing_name]
        total_used = float(per_portion) * float(qty)
        adjust_stock(
            item_id=item_id,
            location=LOC_TRUCK,
            delta=-total_used,
            reason=f"POS sale: {menu_item['name']} x{qty}",
            ref_type="sale",
            ref_id=sale_id
        )

    return sale_id


# ----------------------------
# EVENT MODE (Forecast → Prep Order + Load Sheet)
# ----------------------------
def build_event_forecast(
    revenue_nzd: float,
    kg_per_1k: float,
    buffer_pct: float,
    mix: dict,
):
    """
    mix is shares (0..1) for: kfc, kfc_chips, burger, cauli, chips
    """
    revenue_nzd = float(revenue_nzd)
    buffer_pct = float(buffer_pct)

    base_chicken_kg = (revenue_nzd / 1000.0) * float(kg_per_1k)
    chicken_kg = base_chicken_kg * (1.0 + buffer_pct / 100.0)

    # Portions using 0.18kg chicken per portion where chicken-based items
    chicken_per_portion = 0.18
    chicken_portions = chicken_kg / chicken_per_portion if chicken_per_portion > 0 else 0

    # Recipe-derived ratios per portion (from MENU)
    # For chicken-based items, we use KFC/BURGER/KFC_CHIPS recipes to estimate other needs.
    def per_portion(item_name: str, ingredient: str) -> float:
        for m in MENU:
            if m["name"] == item_name:
                return float(m["recipe"].get(ingredient, 0.0))
        return 0.0

    # Weighted average "chicken-based portion" deductions
    chicken_share_total = mix["kfc"] + mix["kfc_chips"] + mix["burger"]
    if chicken_share_total <= 0:
        chicken_share_total = 1.0

    w_kfc = mix["kfc"] / chicken_share_total
    w_kfc_chips = mix["kfc_chips"] / chicken_share_total
    w_burger = mix["burger"] / chicken_share_total

    flour_pp = (w_kfc * per_portion("Korean Fried Chicken", "Flour") +
                w_kfc_chips * per_portion("KFC on Chips", "Flour") +
                w_burger * per_portion("Chicken Burger", "Flour"))
    corn_pp = (w_kfc * per_portion("Korean Fried Chicken", "Cornstarch") +
               w_kfc_chips * per_portion("KFC on Chips", "Cornstarch") +
               w_burger * per_portion("Chicken Burger", "Cornstarch"))
    oil_pp = (w_kfc * per_portion("Korean Fried Chicken", "Frying oil") +
              w_kfc_chips * per_portion("KFC on Chips", "Frying oil") +
              w_burger * per_portion("Chicken Burger", "Frying oil"))

    # Sauce split: burger sauce for burger share; kfc sauce for kfc + kfc_chips share
    kfc_sauce_pp = (w_kfc * per_portion("Korean Fried Chicken", "KFC sauce") +
                    w_kfc_chips * per_portion("KFC on Chips", "KFC sauce"))
    burger_sauce_pp = w_burger * per_portion("Chicken Burger", "Burger sauce")

    # Totals for chicken-based demand
    flour_kg = chicken_portions * flour_pp
    cornstarch_kg = chicken_portions * corn_pp
    oil_l = chicken_portions * oil_pp
    kfc_sauce = chicken_portions * kfc_sauce_pp
    burger_sauce = chicken_portions * burger_sauce_pp

    # Buns + slaw (burger only)
    buns = chicken_portions * mix["burger"]
    slaw_kg = chicken_portions * mix["burger"] * per_portion("Chicken Burger", "Slaw mix")

    # Chips kg: KFC_CHIPS portions + chips-only portions
    chips_kg = (chicken_portions * mix["kfc_chips"] * per_portion("KFC on Chips", "Chips")) + \
               (chicken_portions * mix["chips"] * per_portion("Chips", "Chips"))

    # Cauli (not chicken based): scale by chicken_portions as a convenience anchor
    cauli_kg = chicken_portions * mix["cauli"] * per_portion("Korean Cauli", "Cauli")
    # Cauli also needs flour/corn/oil/sauce; add those too (small impact)
    flour_kg += chicken_portions * mix["cauli"] * per_portion("Korean Cauli", "Flour")
    cornstarch_kg += chicken_portions * mix["cauli"] * per_portion("Korean Cauli", "Cornstarch")
    oil_l += chicken_portions * mix["cauli"] * per_portion("Korean Cauli", "Frying oil")
    kfc_sauce += chicken_portions * mix["cauli"] * per_portion("Korean Cauli", "KFC sauce")

    # Round sensibly
    forecast = {
        "Chicken thigh diced": round(chicken_kg, 1),
        "Flour": round(flour_kg, 1),
        "Cornstarch": round(cornstarch_kg, 1),
        "Frying oil": round(oil_l, 1),
        "KFC sauce": round(kfc_sauce, 1),
        "Burger sauce": round(burger_sauce, 1),
        "Buns": int(round(buns)),
        "Slaw mix": round(slaw_kg, 1),
        "Chips": round(chips_kg, 1),
        "Cauli": round(cauli_kg, 1),
    }

    meta = {
        "Revenue (NZD)": revenue_nzd,
        "Chicken kg per $1k": kg_per_1k,
        "Buffer %": buffer_pct,
        "Chicken kg (with buffer)": chicken_kg,
        "Estimated chicken-based portions": chicken_portions,
    }

    return meta, forecast


def ensure_order_df():
    if "order_df" not in st.session_state:
        st.session_state["order_df"] = pd.DataFrame(
            [{"Item": "", "Qty": 0.0}],
            columns=["Item", "Qty"]
        )


def set_order_df_from_forecast(forecast: dict):
    rows = []
    for item, qty in forecast.items():
        if qty and float(qty) > 0:
            rows.append({"Item": item, "Qty": float(qty)})
    if not rows:
        rows = [{"Item": "", "Qty": 0.0}]
    st.session_state["order_df"] = pd.DataFrame(rows, columns=["Item", "Qty"])


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="JAEJU Stock", page_icon="jaeju-logo.jpg", layout="wide")
init_db()

st.title("JAEJU Stock + POS + Events + Orders")

tabs = st.tabs(["POS", "Event Mode", "Dashboard", "Items", "Adjust Stock", "Orders", "Movements"])

# ---- POS ----
with tabs[0]:
    st.subheader("POS (Food Truck)")

    menu_names = [m["name"] for m in MENU]
    menu_by_name = {m["name"]: m for m in MENU}

    c1, c2, c3 = st.columns([2, 1, 1])
    chosen = c1.selectbox("Menu item", menu_names)
    qty = c2.number_input("Qty", min_value=1.0, value=1.0, step=1.0)
    pay = c3.selectbox("Payment", [PAYMENT_EFTPOS, PAYMENT_CASH])

    note = st.text_input("Note (optional)", placeholder="e.g., comp / staff meal")

    b1, b2, b3 = st.columns(3)
    if b1.button("Sell x1", type="primary"):
        try:
            sale_id = record_pos_sale(menu_by_name[chosen], 1, pay, note)
            st.success(f"Recorded sale #{sale_id} and deducted stock from Food Truck.")
            st.rerun()
        except Exception as e:
            st.error(str(e))

    if b2.button("Sell qty"):
        try:
            sale_id = record_pos_sale(menu_by_name[chosen], float(qty), pay, note)
            st.success(f"Recorded sale #{sale_id} and deducted stock from Food Truck.")
            st.rerun()
        except Exception as e:
            st.error(str(e))

    if b3.button("Sell x5"):
        try:
            sale_id = record_pos_sale(menu_by_name[chosen], 5, pay, note)
            st.success(f"Recorded sale #{sale_id} and deducted stock from Food Truck.")
            st.rerun()
        except Exception as e:
            st.error(str(e))

    st.divider()
    st.subheader("Today")
    pay_summary, item_summary = get_today_sales_summary()

    colA, colB, colC = st.columns(3)
    total_today = float(pay_summary["total"].sum()) if not pay_summary.empty else 0.0
    eftpos_today = float(pay_summary.loc[pay_summary["payment_method"] == PAYMENT_EFTPOS, "total"].sum()) if not pay_summary.empty else 0.0
    cash_today = float(pay_summary.loc[pay_summary["payment_method"] == PAYMENT_CASH, "total"].sum()) if not pay_summary.empty else 0.0

    colA.metric("Total", f"${total_today:,.2f}")
    colB.metric("EFTPOS", f"${eftpos_today:,.2f}")
    colC.metric("Cash", f"${cash_today:,.2f}")

    if not item_summary.empty:
        st.dataframe(item_summary, use_container_width=True, hide_index=True)
    else:
        st.info("No sales recorded today yet.")


# ---- Event Mode ----
with tabs[1]:
    st.subheader("Event Mode (Forecast → Prep Order + Load Sheet)")

    left, right = st.columns([1.2, 1])

    with left:
        event_name = st.text_input("Event name", placeholder="e.g., Electric Ave Day 1")
        revenue = st.number_input("Target revenue (NZD)", min_value=0.0, value=10000.0, step=500.0)
        kg_per_1k = st.number_input("Chicken kg per $1,000", min_value=1.0, value=11.5, step=0.1)
        buffer_pct = st.number_input("Safety buffer (%)", min_value=0.0, value=10.0, step=1.0)

        st.markdown("### Menu mix (rough)")
        kfc = st.slider("KFC share", 0, 100, 55)
        kfc_chips = st.slider("KFC on Chips share", 0, 100, 25)
        burger = st.slider("Burger share", 0, 100, 15)
        cauli = st.slider("Cauli share", 0, 100, 3)
        chips_only = st.slider("Chips-only share", 0, 100, 2)

        total = kfc + kfc_chips + burger + cauli + chips_only
        if total == 0:
            total = 1

        mix = {
            "kfc": kfc / total,
            "kfc_chips": kfc_chips / total,
            "burger": burger / total,
            "cauli": cauli / total,
            "chips": chips_only / total,
        }

    meta, forecast = build_event_forecast(revenue, kg_per_1k, buffer_pct, mix)

    with right:
        st.markdown("### Forecast summary")
        st.write(meta)

        load_df = pd.DataFrame(
            [{"Item": k, "Qty": v} for k, v in forecast.items() if float(v) > 0],
            columns=["Item", "Qty"]
        ).sort_values("Item")

        st.markdown("### Load sheet (suggested pack list)")
        st.dataframe(load_df, use_container_width=True, hide_index=True)

        st.download_button(
            "Download load sheet (CSV)",
            data=load_df.to_csv(index=False).encode("utf-8"),
            file_name=f"load_sheet_{(event_name or 'event').replace(' ', '_')}.csv",
            mime="text/csv",
        )

        if st.button("Send forecast to Orders as a draft", type="primary"):
            set_order_df_from_forecast(forecast)
            st.success("Draft order created. Go to the Orders tab and press 'Create order'.")
            # No rerun needed; session_state is set


# ---- Dashboard ----
with tabs[2]:
    st.subheader("Stock snapshot")
    pivot = get_stock_pivot()
    if pivot.empty:
        st.info("No items yet. Add items in Items tab.")
    else:
        st.dataframe(
            pivot[["name", "unit", "par_level", LOC_TRUCK, LOC_PREP, "Below PAR?"]],
            use_container_width=True,
            hide_index=True
        )


# ---- Items ----
with tabs[3]:
    st.subheader("Items")
    items_df = get_items_df(active_only=False)

    with st.expander("Add / Update item", expanded=True):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        name = c1.text_input("Item name", placeholder="e.g., Chicken thigh diced")
        unit = c2.text_input("Unit", placeholder="kg / pcs / L")
        par = c3.number_input("PAR level (Prep)", min_value=0.0, value=0.0, step=0.5)
        price = c4.number_input("Price NZD (optional)", min_value=0.0, value=0.0, step=0.1)

        if st.button("Save item", type="primary"):
            try:
                add_item(name=name, unit=unit.strip() or "unit", par_level=float(par), price_nzd=float(price))
                st.success("Saved.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    if not items_df.empty:
        st.dataframe(items_df[["id", "name", "unit", "par_level", "price_nzd", "active"]],
                     use_container_width=True, hide_index=True)


# ---- Adjust stock ----
with tabs[4]:
    st.subheader("Adjust stock (counts, wastage, deliveries)")
    items = get_items_df(active_only=True)
    if items.empty:
        st.info("Add items first.")
    else:
        item_map = dict(zip(items["name"], items["id"]))

        c1, c2, c3, c4 = st.columns([2, 1, 1, 2])
        item_name = c1.selectbox("Item", list(item_map.keys()))
        location = c2.selectbox("Location", LOCATIONS)
        delta = c3.number_input("Delta (+ add / - remove)", value=0.0, step=0.5)
        reason = c4.text_input("Reason", placeholder="delivery / wastage / recount")

        if st.button("Apply adjustment", type="primary"):
            try:
                adjust_stock(int(item_map[item_name]), location, float(delta),
                             reason=reason.strip() or "Manual adjustment",
                             ref_type="manual", ref_id=None)
                st.success("Stock updated.")
                st.rerun()
            except Exception as e:
                st.error(str(e))


# ---- Orders (rewritten: multi-line order builder) ----
with tabs[5]:
    st.subheader("Truck → Prep Kitchen orders (multi-line)")

    items = get_items_df(active_only=True)
    if items.empty:
        st.info("Add items first.")
    else:
        item_names = list(items["name"].tolist())

        ensure_order_df()
        note = st.text_input("Order note (optional)", placeholder="e.g., Friday top-up / Event: Electric Ave Day 1")

        st.markdown("### Build your order (add as many lines as you want)")
        edited = st.data_editor(
            st.session_state["order_df"],
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "Item": st.column_config.SelectboxColumn("Item", options=item_names),
                "Qty": st.column_config.NumberColumn("Qty", min_value=0.0, step=0.5),
            },
            hide_index=True,
            key="order_editor",
        )

        st.session_state["order_df"] = edited

        c1, c2, c3 = st.columns([1, 1, 2])

        if c1.button("Clear draft"):
            st.session_state["order_df"] = pd.DataFrame([{"Item": "", "Qty": 0.0}], columns=["Item", "Qty"])
            st.rerun()

        if c2.button("Remove empty/zero lines"):
            df = st.session_state["order_df"].copy()
            df["Item"] = df["Item"].fillna("").astype(str)
            df = df[(df["Item"].str.strip() != "") & (df["Qty"].fillna(0).astype(float) > 0)]
            if df.empty:
                df = pd.DataFrame([{"Item": "", "Qty": 0.0}], columns=["Item", "Qty"])
            st.session_state["order_df"] = df
            st.rerun()

        if c3.button("Create order", type="primary"):
            try:
                df = st.session_state["order_df"].copy()
                df["Item"] = df["Item"].fillna("").astype(str)
                df["Qty"] = df["Qty"].fillna(0).astype(float)

                df = df[(df["Item"].str.strip() != "") & (df["Qty"] > 0)]
                if df.empty:
                    raise ValueError("Add at least one item with Qty > 0.")

                order_id = create_order(note=note)
                for _, r in df.iterrows():
                    item_id = get_item_id_by_name(r["Item"])
                    if not item_id:
                        raise ValueError(f"Item not found/active: {r['Item']}")
                    add_order_line(order_id, int(item_id), float(r["Qty"]))

                st.success(f"Order #{order_id} created (PENDING).")
                st.session_state["order_df"] = pd.DataFrame([{"Item": "", "Qty": 0.0}], columns=["Item", "Qty"])
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
            set_order_status(int(order_id), ORDER_STATUS_CANCELLED)
            st.success("Cancelled.")
            st.rerun()


# ---- Movements ----
with tabs[6]:
    st.subheader("Movements log (audit trail)")
    mv = get_movements_df()
    if mv.empty:
        st.info("No movements yet.")
    else:
        st.dataframe(mv, use_container_width=True, hide_index=True)
