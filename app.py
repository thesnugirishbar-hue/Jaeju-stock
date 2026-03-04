import os
from datetime import datetime, date
from contextlib import contextmanager

import pandas as pd
import streamlit as st

import psycopg
from psycopg.rows import dict_row


# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
st.set_page_config(page_title="JAEJU Stock + POS + Events", layout="wide")

LOC_TRUCK = "Food Truck"
LOC_PREP = "Prep Kitchen"
LOCATIONS = [LOC_TRUCK, LOC_PREP]

PAYMENT_EFTPOS = "EFTPOS"
PAYMENT_CASH = "CASH"
PAYMENTS = [PAYMENT_EFTPOS, PAYMENT_CASH]


# ------------------------------------------------------------
# DB CONNECTION (FAST + SUPABASE POOLER SAFE)
# - cached connection factory
# - disables prepared statements (Supabase transaction pooler doesn't support PREPARE)
# ------------------------------------------------------------
def _get_database_url() -> str | None:
    # Streamlit secrets first
    if "DATABASE_URL" in st.secrets:
        return st.secrets["DATABASE_URL"]
    # fallback to environment variable
    return os.getenv("DATABASE_URL")


@st.cache_resource(show_spinner=False)
def _get_conninfo() -> str:
    url = _get_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL not set. Add it to Streamlit Secrets or env vars.")
    return url


@contextmanager
def get_conn():
    conninfo = _get_conninfo()
    # dict_row gives us dicts back (easier than tuples)
    conn = psycopg.connect(conninfo, row_factory=dict_row)
    # IMPORTANT for Supabase Transaction Pooler
    try:
        conn.prepare_threshold = 0  # disable prepared statements
    except Exception:
        pass

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def exec_sql(sql: str, params=None, fetch: str | None = None):
    params = params or ()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None


# ------------------------------------------------------------
# SCHEMA
# ------------------------------------------------------------
def init_db():
    exec_sql(
        """
        create table if not exists public.items (
            id bigserial primary key,
            name text not null,
            unit text not null default '',
            par numeric not null default 0,
            active boolean not null default true,
            created_at timestamptz not null default now()
        );

        create unique index if not exists items_name_unique on public.items (lower(name));

        create table if not exists public.stock (
            item_id bigint not null references public.items(id) on delete cascade,
            location text not null,
            qty numeric not null default 0,
            updated_at timestamptz not null default now(),
            primary key (item_id, location)
        );

        create table if not exists public.movements (
            id bigserial primary key,
            item_id bigint not null references public.items(id) on delete cascade,
            location_from text,
            location_to text,
            qty numeric not null,
            reason text not null default '',
            created_at timestamptz not null default now()
        );

        create table if not exists public.menu (
            id bigserial primary key,
            name text not null,
            price numeric not null default 0,
            active boolean not null default true,
            created_at timestamptz not null default now()
        );

        create unique index if not exists menu_name_unique on public.menu (lower(name));

        create table if not exists public.menu_ingredients (
            menu_id bigint not null references public.menu(id) on delete cascade,
            item_id bigint not null references public.items(id) on delete cascade,
            qty_per_unit numeric not null default 0,
            primary key (menu_id, item_id)
        );

        create table if not exists public.sales (
            id bigserial primary key,
            sold_at timestamptz not null default now(),
            menu_id bigint not null references public.menu(id) on delete restrict,
            qty integer not null default 1,
            price_each numeric not null default 0,
            payment text not null default 'EFTPOS',
            location text not null default 'Food Truck',
            event_name text
        );
        """,
        fetch=None,
    )


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------
def ensure_stock_row(item_id: int, location: str):
    exec_sql(
        """
        insert into public.stock (item_id, location, qty)
        values (%s, %s, 0)
        on conflict (item_id, location) do nothing;
        """,
        (item_id, location),
    )


def set_stock(item_id: int, location: str, new_qty: float, reason: str):
    ensure_stock_row(item_id, location)

    row = exec_sql(
        "select qty from public.stock where item_id=%s and location=%s",
        (item_id, location),
        fetch="one",
    )
    old_qty = float(row["qty"]) if row else 0.0
    delta = float(new_qty) - old_qty

    exec_sql(
        """
        update public.stock
        set qty=%s, updated_at=now()
        where item_id=%s and location=%s
        """,
        (new_qty, item_id, location),
    )

    # movement (adjustment)
    exec_sql(
        """
        insert into public.movements (item_id, location_from, location_to, qty, reason)
        values (%s, %s, %s, %s, %s)
        """,
        (item_id, None, location, delta, reason),
    )


def add_stock(item_id: int, location: str, delta: float, reason: str):
    ensure_stock_row(item_id, location)
    exec_sql(
        """
        update public.stock
        set qty = qty + %s,
            updated_at = now()
        where item_id=%s and location=%s
        """,
        (delta, item_id, location),
    )
    exec_sql(
        """
        insert into public.movements (item_id, location_from, location_to, qty, reason)
        values (%s, %s, %s, %s, %s)
        """,
        (item_id, None, location, delta, reason),
    )


def transfer_stock(item_id: int, from_loc: str, to_loc: str, qty: float, reason: str):
    ensure_stock_row(item_id, from_loc)
    ensure_stock_row(item_id, to_loc)

    # decrease from
    exec_sql(
        """
        update public.stock
        set qty = qty - %s,
            updated_at = now()
        where item_id=%s and location=%s
        """,
        (qty, item_id, from_loc),
    )
    # increase to
    exec_sql(
        """
        update public.stock
        set qty = qty + %s,
            updated_at = now()
        where item_id=%s and location=%s
        """,
        (qty, item_id, to_loc),
    )

    exec_sql(
        """
        insert into public.movements (item_id, location_from, location_to, qty, reason)
        values (%s, %s, %s, %s, %s)
        """,
        (item_id, from_loc, to_loc, qty, reason),
    )


@st.cache_data(ttl=8, show_spinner=False)
def load_items(active_only: bool = False) -> pd.DataFrame:
    if active_only:
        rows = exec_sql(
            """
            select id, name, unit, par, active, created_at
            from public.items
            where active = true
            order by lower(name)
            """,
            fetch="all",
        )
    else:
        rows = exec_sql(
            """
            select id, name, unit, par, active, created_at
            from public.items
            order by lower(name)
            """,
            fetch="all",
        )
    return pd.DataFrame(rows or [])


@st.cache_data(ttl=8, show_spinner=False)
def load_stock(location: str) -> pd.DataFrame:
    rows = exec_sql(
        """
        select i.id as item_id, i.name, i.unit, i.par, i.active,
               coalesce(s.qty, 0) as qty,
               s.updated_at
        from public.items i
        left join public.stock s
            on s.item_id = i.id
           and s.location = %s
        where i.active = true
        order by lower(i.name)
        """,
        (location,),
        fetch="all",
    )
    return pd.DataFrame(rows or [])


@st.cache_data(ttl=8, show_spinner=False)
def load_menu(active_only: bool = True) -> pd.DataFrame:
    if active_only:
        rows = exec_sql(
            """
            select id, name, price, active, created_at
            from public.menu
            where active = true
            order by lower(name)
            """,
            fetch="all",
        )
    else:
        rows = exec_sql(
            """
            select id, name, price, active, created_at
            from public.menu
            order by lower(name)
            """,
            fetch="all",
        )
    return pd.DataFrame(rows or [])


@st.cache_data(ttl=8, show_spinner=False)
def load_menu_ingredients(menu_id: int) -> pd.DataFrame:
    rows = exec_sql(
        """
        select mi.menu_id, mi.item_id, i.name as item_name, i.unit, mi.qty_per_unit
        from public.menu_ingredients mi
        join public.items i on i.id = mi.item_id
        where mi.menu_id = %s
        order by lower(i.name)
        """,
        (menu_id,),
        fetch="all",
    )
    return pd.DataFrame(rows or [])


def invalidate_caches():
    load_items.clear()
    load_stock.clear()
    load_menu.clear()
    load_menu_ingredients.clear()


# ------------------------------------------------------------
# UI: DB CONNECTION TEST
# ------------------------------------------------------------
def ui_db_test():
    with st.expander("🔧 DB connection test", expanded=False):
        try:
            url = _get_database_url()
            st.success("DB secrets detected." if url else "No DATABASE_URL found.")
            st.write("Using DATABASE_URL:", bool(url))
            # quick query
            row = exec_sql("select now() as now", fetch="one")
            st.success(f"Connected ✅ Server time: {row['now']}")
        except Exception as e:
            st.error(f"DB connection failed: {e}")


# ------------------------------------------------------------
# PAGES
# ------------------------------------------------------------
def page_items():
    st.header("Items")

    df = load_items(active_only=False)
    if df.empty:
        st.info("No items yet. Add one below.")
    else:
        st.dataframe(df[["id", "name", "unit", "par", "active"]], use_container_width=True)

    with st.expander("➕ Add / Update item", expanded=False):
        col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
        existing = ["(new)"] + df["name"].tolist() if not df.empty else ["(new)"]
        pick = col1.selectbox("Item", existing, key="item_pick")

        name = col1.text_input("Name", value="" if pick == "(new)" else pick, key="item_name")
        unit = col2.text_input("Unit (e.g. Btl, Bag, Each)", value="", key="item_unit")
        par = col3.number_input("Par level", min_value=0.0, value=1.0, step=1.0, key="item_par")
        active = col4.checkbox("Active", value=True, key="item_active")

        if st.button("Save item", type="primary", key="save_item_btn"):
            nm = name.strip()
            if not nm:
                st.error("Name required.")
                return

            # if updating, keep unit/par if blank? we require explicit
            if pick != "(new)":
                # find id
                item_id = int(df[df["name"] == pick]["id"].iloc[0])
                exec_sql(
                    """
                    update public.items
                    set name=%s, unit=%s, par=%s, active=%s
                    where id=%s
                    """,
                    (nm, unit.strip(), float(par), bool(active), item_id),
                )
                # ensure stock rows exist for both locations so dropdowns behave
                ensure_stock_row(item_id, LOC_TRUCK)
                ensure_stock_row(item_id, LOC_PREP)
                st.success("Updated.")
            else:
                row = exec_sql(
                    """
                    insert into public.items (name, unit, par, active)
                    values (%s, %s, %s, %s)
                    returning id
                    """,
                    (nm, unit.strip(), float(par), bool(active)),
                    fetch="one",
                )
                item_id = int(row["id"])
                ensure_stock_row(item_id, LOC_TRUCK)
                ensure_stock_row(item_id, LOC_PREP)
                st.success("Added.")

            invalidate_caches()
            st.rerun()


def page_adjust_stock():
    st.header("Adjust Stock")

    df_items = load_items(active_only=True)
    if df_items.empty:
        st.info("Add items first in Items tab.")
        return

    colA, colB = st.columns([1, 2])
    location = colA.selectbox("Location", LOCATIONS, key="adj_location")

    item_map = {row["name"]: int(row["id"]) for _, row in df_items.iterrows()}
    item_name = colB.selectbox("Item", list(item_map.keys()), key="adj_item")

    item_id = item_map[item_name]
    stock_df = load_stock(location)
    cur_qty = 0.0
    if not stock_df.empty:
        match = stock_df[stock_df["item_id"] == item_id]
        if not match.empty:
            cur_qty = float(match["qty"].iloc[0])

    st.caption(f"Current qty at **{location}**: **{cur_qty:g}**")

    mode = st.radio("Mode", ["Set exact qty", "Add / subtract"], horizontal=True, key="adj_mode")
    reason = st.text_input("Reason (e.g. count, waste, received, correction)", value="count", key="adj_reason")

    if mode == "Set exact qty":
        new_qty = st.number_input("New qty", value=float(cur_qty), step=1.0, key="adj_newqty")
        if st.button("Save adjustment", type="primary", key="adj_save_set"):
            set_stock(item_id, location, float(new_qty), reason)
            invalidate_caches()
            st.success("Saved.")
            st.rerun()
    else:
        delta = st.number_input("Change (+ / -)", value=0.0, step=1.0, key="adj_delta")
        if st.button("Apply change", type="primary", key="adj_save_delta"):
            add_stock(item_id, location, float(delta), reason)
            invalidate_caches()
            st.success("Applied.")
            st.rerun()


def page_movements():
    st.header("Movements")

    rows = exec_sql(
        """
        select m.id, i.name as item, m.location_from, m.location_to, m.qty, m.reason, m.created_at
        from public.movements m
        join public.items i on i.id = m.item_id
        order by m.created_at desc
        limit 200
        """,
        fetch="all",
    )
    df = pd.DataFrame(rows or [])
    if df.empty:
        st.info("No movements yet.")
    else:
        st.dataframe(df, use_container_width=True)


def page_dashboard():
    st.header("Dashboard")

    # ✅ PATCH: this dashboard DOES NOT “lose” items.
    # It always reads public.items + left joins stock for the chosen location.

    location = st.selectbox("View location", LOCATIONS, key="dash_location")

    df = load_stock(location)
    if df.empty:
        st.info("No active items yet. Add items in Items tab.")
        return

    # show low stock flags
    df["qty"] = df["qty"].astype(float)
    df["par"] = df["par"].astype(float)
    df["below_par"] = df["qty"] < df["par"]

    col1, col2, col3 = st.columns(3)
    col1.metric("Active items", int(df.shape[0]))
    col2.metric("Below par", int(df["below_par"].sum()))
    col3.metric("Last updated", "" if df["updated_at"].isna().all() else str(df["updated_at"].max())[:19])

    st.subheader(f"Stock snapshot — {location}")
    show = df[["name", "unit", "qty", "par", "below_par"]].copy()
    st.dataframe(show, use_container_width=True)

    with st.expander("Suggested order to reach par (Food Truck only)", expanded=False):
        if location != LOC_TRUCK:
            st.info("Switch to Food Truck view to generate a par-based order.")
        else:
            order_df = df.copy()
            order_df["need"] = (order_df["par"] - order_df["qty"]).clip(lower=0)
            order_df = order_df[order_df["need"] > 0][["name", "unit", "qty", "par", "need"]]
            if order_df.empty:
                st.success("Nothing needed — all at/above par.")
            else:
                st.dataframe(order_df, use_container_width=True)


def page_menu_admin():
    st.header("Menu Admin")

    menu_df = load_menu(active_only=False)
    if menu_df.empty:
        st.info("No menu items yet. Add one below.")
    else:
        st.dataframe(menu_df[["id", "name", "price", "active"]], use_container_width=True)

    with st.expander("➕ Add / Update menu item", expanded=False):
        existing = ["(new)"] + menu_df["name"].tolist() if not menu_df.empty else ["(new)"]
        pick = st.selectbox("Menu item", existing, key="menu_pick")

        name = st.text_input("Name", value="" if pick == "(new)" else pick, key="menu_name")
        price = st.number_input("Price (NZD)", min_value=0.0, value=20.0, step=0.5, key="menu_price")
        active = st.checkbox("Active", value=True, key="menu_active")

        if st.button("Save menu item", type="primary", key="save_menu_btn"):
            nm = name.strip()
            if not nm:
                st.error("Name required.")
                return

            if pick != "(new)":
                menu_id = int(menu_df[menu_df["name"] == pick]["id"].iloc[0])
                exec_sql(
                    "update public.menu set name=%s, price=%s, active=%s where id=%s",
                    (nm, float(price), bool(active), menu_id),
                )
                st.success("Updated.")
            else:
                exec_sql(
                    "insert into public.menu (name, price, active) values (%s, %s, %s)",
                    (nm, float(price), bool(active)),
                )
                st.success("Added.")

            invalidate_caches()
            st.rerun()

    st.divider()
    st.subheader("Recipe / Ingredient mapping")

    menu_df_active = load_menu(active_only=True)
    items_df = load_items(active_only=True)
    if menu_df_active.empty or items_df.empty:
        st.info("Add menu items and items first.")
        return

    menu_choice = st.selectbox("Choose menu item", menu_df_active["name"].tolist(), key="recipe_menu_pick")
    menu_id = int(menu_df_active[menu_df_active["name"] == menu_choice]["id"].iloc[0])

    cur_map = load_menu_ingredients(menu_id)
    if cur_map.empty:
        st.info("No ingredients mapped yet.")
    else:
        st.dataframe(cur_map[["item_name", "unit", "qty_per_unit"]], use_container_width=True)

    with st.expander("➕ Add / Update ingredient", expanded=False):
        item_name = st.selectbox("Ingredient item", items_df["name"].tolist(), key="recipe_item_pick")
        qty_per = st.number_input("Qty used per 1 sale", min_value=0.0, value=1.0, step=0.1, key="recipe_qty")
        item_id = int(items_df[items_df["name"] == item_name]["id"].iloc[0])

        if st.button("Save ingredient", type="primary", key="recipe_save_btn"):
            exec_sql(
                """
                insert into public.menu_ingredients (menu_id, item_id, qty_per_unit)
                values (%s, %s, %s)
                on conflict (menu_id, item_id)
                do update set qty_per_unit = excluded.qty_per_unit
                """,
                (menu_id, item_id, float(qty_per)),
            )
            invalidate_caches()
            st.success("Saved.")
            st.rerun()

    with st.expander("🗑 Remove ingredient", expanded=False):
        if cur_map.empty:
            st.caption("Nothing to remove.")
        else:
            rem = st.selectbox("Remove which ingredient?", cur_map["item_name"].tolist(), key="recipe_remove_pick")
            rem_id = int(cur_map[cur_map["item_name"] == rem]["item_id"].iloc[0])
            if st.button("Remove", key="recipe_remove_btn"):
                exec_sql("delete from public.menu_ingredients where menu_id=%s and item_id=%s", (menu_id, rem_id))
                invalidate_caches()
                st.success("Removed.")
                st.rerun()


def page_pos():
    st.header("POS")

    menu_df = load_menu(active_only=True)
    if menu_df.empty:
        st.info("Add menu items first in Menu Admin.")
        return

    menu_names = menu_df["name"].tolist()
    menu_pick = st.selectbox("Menu item", menu_names, key="pos_menu_pick")
    menu_id = int(menu_df[menu_df["name"] == menu_pick]["id"].iloc[0])

    qty = st.number_input("Qty", min_value=1, value=1, step=1, key="pos_qty")
    payment = st.selectbox("Payment", PAYMENTS, key="pos_payment")
    location = st.selectbox("Location", LOCATIONS, index=0, key="pos_location")
    event_name = st.text_input("Event name (optional)", value="", key="pos_event_name")

    default_price = float(menu_df[menu_df["name"] == menu_pick]["price"].iloc[0])
    price_each = st.number_input("Price each (NZD)", min_value=0.0, value=default_price, step=0.5, key="pos_price_each")

    total = float(price_each) * int(qty)
    st.subheader(f"Line total: ${total:,.2f}")

    if st.button("Record sale", type="primary", key="pos_record_sale"):
        # record sale
        exec_sql(
            """
            insert into public.sales (menu_id, qty, price_each, payment, location, event_name)
            values (%s, %s, %s, %s, %s, %s)
            """,
            (menu_id, int(qty), float(price_each), payment, location, event_name.strip() or None),
        )

        # consume ingredients from stock (at chosen location)
        ing = load_menu_ingredients(menu_id)
        if not ing.empty:
            for _, r in ing.iterrows():
                item_id = int(r["item_id"])
                used = float(r["qty_per_unit"]) * int(qty)
                # reduce stock (negative allowed)
                ensure_stock_row(item_id, location)
                exec_sql(
                    """
                    update public.stock
                    set qty = qty - %s, updated_at = now()
                    where item_id=%s and location=%s
                    """,
                    (used, item_id, location),
                )
                exec_sql(
                    """
                    insert into public.movements (item_id, location_from, location_to, qty, reason)
                    values (%s, %s, %s, %s, %s)
                    """,
                    (item_id, location, None, -used, f"sale:{menu_pick}"),
                )

        invalidate_caches()
        st.success("Sale recorded.")
        st.rerun()


def page_event_mode():
    st.header("Event Mode")

    st.caption("This is reporting (fast). It reads sales grouped by event name.")

    event_name = st.text_input("Event name", value="", key="event_name_filter", placeholder="Electric Ave Day 1")
    if not event_name.strip():
        st.info("Enter an event name to view event sales.")
        return

    # revenue summary
    rows = exec_sql(
        """
        select
            s.event_name,
            count(*) as lines,
            sum(s.qty) as units,
            sum(s.qty * s.price_each) as revenue
        from public.sales s
        where s.event_name = %s
        group by s.event_name
        """,
        (event_name.strip(),),
        fetch="all",
    )
    df = pd.DataFrame(rows or [])
    if df.empty:
        st.warning("No sales found for this event name (exact match).")
        return

    st.subheader("Revenue summary")
    st.dataframe(df, use_container_width=True)

    # ingredient usage
    usage = exec_sql(
        """
        select
            i.name as ingredient,
            i.unit,
            sum(mi.qty_per_unit * s.qty) as total_used
        from public.sales s
        join public.menu_ingredients mi on mi.menu_id = s.menu_id
        join public.items i on i.id = mi.item_id
        where s.event_name = %s
        group by i.name, i.unit
        order by lower(i.name)
        """,
        (event_name.strip(),),
        fetch="all",
    )
    udf = pd.DataFrame(usage or [])
    st.subheader("Estimated ingredient usage")
    if udf.empty:
        st.info("No ingredient mappings found (Menu Admin → map ingredients to menu items).")
    else:
        st.dataframe(udf, use_container_width=True)


def page_orders():
    st.header("Orders / Transfer (Prep → Truck)")

    st.caption("Creates a suggested transfer based on Truck par levels. Fulfilling will transfer stock Prep → Truck.")

    truck = load_stock(LOC_TRUCK)
    prep = load_stock(LOC_PREP)

    if truck.empty or prep.empty:
        st.info("Add items first.")
        return

    # compute need at truck
    df = truck.merge(
        prep[["item_id", "qty"]].rename(columns={"qty": "prep_qty"}),
        on="item_id",
        how="left",
    )
    df["prep_qty"] = df["prep_qty"].fillna(0.0).astype(float)
    df["need"] = (df["par"].astype(float) - df["qty"].astype(float)).clip(lower=0)

    want = df[df["need"] > 0][["item_id", "name", "unit", "qty", "par", "prep_qty", "need"]].copy()
    if want.empty:
        st.success("Truck is at/above par for all items.")
        return

    st.subheader("Suggested order (to reach par at Truck)")
    st.dataframe(want[["name", "unit", "qty", "par", "prep_qty", "need"]], use_container_width=True)

    st.divider()
    st.subheader("Fulfill transfer")
    st.write("Adjust quantities if you want, then fulfill.")

    # editable quantities
    edited = want.copy()
    edited["transfer_qty"] = edited["need"].clip(upper=edited["prep_qty"]).astype(float)

    edited_df = st.data_editor(
        edited[["name", "unit", "prep_qty", "need", "transfer_qty"]],
        use_container_width=True,
        key="orders_editor",
    )

    reason = st.text_input("Reason", value="restock truck", key="orders_reason")

    if st.button("Fulfill transfer (Prep → Truck)", type="primary", key="orders_fulfill_btn"):
        for _, r in edited_df.iterrows():
            qty = float(r["transfer_qty"])
            if qty <= 0:
                continue
            # find item_id by name
            item_id = int(want[want["name"] == r["name"]]["item_id"].iloc[0])
            transfer_stock(item_id, LOC_PREP, LOC_TRUCK, qty, reason)

        invalidate_caches()
        st.success("Transfer fulfilled.")
        st.rerun()


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
def main():
    # init schema
    init_db()

    ui_db_test()

    mobile_mode = st.toggle("Mobile mode", value=False, key="mobile_mode_toggle")

    if mobile_mode:
        page = st.selectbox(
            "Page",
            ["Dashboard", "POS", "Event Mode", "Orders", "Adjust Stock", "Menu Admin", "Items", "Movements"],
            key="page_select_mobile",
        )
    else:
        page = st.radio(
            "Page",
            ["POS", "Event Mode", "Orders", "Dashboard", "Adjust Stock", "Menu Admin", "Items", "Movements"],
            horizontal=True,
            key="page_radio_desktop",
        )

    # route
    if page == "Items":
        page_items()
    elif page == "Adjust Stock":
        page_adjust_stock()
    elif page == "Movements":
        page_movements()
    elif page == "Menu Admin":
        page_menu_admin()
    elif page == "POS":
        page_pos()
    elif page == "Event Mode":
        page_event_mode()
    elif page == "Orders":
        page_orders()
    else:
        page_dashboard()


if __name__ == "__main__":
    main()
