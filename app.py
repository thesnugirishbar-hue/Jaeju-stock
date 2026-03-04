import os
import time
from contextlib import contextmanager
from datetime import datetime, date

import pandas as pd
import streamlit as st
import psycopg

# Optional pool (fast). If not installed, we fall back safely.
try:
    from psycopg_pool import ConnectionPool  # provided by psycopg[pool] extra
    HAS_POOL = True
except Exception:
    ConnectionPool = None
    HAS_POOL = False


# ---------------------------
# APP CONFIG
# ---------------------------
st.set_page_config(page_title="JAEJU Ops (Postgres)", layout="wide")

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
# DATABASE URL + CONNECTIONS
# ---------------------------
def get_database_url() -> str:
    """
    Supports either st.secrets["DATABASE_URL"] or env var DATABASE_URL.
    """
    db_url = None
    if "DATABASE_URL" in st.secrets:
        db_url = st.secrets["DATABASE_URL"]
    else:
        db_url = os.getenv("DATABASE_URL")

    if not db_url or not isinstance(db_url, str):
        raise ValueError("Missing DATABASE_URL in Streamlit Secrets (Manage app → Secrets).")

    return db_url.strip().strip('"').strip("'")


@st.cache_resource(show_spinner=False)
def get_pool():
    """
    Connection pool (fast). If psycopg_pool isn't available, returns None and we use direct connections.
    """
    if not HAS_POOL:
        return None

    dsn = get_database_url()

    # Small, safe pool sizes for Streamlit Cloud
    # (too big can get you rate-limited by Supabase pooler)
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=4,
        max_idle=60,
        timeout=10,
        kwargs={
            "connect_timeout": 8,
            "application_name": "jaeju-streamlit",
            # "prepare_threshold": None  # NOTE: if you use Supabase "transaction pooler", prepared statements can be an issue.
        },
    )
    return pool


@contextmanager
def get_conn():
    """
    Uses pool if available; otherwise direct psycopg.connect().
    """
    pool = get_pool()
    if pool is not None:
        with pool.connection() as conn:
            yield conn
    else:
        dsn = get_database_url()
        conn = psycopg.connect(
            dsn,
            connect_timeout=8,
            application_name="jaeju-streamlit",
        )
        try:
            yield conn
        finally:
            conn.close()


def exec_sql(sql: str, params=None, fetch: str | None = None):
    """
    fetch: None | 'one' | 'all'
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            if fetch == "one":
                row = cur.fetchone()
                conn.commit()
                return row
            if fetch == "all":
                rows = cur.fetchall()
                conn.commit()
                return rows
        conn.commit()
    return None


# ---------------------------
# SCHEMA / INIT
# ---------------------------
def init_db():
    exec_sql(
        """
        create table if not exists items (
            id bigserial primary key,
            name text not null,
            unit text not null default 'unit',
            par numeric not null default 0,
            active boolean not null default true,
            created_at timestamptz not null default now()
        );

        create table if not exists stock (
            id bigserial primary key,
            item_id bigint not null references items(id) on delete cascade,
            location text not null,
            qty numeric not null default 0,
            updated_at timestamptz not null default now(),
            unique (item_id, location)
        );

        create table if not exists movements (
            id bigserial primary key,
            item_id bigint not null references items(id) on delete cascade,
            location text not null,
            delta numeric not null,
            reason text not null,
            ref text,
            created_at timestamptz not null default now()
        );

        create table if not exists menu_items (
            id bigserial primary key,
            name text not null,
            price numeric not null default 0,
            active boolean not null default true,
            created_at timestamptz not null default now()
        );

        create table if not exists menu_recipe (
            id bigserial primary key,
            menu_id bigint not null references menu_items(id) on delete cascade,
            item_id bigint not null references items(id) on delete cascade,
            qty numeric not null default 0,
            unique(menu_id, item_id)
        );

        create table if not exists orders (
            id bigserial primary key,
            created_at timestamptz not null default now(),
            status text not null default 'PENDING',
            location text not null default 'Food Truck',
            notes text
        );

        create table if not exists order_lines (
            id bigserial primary key,
            order_id bigint not null references orders(id) on delete cascade,
            item_id bigint not null references items(id) on delete restrict,
            qty numeric not null default 0
        );

        create table if not exists sales (
            id bigserial primary key,
            created_at timestamptz not null default now(),
            location text not null default 'Food Truck',
            menu_id bigint references menu_items(id),
            qty numeric not null default 1,
            price numeric not null default 0,
            payment_method text not null default 'EFTPOS',
            event_name text
        );

        -- SPEED INDEXES
        create index if not exists idx_movements_created_at on movements(created_at desc);
        create index if not exists idx_sales_created_at on sales(created_at desc);
        create index if not exists idx_orders_created_at on orders(created_at desc);
        create index if not exists idx_orders_status on orders(status);
        """
    )


# ---------------------------
# FAST DATA LOADERS (CACHE)
# ---------------------------
def _cache_buster() -> int:
    return int(st.session_state.get("cache_buster", 0))


def bust_cache():
    st.session_state["cache_buster"] = _cache_buster() + 1


@st.cache_data(ttl=15, show_spinner=False)
def get_items_df(_bust: int) -> pd.DataFrame:
    rows = exec_sql(
        """
        select id, name, unit, par, active
        from items
        order by active desc, name asc
        """,
        fetch="all",
    )
    return pd.DataFrame(rows, columns=["id", "name", "unit", "par", "active"]) if rows else pd.DataFrame(
        columns=["id", "name", "unit", "par", "active"]
    )


@st.cache_data(ttl=15, show_spinner=False)
def get_stock_df(_bust: int) -> pd.DataFrame:
    rows = exec_sql(
        """
        select s.item_id, i.name, s.location, s.qty, s.updated_at
        from stock s
        join items i on i.id = s.item_id
        where i.active = true
        order by i.name, s.location
        """,
        fetch="all",
    )
    return pd.DataFrame(rows, columns=["item_id", "item_name", "location", "qty", "updated_at"]) if rows else pd.DataFrame(
        columns=["item_id", "item_name", "location", "qty", "updated_at"]
    )


@st.cache_data(ttl=15, show_spinner=False)
def get_menu_df(_bust: int) -> pd.DataFrame:
    rows = exec_sql(
        """
        select id, name, price, active
        from menu_items
        order by active desc, name asc
        """,
        fetch="all",
    )
    return pd.DataFrame(rows, columns=["id", "name", "price", "active"]) if rows else pd.DataFrame(
        columns=["id", "name", "price", "active"]
    )


@st.cache_data(ttl=15, show_spinner=False)
def get_recipe_df(menu_id: int, _bust: int) -> pd.DataFrame:
    rows = exec_sql(
        """
        select r.id, r.menu_id, r.item_id, i.name as item_name, r.qty
        from menu_recipe r
        join items i on i.id = r.item_id
        where r.menu_id = %s
        order by i.name asc
        """,
        (menu_id,),
        fetch="all",
    )
    return pd.DataFrame(rows, columns=["id", "menu_id", "item_id", "item_name", "qty"]) if rows else pd.DataFrame(
        columns=["id", "menu_id", "item_id", "item_name", "qty"]
    )


# ---------------------------
# HELPERS
# ---------------------------
def ensure_stock_row(item_id: int, location: str):
    exec_sql(
        """
        insert into stock(item_id, location, qty)
        values (%s, %s, 0)
        on conflict (item_id, location) do nothing
        """,
        (item_id, location),
    )


def adjust_stock(item_id: int, location: str, delta: float, reason: str, ref: str | None = None):
    ensure_stock_row(item_id, location)
    exec_sql(
        """
        update stock
        set qty = qty + %s, updated_at = now()
        where item_id = %s and location = %s
        """,
        (delta, item_id, location),
    )
    exec_sql(
        """
        insert into movements(item_id, location, delta, reason, ref)
        values (%s, %s, %s, %s, %s)
        """,
        (item_id, location, delta, reason, ref),
    )
    bust_cache()


def today_range():
    start = datetime.combine(date.today(), datetime.min.time())
    end = datetime.combine(date.today(), datetime.max.time())
    return start, end


# ---------------------------
# UI: DB CONNECTION TEST
# ---------------------------
def db_status_panel():
    with st.expander("🔧 DB connection test", expanded=False):
        try:
            db_url = get_database_url()
            st.success("DB secrets detected.")
            st.write("Using DATABASE_URL:", True)
            st.write("Pool available:", HAS_POOL)
            # quick ping
            row = exec_sql("select now()", fetch="one")
            st.write("DB time:", row[0] if row else "(no response)")
        except Exception as e:
            st.error(str(e))


# ---------------------------
# PAGES
# ---------------------------
def page_items():
    st.subheader("Items")
    items = get_items_df(_cache_buster())
    st.dataframe(items, use_container_width=True, hide_index=True)

    with st.expander("➕ Add / Update item", expanded=False):
        c1, c2, c3, c4 = st.columns([3, 2, 2, 2])
        with c1:
            name = st.text_input("Item name", key="item_name")
        with c2:
            unit = st.text_input("Unit", value="unit", key="item_unit")
        with c3:
            par = st.number_input("PAR", min_value=0.0, step=0.1, key="item_par")
        with c4:
            active = st.checkbox("Active", value=True, key="item_active")

        if st.button("Save item", type="primary", key="save_item_btn"):
            if not name.strip():
                st.error("Item name required.")
                return
            exec_sql(
                """
                insert into items(name, unit, par, active)
                values (%s, %s, %s, %s)
                """,
                (name.strip(), unit.strip() or "unit", par, active),
            )
            bust_cache()
            st.success("Saved.")
            st.rerun()


def page_adjust_stock():
    st.subheader("Adjust stock")
    items = get_items_df(_cache_buster())
    if items.empty:
        st.info("No items yet. Add items in Items tab.")
        return

    item_map = dict(zip(items["name"], items["id"]))
    c1, c2, c3, c4 = st.columns([4, 2, 2, 3])
    with c1:
        item_name = st.selectbox("Item", list(item_map.keys()), key="adj_item")
    with c2:
        location = st.selectbox("Location", LOCATIONS, key="adj_loc")
    with c3:
        delta = st.number_input("Delta (+/-)", step=0.1, key="adj_delta")
    with c4:
        reason = st.text_input("Reason", value="Adjustment", key="adj_reason")

    if st.button("Apply", type="primary", key="apply_adj_btn"):
        adjust_stock(item_map[item_name], location, float(delta), reason.strip() or "Adjustment")
        st.success("Stock updated.")
        st.rerun()


def page_movements():
    st.subheader("Movements (latest)")
    rows = exec_sql(
        """
        select m.created_at, i.name, m.location, m.delta, m.reason, m.ref
        from movements m
        join items i on i.id = m.item_id
        order by m.created_at desc
        limit 300
        """,
        fetch="all",
    )
    df = pd.DataFrame(rows, columns=["created_at", "item", "location", "delta", "reason", "ref"]) if rows else pd.DataFrame(
        columns=["created_at", "item", "location", "delta", "reason", "ref"]
    )
    st.dataframe(df, use_container_width=True, hide_index=True)


def page_menu_admin():
    st.subheader("Menu Admin")
    menu = get_menu_df(_cache_buster())
    st.dataframe(menu, use_container_width=True, hide_index=True)

    with st.expander("➕ Add menu item", expanded=False):
        c1, c2, c3 = st.columns([4, 2, 2])
        with c1:
            name = st.text_input("Menu name", key="menu_name")
        with c2:
            price = st.number_input("Price NZD", min_value=0.0, step=0.5, key="menu_price")
        with c3:
            active = st.checkbox("Active", value=True, key="menu_active")
        if st.button("Save menu item", type="primary", key="save_menu_btn"):
            if not name.strip():
                st.error("Menu name required.")
                return
            exec_sql(
                "insert into menu_items(name, price, active) values (%s, %s, %s)",
                (name.strip(), float(price), active),
            )
            bust_cache()
            st.success("Saved.")
            st.rerun()

    if menu.empty:
        st.info("Add a menu item first.")
        return

    st.divider()
    st.markdown("### Recipes (ingredients per menu item)")
    menu_name = st.selectbox("Select menu item", menu["name"].tolist(), key="recipe_menu_sel")
    menu_id = int(menu.loc[menu["name"] == menu_name, "id"].iloc[0])

    recipe = get_recipe_df(menu_id, _cache_buster())
    st.dataframe(recipe, use_container_width=True, hide_index=True)

    items = get_items_df(_cache_buster())
    item_map = dict(zip(items["name"], items["id"]))

    c1, c2, c3 = st.columns([5, 2, 2])
    with c1:
        ing_name = st.selectbox("Ingredient item", list(item_map.keys()), key="recipe_ing")
    with c2:
        qty = st.number_input("Qty per serve", min_value=0.0, step=0.01, key="recipe_qty")
    with c3:
        if st.button("Upsert ingredient", type="primary", key="recipe_upsert_btn"):
            exec_sql(
                """
                insert into menu_recipe(menu_id, item_id, qty)
                values (%s, %s, %s)
                on conflict (menu_id, item_id) do update set qty = excluded.qty
                """,
                (menu_id, item_map[ing_name], float(qty)),
            )
            bust_cache()
            st.success("Updated.")
            st.rerun()


def page_pos():
    st.subheader("POS")
    menu = get_menu_df(_cache_buster())
    if menu.empty:
        st.info("No menu items yet. Add them in Menu Admin.")
        return

    # QUICK sales entry
    c1, c2, c3, c4, c5 = st.columns([4, 1, 2, 2, 3])
    with c1:
        menu_name = st.selectbox("Menu item", menu["name"].tolist(), key="pos_menu")
    with c2:
        qty = st.number_input("Qty", min_value=1, step=1, value=1, key="pos_qty")
    with c3:
        payment = st.selectbox("Payment", [PAYMENT_EFTPOS, PAYMENT_CASH], key="pos_pay")
    with c4:
        location = st.selectbox("Location", LOCATIONS, key="pos_loc")
    with c5:
        event_name = st.text_input("Event name (optional)", key="pos_event_name")

    menu_row = menu.loc[menu["name"] == menu_name].iloc[0]
    default_price = float(menu_row["price"])

    price = st.number_input("Price each (NZD)", min_value=0.0, step=0.5, value=default_price, key="pos_price_each")
    total = float(qty) * float(price)
    st.metric("Line total", f"${total:,.2f}")

    if st.button("Record sale", type="primary", key="record_sale_btn"):
        exec_sql(
            """
            insert into sales(location, menu_id, qty, price, payment_method, event_name)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (location, int(menu_row["id"]), float(qty), float(price), payment, event_name.strip() or None),
        )
        bust_cache()
        st.success("Saved.")
        st.rerun()

    st.divider()

    # TODAY summary (fast: only aggregates)
    start, end = today_range()
    row = exec_sql(
        """
        select coalesce(sum(qty * price), 0) as revenue,
               coalesce(sum(case when payment_method='EFTPOS' then qty*price else 0 end), 0) as eftpos,
               coalesce(sum(case when payment_method='CASH' then qty*price else 0 end), 0) as cash
        from sales
        where created_at between %s and %s
        """,
        (start, end),
        fetch="one",
    )
    rev, eft, cash = (float(row[0]), float(row[1]), float(row[2])) if row else (0.0, 0.0, 0.0)
    c1, c2, c3 = st.columns(3)
    c1.metric("Today revenue", f"${rev:,.2f}")
    c2.metric("EFTPOS", f"${eft:,.2f}")
    c3.metric("Cash", f"${cash:,.2f}")

    # Latest sales (limit!)
    rows = exec_sql(
        """
        select s.created_at, m.name, s.qty, s.price, (s.qty*s.price) as total, s.payment_method, s.event_name
        from sales s
        left join menu_items m on m.id = s.menu_id
        order by s.created_at desc
        limit 200
        """,
        fetch="all",
    )
    df = pd.DataFrame(rows, columns=["time", "menu", "qty", "price_each", "total", "payment", "event"]) if rows else pd.DataFrame(
        columns=["time", "menu", "qty", "price_each", "total", "payment", "event"]
    )
    st.dataframe(df, use_container_width=True, hide_index=True)


def page_dashboard():
    st.subheader("Dashboard")
    stock = get_stock_df(_cache_buster())
    if stock.empty:
        st.info("No items yet. Add items in Items tab.")
        return

    st.markdown("### Stock snapshot (active items)")
    pivot = stock.pivot_table(index="item_name", columns="location", values="qty", aggfunc="sum", fill_value=0)
    st.dataframe(pivot.reset_index(), use_container_width=True, hide_index=True)


def page_orders():
    st.subheader("Orders (simple)")
    items = get_items_df(_cache_buster())
    if items.empty:
        st.info("Add items first.")
        return

    # Create order
    with st.expander("➕ Create order", expanded=False):
        location = st.selectbox("Deliver to", LOCATIONS, key="order_loc")
        notes = st.text_input("Notes", key="order_notes")
        if st.button("Create order", type="primary", key="create_order_btn"):
            row = exec_sql(
                "insert into orders(status, location, notes) values (%s, %s, %s) returning id",
                (ORDER_STATUS_PENDING, location, notes.strip() or None),
                fetch="one",
            )
            bust_cache()
            st.success(f"Created order #{row[0]}")
            st.session_state["active_order_id"] = int(row[0])
            st.rerun()

    active_order_id = st.session_state.get("active_order_id")

    # Show recent orders (limit!)
    rows = exec_sql(
        """
        select id, created_at, status, location, coalesce(notes,'')
        from orders
        order by created_at desc
        limit 50
        """,
        fetch="all",
    )
    df = pd.DataFrame(rows, columns=["id", "created_at", "status", "location", "notes"]) if rows else pd.DataFrame(
        columns=["id", "created_at", "status", "location", "notes"]
    )
    st.dataframe(df, use_container_width=True, hide_index=True)

    if not df.empty:
        pick = st.selectbox("Select active order", df["id"].astype(int).tolist(), index=0, key="order_pick")
        active_order_id = int(pick)
        st.session_state["active_order_id"] = active_order_id

    if not active_order_id:
        return

    st.markdown(f"### Order #{active_order_id} lines")
    item_map = dict(zip(items["name"], items["id"]))
    c1, c2, c3 = st.columns([5, 2, 2])
    with c1:
        item_name = st.selectbox("Item", list(item_map.keys()), key="order_line_item")
    with c2:
        qty = st.number_input("Qty", min_value=0.0, step=0.1, key="order_line_qty")
    with c3:
        if st.button("Add line", type="primary", key="order_add_line_btn"):
            exec_sql(
                "insert into order_lines(order_id, item_id, qty) values (%s, %s, %s)",
                (active_order_id, int(item_map[item_name]), float(qty)),
            )
            bust_cache()
            st.success("Added.")
            st.rerun()

    rows = exec_sql(
        """
        select l.id, i.name, l.qty
        from order_lines l
        join items i on i.id = l.item_id
        where l.order_id = %s
        order by l.id desc
        """,
        (active_order_id,),
        fetch="all",
    )
    lines = pd.DataFrame(rows, columns=["line_id", "item", "qty"]) if rows else pd.DataFrame(
        columns=["line_id", "item", "qty"]
    )
    st.dataframe(lines, use_container_width=True, hide_index=True)

    c1, c2, c3 = st.columns([2, 2, 6])
    with c1:
        if st.button("Mark fulfilled", key="order_fulfill_btn"):
            exec_sql("update orders set status=%s where id=%s", (ORDER_STATUS_FULFILLED, active_order_id))
            bust_cache()
            st.success("Fulfilled.")
            st.rerun()
    with c2:
        if st.button("Cancel order", key="order_cancel_btn"):
            exec_sql("update orders set status=%s where id=%s", (ORDER_STATUS_CANCELLED, active_order_id))
            bust_cache()
            st.warning("Cancelled.")
            st.rerun()


def page_event_mode():
    st.subheader("Event Mode (fast draft)")
    # IMPORTANT: unique key to avoid DuplicateElementId
    event_name = st.text_input("Event name", placeholder="Electric Ave Day 1", key="event_mode_name")
    st.caption("Tip: Use POS and fill Event name to group sales by event.")

    if not event_name.strip():
        st.info("Enter an event name to view event sales.")
        return

    rows = exec_sql(
        """
        select m.name, sum(s.qty) as qty, sum(s.qty*s.price) as revenue
        from sales s
        left join menu_items m on m.id = s.menu_id
        where s.event_name = %s
        group by m.name
        order by revenue desc nulls last
        """,
        (event_name.strip(),),
        fetch="all",
    )
    df = pd.DataFrame(rows, columns=["menu", "qty", "revenue"]) if rows else pd.DataFrame(
        columns=["menu", "qty", "revenue"]
    )
    st.dataframe(df, use_container_width=True, hide_index=True)


# ---------------------------
# MAIN
# ---------------------------
st.title("JAEJU Stock + POS + Events (Postgres)")
db_status_panel()

# Init DB once (kept light)
try:
    init_db()
except Exception as e:
    st.error(str(e))
    st.stop()

# UI mode toggle (unique key!)
mobile_mode = st.toggle("Mobile mode", value=False, key="mobile_mode_toggle")

if mobile_mode:
    page = st.selectbox("Page", PAGES, key="page_select_mobile")
else:
    # Tabs in laptop mode
    page = st.radio("Page", PAGES, horizontal=True, key="page_select_radio")

# Render pages
if page == "POS":
    page_pos()
elif page == "Event Mode":
    page_event_mode()
elif page == "Orders":
    page_orders()
elif page == "Dashboard":
    page_dashboard()
elif page == "Adjust Stock":
    page_adjust_stock()
elif page == "Menu Admin":
    page_menu_admin()
elif page == "Items":
    page_items()
elif page == "Movements":
    page_movements()
