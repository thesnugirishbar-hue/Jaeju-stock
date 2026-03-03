import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
import pandas as pd
import streamlit as st

DB_PATH = "jaeju_stock.db"

LOC_TRUCK = "Food Truck"
LOC_PREP = "Prep Kitchen"
LOCATIONS = [LOC_TRUCK, LOC_PREP]

ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_FULFILLED = "FULFILLED"
ORDER_STATUS_CANCELLED = "CANCELLED"

PAYMENT_EFTPOS = "EFTPOS"
PAYMENT_CASH = "CASH"


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

        # Core inventory
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

        # POS sales
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
            menu_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            name TEXT NOT NULL,
            qty REAL NOT NULL,
            unit_price REAL NOT NULL,
            line_total REAL NOT NULL,
            FOREIGN KEY (sale_id) REFERENCES sales(id)
        );
        """)

        # Menu + recipes (editable in-app)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS menu_recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            qty REAL NOT NULL,
            UNIQUE(menu_id, item_id),
            FOREIGN KEY (menu_id) REFERENCES menu_items(id),
            FOREIGN KEY (item_id) REFERENCES items(id)
        );
        """)

        conn.commit()


def ensure_stock_rows_for_item(item_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        for loc in LOCATIONS:
            cur.execute("INSERT OR IGNORE INTO stock(item_id, location, qty) VALUES (?, ?, 0)",
                        (item_id, loc))
        conn.commit()


def get_items_df(active_only=True):
    with get_conn() as conn:
        q = "SELECT id, name, unit, par_level, price_nzd, active FROM items"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY name COLLATE NOCASE"
        return pd.read_sql_query(q, conn)


# Upsert items (avoids UNIQUE crash)
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


def get_item_id_by_name(name: str):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM items WHERE name = ? AND active = 1", (name,))
        row = cur.fetchone()
        return int(row["id"]) if row else None


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


def get_stock_df():
    with get_conn() as conn:
        q = """
        SELECT i.id AS item_id, i.name, i.unit, i.par_level,
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
        index=["item_id", "name", "unit", "par_level"],
        columns="location",
        values="qty",
        aggfunc="sum"
    ).reset_index()
    for loc in LOCATIONS:
        if loc not in pivot.columns:
            pivot[loc] = 0.0
    pivot["Below PAR?"] = (pivot[LOC_PREP] < pivot["par_level"]).map({True: "YES", False: ""})
    return pivot


# ----------------------------
# Orders
# ----------------------------
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
        return pd.read_sql_query(
            "SELECT id, created_at, status, note FROM orders ORDER BY id DESC LIMIT ?",
            conn, params=(limit,)
        )


def get_order_lines_df(order_id: int):
    with get_conn() as conn:
        return pd.read_sql_query("""
            SELECT i.name, i.unit, ol.qty, ol.item_id
            FROM order_lines ol
            JOIN items i ON i.id = ol.item_id
            WHERE ol.order_id = ?
            ORDER BY i.name COLLATE NOCASE
        """, conn, params=(order_id,))


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

    # Check prep stock
    prep = get_stock_df()
    prep_map = {int(r["item_id"]): float(r["qty"]) for _, r in prep.iterrows() if r["location"] == LOC_PREP}

    for _, line in lines.iterrows():
        item_id = int(line["item_id"])
        qty = float(line["qty"])
        if prep_map.get(item_id, 0.0) < qty:
            raise ValueError(f"Not enough Prep stock for {line['name']} (have {prep_map.get(item_id, 0.0)}, need {qty})")

    # Move stock
    for _, line in lines.iterrows():
        item_id = int(line["item_id"])
        qty = float(line["qty"])
        adjust_stock(item_id, LOC_PREP, -qty, "Order fulfillment (Prep → Truck)", ref_type="order", ref_id=order_id)
        adjust_stock(item_id, LOC_TRUCK, +qty, "Order fulfillment (Prep → Truck)", ref_type="order", ref_id=order_id)

    set_order_status(order_id, ORDER_STATUS_FULFILLED)


def get_movements_df(limit=300):
    with get_conn() as conn:
        return pd.read_sql_query("""
            SELECT m.created_at, i.name, m.location, m.delta, m.reason, m.ref_type, m.ref_id
            FROM movements m
            JOIN items i ON i.id = m.item_id
            ORDER BY m.id DESC
            LIMIT ?
        """, conn, params=(limit,))


# ----------------------------
# Menu DB (editable)
# ----------------------------
def seed_menu_if_empty():
    """
    Creates some starter menu items if menu_items is empty.
    You can delete/rename/edit these in Menu Admin.
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM menu_items")
        if int(cur.fetchone()["c"]) > 0:
            return

        starters = [
            ("JUST_CHICKEN", "Just Chicken", 20.00, 1, 10),
            ("SMALL_CHIPS", "Small Chicken on Chips", 22.00, 1, 20),
            ("LARGE_CHIPS", "Large Chicken on Chips", 26.00, 1, 30),
            ("BURGER", "Chicken Burger", 20.00, 1, 40),
            ("CAULI", "Korean Cauli", 18.00, 1, 50),
            ("CHIPS", "Chips", 8.00, 1, 60),
        ]
        cur.executemany("""
            INSERT INTO menu_items(sku, name, price, active, sort_order)
            VALUES (?, ?, ?, ?, ?)
        """, starters)
        conn.commit()


def get_menu_items(active_only=True):
    with get_conn() as conn:
        q = "SELECT id, sku, name, price, active, sort_order FROM menu_items"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY sort_order ASC, name COLLATE NOCASE"
        return pd.read_sql_query(q, conn)


def upsert_menu_items(df: pd.DataFrame):
    with get_conn() as conn:
        cur = conn.cursor()
        for _, r in df.iterrows():
            sku = str(r["sku"]).strip()
            name = str(r["name"]).strip()
            price = float(r["price"])
            active = int(r["active"])
            sort_order = int(r.get("sort_order", 0))

            if not sku or not name:
                continue

            if pd.isna(r.get("id")):
                cur.execute("""
                    INSERT OR IGNORE INTO menu_items(sku, name, price, active, sort_order)
                    VALUES (?, ?, ?, ?, ?)
                """, (sku, name, price, active, sort_order))
            else:
                cur.execute("""
                    UPDATE menu_items
                    SET sku = ?, name = ?, price = ?, active = ?, sort_order = ?
                    WHERE id = ?
                """, (sku, name, price, active, sort_order, int(r["id"])))
        conn.commit()


def get_menu_recipe(menu_id: int):
    with get_conn() as conn:
        return pd.read_sql_query("""
            SELECT mr.id, mr.menu_id, i.id AS item_id, i.name AS item_name, mr.qty
            FROM menu_recipes mr
            JOIN items i ON i.id = mr.item_id
            WHERE mr.menu_id = ?
            ORDER BY i.name COLLATE NOCASE
        """, conn, params=(menu_id,))


def upsert_menu_recipe(menu_id: int, df: pd.DataFrame):
    """
    df columns: id(optional), item_id, qty
    Enforces unique (menu_id,item_id).
    """
    with get_conn() as conn:
        cur = conn.cursor()

        # Remove rows that are deleted in editor: easiest is to wipe and re-insert
        cur.execute("DELETE FROM menu_recipes WHERE menu_id = ?", (menu_id,))
        conn.commit()

        for _, r in df.iterrows():
            try:
                item_id = int(r["item_id"])
                qty = float(r["qty"])
            except Exception:
                continue
            if item_id <= 0 or qty == 0:
                continue
            cur.execute("""
                INSERT OR REPLACE INTO menu_recipes(menu_id, item_id, qty)
                VALUES (?, ?, ?)
            """, (menu_id, item_id, qty))
        conn.commit()


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


def add_sale_line(sale_id: int, menu_id: int, sku: str, name: str, qty: float, unit_price: float):
    line_total = float(qty) * float(unit_price)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO sale_lines(sale_id, menu_id, sku, name, qty, unit_price, line_total)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (sale_id, menu_id, sku, name, qty, unit_price, line_total))
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


def get_recipe_map(menu_id: int):
    """
    Returns dict item_id -> qty_per_menu_unit
    """
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT item_id, qty FROM menu_recipes WHERE menu_id = ?",
            conn, params=(menu_id,)
        )
    return {int(r["item_id"]): float(r["qty"]) for _, r in df.iterrows()}


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

    # Create sale + line
    sale_id = create_sale(payment_method=payment_method, note=note)
    add_sale_line(sale_id, menu_id, sku, name, float(qty), price)

    # Deduct from TRUCK
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


# ----------------------------
# Event Forecast (Revenue + mix -> qty -> ingredients)
# ----------------------------
def forecast_from_revenue(revenue_nzd: float, mix: dict, menu_df: pd.DataFrame):
    """
    mix: menu_id -> share (0..1)
    Uses qty_sold = revenue*share / price, then ingredients = sum(qty_sold*recipe).
    """
    revenue_nzd = float(revenue_nzd)
    menu_df = menu_df.copy()
    menu_df["price"] = menu_df["price"].astype(float)

    # Estimate quantities sold per menu item
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

    # Ingredients aggregation by item_id
    ing_totals = {}
    for mid, qty_est in qty_rows:
        recipe = get_recipe_map(int(mid))
        for item_id, per_unit in recipe.items():
            ing_totals[item_id] = ing_totals.get(item_id, 0.0) + (float(per_unit) * float(qty_est))

    return qty_rows, ing_totals


def ensure_order_df():
    if "order_df" not in st.session_state:
        st.session_state["order_df"] = pd.DataFrame([{"Item": "", "Qty": 0.0}], columns=["Item", "Qty"])


def set_order_df_from_item_totals(item_totals_by_name: dict):
    rows = [{"Item": k, "Qty": float(v)} for k, v in item_totals_by_name.items() if float(v) > 0]
    if not rows:
        rows = [{"Item": "", "Qty": 0.0}]
    st.session_state["order_df"] = pd.DataFrame(rows, columns=["Item", "Qty"]).sort_values("Item", ignore_index=True)


# ----------------------------
# App UI
# ----------------------------
st.set_page_config(page_title="JAEJU Ops", page_icon="jaeju-logo.jpg", layout="wide")
init_db()
seed_menu_if_empty()

st.title("JAEJU Stock + POS + Events")

tabs = st.tabs(["POS", "Event Mode", "Menu Admin", "Dashboard", "Items", "Adjust Stock", "Orders", "Movements"])

# ---- POS ----
with tabs[0]:
    st.subheader("POS (Food Truck)")

    menu = get_menu_items(active_only=True)
    if menu.empty:
        st.warning("No active menu items. Go to Menu Admin.")
    else:
        names = menu["name"].tolist()
        chosen_name = st.selectbox("Menu item", names)
        chosen = menu.loc[menu["name"] == chosen_name].iloc[0]

        c1, c2, c3 = st.columns([1, 1, 1])
        qty = c1.number_input("Qty", min_value=1.0, value=1.0, step=1.0)
        pay = c2.selectbox("Payment", [PAYMENT_EFTPOS, PAYMENT_CASH])
        note = c3.text_input("Note (optional)", placeholder="comp / staff meal")

        b1, b2 = st.columns(2)
        if b1.button("Sell x1", type="primary"):
            try:
                sale_id = record_pos_sale(chosen, 1, pay, note)
                st.success(f"Sale #{sale_id} recorded. Stock deducted from Food Truck.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

        if b2.button("Sell qty"):
            try:
                sale_id = record_pos_sale(chosen, float(qty), pay, note)
                st.success(f"Sale #{sale_id} recorded. Stock deducted from Food Truck.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    st.divider()
    st.subheader("Today")
    pay_summary, item_summary = get_today_sales_summary()
    total_today = float(pay_summary["total"].sum()) if not pay_summary.empty else 0.0
    st.metric("Total", f"${total_today:,.2f}")
    if not item_summary.empty:
        st.dataframe(item_summary, use_container_width=True, hide_index=True)
    else:
        st.info("No sales recorded today yet.")

# ---- Event Mode ----
with tabs[1]:
    st.subheader("Event Mode (Revenue → Ingredients → Draft Order + Load Sheet)")

    menu = get_menu_items(active_only=True)
    if menu.empty:
        st.warning("No active menu items. Add them in Menu Admin.")
    else:
        event_name = st.text_input("Event name", placeholder="Electric Ave Day 1")
        revenue = st.number_input("Target revenue (NZD)", min_value=0.0, value=10000.0, step=500.0)
        buffer_pct = st.number_input("Safety buffer (%)", min_value=0.0, value=10.0, step=1.0)

        st.markdown("### Menu mix")
        mix = {}
        total = 0.0
        for _, r in menu.iterrows():
            key = int(r["id"])
            default = int(100 / max(len(menu), 1))
            val = st.slider(f"{r['name']} share (%)", 0, 100, default)
            mix[key] = float(val)
            total += float(val)

        if total <= 0:
            st.warning("Set at least one menu share above 0%.")
        else:
            # normalize
            mix_norm = {k: v / total for k, v in mix.items() if v > 0}

            qty_rows, ing_totals = forecast_from_revenue(revenue, mix_norm, menu)

            # Apply buffer
            ing_totals = {k: v * (1.0 + buffer_pct / 100.0) for k, v in ing_totals.items()}

            # Convert item_id totals to names
            items = get_items_df(active_only=True)
            id_to_name = {int(r["id"]): str(r["name"]) for _, r in items.iterrows()}
            name_totals = {}
            missing_item_ids = []
            for item_id, qty_total in ing_totals.items():
                if item_id in id_to_name:
                    name_totals[id_to_name[item_id]] = qty_total
                else:
                    missing_item_ids.append(item_id)

            st.markdown("### Estimated quantities sold")
            qty_view = []
            for mid, qty_est in qty_rows:
                row = menu.loc[menu["id"] == mid].iloc[0]
                qty_view.append({"Menu item": row["name"], "Qty (est)": round(float(qty_est), 1), "Price": float(row["price"])})
            st.dataframe(pd.DataFrame(qty_view), use_container_width=True, hide_index=True)

            st.markdown("### Load sheet (ingredients)")
            load_df = pd.DataFrame(
                [{"Item": k, "Qty": round(float(v), 2)} for k, v in name_totals.items() if float(v) > 0],
                columns=["Item", "Qty"]
            ).sort_values("Item")
            st.dataframe(load_df, use_container_width=True, hide_index=True)

            st.download_button(
                "Download load sheet (CSV)",
                data=load_df.to_csv(index=False).encode("utf-8"),
                file_name=f"load_sheet_{(event_name or 'event').replace(' ', '_')}.csv",
                mime="text/csv",
            )

            if st.button("Send to Orders as draft", type="primary"):
                set_order_df_from_item_totals(name_totals)
                st.success("Draft created. Go to Orders tab and press Create order.")

            if missing_item_ids:
                st.warning("Some recipe ingredient items are not active/missing in Items (item_ids): " + ", ".join(map(str, missing_item_ids)))

# ---- Menu Admin ----
with tabs[2]:
    st.subheader("Menu Admin (edit POS menu + recipes inside the app)")

    st.info("Step 1: Edit menu items. Step 2: Click a menu item and edit its recipe.")

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
    st.markdown("### Edit recipe for a menu item")

    menu_active = get_menu_items(active_only=False)
    if menu_active.empty:
        st.warning("No menu items yet.")
    else:
        pick = st.selectbox("Choose menu item", menu_active["name"].tolist())
        picked_row = menu_active.loc[menu_active["name"] == pick].iloc[0]
        menu_id = int(picked_row["id"])

        items = get_items_df(active_only=True)
        if items.empty:
            st.warning("Add ingredient items in Items tab first (e.g., Chicken thigh diced, Flour, etc.).")
        else:
            # Current recipe
            recipe_df = get_menu_recipe(menu_id)
            # Build editor df: item_id + qty
            if recipe_df.empty:
                edit_df = pd.DataFrame([{"item_id": int(items.iloc[0]["id"]), "qty": 0.0}], columns=["item_id", "qty"])
            else:
                edit_df = recipe_df[["item_id", "qty"]].copy()

            id_to_name = {int(r["id"]): str(r["name"]) for _, r in items.iterrows()}
            options = list(id_to_name.keys())

            st.markdown("Add ingredient lines (per 1 menu item sold). Example: Chicken thigh diced = 0.30 (kg)")
            edited_recipe = st.data_editor(
                edit_df,
                num_rows="dynamic",
                use_container_width=True,
                hide_index=True,
                column_config={
                    "item_id": st.column_config.SelectboxColumn("Ingredient item", options=options, format_func=lambda x: id_to_name.get(int(x), str(x))),
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

# ---- Dashboard ----
with tabs[3]:
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
with tabs[4]:
    st.subheader("Items")
    with st.expander("Add / Update item", expanded=True):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        name = c1.text_input("Item name", placeholder="Chicken thigh diced")
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

    items_df = get_items_df(active_only=False)
    if not items_df.empty:
        st.dataframe(items_df, use_container_width=True, hide_index=True)

# ---- Adjust stock ----
with tabs[5]:
    st.subheader("Adjust stock")
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

# ---- Orders ----
with tabs[6]:
    st.subheader("Truck → Prep Kitchen orders (multi-line)")

    items = get_items_df(active_only=True)
    if items.empty:
        st.info("Add items first.")
    else:
        item_names = list(items["name"].tolist())
        ensure_order_df()

        note = st.text_input("Order note (optional)", placeholder="Friday top-up / Event name")

        edited = st.data_editor(
            st.session_state["order_df"],
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            column_config={
                "Item": st.column_config.SelectboxColumn("Item", options=item_names),
                "Qty": st.column_config.NumberColumn("Qty", min_value=0.0, step=0.5),
            },
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
            df["Qty"] = df["Qty"].fillna(0).astype(float)
            df = df[(df["Item"].str.strip() != "") & (df["Qty"] > 0)]
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
with tabs[7]:
    st.subheader("Movements log")
    mv = get_movements_df()
    if mv.empty:
        st.info("No movements yet.")
    else:
        st.dataframe(mv, use_container_width=True, hide_index=True)
