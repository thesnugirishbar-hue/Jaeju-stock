import os
from contextlib import contextmanager
from datetime import datetime, date

import pandas as pd
import streamlit as st
import psycopg

# ---------------------------
# CONFIG
# ---------------------------
LOC_TRUCK = "Food Truck"
LOC_PREP = "Prep Kitchen"
LOCATIONS = [LOC_TRUCK, LOC_PREP]

ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_FULFILLED = "FULFILLED"
ORDER_STATUS_CANCELLED = "CANCELLED"

PAYMENT_EFTPOS = "EFTPOS"
PAYMENT_CASH = "CASH"

PAGES = ["POS", "Event Mode", "Orders", "Dashboard", "Adjust Stock", "Menu Admin", "Items", "Movements"]


# ---------------------------
# TIME HELPERS
# ---------------------------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today_str() -> str:
    return date.today().isoformat()


# ---------------------------
# DB / CONNECTION
# ---------------------------
def build_dsn() -> str:
    """
    Supports:
      - Secrets: DATABASE_URL
      - Env: DATABASE_URL
      - Secrets: [db] host/port/dbname/user/password/sslmode
    """
    # 1) DATABASE_URL in Streamlit secrets
    if "DATABASE_URL" in st.secrets:
        return str(st.secrets["DATABASE_URL"]).strip()

    # 2) DATABASE_URL in env
    env_url = os.environ.get("DATABASE_URL", "").strip()
    if env_url:
        return env_url

    # 3) [db] block in secrets
    if "db" in st.secrets:
        db = st.secrets["db"]
        missing = [k for k in ["host", "port", "dbname", "user", "password"] if k not in db]
        if missing:
            raise ValueError(f"Secrets [db] is missing keys: {missing}")
        sslmode = db.get("sslmode", "require")
        return (
            f"host={db['host']} port={db['port']} dbname={db['dbname']} "
            f"user={db['user']} password={db['password']} sslmode={sslmode}"
        )

    raise ValueError(
        "Missing DB secrets. Add either DATABASE_URL or a [db] block in Streamlit Secrets."
    )


@contextmanager
def get_conn():
    """
    Open a short-lived connection. This is safest on Streamlit Cloud.
    """
    dsn = build_dsn()
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def exec_sql(sql: str, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())


def exec_many(sql: str, rows: list[tuple]):
    if not rows:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)


@st.cache_data(ttl=2, show_spinner=False)
def query_df(sql: str, params=None) -> pd.DataFrame:
    """
    Cached reads to keep UI fast and avoid repeated queries.
    TTL is low so updates show quickly.
    """
    with get_conn() as conn:
        return pd.read_sql(sql, conn, params=params)


def invalidate_cache():
    query_df.clear()


# ---------------------------
# DB INIT (POSTGRES SAFE)
# ---------------------------
def init_db():
    """
    Postgres-safe schema. Uses SERIAL, NUMERIC, BOOLEAN.
    """
    exec_sql(
        """
        CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            unit TEXT NOT NULL DEFAULT 'unit',
            par_level NUMERIC NOT NULL DEFAULT 0,
            price_nzd NUMERIC NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT TRUE
        );

        CREATE TABLE IF NOT EXISTS stock (
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            location TEXT NOT NULL,
            qty NUMERIC NOT NULL DEFAULT 0,
            PRIMARY KEY (item_id, location)
        );

        CREATE TABLE IF NOT EXISTS movements (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
            location TEXT NOT NULL,
            delta NUMERIC NOT NULL,
            reason TEXT NOT NULL,
            ref_type TEXT,
            ref_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            from_location TEXT NOT NULL,
            to_location TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT
        );

        CREATE TABLE IF NOT EXISTS order_lines (
            id SERIAL PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            item_id INTEGER NOT NULL REFERENCES items(id),
            qty NUMERIC NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sales (
            id SERIAL PRIMARY KEY,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            sale_date DATE NOT NULL DEFAULT CURRENT_DATE,
            payment_method TEXT NOT NULL,
            note TEXT
        );

        CREATE TABLE IF NOT EXISTS sale_lines (
            id SERIAL PRIMARY KEY,
            sale_id INTEGER NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
            menu_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            name TEXT NOT NULL,
            qty NUMERIC NOT NULL,
            unit_price NUMERIC NOT NULL,
            line_total NUMERIC NOT NULL
        );

        CREATE TABLE IF NOT EXISTS menu_items (
            id SERIAL PRIMARY KEY,
            sku TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            price NUMERIC NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            sort_order INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS menu_recipes (
            id SERIAL PRIMARY KEY,
            menu_id INTEGER NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
            item_id INTEGER NOT NULL REFERENCES items(id),
            qty NUMERIC NOT NULL,
            UNIQUE(menu_id, item_id)
        );
        """
    )


def seed_menu_if_empty():
    df = query_df("SELECT COUNT(*) AS c FROM menu_items;")
    if int(df.iloc[0]["c"]) > 0:
        return

    starters = [
        ("JUST_CHICKEN", "Just Chicken", 20.00, True, 10),
        ("SMALL_CHIPS", "Small Chicken on Chips", 22.00, True, 20),
        ("LARGE_CHIPS", "Large Chicken on Chips", 26.00, True, 30),
        ("BURGER", "Chicken Burger", 20.00, True, 40),
        ("CAULI", "Korean Cauli", 18.00, True, 50),
        ("CHIPS", "Chips", 8.00, True, 60),
    ]
    exec_many(
        """
        INSERT INTO menu_items (sku, name, price, active, sort_order)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (sku) DO NOTHING;
        """,
        starters,
    )
    invalidate_cache()


def ensure_stock_rows_for_item(item_id: int):
    rows = [(item_id, loc, 0) for loc in LOCATIONS]
    exec_many(
        """
        INSERT INTO stock (item_id, location, qty)
        VALUES (%s, %s, %s)
        ON CONFLICT (item_id, location) DO NOTHING;
        """,
        rows,
    )
    invalidate_cache()


# ---------------------------
# ITEMS + STOCK
# ---------------------------
def get_items_df(active_only=True) -> pd.DataFrame:
    if active_only:
        return query_df(
            """
            SELECT id, name, unit, par_level::float AS par_level, price_nzd::float AS price_nzd, active
            FROM items
            WHERE active = TRUE
            ORDER BY name;
            """
        )
    return query_df(
        """
        SELECT id, name, unit, par_level::float AS par_level, price_nzd::float AS price_nzd, active
        FROM items
        ORDER BY name;
        """
    )


def add_item(name: str, unit: str, par_level: float, price_nzd: float, active: bool = True):
    name = (name or "").strip()
    if not name:
        raise ValueError("Item name cannot be empty.")

    exec_sql(
        """
        INSERT INTO items (name, unit, par_level, price_nzd, active)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (name)
        DO UPDATE SET unit=EXCLUDED.unit, par_level=EXCLUDED.par_level, price_nzd=EXCLUDED.price_nzd, active=EXCLUDED.active;
        """,
        (name, unit or "unit", float(par_level), float(price_nzd), bool(active)),
    )

    # get id
    df = query_df("SELECT id FROM items WHERE name = %s;", (name,))
    item_id = int(df.iloc[0]["id"])
    ensure_stock_rows_for_item(item_id)
    invalidate_cache()


def get_item_id_by_name(name: str):
    df = query_df("SELECT id FROM items WHERE name=%s AND active=TRUE;", (name,))
    if df.empty:
        return None
    return int(df.iloc[0]["id"])


def adjust_stock(item_id: int, location: str, delta: float, reason: str, ref_type=None, ref_id=None):
    if location not in LOCATIONS:
        raise ValueError("Invalid location.")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Reason is required.")

    ensure_stock_rows_for_item(item_id)

    exec_sql(
        """
        UPDATE stock
        SET qty = qty + %s
        WHERE item_id = %s AND location = %s;
        """,
        (float(delta), int(item_id), location),
    )

    exec_sql(
        """
        INSERT INTO movements (item_id, location, delta, reason, ref_type, ref_id)
        VALUES (%s, %s, %s, %s, %s, %s);
        """,
        (int(item_id), location, float(delta), reason, ref_type, ref_id),
    )
    invalidate_cache()


def get_stock_df() -> pd.DataFrame:
    return query_df(
        """
        SELECT
            i.id AS item_id,
            i.name,
            i.unit,
            i.par_level::float AS par_level,
            s.location,
            s.qty::float AS qty
        FROM stock s
        JOIN items i ON i.id = s.item_id
        WHERE i.active = TRUE
        ORDER BY i.name, s.location;
        """
    )


def get_stock_pivot() -> pd.DataFrame:
    df = get_stock_df()
    if df.empty:
        return df
    pivot = df.pivot_table(
        index=["item_id", "name", "unit", "par_level"],
        columns="location",
        values="qty",
        aggfunc="sum",
        fill_value=0.0,
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
    df = query_df(
        """
        INSERT INTO orders (from_location, to_location, status, note)
        VALUES (%s, %s, %s, %s)
        RETURNING id;
        """,
        (LOC_TRUCK, LOC_PREP, ORDER_STATUS_PENDING, (note or "").strip()),
    )
    invalidate_cache()
    return int(df.iloc[0]["id"])


def add_order_line(order_id: int, item_id: int, qty: float):
    if float(qty) <= 0:
        raise ValueError("Qty must be > 0.")
    exec_sql(
        """
        INSERT INTO order_lines (order_id, item_id, qty)
        VALUES (%s, %s, %s);
        """,
        (int(order_id), int(item_id), float(qty)),
    )
    invalidate_cache()


def get_orders_df(limit=100) -> pd.DataFrame:
    return query_df(
        """
        SELECT id, created_at::text AS created_at, status, COALESCE(note,'') AS note
        FROM orders
        ORDER BY id DESC
        LIMIT %s;
        """,
        (int(limit),),
    )


def get_order_lines_df(order_id: int) -> pd.DataFrame:
    return query_df(
        """
        SELECT i.name, i.unit, ol.qty::float AS qty, ol.item_id
        FROM order_lines ol
        JOIN items i ON i.id = ol.item_id
        WHERE ol.order_id = %s
        ORDER BY i.name;
        """,
        (int(order_id),),
    )


def set_order_status(order_id: int, status: str):
    exec_sql("UPDATE orders SET status=%s WHERE id=%s;", (status, int(order_id)))
    invalidate_cache()


def fulfill_order(order_id: int):
    # Must be pending
    s = query_df("SELECT status FROM orders WHERE id=%s;", (int(order_id),))
    if s.empty:
        raise ValueError("Order not found.")
    if str(s.iloc[0]["status"]) != ORDER_STATUS_PENDING:
        raise ValueError(f"Order must be PENDING to fulfill (currently {s.iloc[0]['status']}).")

    lines = get_order_lines_df(order_id)
    if lines.empty:
        raise ValueError("Order has no lines.")

    prep = get_stock_df()
    prep_map = {int(r["item_id"]): float(r["qty"]) for _, r in prep.iterrows() if r["location"] == LOC_PREP}

    for _, line in lines.iterrows():
        item_id = int(line["item_id"])
        qty = float(line["qty"])
        have = prep_map.get(item_id, 0.0)
        if have < qty:
            raise ValueError(f"Not enough Prep stock for {line['name']} (have {have}, need {qty})")

    for _, line in lines.iterrows():
        item_id = int(line["item_id"])
        qty = float(line["qty"])
        adjust_stock(item_id, LOC_PREP, -qty, "Order fulfillment (Prep → Truck)", ref_type="order", ref_id=int(order_id))
        adjust_stock(item_id, LOC_TRUCK, +qty, "Order fulfillment (Prep → Truck)", ref_type="order", ref_id=int(order_id))

    set_order_status(order_id, ORDER_STATUS_FULFILLED)
    invalidate_cache()


def get_movements_df(limit=300) -> pd.DataFrame:
    return query_df(
        """
        SELECT m.created_at::text AS created_at, i.name, m.location, m.delta::float AS delta,
               m.reason, COALESCE(m.ref_type,'') AS ref_type, COALESCE(m.ref_id::text,'') AS ref_id
        FROM movements m
        JOIN items i ON i.id = m.item_id
        ORDER BY m.id DESC
        LIMIT %s;
        """,
        (int(limit),),
    )


# ---------------------------
# MENU + RECIPES
# ---------------------------
def get_menu_items(active_only=True) -> pd.DataFrame:
    if active_only:
        return query_df(
            """
            SELECT id, sku, name, price::float AS price, active, sort_order
            FROM menu_items
            WHERE active = TRUE
            ORDER BY sort_order, name;
            """
        )
    return query_df(
        """
        SELECT id, sku, name, price::float AS price, active, sort_order
        FROM menu_items
        ORDER BY sort_order, name;
        """
    )


def upsert_menu_items(df: pd.DataFrame):
    rows = []
    for _, r in df.iterrows():
        sku = str(r.get("sku", "")).strip()
        name = str(r.get("name", "")).strip()
        if not sku or not name:
            continue
        price = float(r.get("price", 0.0))
        active = bool(r.get("active", True))
        sort_order = int(r.get("sort_order", 0))
        rid = r.get("id", None)
        if pd.isna(rid):
            rid = None
        rows.append((rid, sku, name, price, active, sort_order))

    with get_conn() as conn:
        with conn.cursor() as cur:
            for rid, sku, name, price, active, sort_order in rows:
                if rid is None:
                    cur.execute(
                        """
                        INSERT INTO menu_items (sku, name, price, active, sort_order)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (sku)
                        DO UPDATE SET name=EXCLUDED.name, price=EXCLUDED.price, active=EXCLUDED.active, sort_order=EXCLUDED.sort_order;
                        """,
                        (sku, name, price, active, sort_order),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE menu_items
                        SET sku=%s, name=%s, price=%s, active=%s, sort_order=%s
                        WHERE id=%s;
                        """,
                        (sku, name, price, active, sort_order, int(rid)),
                    )
    invalidate_cache()


def get_menu_recipe(menu_id: int) -> pd.DataFrame:
    return query_df(
        """
        SELECT mr.id, mr.menu_id, mr.item_id, i.name AS item_name, mr.qty::float AS qty
        FROM menu_recipes mr
        JOIN items i ON i.id = mr.item_id
        WHERE mr.menu_id = %s
        ORDER BY i.name;
        """,
        (int(menu_id),),
    )


def upsert_menu_recipe(menu_id: int, df: pd.DataFrame):
    # wipe + reinsert (reliable + simple)
    exec_sql("DELETE FROM menu_recipes WHERE menu_id=%s;", (int(menu_id),))

    rows = []
    for _, r in df.iterrows():
        try:
            item_id = int(r["item_id"])
            qty = float(r["qty"])
        except Exception:
            continue
        if item_id <= 0 or qty <= 0:
            continue
        rows.append((int(menu_id), item_id, qty))

    exec_many(
        """
        INSERT INTO menu_recipes (menu_id, item_id, qty)
        VALUES (%s, %s, %s)
        ON CONFLICT (menu_id, item_id)
        DO UPDATE SET qty=EXCLUDED.qty;
        """,
        rows,
    )
    invalidate_cache()


def get_recipe_map(menu_id: int) -> dict[int, float]:
    df = query_df("SELECT item_id, qty::float AS qty FROM menu_recipes WHERE menu_id=%s;", (int(menu_id),))
    return {int(r["item_id"]): float(r["qty"]) for _, r in df.iterrows()}


# ---------------------------
# SALES / POS
# ---------------------------
def create_sale(payment_method: str, note: str = "") -> int:
    df = query_df(
        """
        INSERT INTO sales (sale_date, payment_method, note)
        VALUES (%s, %s, %s)
        RETURNING id;
        """,
        (today_str(), payment_method, (note or "").strip()),
    )
    invalidate_cache()
    return int(df.iloc[0]["id"])


def add_sale_line(sale_id: int, menu_id: int, sku: str, name: str, qty: float, unit_price: float):
    line_total = float(qty) * float(unit_price)
    exec_sql(
        """
        INSERT INTO sale_lines (sale_id, menu_id, sku, name, qty, unit_price, line_total)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
        """,
        (int(sale_id), int(menu_id), sku, name, float(qty), float(unit_price), float(line_total)),
    )
    invalidate_cache()


def get_today_sales_summary():
    pay = query_df(
        """
        SELECT s.sale_date::text AS sale_date, s.payment_method, SUM(sl.line_total)::float AS total
        FROM sales s
        JOIN sale_lines sl ON sl.sale_id = s.id
        WHERE s.sale_date = %s
        GROUP BY s.sale_date, s.payment_method
        ORDER BY s.payment_method;
        """,
        (today_str(),),
    )

    lines = query_df(
        """
        SELECT sl.sku, sl.name, SUM(sl.qty)::float AS qty, SUM(sl.line_total)::float AS total
        FROM sales s
        JOIN sale_lines sl ON sl.sale_id = s.id
        WHERE s.sale_date = %s
        GROUP BY sl.sku, sl.name
        ORDER BY total DESC;
        """,
        (today_str(),),
    )
    return pay, lines


def record_pos_sale(menu_row: pd.Series, qty: float, payment_method: str, note: str = ""):
    if float(qty) <= 0:
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
    invalidate_cache()
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
# Orders draft state (mobile friendly)
# ---------------------------
def ensure_order_lines_state():
    if "order_lines" not in st.session_state:
        st.session_state["order_lines"] = []


def add_to_order_draft(item_name: str, qty: float):
    ensure_order_lines_state()
    if float(qty) <= 0:
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
# UI
# ---------------------------
st.set_page_config(page_title="JAEJU Ops (Postgres)", page_icon="🍗", layout="wide")
st.title("JAEJU Stock + POS + Events (Postgres)")

# Connection banner
with st.expander("🔧 DB connection", expanded=False):
    try:
        dsn = build_dsn()
        st.success("DB secrets detected.")
        st.write("Using DATABASE_URL:", "DATABASE_URL" in st.secrets or bool(os.environ.get("DATABASE_URL")))
    except Exception as e:
        st.error(str(e))

# Init DB
init_db()
seed_menu_if_empty()

mobile_mode = st.toggle("Mobile mode", value=True)
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
                set_order_status(int(order_id), ORDER_STATUS_CANCELLED)
                st.success("Cancelled.")
                st.rerun()


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
                    adjust_stock(
                        int(item_map[item_name]),
                        location,
                        float(delta),
                        reason=reason.strip() or "Manual adjustment",
                        ref_type="manual",
                        ref_id=None,
                    )
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
                edit_df = recipe_df[["item_id", "qty"]].copy() if not recipe_df.empty else pd.DataFrame(
                    [{"item_id": int(items.iloc[0]["id"]), "qty": 0.0}],
                    columns=["item_id", "qty"],
                )

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
                    add_item(name=name, unit=unit.strip() or "unit", par_level=float(par), price_nzd=float(price), active=active)
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
import socket
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# ---------------------------
# CONFIG
# ---------------------------
LOC_TRUCK = "Food Truck"
LOC_PREP = "Prep Kitchen"
LOCATIONS = [LOC_TRUCK, LOC_PREP]

ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_FULFILLED = "FULFILLED"
ORDER_STATUS_CANCELLED = "CANCELLED"

PAYMENT_EFTPOS = "EFTPOS"
PAYMENT_CASH = "CASH"

APP_TITLE = "JAEJU Ops (Postgres)"

# ---------------------------
# TIME HELPERS
# ---------------------------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def today_str() -> str:
    return date.today().isoformat()

# ---------------------------
# SAFE CASTS
# ---------------------------
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

# ---------------------------
# DATABASE (Postgres via psycopg)
# ---------------------------
def _build_dsn_from_secrets() -> str:
    """
    Supports either:
      - st.secrets["DATABASE_URL"]
      - or [db] host/port/dbname/user/password/sslmode
    Also forces sslmode=require if missing.
    """
    if "DATABASE_URL" in st.secrets:
        dsn = st.secrets["DATABASE_URL"]
    elif "db" in st.secrets:
        db = st.secrets["db"]
        required = ["host", "port", "dbname", "user", "password", "sslmode"]
        missing = [k for k in required if k not in db]
        if missing:
            raise ValueError(f"Secrets [db] is missing keys: {missing}")
        dsn = (
            f"postgresql://{db['user']}:{db['password']}"
            f"@{db['host']}:{db['port']}/{db['dbname']}"
            f"?sslmode={db['sslmode']}"
        )
    else:
        raise ValueError(
            "Missing DATABASE_URL (or [db] block) in Streamlit Secrets.\n"
            "Manage app → Secrets → add DATABASE_URL = \"postgresql://...\""
        )

    # Ensure sslmode=require exists
    try:
        u = urlparse(dsn)
        q = parse_qs(u.query)
        if "sslmode" not in q:
            q["sslmode"] = ["require"]
            u = u._replace(query=urlencode(q, doseq=True))
            dsn = urlunparse(u)
    except Exception:
        # If parsing fails, leave as-is
        pass

    return dsn

def _prefer_ipv4_hostaddr(dsn: str) -> str:
    """
    Streamlit Cloud can fail on IPv6-only resolution.
    This converts the hostname to an IPv4 hostaddr=... in the conninfo string.
    psycopg accepts "host=... hostaddr=...".
    """
    u = urlparse(dsn)
    host = u.hostname
    if not host:
        return dsn

    ipv4 = None
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        if infos:
            ipv4 = infos[0][4][0]
    except Exception:
        ipv4 = None

    if not ipv4:
        return dsn  # fallback (maybe IPv6 works in your env)

    # Reconstruct a safe conninfo string
    user = u.username or ""
    password = u.password or ""
    port = u.port or 5432
    dbname = (u.path or "").lstrip("/") or "postgres"
    q = parse_qs(u.query)
    sslmode = (q.get("sslmode", ["require"])[0]) if q else "require"

    # psycopg conninfo format (space-separated key=val)
    # Note: password may contain special chars; conninfo can handle it as-is.
    conninfo = (
        f"host={host} hostaddr={ipv4} port={port} dbname={dbname} "
        f"user={user} password={password} sslmode={sslmode}"
    )
    return conninfo

@st.cache_resource
def _dsn() -> str:
    raw = _build_dsn_from_secrets()
    return _prefer_ipv4_hostaddr(raw)

@contextmanager
def get_conn():
    conn = psycopg.connect(_dsn(), autocommit=False)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def exec_sql(sql: str, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur

def fetch_df(sql: str, params=None) -> pd.DataFrame:
    with get_conn() as conn:
        return pd.read_sql_query(sql, conn, params=params)

def clear_caches():
    # Clear cached reads after writes so UI updates immediately
    try:
        read_items_cached.clear()
        read_stock_cached.clear()
        read_menu_cached.clear()
        read_menu_recipes_cached.clear()
        read_orders_cached.clear()
        read_order_lines_cached.clear()
        read_movements_cached.clear()
        read_sales_cached.clear()
        read_sale_lines_cached.clear()
    except Exception:
        pass

# ---------------------------
# INIT DB (tables)
# ---------------------------
def init_db():
    # Use BIGSERIAL to avoid manual ID management
    exec_sql("""
    CREATE TABLE IF NOT EXISTS items (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        unit TEXT NOT NULL,
        par_level DOUBLE PRECISION DEFAULT 0,
        price_nzd DOUBLE PRECISION DEFAULT 0,
        active BOOLEAN DEFAULT TRUE
    );

    CREATE TABLE IF NOT EXISTS stock (
        item_id BIGINT NOT NULL REFERENCES items(id),
        location TEXT NOT NULL,
        qty DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY (item_id, location)
    );

    CREATE TABLE IF NOT EXISTS movements (
        id BIGSERIAL PRIMARY KEY,
        created_at TEXT NOT NULL,
        item_id BIGINT NOT NULL REFERENCES items(id),
        location TEXT NOT NULL,
        delta DOUBLE PRECISION NOT NULL,
        reason TEXT NOT NULL,
        ref_type TEXT,
        ref_id BIGINT
    );

    CREATE TABLE IF NOT EXISTS orders (
        id BIGSERIAL PRIMARY KEY,
        created_at TEXT NOT NULL,
        from_location TEXT NOT NULL,
        to_location TEXT NOT NULL,
        status TEXT NOT NULL,
        note TEXT
    );

    CREATE TABLE IF NOT EXISTS order_lines (
        id BIGSERIAL PRIMARY KEY,
        order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        item_id BIGINT NOT NULL REFERENCES items(id),
        qty DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sales (
        id BIGSERIAL PRIMARY KEY,
        created_at TEXT NOT NULL,
        sale_date TEXT NOT NULL,
        payment_method TEXT NOT NULL,
        note TEXT
    );

    CREATE TABLE IF NOT EXISTS sale_lines (
        id BIGSERIAL PRIMARY KEY,
        sale_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
        menu_id BIGINT NOT NULL,
        sku TEXT NOT NULL,
        name TEXT NOT NULL,
        qty DOUBLE PRECISION NOT NULL,
        unit_price DOUBLE PRECISION NOT NULL,
        line_total DOUBLE PRECISION NOT NULL
    );

    CREATE TABLE IF NOT EXISTS menu_items (
        id BIGSERIAL PRIMARY KEY,
        sku TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        price DOUBLE PRECISION NOT NULL DEFAULT 0,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order BIGINT NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS menu_recipes (
        id BIGSERIAL PRIMARY KEY,
        menu_id BIGINT NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
        item_id BIGINT NOT NULL REFERENCES items(id),
        qty DOUBLE PRECISION NOT NULL,
        UNIQUE(menu_id, item_id)
    );
    """)

# ---------------------------
# CACHED READS (keeps app fast)
# ---------------------------
@st.cache_data(ttl=3)
def read_items_cached(active_only: bool) -> pd.DataFrame:
    q = "SELECT id, name, unit, par_level, price_nzd, active FROM items"
    if active_only:
        q += " WHERE active = TRUE"
    q += " ORDER BY LOWER(name)"
    return fetch_df(q)

@st.cache_data(ttl=3)
def read_stock_cached() -> pd.DataFrame:
    return fetch_df("""
        SELECT s.item_id, i.name, i.unit, i.par_level, s.location, s.qty
        FROM stock s
        JOIN items i ON i.id = s.item_id
        WHERE i.active = TRUE
        ORDER BY LOWER(i.name), s.location
    """)

@st.cache_data(ttl=3)
def read_menu_cached(active_only: bool) -> pd.DataFrame:
    q = "SELECT id, sku, name, price, active, sort_order FROM menu_items"
    if active_only:
        q += " WHERE active = TRUE"
    q += " ORDER BY sort_order ASC, LOWER(name)"
    return fetch_df(q)

@st.cache_data(ttl=3)
def read_menu_recipes_cached(menu_id: int) -> pd.DataFrame:
    return fetch_df("""
        SELECT mr.id, mr.menu_id, mr.item_id, i.name AS item_name, mr.qty
        FROM menu_recipes mr
        JOIN items i ON i.id = mr.item_id
        WHERE mr.menu_id = %s
        ORDER BY LOWER(i.name)
    """, (menu_id,))

@st.cache_data(ttl=3)
def read_orders_cached(limit: int) -> pd.DataFrame:
    return fetch_df("""
        SELECT id, created_at, status, note
        FROM orders
        ORDER BY id DESC
        LIMIT %s
    """, (limit,))

@st.cache_data(ttl=3)
def read_order_lines_cached(order_id: int) -> pd.DataFrame:
    return fetch_df("""
        SELECT i.name, i.unit, ol.qty, ol.item_id
        FROM order_lines ol
        JOIN items i ON i.id = ol.item_id
        WHERE ol.order_id = %s
        ORDER BY LOWER(i.name)
    """, (order_id,))

@st.cache_data(ttl=3)
def read_movements_cached(limit: int) -> pd.DataFrame:
    return fetch_df("""
        SELECT m.created_at, i.name, m.location, m.delta, m.reason, m.ref_type, m.ref_id
        FROM movements m
        JOIN items i ON i.id = m.item_id
        ORDER BY m.id DESC
        LIMIT %s
    """, (limit,))

@st.cache_data(ttl=3)
def read_sales_cached() -> pd.DataFrame:
    return fetch_df("SELECT id, sale_date, payment_method FROM sales")

@st.cache_data(ttl=3)
def read_sale_lines_cached() -> pd.DataFrame:
    return fetch_df("SELECT sale_id, sku, name, qty, line_total FROM sale_lines")

# ---------------------------
# BUSINESS LOGIC
# ---------------------------
def ensure_stock_rows_for_item(item_id: int):
    for loc in LOCATIONS:
        exec_sql("""
            INSERT INTO stock(item_id, location, qty)
            VALUES (%s, %s, 0)
            ON CONFLICT (item_id, location) DO NOTHING
        """, (item_id, loc))

def get_items_df(active_only=True):
    return read_items_cached(active_only)

def add_item(name: str, unit: str, par_level: float, price_nzd: float, active: bool = True):
    name = name.strip()
    if not name:
        raise ValueError("Item name cannot be empty.")
    unit = unit.strip() or "unit"

    # Upsert by name
    row = fetch_df("SELECT id FROM items WHERE name = %s", (name,))
    if row.empty:
        exec_sql("""
            INSERT INTO items(name, unit, par_level, price_nzd, active)
            VALUES (%s, %s, %s, %s, %s)
        """, (name, unit, float(par_level), float(price_nzd), bool(active)))
        item_id = int(fetch_df("SELECT id FROM items WHERE name = %s", (name,)).iloc[0]["id"])
    else:
        item_id = int(row.iloc[0]["id"])
        exec_sql("""
            UPDATE items
            SET unit=%s, par_level=%s, price_nzd=%s, active=%s
            WHERE id=%s
        """, (unit, float(par_level), float(price_nzd), bool(active), item_id))

    ensure_stock_rows_for_item(item_id)
    clear_caches()

def get_item_id_by_name(name: str):
    row = fetch_df("SELECT id FROM items WHERE name = %s AND active = TRUE", (name,))
    if row.empty:
        return None
    return int(row.iloc[0]["id"])

def adjust_stock(item_id: int, location: str, delta: float, reason: str, ref_type=None, ref_id=None):
    if location not in LOCATIONS:
        raise ValueError("Invalid location.")
    if not reason.strip():
        raise ValueError("Reason is required.")

    ensure_stock_rows_for_item(item_id)

    exec_sql("""
        UPDATE stock
        SET qty = qty + %s
        WHERE item_id = %s AND location = %s
    """, (float(delta), int(item_id), location))

    exec_sql("""
        INSERT INTO movements(created_at, item_id, location, delta, reason, ref_type, ref_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (now_iso(), int(item_id), location, float(delta), reason, ref_type, ref_id))

    clear_caches()

def get_stock_df():
    return read_stock_cached()

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

# Orders
def create_order(note: str = "") -> int:
    exec_sql("""
        INSERT INTO orders(created_at, from_location, to_location, status, note)
        VALUES (%s, %s, %s, %s, %s)
    """, (now_iso(), LOC_TRUCK, LOC_PREP, ORDER_STATUS_PENDING, note.strip()))
    oid = int(fetch_df("SELECT MAX(id) AS id FROM orders").iloc[0]["id"])
    clear_caches()
    return oid

def add_order_line(order_id: int, item_id: int, qty: float):
    if qty <= 0:
        raise ValueError("Qty must be > 0.")
    exec_sql("""
        INSERT INTO order_lines(order_id, item_id, qty)
        VALUES (%s, %s, %s)
    """, (int(order_id), int(item_id), float(qty)))
    clear_caches()

def get_orders_df(limit=100):
    return read_orders_cached(limit)

def get_order_lines_df(order_id: int):
    return read_order_lines_cached(int(order_id))

def set_order_status(order_id: int, status: str):
    exec_sql("UPDATE orders SET status=%s WHERE id=%s", (status, int(order_id)))
    clear_caches()

def fulfill_order(order_id: int):
    # Must be pending
    row = fetch_df("SELECT status FROM orders WHERE id=%s", (int(order_id),))
    if row.empty:
        raise ValueError("Order not found.")
    if row.iloc[0]["status"] != ORDER_STATUS_PENDING:
        raise ValueError(f"Order must be PENDING to fulfill (currently {row.iloc[0]['status']}).")

    lines = get_order_lines_df(order_id)
    if lines.empty:
        raise ValueError("Order has no lines.")

    prep = get_stock_df()
    prep_map = {
        int(r["item_id"]): float(r["qty"])
        for _, r in prep.iterrows()
        if r["location"] == LOC_PREP
    }

    for _, line in lines.iterrows():
        item_id = int(line["item_id"])
        qty = float(line["qty"])
        have = prep_map.get(item_id, 0.0)
        if have < qty:
            raise ValueError(f"Not enough Prep stock for {line['name']} (have {have}, need {qty})")

    for _, line in lines.iterrows():
        item_id = int(line["item_id"])
        qty = float(line["qty"])
        adjust_stock(item_id, LOC_PREP, -qty, "Order fulfillment (Prep → Truck)", ref_type="order", ref_id=order_id)
        adjust_stock(item_id, LOC_TRUCK, +qty, "Order fulfillment (Prep → Truck)", ref_type="order", ref_id=order_id)

    set_order_status(order_id, ORDER_STATUS_FULFILLED)
    clear_caches()

def get_movements_df(limit=300):
    return read_movements_cached(limit)

# Menu
def seed_menu_if_empty():
    df = read_menu_cached(active_only=False)
    if not df.empty:
        return
    starters = [
        ("JUST_CHICKEN", "Just Chicken", 20.00, True, 10),
        ("SMALL_CHIPS", "Small Chicken on Chips", 22.00, True, 20),
        ("LARGE_CHIPS", "Large Chicken on Chips", 26.00, True, 30),
        ("BURGER", "Chicken Burger", 20.00, True, 40),
        ("CAULI", "Korean Cauli", 18.00, True, 50),
        ("CHIPS", "Chips", 8.00, True, 60),
    ]
    for sku, name, price, active, sort_order in starters:
        exec_sql("""
            INSERT INTO menu_items(sku, name, price, active, sort_order)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (sku) DO NOTHING
        """, (sku, name, float(price), bool(active), int(sort_order)))
    clear_caches()

def get_menu_items(active_only=True):
    return read_menu_cached(active_only)

def upsert_menu_items(df: pd.DataFrame):
    for _, r in df.iterrows():
        sku = str(r.get("sku", "")).strip()
        name = str(r.get("name", "")).strip()
        if not sku or not name:
            continue
        price = float(r.get("price", 0.0))
        active = bool(r.get("active", True))
        sort_order = int(_safe_int(r.get("sort_order", 0), 0))

        rid = r.get("id", None)
        if pd.isna(rid):
            exec_sql("""
                INSERT INTO menu_items(sku, name, price, active, sort_order)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (sku) DO UPDATE SET
                    name=EXCLUDED.name,
                    price=EXCLUDED.price,
                    active=EXCLUDED.active,
                    sort_order=EXCLUDED.sort_order
            """, (sku, name, price, active, sort_order))
        else:
            exec_sql("""
                UPDATE menu_items
                SET sku=%s, name=%s, price=%s, active=%s, sort_order=%s
                WHERE id=%s
            """, (sku, name, price, active, sort_order, int(rid)))

    clear_caches()

def get_menu_recipe(menu_id: int):
    return read_menu_recipes_cached(int(menu_id))

def upsert_menu_recipe(menu_id: int, df: pd.DataFrame):
    # Replace recipe for menu_id
    exec_sql("DELETE FROM menu_recipes WHERE menu_id=%s", (int(menu_id),))
    for _, r in df.iterrows():
        try:
            item_id = int(r["item_id"])
            qty = float(r["qty"])
        except Exception:
            continue
        if item_id <= 0 or qty == 0:
            continue
        exec_sql("""
            INSERT INTO menu_recipes(menu_id, item_id, qty)
            VALUES (%s, %s, %s)
            ON CONFLICT (menu_id, item_id) DO UPDATE SET qty=EXCLUDED.qty
        """, (int(menu_id), int(item_id), float(qty)))

    clear_caches()

def get_recipe_map(menu_id: int):
    df = get_menu_recipe(menu_id)
    if df.empty:
        return {}
    return {int(r["item_id"]): float(r["qty"]) for _, r in df.iterrows()}

# Sales / POS
def create_sale(payment_method: str, note: str = "") -> int:
    exec_sql("""
        INSERT INTO sales(created_at, sale_date, payment_method, note)
        VALUES (%s, %s, %s, %s)
    """, (now_iso(), today_str(), payment_method, note.strip()))
    sid = int(fetch_df("SELECT MAX(id) AS id FROM sales").iloc[0]["id"])
    clear_caches()
    return sid

def add_sale_line(sale_id: int, menu_id: int, sku: str, name: str, qty: float, unit_price: float):
    line_total = float(qty) * float(unit_price)
    exec_sql("""
        INSERT INTO sale_lines(sale_id, menu_id, sku, name, qty, unit_price, line_total)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (int(sale_id), int(menu_id), sku, name, float(qty), float(unit_price), float(line_total)))
    clear_caches()

def get_today_sales_summary():
    sales = fetch_df("""
        SELECT id, sale_date, payment_method
        FROM sales
        WHERE sale_date = %s
    """, (today_str(),))

    if sales.empty:
        return (
            pd.DataFrame(columns=["sale_date", "payment_method", "total"]),
            pd.DataFrame(columns=["sku", "name", "qty", "total"]),
        )

    lines = fetch_df("""
        SELECT sale_id, sku, name, qty, line_total
        FROM sale_lines
        WHERE sale_id = ANY(%s)
    """, (list(sales["id"].astype(int).tolist()),))

    if lines.empty:
        return (
            pd.DataFrame(columns=["sale_date", "payment_method", "total"]),
            pd.DataFrame(columns=["sku", "name", "qty", "total"]),
        )

    merged = lines.merge(sales, left_on="sale_id", right_on="id", how="left")

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

# Event Mode
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

# Orders draft state
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
# UI
# ---------------------------
st.set_page_config(page_title=APP_TITLE, page_icon="🍗", layout="wide")

with st.expander("🔧 DB connection test", expanded=False):
    st.write("Using DATABASE_URL:", "DATABASE_URL" in st.secrets)
    if "DATABASE_URL" in st.secrets:
        # Don’t print secrets; just show host and dbname
        try:
            u = urlparse(st.secrets["DATABASE_URL"])
            st.write("Host:", u.hostname)
            st.write("DB:", (u.path or "").lstrip("/") or "postgres")
        except Exception:
            st.write("DB URL present (could not parse safely).")
    st.write("If you see IPv6 errors, this app auto-forces IPv4 where possible.")

# Init DB + seed menu
init_db()
seed_menu_if_empty()

st.title("JAEJU Stock + POS + Events (Postgres)")

mobile_mode = st.toggle("Mobile mode", value=True, key="mobile_mode_toggle")
PAGES = ["POS", "Event Mode", "Orders", "Dashboard", "Adjust Stock", "Menu Admin", "Items", "Movements"]

if mobile_mode:
    page = st.selectbox("Go to", PAGES)
    tabs = None
else:
    tabs = st.tabs(PAGES)
    page = None

def _container(name: str):
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
        c2.metric("EFTPOS", f"${eftpos_today:,.2f}")
        c3.metric("Cash", f"${cash_today:,.2f}")

        if not item_summary.empty:
            st.dataframe(item_summary, use_container_width=True, hide_index=True)
        else:
            st.info("No sales recorded today yet.")

# -------- Event Mode --------
if (mobile_mode and page == "Event Mode") or (not mobile_mode):
    with _container("Event Mode"):
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
                    columns=["Item", "Qty"]
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
    with _container("Orders"):
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
                set_order_status(int(order_id), ORDER_STATUS_CANCELLED)
                st.success("Cancelled.")
                st.rerun()

# -------- Dashboard --------
if (mobile_mode and page == "Dashboard") or (not mobile_mode):
    with _container("Dashboard"):
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

# -------- Adjust Stock --------
if (mobile_mode and page == "Adjust Stock") or (not mobile_mode):
    with _container("Adjust Stock"):
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
    with _container("Menu Admin"):
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
                edit_df = recipe_df[["item_id", "qty"]].copy() if not recipe_df.empty else pd.DataFrame(
                    [{"item_id": int(items.iloc[0]["id"]), "qty": 0.0}],
                    columns=["item_id", "qty"]
                )

                edited_recipe = st.data_editor(
                    edit_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "item_id": st.column_config.SelectboxColumn(
                            "Ingredient item",
                            options=options,
                            format_func=lambda x: id_to_name.get(int(x), str(x))
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
    with _container("Items"):
        st.subheader("Items")

        with st.expander("Add / Update item", expanded=True):
            name = st.text_input("Item name", placeholder="Chicken thigh diced")
            unit = st.text_input("Unit", placeholder="kg / pcs / L")
            par = st.number_input("PAR level (Prep)", min_value=0.0, value=0.0, step=0.5)
            price = st.number_input("Price NZD (optional)", min_value=0.0, value=0.0, step=0.1)

            if st.button("Save item", type="primary"):
                try:
                    add_item(name=name, unit=unit, par_level=float(par), price_nzd=float(price), active=True)
                    st.success("Saved.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        df = get_items_df(active_only=False)
        if not df.empty:
            st.dataframe(df, use_container_width=True, hide_index=True)

# -------- Movements --------
if (mobile_mode and page == "Movements") or (not mobile_mode):
    with _container("Movements"):
        st.subheader("Movements log (audit trail)")
        mv = get_movements_df()
        if mv.empty:
            st.info("No movements yet.")
        else:
            st.dataframe(mv, use_container_width=True, hide_index=True)
