import streamlit as st
import pandas as pd
from datetime import datetime, date
from contextlib import contextmanager
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

st.set_page_config(page_title="JAEJU Ops", page_icon="🍗", layout="wide")

# ---------------------------
# TIME HELPERS
# ---------------------------
def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def today_str():
    return date.today().isoformat()

def _safe_int(x, default=0):
    try:
        return int(float(x))
    except Exception:
        return default

# ---------------------------
# DB CONNECTION
# ---------------------------
def get_db_cfg():
    # Give a crystal-clear error instead of hidden stack traces
    if "db" not in st.secrets:
        st.error("Missing [db] in Streamlit Secrets. Go to Manage app → Secrets and add the [db] block.")
        st.stop()

    cfg = st.secrets["db"]
    required = ["host", "port", "dbname", "user", "password", "sslmode"]
    missing = [k for k in required if k not in cfg]
    if missing:
        st.error(f"Secrets [db] is missing keys: {missing}")
        st.stop()

    return cfg

@st.cache_resource
def db_params():
    cfg = get_db_cfg()
    return dict(
        host=cfg["host"],
        port=int(cfg["port"]),
        dbname=cfg["dbname"],
        user=cfg["user"],
        password=cfg["password"],
        sslmode=cfg.get("sslmode", "require"),
        connect_timeout=10,
    )

@contextmanager
def get_conn():
    conn = psycopg.connect(**db_params())
    try:
        yield conn
    finally:
        conn.close()

def exec_sql(sql: str, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        conn.commit()

def fetch_df(sql: str, params=None) -> pd.DataFrame:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchall() if cur.description else []
    return pd.DataFrame(rows, columns=cols)

# ---------------------------
# CACHING (stop freezing / DB hammering)
# ---------------------------
@st.cache_data(ttl=3)
def get_items_df(active_only=True) -> pd.DataFrame:
    q = "SELECT id, name, unit, par_level, price_nzd, active FROM items"
    if active_only:
        q += " WHERE active = TRUE"
    q += " ORDER BY LOWER(name)"
    return fetch_df(q)

@st.cache_data(ttl=3)
def get_menu_items(active_only=True) -> pd.DataFrame:
    q = "SELECT id, sku, name, price, active, sort_order FROM menu_items"
    if active_only:
        q += " WHERE active = TRUE"
    q += " ORDER BY sort_order ASC, LOWER(name)"
    return fetch_df(q)

@st.cache_data(ttl=3)
def get_recipe_map(menu_id: int):
    df = fetch_df("SELECT item_id, qty FROM menu_recipes WHERE menu_id=%s", (int(menu_id),))
    if df.empty:
        return {}
    return {int(r["item_id"]): float(r["qty"]) for _, r in df.iterrows()}

def clear_caches():
    st.cache_data.clear()

# ---------------------------
# DB INIT
# ---------------------------
def init_db():
    exec_sql("""
    CREATE TABLE IF NOT EXISTS items (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        unit TEXT NOT NULL,
        par_level DOUBLE PRECISION DEFAULT 0,
        price_nzd DOUBLE PRECISION DEFAULT 0,
        active BOOLEAN DEFAULT TRUE
    );
    """)
    exec_sql("""
    CREATE TABLE IF NOT EXISTS stock (
        item_id INTEGER NOT NULL REFERENCES items(id),
        location TEXT NOT NULL,
        qty DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY (item_id, location)
    );
    """)
    exec_sql("""
    CREATE TABLE IF NOT EXISTS movements (
        id SERIAL PRIMARY KEY,
        created_at TEXT NOT NULL,
        item_id INTEGER NOT NULL REFERENCES items(id),
        location TEXT NOT NULL,
        delta DOUBLE PRECISION NOT NULL,
        reason TEXT NOT NULL,
        ref_type TEXT,
        ref_id INTEGER
    );
    """)
    exec_sql("""
    CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        created_at TEXT NOT NULL,
        from_location TEXT NOT NULL,
        to_location TEXT NOT NULL,
        status TEXT NOT NULL,
        note TEXT
    );
    """)
    exec_sql("""
    CREATE TABLE IF NOT EXISTS order_lines (
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        item_id INTEGER NOT NULL REFERENCES items(id),
        qty DOUBLE PRECISION NOT NULL
    );
    """)
    exec_sql("""
    CREATE TABLE IF NOT EXISTS sales (
        id SERIAL PRIMARY KEY,
        created_at TEXT NOT NULL,
        sale_date TEXT NOT NULL,
        payment_method TEXT NOT NULL,
        note TEXT
    );
    """)
    exec_sql("""
    CREATE TABLE IF NOT EXISTS sale_lines (
        id SERIAL PRIMARY KEY,
        sale_id INTEGER NOT NULL REFERENCES sales(id),
        menu_id INTEGER NOT NULL,
        sku TEXT NOT NULL,
        name TEXT NOT NULL,
        qty DOUBLE PRECISION NOT NULL,
        unit_price DOUBLE PRECISION NOT NULL,
        line_total DOUBLE PRECISION NOT NULL
    );
    """)
    exec_sql("""
    CREATE TABLE IF NOT EXISTS menu_items (
        id SERIAL PRIMARY KEY,
        sku TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        price DOUBLE PRECISION NOT NULL DEFAULT 0,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        sort_order INTEGER NOT NULL DEFAULT 0
    );
    """)
    exec_sql("""
    CREATE TABLE IF NOT EXISTS menu_recipes (
        id SERIAL PRIMARY KEY,
        menu_id INTEGER NOT NULL REFERENCES menu_items(id),
        item_id INTEGER NOT NULL REFERENCES items(id),
        qty DOUBLE PRECISION NOT NULL,
        UNIQUE(menu_id, item_id)
    );
    """)

def seed_menu_if_empty():
    df = get_menu_items(active_only=False)
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
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (sku) DO NOTHING
        """, (sku, name, float(price), bool(active), int(sort_order)))
    clear_caches()

# ---------------------------
# EVENT MODE HELPERS
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
# UI
# ---------------------------
with st.expander("🔧 DB connection", expanded=False):
    cfg = get_db_cfg()
    st.write("Host:", cfg["host"])
    st.write("Port:", cfg["port"])
    st.write("DB:", cfg["dbname"])
    try:
        ping = fetch_df("SELECT 1 AS ok;")
        if not ping.empty and int(ping.iloc[0]["ok"]) == 1:
            st.success("DB reachable ✅")
    except Exception as e:
        st.error(f"DB not reachable: {e}")

init_db()
seed_menu_if_empty()

st.title("JAEJU Ops (Supabase)")

mobile_mode = st.toggle("Mobile mode", value=True)
PAGES = ["POS", "Event Mode"]
page = st.selectbox("Go to", PAGES) if mobile_mode else None

# POS (minimal for now; keep fast)
if page == "POS":
    st.subheader("POS")
    menu = get_menu_items(active_only=True)
    if menu.empty:
        st.warning("No menu items found.")
    else:
        st.write(menu[["sku", "name", "price"]])

# Event Mode
if page == "Event Mode":
    st.subheader("Event Mode")
    menu = get_menu_items(active_only=True)
    if menu.empty:
        st.warning("No active menu items.")
        st.stop()

    event_name = st.text_input("Event name", placeholder="Electric Ave Day 1")
    revenue = st.number_input("Target revenue (NZD)", min_value=0.0, value=10000.0, step=500.0)
    buffer_pct = st.number_input("Safety buffer (%)", min_value=0.0, value=10.0, step=1.0)

    st.markdown("### Menu mix")

    # Use stable defaults (avoid odd reruns)
    n = max(len(menu), 1)
    default_each = int(100 / n)

    mix_raw = {}
    total = 0.0
    for _, r in menu.iterrows():
        mid = int(r["id"])
        val = st.slider(f"{r['name']} (%)", 0, 100, default_each, key=f"mix_{mid}")
        mix_raw[mid] = float(val)
        total += float(val)

    if total <= 0:
        st.warning("Set at least one menu share above 0%.")
        st.stop()

    mix = {k: v / total for k, v in mix_raw.items() if v > 0}
    qty_rows, ing_totals = forecast_from_revenue(revenue, mix, menu)
    ing_totals = {k: v * (1.0 + buffer_pct / 100.0) for k, v in ing_totals.items()}

    st.write("OK ✅ Event Mode computed.")
    st.write(qty_rows[:10])
