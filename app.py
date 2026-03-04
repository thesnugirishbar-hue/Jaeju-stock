import os
from datetime import datetime, date
from contextlib import contextmanager

import pandas as pd
import streamlit as st

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


# ===========================
# CONFIG
# ===========================
APP_TITLE = "JAEJU Stock + POS + Events (Postgres)"

LOC_TRUCK = "Food Truck"
LOC_PREP = "Prep Kitchen"
LOCATIONS = [LOC_TRUCK, LOC_PREP]

ORDER_STATUS_PENDING = "PENDING"
ORDER_STATUS_FULFILLED = "FULFILLED"
ORDER_STATUS_CANCELLED = "CANCELLED"

PAYMENT_EFTPOS = "EFTPOS"
PAYMENT_CASH = "CASH"

# Cache + paging defaults (tune as needed)
CACHE_TTL_SECONDS = 20
MOVEMENTS_LIMIT = 250
ORDERS_LIMIT = 150


# ===========================
# TIME HELPERS
# ===========================
def now_utc() -> datetime:
    return datetime.utcnow()


def today_local() -> date:
    # Streamlit Cloud runs in UTC; if you want NZ-local dates, you can handle TZ later.
    return date.today()


# ===========================
# SECRETS / DSN
# ===========================
def get_database_url() -> str:
    # Prefer Streamlit Secrets; fallback to env var.
    if "DATABASE_URL" in st.secrets:
        return str(st.secrets["DATABASE_URL"]).strip()
    env = os.getenv("DATABASE_URL", "").strip()
    if env:
        return env
    raise ValueError("Missing DATABASE_URL in Streamlit Secrets (Manage app → Secrets).")


# ===========================
# DB POOL
# ===========================
@st.cache_resource
def get_pool() -> ConnectionPool:
    """
    Connection pooling makes the app MUCH faster and avoids creating a new TCP connection per click.
    We also disable prepared statements (prepare_threshold=0) because Supabase transaction pooler
    commonly breaks with prepared statements.
    """
    dsn = get_database_url()

    # Important: prepared statements can cause issues with transaction poolers.
    # psycopg: prepare_threshold=0 disables automatic prepared statements.
    # https://www.psycopg.org/psycopg3/docs/api/connections.html#psycopg.Connection.connect
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=6,
        kwargs={
            "autocommit": False,
            "prepare_threshold": 0,  # <- key fix for Supabase transaction pooler
            "row_factory": dict_row,
        },
        timeout=15,
    )
    return pool


@contextmanager
def get_conn():
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


def exec_sql(sql: str, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()


def fetch_all(sql: str, params=None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall()
        # rows are dicts due to dict_row
        return rows


def fetch_df(sql: str, params=None) -> pd.DataFrame:
    rows = fetch_all(sql, params=params)
    return pd.DataFrame(rows)


# ===========================
# CACHE VERSIONING
# ===========================
def _version_key() -> int:
    return int(st.session_state.get("_data_version", 0))


def bump_version():
    st.session_state["_data_version"] = _version_key() + 1


@st.cache_data(ttl=CACHE_TTL_SECONDS)
def cached_df(sql: str, params_tuple: tuple, version: int) -> pd.DataFrame:
    # params must be hashable -> use tuple
    df = fetch_df(sql, params=params_tuple)
    return df


def read_df(sql: str, params=None) -> pd.DataFrame:
    params = params or ()
    if not isinstance(params, tuple):
        params = tuple(params)
    return cached_df(sql, params, _version_key())


# ===========================
# DB INIT (schema)
# ===========================
@st.cache_resource
def init_db_once():
    schema_sql = """
    CREATE TABLE IF NOT EXISTS items (
        id BIGSERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        unit TEXT NOT NULL DEFAULT 'unit',
        par_level NUMERIC NOT NULL DEFAULT 0,
        price_nzd NUMERIC NOT NULL DEFAULT 0,
        active BOOLEAN NOT NULL DEFAULT TRUE
    );

    CREATE TABLE IF NOT EXISTS stock (
        item_id BIGINT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        location TEXT NOT NULL,
        qty NUMERIC NOT NULL DEFAULT 0,
        PRIMARY KEY (item_id, location)
    );

    CREATE TABLE IF NOT EXISTS movements (
        id BIGSERIAL PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        item_id BIGINT NOT NULL REFERENCES items(id),
        location TEXT NOT NULL,
        delta NUMERIC NOT NULL,
        reason TEXT NOT NULL,
        ref_type TEXT NOT NULL DEFAULT '',
        ref_id BIGINT
    );

    CREATE TABLE IF NOT EXISTS orders (
        id BIGSERIAL PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        from_location TEXT NOT NULL,
        to_location TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'PENDING',
        note TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS order_lines (
        id BIGSERIAL PRIMARY KEY,
        order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        item_id BIGINT NOT NULL REFERENCES items(id),
        qty NUMERIC NOT NULL
    );

    CREATE TABLE IF NOT EXISTS menu_items (
        id BIGSERIAL PRIMARY KEY,
        sku TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        price NUMERIC NOT NULL DEFAULT 0,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order INT NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS menu_recipes (
        id BIGSERIAL PRIMARY KEY,
        menu_id BIGINT NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
        item_id BIGINT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        qty NUMERIC NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS sales (
        id BIGSERIAL PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        sale_date DATE NOT NULL,
        payment_method TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS sale_lines (
        id BIGSERIAL PRIMARY KEY,
        sale_id BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
        menu_id BIGINT NOT NULL REFERENCES menu_items(id),
        sku TEXT NOT NULL,
        name TEXT NOT NULL,
        qty NUMERIC NOT NULL,
        unit_price NUMERIC NOT NULL,
        line_total NUMERIC NOT NULL
    );

    -- Helpful indexes
    CREATE INDEX IF NOT EXISTS idx_movements_created_at ON movements(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_sales_sale_date ON sales(sale_date);
    CREATE INDEX IF NOT EXISTS idx_order_lines_order_id ON order_lines(order_id);
    CREATE INDEX IF NOT EXISTS idx_sale_lines_sale_id ON sale_lines(sale_id);
    """
    exec_sql(schema_sql)

    # Seed menu if empty
    count_df = fetch_df("SELECT COUNT(*)::int AS c FROM menu_items;")
    c = int(count_df.iloc[0]["c"]) if not count_df.empty else 0
    if c == 0:
        starters = [
            ("JUST_CHICKEN", "Just Chicken", 20.00, True, 10),
            ("SMALL_CHIPS", "Small Chicken on Chips", 22.00, True, 20),
            ("LARGE_CHIPS", "Large Chicken on Chips", 26.00, True, 30),
            ("BURGER", "Chicken Burger", 20.00, True, 40),
            ("CAULI", "Korean Cauli", 18.00, True, 50),
            ("CHIPS", "Chips", 8.00, True, 60),
        ]
        for sku, name, price, active, sort_order in starters:
            exec_sql(
                """
                INSERT INTO menu_items (sku, name, price, active, sort_order)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (sku) DO NOTHING;
                """,
                (sku, name, price, active, sort_order),
            )


def ensure_stock_rows_for_item(item_id: int):
    for loc in LOCATIONS:
        exec_sql(
            """
            INSERT INTO stock (item_id, location, qty)
            VALUES (%s, %s, 0)
            ON CONFLICT (item_id, location) DO NOTHING;
            """,
            (item_id, loc),
        )


# ===========================
# ITEMS / STOCK
# ===========================
def get_items(active_only=True) -> pd.DataFrame:
    if active_only:
        return read_df(
            """
            SELECT id, name, unit, par_level, price_nzd, active
            FROM items
            WHERE active = TRUE
            ORDER BY LOWER(name);
            """
        )
    return read_df(
        """
        SELECT id, name, unit, par_level, price_nzd, active
        FROM items
        ORDER BY LOWER(name);
        """
    )


def add_or_update_item(name: str, unit: str, par_level: float, price_nzd: float, active: bool = True):
    name = (name or "").strip()
    if not name:
        raise ValueError("Item name cannot be empty.")

    unit = (unit or "unit").strip()
    exec_sql(
        """
        INSERT INTO items (name, unit, par_level, price_nzd, active)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (name) DO UPDATE SET
            unit = EXCLUDED.unit,
            par_level = EXCLUDED.par_level,
            price_nzd = EXCLUDED.price_nzd,
            active = EXCLUDED.active
        RETURNING id;
        """,
        (name, unit, float(par_level), float(price_nzd), bool(active)),
    )
    # Get item id for stock rows
    row = fetch_all("SELECT id FROM items WHERE name=%s;", (name,))
    item_id = int(row[0]["id"])
    ensure_stock_rows_for_item(item_id)
    bump_version()


def adjust_stock(item_id: int, location: str, delta: float, reason: str, ref_type: str = "", ref_id=None):
    if location not in LOCATIONS:
        raise ValueError("Invalid location.")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Reason is required.")
    ensure_stock_rows_for_item(int(item_id))

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
        (int(item_id), location, float(delta), reason, ref_type or "", ref_id),
    )
    bump_version()


def get_stock_pivot() -> pd.DataFrame:
    df = read_df(
        """
        SELECT
            i.id AS item_id,
            i.name,
            i.unit,
            i.par_level,
            s.location,
            s.qty
        FROM stock s
        JOIN items i ON i.id = s.item_id
        WHERE i.active = TRUE
        ORDER BY LOWER(i.name), s.location;
        """
    )
    if df.empty:
        return df

    pivot = df.pivot_table(
        index=["item_id", "name", "unit", "par_level"],
        columns="location",
        values="qty",
        aggfunc="sum",
        fill_value=0,
    ).reset_index()

    for loc in LOCATIONS:
        if loc not in pivot.columns:
            pivot[loc] = 0

    pivot["Below PAR?"] = (pivot[LOC_PREP] < pivot["par_level"]).map({True: "YES", False: ""})
    return pivot


def get_movements(limit=MOVEMENTS_LIMIT) -> pd.DataFrame:
    return read_df(
        """
        SELECT
            m.created_at,
            i.name,
            m.location,
            m.delta,
            m.reason,
            m.ref_type,
            COALESCE(m.ref_id, NULL) AS ref_id
        FROM movements m
        JOIN items i ON i.id = m.item_id
        ORDER BY m.created_at DESC
        LIMIT %s;
        """,
        (int(limit),),
    )


# ===========================
# MENU / RECIPES
# ===========================
def get_menu(active_only=True) -> pd.DataFrame:
    if active_only:
        return read_df(
            """
            SELECT id, sku, name, price, active, sort_order
            FROM menu_items
            WHERE active = TRUE
            ORDER BY sort_order, LOWER(name);
            """
        )
    return read_df(
        """
        SELECT id, sku, name, price, active, sort_order
        FROM menu_items
        ORDER BY sort_order, LOWER(name);
        """
    )


def upsert_menu_items(df: pd.DataFrame):
    for _, r in df.iterrows():
        sku = str(r.get("sku", "")).strip()
        name = str(r.get("name", "")).strip()
        if not sku or not name:
            continue
        price = float(r.get("price", 0) or 0)
        active = bool(r.get("active", True))
        sort_order = int(r.get("sort_order", 0) or 0)

        exec_sql(
            """
            INSERT INTO menu_items (sku, name, price, active, sort_order)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (sku) DO UPDATE SET
                name = EXCLUDED.name,
                price = EXCLUDED.price,
                active = EXCLUDED.active,
                sort_order = EXCLUDED.sort_order;
            """,
            (sku, name, price, active, sort_order),
        )
    bump_version()


def get_menu_recipe(menu_id: int) -> pd.DataFrame:
    return read_df(
        """
        SELECT
            mr.id,
            mr.menu_id,
            mr.item_id,
            i.name AS item_name,
            mr.qty
        FROM menu_recipes mr
        JOIN items i ON i.id = mr.item_id
        WHERE mr.menu_id = %s AND mr.qty > 0
        ORDER BY LOWER(i.name);
        """,
        (int(menu_id),),
    )


def upsert_menu_recipe(menu_id: int, df: pd.DataFrame):
    # wipe existing recipe rows for this menu item (simple + safe)
    exec_sql("DELETE FROM menu_recipes WHERE menu_id=%s;", (int(menu_id),))

    for _, r in df.iterrows():
        try:
            item_id = int(r["item_id"])
            qty = float(r["qty"])
        except Exception:
            continue
        if item_id <= 0 or qty <= 0:
            continue
        exec_sql(
            """
            INSERT INTO menu_recipes (menu_id, item_id, qty)
            VALUES (%s, %s, %s);
            """,
            (int(menu_id), int(item_id), float(qty)),
        )
    bump_version()


def recipe_map(menu_id: int) -> dict[int, float]:
    df = read_df(
        "SELECT item_id, qty FROM menu_recipes WHERE menu_id=%s AND qty>0;",
        (int(menu_id),),
    )
    if df.empty:
        return {}
    return {int(r["item_id"]): float(r["qty"]) for _, r in df.iterrows()}


# ===========================
# SALES / POS
# ===========================
def create_sale(payment_method: str, note: str = "") -> int:
    rows = fetch_all(
        """
        INSERT INTO sales (sale_date, payment_method, note)
        VALUES (%s, %s, %s)
        RETURNING id;
        """,
        (today_local(), payment_method, (note or "").strip()),
    )
    bump_version()
    return int(rows[0]["id"])


def add_sale_line(sale_id: int, menu_id: int, sku: str, name: str, qty: float, unit_price: float):
    line_total = float(qty) * float(unit_price)
    exec_sql(
        """
        INSERT INTO sale_lines (sale_id, menu_id, sku, name, qty, unit_price, line_total)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
        """,
        (int(sale_id), int(menu_id), sku, name, float(qty), float(unit_price), float(line_total)),
    )
    bump_version()


def record_pos_sale(menu_row: pd.Series, qty: float, payment_method: str, note: str = "") -> int:
    if qty <= 0:
        raise ValueError("Qty must be > 0.")

    menu_id = int(menu_row["id"])
    sku = str(menu_row["sku"])
    name = str(menu_row["name"])
    price = float(menu_row["price"])

    rec = recipe_map(menu_id)
    if not rec:
        raise ValueError("This menu item has no recipe yet. Add it in Menu Admin.")

    sale_id = create_sale(payment_method=payment_method, note=note)
    add_sale_line(sale_id, menu_id, sku, name, float(qty), price)

    # Deduct ingredients from truck stock
    for item_id, per_unit in rec.items():
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


def today_sales_summary():
    df = read_df(
        """
        SELECT
            s.sale_date,
            s.payment_method,
            sl.sku,
            sl.name,
            sl.qty,
            sl.line_total
        FROM sale_lines sl
        JOIN sales s ON s.id = sl.sale_id
        WHERE s.sale_date = %s;
        """,
        (today_local(),),
    )
    if df.empty:
        return (
            pd.DataFrame(columns=["sale_date", "payment_method", "total"]),
            pd.DataFrame(columns=["sku", "name", "qty", "total"]),
        )

    pay = (
        df.groupby(["sale_date", "payment_method"], as_index=False)["line_total"]
        .sum()
        .rename(columns={"line_total": "total"})
    )
    items = (
        df.groupby(["sku", "name"], as_index=False)
        .agg(qty=("qty", "sum"), total=("line_total", "sum"))
        .sort_values("total", ascending=False)
    )
    return pay, items


# ===========================
# ORDERS
# ===========================
def create_order(note: str = "") -> int:
    rows = fetch_all(
        """
        INSERT INTO orders (from_location, to_location, status, note)
        VALUES (%s, %s, %s, %s)
        RETURNING id;
        """,
        (LOC_PREP, LOC_TRUCK, ORDER_STATUS_PENDING, (note or "").strip()),
    )
    bump_version()
    return int(rows[0]["id"])


def add_order_line(order_id: int, item_id: int, qty: float):
    if qty <= 0:
        raise ValueError("Qty must be > 0.")
    exec_sql(
        """
        INSERT INTO order_lines (order_id, item_id, qty)
        VALUES (%s, %s, %s);
        """,
        (int(order_id), int(item_id), float(qty)),
    )
    bump_version()


def get_orders(limit=ORDERS_LIMIT) -> pd.DataFrame:
    return read_df(
        """
        SELECT id, created_at, status, note
        FROM orders
        ORDER BY created_at DESC
        LIMIT %s;
        """,
        (int(limit),),
    )


def get_order_lines(order_id: int) -> pd.DataFrame:
    return read_df(
        """
        SELECT
            i.name,
            i.unit,
            ol.qty,
            ol.item_id
        FROM order_lines ol
        JOIN items i ON i.id = ol.item_id
        WHERE ol.order_id = %s
        ORDER BY LOWER(i.name);
        """,
        (int(order_id),),
    )


def set_order_status(order_id: int, status: str):
    exec_sql("UPDATE orders SET status=%s WHERE id=%s;", (status, int(order_id)))
    bump_version()


def fulfill_order(order_id: int):
    order_df = read_df("SELECT id, status FROM orders WHERE id=%s;", (int(order_id),))
    if order_df.empty:
        raise ValueError("Order not found.")
    if str(order_df.iloc[0]["status"]) != ORDER_STATUS_PENDING:
        raise ValueError("Order must be PENDING to fulfill.")

    lines = get_order_lines(order_id)
    if lines.empty:
        raise ValueError("Order has no lines.")

    # check prep stock
    prep_stock = read_df(
        """
        SELECT item_id, qty
        FROM stock
        WHERE location = %s;
        """,
        (LOC_PREP,),
    )
    prep_map = {int(r["item_id"]): float(r["qty"]) for _, r in prep_stock.iterrows()} if not prep_stock.empty else {}

    for _, line in lines.iterrows():
        item_id = int(line["item_id"])
        qty = float(line["qty"])
        have = float(prep_map.get(item_id, 0.0))
        if have < qty:
            raise ValueError(f"Not enough Prep stock for {line['name']} (have {have}, need {qty}).")

    # move stock
    for _, line in lines.iterrows():
        item_id = int(line["item_id"])
        qty = float(line["qty"])
        adjust_stock(item_id, LOC_PREP, -qty, "Order fulfillment (Prep → Truck)", ref_type="order", ref_id=order_id)
        adjust_stock(item_id, LOC_TRUCK, +qty, "Order fulfillment (Prep → Truck)", ref_type="order", ref_id=order_id)

    set_order_status(order_id, ORDER_STATUS_FULFILLED)


# ===========================
# EVENT MODE
# ===========================
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

    ing_totals: dict[int, float] = {}
    for mid, qty_est in qty_rows:
        rec = recipe_map(int(mid))
        for item_id, per_unit in rec.items():
            ing_totals[item_id] = ing_totals.get(item_id, 0.0) + (float(per_unit) * float(qty_est))

    return qty_rows, ing_totals


# ===========================
# UI HELPERS
# ===========================
def ensure_order_draft_state():
    if "order_lines" not in st.session_state:
        st.session_state["order_lines"] = []


def add_to_order_draft(item_name: str, qty: float):
    ensure_order_draft_state()
    if qty <= 0:
        return
    for line in st.session_state["order_lines"]:
        if line["Item"] == item_name:
            line["Qty"] = float(line["Qty"]) + float(qty)
            return
    st.session_state["order_lines"].append({"Item": item_name, "Qty": float(qty)})


def set_order_draft_from_name_totals(name_totals: dict):
    ensure_order_draft_state()
    st.session_state["order_lines"] = [{"Item": k, "Qty": float(v)} for k, v in name_totals.items() if float(v) > 0]


def db_connection_panel():
    with st.expander("🔧 DB connection test", expanded=False):
        try:
            url = get_database_url()
            st.success("DATABASE_URL found in secrets/env.")
            # show only safe parts
            st.write("Host:", url.split("@")[-1].split("/")[0] if "@" in url else "(hidden)")
            st.write("Tip: If you are using Supabase Pooler (Transaction mode), prepared statements must be disabled.")
            # quick ping
            ping = fetch_df("SELECT NOW() AS now;")
            if not ping.empty:
                st.success(f"DB reachable ✅ ({ping.iloc[0]['now']})")
        except Exception as e:
            st.error(f"DB not connected: {e}")


# ===========================
# APP START
# ===========================
st.set_page_config(page_title=APP_TITLE, page_icon="🍗", layout="wide")

# init DB schema once
init_db_once()

st.title(APP_TITLE)
db_connection_panel()

# SINGLE ROUTER (fast + avoids duplicate widgets)
st.sidebar.header("Navigation")
mobile_mode = st.sidebar.toggle("Mobile mode", value=True, key="mobile_mode_toggle")

PAGES = ["POS", "Event Mode", "Orders", "Dashboard", "Adjust Stock", "Menu Admin", "Items", "Movements"]
page = st.sidebar.selectbox("Go to", PAGES, index=0, key="page_select")

# ===========================
# POS
# ===========================
if page == "POS":
    st.subheader("POS (one-tap buttons)")
    menu = get_menu(active_only=True)

    if menu.empty:
        st.warning("No active menu items. Go to Menu Admin.")
    else:
        pay = st.segmented_control("Payment", [PAYMENT_EFTPOS, PAYMENT_CASH], default=PAYMENT_EFTPOS, key="pos_pay")
        qty_mode = st.toggle("Qty mode (sell more than 1)", value=False, key="pos_qty_mode")
        qty = 1.0
        if qty_mode:
            qty = st.number_input("Qty", min_value=1.0, value=1.0, step=1.0, key="pos_qty")

        cols = st.columns(2)
        for i, (_, r) in enumerate(menu.iterrows()):
            with cols[i % 2]:
                label = f"{r['name']}\n${float(r['price']):.0f}"
                if st.button(label, use_container_width=True, key=f"pos_btn_{int(r['id'])}"):
                    try:
                        sale_id = record_pos_sale(r, float(qty), pay, "")
                        st.toast(f"Sold: {r['name']} (#{sale_id})")
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))

    st.divider()
    st.subheader("Today totals")
    pay_summary, item_summary = today_sales_summary()
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

# ===========================
# EVENT MODE
# ===========================
elif page == "Event Mode":
    st.subheader("Event Mode (Revenue → Ingredients → Draft Order)")

    menu = get_menu(active_only=True)
    if menu.empty:
        st.warning("No active menu items. Add them in Menu Admin.")
    else:
        event_name = st.text_input("Event name", placeholder="Electric Ave Day 1", key="ev_name")
        revenue = st.number_input("Target revenue (NZD)", min_value=0.0, value=10000.0, step=500.0, key="ev_rev")
        buffer_pct = st.number_input("Safety buffer (%)", min_value=0.0, value=10.0, step=1.0, key="ev_buf")

        st.markdown("### Menu mix")
        mix_raw = {}
        total = 0.0
        default_share = int(100 / max(len(menu), 1))

        for _, r in menu.iterrows():
            mid = int(r["id"])
            val = st.slider(f"{r['name']} (%)", 0, 100, default_share, key=f"mix_{mid}")
            mix_raw[mid] = float(val)
            total += float(val)

        if total <= 0:
            st.warning("Set at least one menu share above 0%.")
        else:
            mix = {k: v / total for k, v in mix_raw.items() if v > 0}

            qty_rows, ing_totals = forecast_from_revenue(revenue, mix, menu)
            ing_totals = {k: v * (1.0 + buffer_pct / 100.0) for k, v in ing_totals.items()}

            items = get_items(active_only=True)
            id_to_name = {int(r["id"]): str(r["name"]) for _, r in items.iterrows()} if not items.empty else {}

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

            if st.button("Send to Orders draft", type="primary", key="ev_send_to_draft"):
                set_order_draft_from_name_totals(name_totals)
                st.success("Draft created. Go to Orders and press Create order.")

# ===========================
# ORDERS
# ===========================
elif page == "Orders":
    st.subheader("Prep Kitchen → Truck Orders (mobile friendly)")

    items = get_items(active_only=True)
    if items.empty:
        st.info("Add items first.")
    else:
        item_names = list(items["name"].tolist())
        ensure_order_draft_state()

        note = st.text_input("Order note (optional)", placeholder="Friday top-up / Event name", key="ord_note")

        st.markdown("### Add to order")
        c1, c2, c3 = st.columns([2, 1, 1])
        pick_item = c1.selectbox("Item", item_names, key="order_pick_item")
        pick_qty = c2.number_input("Qty", min_value=0.0, value=1.0, step=0.5, key="order_pick_qty")

        if c3.button("Add", type="primary", key="order_add_btn"):
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
            if c1.button("Clear draft", key="order_clear"):
                st.session_state["order_lines"] = []
                st.rerun()

            if c2.button("Create order", type="primary", key="order_create"):
                try:
                    if not st.session_state["order_lines"]:
                        raise ValueError("Add at least one item to the draft.")
                    order_id = create_order(note=note)

                    # Map name -> id
                    name_to_id = {str(r["name"]): int(r["id"]) for _, r in items.iterrows()}
                    for line in st.session_state["order_lines"]:
                        item_id = name_to_id.get(line["Item"])
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
    orders = get_orders()
    if orders.empty:
        st.info("No orders yet.")
    else:
        st.dataframe(orders, use_container_width=True, hide_index=True)
        default_oid = int(orders.iloc[0]["id"])
        order_id = st.number_input("Order ID", min_value=1, step=1, value=default_oid, key="manage_order_id")
        lines = get_order_lines(int(order_id))
        if not lines.empty:
            st.dataframe(lines[["name", "unit", "qty"]], use_container_width=True, hide_index=True)

        c1, c2 = st.columns(2)
        if c1.button("Fulfill (Prep → Truck)", type="primary", key="order_fulfill"):
            try:
                fulfill_order(int(order_id))
                st.success("Fulfilled. Stock moved Prep → Truck.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

        if c2.button("Cancel order", key="order_cancel"):
            set_order_status(int(order_id), ORDER_STATUS_CANCELLED)
            st.success("Cancelled.")
            st.rerun()

# ===========================
# DASHBOARD
# ===========================
elif page == "Dashboard":
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

# ===========================
# ADJUST STOCK
# ===========================
elif page == "Adjust Stock":
    st.subheader("Adjust stock (counts, wastage, deliveries)")
    items = get_items(active_only=True)
    if items.empty:
        st.info("Add items first.")
    else:
        item_map = dict(zip(items["name"], items["id"]))

        item_name = st.selectbox("Item", list(item_map.keys()), key="adj_item")
        location = st.selectbox("Location", LOCATIONS, key="adj_loc")
        delta = st.number_input("Delta (+ add / - remove)", value=0.0, step=0.5, key="adj_delta")
        reason = st.text_input("Reason", placeholder="delivery / wastage / recount", key="adj_reason")

        if st.button("Apply adjustment", type="primary", key="adj_apply"):
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

# ===========================
# MENU ADMIN
# ===========================
elif page == "Menu Admin":
    st.subheader("Menu Admin (edit menu + recipes)")
    st.info("Tip: This is easiest on a laptop.")

    menu_df = get_menu(active_only=False)
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
    if st.button("Save menu items", type="primary", key="menu_save"):
        try:
            upsert_menu_items(edited_menu)
            st.success("Menu saved.")
            st.rerun()
        except Exception as e:
            st.error(str(e))

    st.divider()
    st.markdown("### Edit recipe (ingredients per 1 sale)")

    menu_all = get_menu(active_only=False)
    if menu_all.empty:
        st.warning("No menu items yet.")
    else:
        pick = st.selectbox("Choose menu item", menu_all["name"].tolist(), key="recipe_pick_menu")
        menu_id = int(menu_all.loc[menu_all["name"] == pick].iloc[0]["id"])

        items = get_items(active_only=True)
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
            if st.button("Save recipe", type="primary", key="recipe_save"):
                try:
                    upsert_menu_recipe(menu_id, edited_recipe)
                    st.success("Recipe saved.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

# ===========================
# ITEMS
# ===========================
elif page == "Items":
    st.subheader("Items")

    with st.expander("Add / Update item", expanded=True):
        name = st.text_input("Item name", placeholder="Chicken thigh diced", key="item_name")
        unit = st.text_input("Unit", placeholder="kg / pcs / L", key="item_unit")
        par = st.number_input("PAR level (Prep)", min_value=0.0, value=0.0, step=0.5, key="item_par")
        price = st.number_input("Price NZD (optional)", min_value=0.0, value=0.0, step=0.1, key="item_price")
        active = st.checkbox("Active", value=True, key="item_active")

        if st.button("Save item", type="primary", key="item_save"):
            try:
                add_or_update_item(
                    name=name,
                    unit=unit.strip() or "unit",
                    par_level=float(par),
                    price_nzd=float(price),
                    active=bool(active),
                )
                st.success("Saved.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    df = get_items(active_only=False)
    if not df.empty:
        st.dataframe(df, use_container_width=True, hide_index=True)

# ===========================
# MOVEMENTS
# ===========================
elif page == "Movements":
    st.subheader("Movements log (audit trail)")
    mv = get_movements()
    if mv.empty:
        st.info("No movements yet.")
    else:
        st.dataframe(mv, use_container_width=True, hide_index=True)
