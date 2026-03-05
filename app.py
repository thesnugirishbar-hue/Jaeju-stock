import os
from datetime import datetime
from decimal import Decimal

import pandas as pd
import streamlit as st

import psycopg
from psycopg.rows import dict_row

# =========================
# Config
# =========================
APP_TITLE = "JAEJU Stock + POS + Events (Postgres)"
DEFAULT_LOCATIONS = ["Food Truck", "Prep Kitchen"]

st.set_page_config(page_title=APP_TITLE, layout="wide")

# =========================
# Helper for menu mix
# =========================
def rebalance_mix(changed_key: str, keys: list[str]):
    vals = {k: float(st.session_state.get(k, 0.0)) for k in keys}

    changed_val = max(0.0, min(100.0, float(st.session_state.get(changed_key, 0.0))))
    st.session_state[changed_key] = changed_val

    other_keys = [k for k in keys if k != changed_key]
    remaining = 100.0 - changed_val

    if not other_keys:
        return

    other_sum = sum(vals[k] for k in other_keys)

    if other_sum <= 1e-9:
        even = remaining / len(other_keys)
        for k in other_keys:
            st.session_state[k] = even
    else:
        scale = remaining / other_sum
        for k in other_keys:
            st.session_state[k] = float(st.session_state.get(k, 0.0)) * scale


# =========================
# Secrets / DB URL
# =========================
def get_database_url() -> str | None:
    try:
        if "DATABASE_URL" in st.secrets:
            return str(st.secrets["DATABASE_URL"]).strip()
    except Exception:
        pass

    url = os.getenv("DATABASE_URL", "").strip()
    return url or None


DATABASE_URL = get_database_url()

# =========================
# Schema (CLEAN + ORDER TRANSFERS)
# =========================
SCHEMA_SQL = r"""
create table if not exists public.locations (
  name text primary key
);

create table if not exists public.items (
  id bigserial primary key,
  name text not null unique,
  unit text not null default 'Each',
  par numeric not null default 0,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

-- Current stock snapshot (one row per item per location)
create table if not exists public.stocks (
  item_id bigint not null references public.items(id) on delete cascade,
  location text not null references public.locations(name) on delete cascade,
  qty numeric not null default 0,
  updated_at timestamptz not null default now(),
  primary key (item_id, location)
);

-- Movement log (audit trail)
create table if not exists public.movements (
  id bigserial primary key,
  item_id bigint not null references public.items(id) on delete cascade,
  location text not null references public.locations(name) on delete cascade,
  qty_delta numeric not null,
  reason text not null default 'adjustment',
  note text null,
  created_at timestamptz not null default now()
);

create table if not exists public.menu_items (
  id bigserial primary key,
  name text not null unique,
  price numeric not null default 0,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

-- Recipe / ingredient usage per menu item
create table if not exists public.menu_item_ingredients (
  menu_item_id bigint not null references public.menu_items(id) on delete cascade,
  item_id bigint not null references public.items(id) on delete cascade,
  qty_per_sale numeric not null default 0,
  primary key (menu_item_id, item_id)
);

create table if not exists public.events (
  id bigserial primary key,
  name text not null unique,
  starts_on date null,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

-- POS sales log (menu-based)
create table if not exists public.sales (
  id bigserial primary key,
  sold_at timestamptz not null default now(),
  menu_item_id bigint not null references public.menu_items(id),
  qty numeric not null default 1,
  price_each numeric not null default 0,
  payment text not null default 'EFTPOS',
  location text not null references public.locations(name),
  event_name text null
);

-- =========================
-- Transfer Orders (Prep Kitchen -> Food Truck etc)
-- =========================
create table if not exists public.transfer_orders (
  id bigserial primary key,
  created_at timestamptz not null default now(),
  from_location text not null references public.locations(name),
  to_location text not null references public.locations(name),
  status text not null default 'draft', -- draft | received | cancelled
  note text null
);

create table if not exists public.transfer_order_lines (
  id bigserial primary key,
  order_id bigint not null references public.transfer_orders(id) on delete cascade,
  item_id bigint not null references public.items(id) on delete cascade,
  qty numeric not null default 0
);

-- Helpful indexes
create index if not exists idx_movements_item_time on public.movements(item_id, created_at desc);
create index if not exists idx_sales_time on public.sales(sold_at desc);
create index if not exists idx_sales_event on public.sales(event_name);
create index if not exists idx_transfer_orders_time on public.transfer_orders(created_at desc);
create index if not exists idx_transfer_lines_order on public.transfer_order_lines(order_id);
"""


# =========================
# DB Helpers (POOLER SAFE)
#  - open/close per query
#  - prepare_threshold=0 disables prepared statements (fixes pooler DuplicatePreparedStatement)
# =========================
def connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set.")

    return psycopg.connect(
        DATABASE_URL,
        autocommit=True,
        row_factory=dict_row,
    )
  
def exec_sql(sql: str, params=None, fetch: str | None = None):
    params = params or ()
    with connect() as conn:
        with conn.cursor(binary=False) as cur:   # ← important
            cur.execute(sql, params)

            if fetch == "one":
                return cur.fetchone()

            if fetch == "all":
                return cur.fetchall()

    return None

def exec_schema(sql_blob: str):
    """
    Runs a big SQL schema safely by executing one statement at a time.
    Fixes: 'cannot insert multiple commands into a prepared statement'
    """
    # Split on semicolons
    statements = [s.strip() for s in sql_blob.split(";") if s.strip()]

    # Run each statement separately
    for stmt in statements:
        exec_sql(stmt + ";")

@st.cache_data(ttl=10)
def read_sql(sql: str, params=None) -> list[dict]:
    params = params or ()
    rows = exec_sql(sql, params=params, fetch="all")
    return rows or []


def invalidate_cache():
    st.cache_data.clear()


    # Ensure default locations exist
    for loc in DEFAULT_LOCATIONS:
        exec_sql(
            "insert into public.locations(name) values (%s) on conflict do nothing;",
            (loc,),
        )


# =========================
# Business logic
# =========================
def ensure_stocks_for_item(item_id: int):
    for loc in DEFAULT_LOCATIONS:
        exec_sql(
            """
            insert into public.stocks(item_id, location, qty)
            values (%s, %s, 0)
            on conflict (item_id, location) do nothing;
            """,
            (item_id, loc),
        )


def upsert_item(name: str, unit: str, par: Decimal, active: bool):
    row = exec_sql(
        """
        insert into public.items(name, unit, par, active)
        values (%s, %s, %s, %s)
        on conflict (name)
        do update set unit=excluded.unit, par=excluded.par, active=excluded.active
        returning id;
        """,
        (name.strip(), unit.strip(), par, active),
        fetch="one",
    )
    if row and "id" in row:
        ensure_stocks_for_item(int(row["id"]))
    invalidate_cache()


def add_stock_delta(item_id: int, location: str, qty_delta: Decimal, reason: str, note: str | None = None):
    # Ensure snapshot row exists
    exec_sql(
        """
        insert into public.stocks(item_id, location, qty, updated_at)
        values (%s, %s, 0, now())
        on conflict (item_id, location) do nothing;
        """,
        (item_id, location),
    )
    # Apply delta
    exec_sql(
        """
        update public.stocks
        set qty = qty + %s, updated_at = now()
        where item_id=%s and location=%s;
        """,
        (qty_delta, item_id, location),
    )
    # Log movement
    exec_sql(
        """
        insert into public.movements(item_id, location, qty_delta, reason, note)
        values (%s, %s, %s, %s, %s);
        """,
        (item_id, location, qty_delta, reason, (note.strip() if note else None)),
    )
    invalidate_cache()


def set_stock(item_id: int, location: str, new_qty: Decimal, note: str = ""):
    cur = exec_sql(
        "select qty from public.stocks where item_id=%s and location=%s;",
        (item_id, location),
        fetch="one",
    )
    cur_qty = Decimal(str(cur["qty"])) if cur else Decimal("0")
    delta = new_qty - cur_qty

    exec_sql(
        """
        insert into public.stocks(item_id, location, qty, updated_at)
        values (%s, %s, %s, now())
        on conflict (item_id, location)
        do update set qty=excluded.qty, updated_at=now();
        """,
        (item_id, location, new_qty),
    )

    exec_sql(
        """
        insert into public.movements(item_id, location, qty_delta, reason, note)
        values (%s, %s, %s, 'adjustment', %s);
        """,
        (item_id, location, delta, note.strip() or None),
    )

    invalidate_cache()


def upsert_menu_item(name: str, price: Decimal, active: bool):
    exec_sql(
        """
        insert into public.menu_items(name, price, active)
        values (%s, %s, %s)
        on conflict (name)
        do update set price=excluded.price, active=excluded.active;
        """,
        (name.strip(), price, active),
    )
    invalidate_cache()


def set_recipe(menu_item_id: int, item_id: int, qty_per_sale: Decimal):
    exec_sql(
        """
        insert into public.menu_item_ingredients(menu_item_id, item_id, qty_per_sale)
        values (%s, %s, %s)
        on conflict (menu_item_id, item_id)
        do update set qty_per_sale=excluded.qty_per_sale;
        """,
        (menu_item_id, item_id, qty_per_sale),
    )
    invalidate_cache()


def record_sale(menu_item_id: int, qty: Decimal, price_each: Decimal, payment: str, location: str, event_name: str | None):
    exec_sql(
        """
        insert into public.sales(menu_item_id, qty, price_each, payment, location, event_name)
        values (%s, %s, %s, %s, %s, %s);
        """,
        (menu_item_id, qty, price_each, payment, location, (event_name.strip() if event_name else None)),
    )

    # Deduct ingredients based on recipe
    ingredients = read_sql(
        """
        select mii.item_id, mii.qty_per_sale
        from public.menu_item_ingredients mii
        where mii.menu_item_id = %s;
        """,
        (menu_item_id,),
    )

    for ing in ingredients:
        ing_item_id = int(ing["item_id"])
        per_sale = Decimal(str(ing["qty_per_sale"]))
        deduct = -(per_sale * qty)
        add_stock_delta(
            item_id=ing_item_id,
            location=location,
            qty_delta=deduct,
            reason="sale_deduct",
            note=f"Auto-deduct via sale menu_item_id={menu_item_id}",
        )

    invalidate_cache()


# =========================
# Transfer Orders
# =========================
def create_transfer_order(from_location: str, to_location: str, note: str | None, lines: list[dict]) -> int:
    """
    lines: [{"item_id": int, "qty": Decimal}, ...]
    """
    row = exec_sql(
        """
        insert into public.transfer_orders(from_location, to_location, status, note)
        values (%s, %s, 'draft', %s)
        returning id;
        """,
        (from_location, to_location, (note.strip() if note else None)),
        fetch="one",
    )
    order_id = int(row["id"])
    for ln in lines:
        exec_sql(
            """
            insert into public.transfer_order_lines(order_id, item_id, qty)
            values (%s, %s, %s);
            """,
            (order_id, int(ln["item_id"]), Decimal(str(ln["qty"]))),
        )
    invalidate_cache()
    return order_id


def list_transfer_orders(status: str = "draft") -> pd.DataFrame:
    return pd.DataFrame(
        read_sql(
            """
            select id, created_at, from_location, to_location, status, note
            from public.transfer_orders
            where status = %s
            order by created_at desc;
            """,
            (status,),
        )
    )


def get_transfer_order_lines(order_id: int) -> pd.DataFrame:
    return pd.DataFrame(
        read_sql(
            """
            select l.id as line_id, l.order_id, i.name as item, i.unit, l.qty, l.item_id
            from public.transfer_order_lines l
            join public.items i on i.id = l.item_id
            where l.order_id = %s
            order by i.name asc;
            """,
            (order_id,),
        )
    )


def receive_transfer_order(order_id: int):
    ord_row = exec_sql(
        "select * from public.transfer_orders where id=%s;",
        (order_id,),
        fetch="one",
    )
    if not ord_row:
        raise RuntimeError("Order not found.")
    if ord_row["status"] != "draft":
        raise RuntimeError("Only draft orders can be received.")

    from_loc = ord_row["from_location"]
    to_loc = ord_row["to_location"]

    lines = read_sql(
        "select item_id, qty from public.transfer_order_lines where order_id=%s;",
        (order_id,),
    )
    if not lines:
        raise RuntimeError("No lines on this order.")

    # Apply stock movements:
    # - deduct from from_loc
    # - add to to_loc
    for ln in lines:
        item_id = int(ln["item_id"])
        qty = Decimal(str(ln["qty"]))

        if qty == 0:
            continue

        add_stock_delta(
            item_id=item_id,
            location=from_loc,
            qty_delta=-qty,
            reason="transfer_out",
            note=f"Transfer order #{order_id} to {to_loc}",
        )
        add_stock_delta(
            item_id=item_id,
            location=to_loc,
            qty_delta=qty,
            reason="transfer_in",
            note=f"Transfer order #{order_id} from {from_loc}",
        )

    exec_sql(
        "update public.transfer_orders set status='received' where id=%s;",
        (order_id,),
    )
    invalidate_cache()


# =========================
# Data for UI
# =========================
def df_items(active_only: bool = False) -> pd.DataFrame:
    sql = "select id, name, unit, par, active, created_at from public.items"
    params = ()
    if active_only:
        sql += " where active = true"
    sql += " order by name asc"
    return pd.DataFrame(read_sql(sql, params))


def df_menu(active_only: bool = False) -> pd.DataFrame:
    sql = "select id, name, price, active, created_at from public.menu_items"
    if active_only:
        sql += " where active = true"
    sql += " order by name asc"
    return pd.DataFrame(read_sql(sql))


def df_stocks() -> pd.DataFrame:
    return pd.DataFrame(
        read_sql(
            """
            select i.name as item, i.unit, s.location, s.qty, s.updated_at
            from public.stocks s
            join public.items i on i.id = s.item_id
            where i.active = true
            order by i.name asc, s.location asc;
            """
        )
    )


def df_low_stock() -> pd.DataFrame:
    return pd.DataFrame(
        read_sql(
            """
            select i.name as item, i.unit, i.par, s.location, s.qty
            from public.stocks s
            join public.items i on i.id = s.item_id
            where i.active = true and s.qty < i.par
            order by (i.par - s.qty) desc, i.name asc;
            """
        )
    )


def df_recent_movements(limit: int = 50) -> pd.DataFrame:
    return pd.DataFrame(
        read_sql(
            """
            select m.created_at, i.name as item, m.location, m.qty_delta, m.reason, m.note
            from public.movements m
            join public.items i on i.id = m.item_id
            order by m.created_at desc
            limit %s;
            """,
            (limit,),
        )
    )


def df_sales_today() -> pd.DataFrame:
    return pd.DataFrame(
        read_sql(
            """
            select s.sold_at, mi.name as menu_item, s.qty, s.price_each, (s.qty*s.price_each) as total,
                   s.payment, s.location, s.event_name
            from public.sales s
            join public.menu_items mi on mi.id = s.menu_item_id
            where s.sold_at::date = current_date
            order by s.sold_at desc;
            """
        )
    )


# =========================
# UI Pages
# =========================
def page_dashboard():
    st.header("Dashboard")

    items = df_items(active_only=True)
    if items.empty:
        st.info("No items yet. Add items in Items tab.")
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Active items", int(items.shape[0]))

    sales_today = df_sales_today()
    with col2:
        total_today = float(sales_today["total"].sum()) if not sales_today.empty else 0.0
        st.metric("Sales today (NZD)", f"{total_today:,.2f}")

    low = df_low_stock()
    with col3:
        st.metric("Low stock rows", int(0 if low.empty else low.shape[0]))

    st.subheader("Low stock")
    if low.empty:
        st.success("No low stock (qty >= par).")
    else:
        st.dataframe(low, use_container_width=True, hide_index=True)

    st.subheader("Stock snapshot")
    snap = df_stocks()
    if snap.empty:
        st.warning("No stock rows yet. Use Adjust Stock or Receive a transfer order once.")
    else:
        st.dataframe(snap, use_container_width=True, hide_index=True)

    st.subheader("Sales today")
    if sales_today.empty:
        st.caption("No sales recorded today yet.")
    else:
        st.dataframe(sales_today, use_container_width=True, hide_index=True)


def page_items():
    st.header("Items")

    df = df_items(active_only=False)
    if df.empty:
        st.info("No items yet. Add one below.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Add / Update item")

    with st.form("item_form", clear_on_submit=True):
        name = st.text_input("Name")
        unit = st.text_input("Unit", value="Each")
        par = st.number_input("Par level", min_value=0.0, value=0.0, step=1.0)
        active = st.checkbox("Active", value=True)
        submitted = st.form_submit_button("Save item")

    if submitted:
        if not name.strip():
            st.error("Item name is required.")
            return
        upsert_item(name=name, unit=unit, par=Decimal(str(par)), active=active)
        st.success("Saved.")
        st.rerun()


def page_menu_admin():
    st.header("Menu Admin")

    menu = df_menu(active_only=False)
    if menu.empty:
        st.info("No menu items yet.")
    else:
        st.dataframe(menu, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Add / Update menu item")
    with st.form("menu_form", clear_on_submit=True):
        name = st.text_input("Menu item name")
        price = st.number_input("Price (NZD)", min_value=0.0, value=0.0, step=0.5)
        active = st.checkbox("Active", value=True)
        submit = st.form_submit_button("Save menu item")

    if submit:
        if not name.strip():
            st.error("Menu item name is required.")
            return
        upsert_menu_item(name=name, price=Decimal(str(price)), active=active)
        st.success("Saved.")
        st.rerun()

    st.divider()
    st.subheader("Recipe builder (ingredients per 1 sale)")

    items = df_items(active_only=True)
    menu_active = df_menu(active_only=True)
    if items.empty or menu_active.empty:
        st.info("You need at least 1 active item AND 1 active menu item.")
        return

    mi = st.selectbox("Menu item", menu_active["name"].tolist())
    mi_id = int(menu_active.loc[menu_active["name"] == mi, "id"].iloc[0])

    recipe_rows = read_sql(
        """
        select i.name as item, mii.qty_per_sale
        from public.menu_item_ingredients mii
        join public.items i on i.id = mii.item_id
        where mii.menu_item_id = %s
        order by i.name;
        """,
        (mi_id,),
    )
    if recipe_rows:
        st.caption("Current recipe:")
        st.dataframe(pd.DataFrame(recipe_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No ingredients set yet for this menu item.")

    with st.form("recipe_form", clear_on_submit=True):
        ing_name = st.selectbox("Ingredient item", items["name"].tolist())
        ing_id = int(items.loc[items["name"] == ing_name, "id"].iloc[0])
        qty_per_sale = st.number_input("Qty per sale (in the item unit)", value=0.0, step=0.1)
        save_ing = st.form_submit_button("Save ingredient line")

    if save_ing:
        set_recipe(menu_item_id=mi_id, item_id=ing_id, qty_per_sale=Decimal(str(qty_per_sale)))
        st.success("Saved recipe line.")
        st.rerun()


def page_adjust_stock():
    st.header("Adjust Stock")

    items = df_items(active_only=True)
    if items.empty:
        st.info("Add items first.")
        return

    item_name = st.selectbox("Item", items["name"].tolist())
    item_id = int(items.loc[items["name"] == item_name, "id"].iloc[0])

    location = st.selectbox("Location", DEFAULT_LOCATIONS)
    current = exec_sql(
        "select qty from public.stocks where item_id=%s and location=%s;",
        (item_id, location),
        fetch="one",
    )
    cur_qty = float(current["qty"]) if current else 0.0
    st.caption(f"Current qty: {cur_qty:g}")

    new_qty = st.number_input("Set new qty", value=float(cur_qty), step=1.0)
    note = st.text_input("Note (optional)")

    if st.button("Save stock adjustment", type="primary"):
        set_stock(item_id=item_id, location=location, new_qty=Decimal(str(new_qty)), note=note)
        st.success("Stock updated.")
        st.rerun()


def page_movements():
    st.header("Movements")
    df = df_recent_movements(limit=200)
    if df.empty:
        st.info("No movements yet.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)


def page_pos():
    st.header("POS")

    menu = df_menu(active_only=True)
    if menu.empty:
        st.info("Add menu items in Menu Admin first.")
        return

    col1, col2, col3, col4, col5 = st.columns([3, 1, 2, 2, 3])

    with col1:
        menu_name = st.selectbox("Menu item", menu["name"].tolist())
    menu_id = int(menu.loc[menu["name"] == menu_name, "id"].iloc[0])
    default_price = float(menu.loc[menu["name"] == menu_name, "price"].iloc[0])

    with col2:
        qty = st.number_input("Qty", min_value=1.0, value=1.0, step=1.0)

    with col3:
        payment = st.selectbox("Payment", ["EFTPOS", "Cash", "Online", "Other"])

    with col4:
        location = st.selectbox("Location", DEFAULT_LOCATIONS, index=0)

    with col5:
        event_name = st.text_input("Event name (optional)", placeholder="Electric Ave Day 1")

    price_each = st.number_input("Price each (NZD)", min_value=0.0, value=float(default_price), step=0.5)
    line_total = float(qty) * float(price_each)
    st.metric("Line total", f"${line_total:,.2f}")

    if st.button("Record sale", type="primary"):
        record_sale(
            menu_item_id=menu_id,
            qty=Decimal(str(qty)),
            price_each=Decimal(str(price_each)),
            payment=payment,
            location=location,
            event_name=event_name.strip() or None,
        )
        st.success("Sale recorded (and ingredients deducted if recipe exists).")
        st.rerun()


def page_event_mode():
    st.header("Event Mode")

    event_name = st.text_input("Event name", placeholder="Electric Ave Day 1")
    st.caption("Tip: Use POS and fill Event name to group sales by event.")

    if not event_name.strip():
        st.info("Enter an event name to view event sales.")
        return

    rows = read_sql(
        """
        select mi.name as menu_item, sum(s.qty) as qty, sum(s.qty*s.price_each) as revenue
        from public.sales s
        join public.menu_items mi on mi.id = s.menu_item_id
        where s.event_name = %s
        group by mi.name
        order by revenue desc;
        """,
        (event_name.strip(),),
    )
    if not rows:
        st.warning("No sales found for that event name yet.")
        return

    df = pd.DataFrame(rows)
    st.subheader("Event sales summary")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.metric("Total revenue", f"${float(df['revenue'].sum()):,.2f}")


def page_orders():
    st.header("Orders / Transfers")

    tab1, tab2, tab3 = st.tabs(["Create transfer (Prep → Truck)", "Receive transfer", "Below par list"])

    items = df_items(active_only=True)

    with tab1:
        st.subheader("Create transfer order")
        if items.empty:
            st.info("Add items first.")
        else:
            st.caption("Build an order in the prep kitchen, then receive it into the truck (stock transfer).")

            from_location = st.selectbox(
                "From location",
                DEFAULT_LOCATIONS,
                index=DEFAULT_LOCATIONS.index("Prep Kitchen") if "Prep Kitchen" in DEFAULT_LOCATIONS else 0,
                key="tr_from_loc",
            )
            to_location = st.selectbox(
                "To location",
                DEFAULT_LOCATIONS,
                index=DEFAULT_LOCATIONS.index("Food Truck") if "Food Truck" in DEFAULT_LOCATIONS else 0,
                key="tr_to_loc",
            )
            note = st.text_input("Note (optional)", key="tr_note", placeholder="Electric Ave Day 1 restock")

            st.write("")

            # cart stored in session
            st.session_state.setdefault("transfer_cart", [])  # list of dicts {item_id, item, unit, qty}

            c1, c2, c3 = st.columns([3, 1, 2])
            with c1:
                item_name = st.selectbox("Item", items["name"].tolist(), key="tr_item_pick")
            with c2:
                qty = st.number_input("Qty", min_value=0.0, value=0.0, step=1.0, key="tr_qty_pick")
            with c3:
                st.write("")
                st.write("")
                add_btn = st.button("Add line", type="primary")

            if add_btn:
                if qty <= 0:
                    st.error("Qty must be > 0")
                else:
                    row = items.loc[items["name"] == item_name].iloc[0]
                    st.session_state["transfer_cart"].append(
                        {
                            "item_id": int(row["id"]),
                            "item": row["name"],
                            "unit": row["unit"],
                            "qty": float(qty),
                        }
                    )
                    st.session_state["tr_qty_pick"] = 0.0
                    st.rerun()

            cart = st.session_state.get("transfer_cart", [])
            if cart:
                st.write("### Draft lines")
                df_cart = pd.DataFrame(cart)
                st.dataframe(df_cart[["item", "unit", "qty"]], use_container_width=True, hide_index=True)

                colA, colB = st.columns([1, 1])
                with colA:
                    if st.button("Clear draft"):
                        st.session_state["transfer_cart"] = []
                        st.rerun()
                with colB:
                    if st.button("Save transfer order", type="primary"):
                        if from_location == to_location:
                            st.error("From and To locations must be different.")
                        else:
                            order_id = create_transfer_order(
                                from_location=from_location,
                                to_location=to_location,
                                note=note,
                                lines=[{"item_id": ln["item_id"], "qty": Decimal(str(ln["qty"]))} for ln in cart],
                            )
                            st.session_state["transfer_cart"] = []
                            st.success(f"Saved transfer order #{order_id} (draft). Now go to Receive transfer tab.")
                            st.rerun()
            else:
                st.info("No lines yet. Add at least 1 line to create an order.")

    with tab2:
        st.subheader("Receive transfer order (applies stock movement)")
        drafts = list_transfer_orders(status="draft")
        if drafts.empty:
            st.success("No draft transfer orders.")
        else:
            drafts_display = drafts.copy()
            drafts_display["label"] = drafts_display.apply(
                lambda r: f"#{int(r['id'])} | {r['created_at']} | {r['from_location']} → {r['to_location']} | {r.get('note') or ''}".strip(),
                axis=1,
            )
            picked = st.selectbox("Draft orders", drafts_display["label"].tolist())
            picked_id = int(picked.split("|")[0].replace("#", "").strip())

            lines = get_transfer_order_lines(picked_id)
            if not lines.empty:
                st.write("### Lines")
                st.dataframe(lines[["item", "unit", "qty"]], use_container_width=True, hide_index=True)

            st.warning("Receiving will: deduct stock from FROM location and add to TO location. This is permanent.")
            if st.button("Receive this order", type="primary"):
                try:
                    receive_transfer_order(picked_id)
                    st.success(f"Order #{picked_id} received. Stock moved.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

    with tab3:
        st.subheader("Below par list (quick shopping list)")
        low = df_low_stock()
        if low.empty:
            st.success("Nothing below par right now.")
        else:
            st.dataframe(low, use_container_width=True, hide_index=True)


def page_prep_planner():
    st.header("Prep Planner (Turnover → Units → Ingredients)")

    menu = [
        ("Small KFC on chips", 18.0, "mix_small"),
        ("Large KFC on chips", 25.0, "mix_large"),
        ("KFC burger", 17.0, "mix_kfc_burger"),
        ("Cauliflower burger", 16.0, "mix_caul_burger"),
        ("Bulgogi smash", 14.0, "mix_bulgogi"),
        ("Double Bulgogi smash", 19.0, "mix_double_bulgogi"),
        ("Chips", 9.0, "mix_chips"),
    ]
    keys = [k for _, _, k in menu]

    defaults = {
        "mix_small": 35.0,
        "mix_large": 25.0,
        "mix_kfc_burger": 10.0,
        "mix_caul_burger": 5.0,
        "mix_bulgogi": 10.0,
        "mix_double_bulgogi": 10.0,
        "mix_chips": 5.0,
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)

    st.subheader("1) Target turnover")
    target = st.number_input("Target turnover ($)", min_value=0.0, value=8000.0, step=100.0)

    st.subheader("2) Menu mix (auto-balances to 100%)")
    for label, price, key in menu:
        st.slider(
            f"{label} (${price:.0f})",
            min_value=0.0,
            max_value=100.0,
            step=1.0,
            key=key,
            on_change=rebalance_mix,
            kwargs={"changed_key": key, "keys": keys},
        )

    total_pct = sum(float(st.session_state[k]) for k in keys)
    st.caption(f"Total: {total_pct:.0f}% (always stays at 100%)")

    st.subheader("3) Estimated units sold")
    rows = []
    for label, price, key in menu:
        pct = float(st.session_state[key])
        revenue = target * (pct / 100.0)
        units = 0.0 if price <= 0 else (revenue / price)
        rows.append((label, price, pct, revenue, units))

    st.dataframe(
        [
            {
                "Item": r[0],
                "Price": r[1],
                "Mix %": round(r[2], 0),
                "Revenue share ($)": round(r[3], 2),
                "Estimated units": round(r[4], 1),
            }
            for r in rows
        ],
        use_container_width=True,
    )

    st.info("Next step: connect recipes so this outputs ingredient quantities + a draft transfer order.")


# =========================
# Main
# =========================
def main():
    st.title(APP_TITLE)

    if not DATABASE_URL:
        st.error(
            "DATABASE_URL not set.\n\n"
            "In Streamlit → App settings → Secrets:\n\n"
            '```toml\nDATABASE_URL = "postgresql://...:6543/postgres"\n```\n\n'
            "Use Supabase **Transaction pooler** (port 6543)."
        )
        st.stop()

    with st.expander("🔧 DB connection test"):
        try:
            test = exec_sql("select now() as now;", fetch="one")
            st.success("DB connected.")
            st.write(test)
            st.caption("If you see DuplicatePreparedStatement: pooler + prepared statements weren’t disabled.")
        except Exception as e:
            st.error("DB init failed. Check DATABASE_URL and Supabase pooler settings.")
            st.exception(e)
            st.stop()

    mobile_mode = st.toggle("Mobile mode", value=False)

    pages = [
        "POS",
        "Event Mode",
        "Orders",
        "Dashboard",
        "Adjust Stock",
        "Menu Admin",
        "Items",
        "Movements",
        "Prep Planner",
    ]

    st.write("")

    if mobile_mode:
        page = st.selectbox("Page", pages, index=pages.index("Dashboard"))
    else:
        page = st.radio("Page", pages, horizontal=True, index=pages.index("Dashboard"))

    if page == "Dashboard":
        page_dashboard()
    elif page == "Items":
        page_items()
    elif page == "Menu Admin":
        page_menu_admin()
    elif page == "Adjust Stock":
        page_adjust_stock()
    elif page == "Movements":
        page_movements()
    elif page == "POS":
        page_pos()
    elif page == "Event Mode":
        page_event_mode()
    elif page == "Prep Planner":
        page_prep_planner()
    elif page == "Orders":
        page_orders()


if __name__ == "__main__":
    main()
