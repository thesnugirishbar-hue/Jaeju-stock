import os
import time
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import psycopg
from psycopg.rows import dict_row


# ---------------------------
# Config / helpers
# ---------------------------

APP_TITLE = "JAEJU Stock + POS + Events (Postgres)"
DEFAULT_LOCATIONS = ["Prep Kitchen", "Food Truck"]

st.set_page_config(page_title=APP_TITLE, layout="wide")


def _now_utc():
    return datetime.now(timezone.utc)


def get_database_url() -> str:
    """
    Read DATABASE_URL from Streamlit secrets or env.
    Accepts either:
    - st.secrets["DATABASE_URL"]
    - env DATABASE_URL
    """
    url = None
    try:
        if "DATABASE_URL" in st.secrets:
            url = st.secrets["DATABASE_URL"]
    except Exception:
        pass

    if not url:
        url = os.getenv("DATABASE_URL")

    if not url:
        raise RuntimeError("DATABASE_URL not set. Add it in Streamlit Secrets or env vars.")

    return url.strip()


@st.cache_resource
def get_conn():
    """
    Supabase Transaction Pooler does NOT support PREPARE statements.
    psycopg can auto-prepare after N executions. Disable it with prepare_threshold=0.
    Also set sslmode=require unless already in the URL.
    """
    dsn = get_database_url()

    # If user didn't include sslmode, add it safely
    if "sslmode=" not in dsn:
        joiner = "&" if "?" in dsn else "?"
        dsn = f"{dsn}{joiner}sslmode=require"

    # connect_timeout helps avoid "hangs" when DNS/host issues
    conn = psycopg.connect(
        dsn,
        row_factory=dict_row,
        autocommit=True,
        connect_timeout=10,
        prepare_threshold=0,  # IMPORTANT for Supabase Transaction Pooler
    )
    return conn


def exec_sql(sql: str, params=None, fetch: str | None = None):
    """
    fetch: None | 'one' | 'all'
    """
    if params is None:
        params = ()
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if fetch == "one":
            return cur.fetchone()
        if fetch == "all":
            return cur.fetchall()
        return None


def init_db():
    """
    Create required tables + seed locations.
    Using explicit public.<table> everywhere to avoid schema/search_path confusion.
    """
    # locations
    exec_sql(
        """
        create table if not exists public.locations (
            name text primary key,
            active boolean not null default true,
            created_at timestamptz not null default now()
        );
        """
    )

    # items
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
        """
    )

    # movements = stock changes
    exec_sql(
        """
        create table if not exists public.movements (
            id bigserial primary key,
            item_id bigint not null references public.items(id) on delete cascade,
            location text not null references public.locations(name),
            qty numeric not null,
            reason text not null default 'adjust',
            note text,
            created_at timestamptz not null default now()
        );
        """
    )

    # menu items (POS)
    exec_sql(
        """
        create table if not exists public.menu_items (
            id bigserial primary key,
            name text not null,
            price numeric not null default 0,
            active boolean not null default true,
            created_at timestamptz not null default now()
        );
        """
    )

    # sales (POS)
    exec_sql(
        """
        create table if not exists public.sales (
            id bigserial primary key,
            menu_item_id bigint references public.menu_items(id) on delete set null,
            menu_item_name text not null,
            qty numeric not null default 1,
            price_each numeric not null default 0,
            payment text not null default 'EFTPOS',
            location text not null references public.locations(name),
            event_name text,
            created_at timestamptz not null default now()
        );
        """
    )

    # seed locations
    for loc in DEFAULT_LOCATIONS:
        exec_sql(
            """
            insert into public.locations(name, active)
            values (%s, true)
            on conflict (name) do update set active = excluded.active;
            """,
            (loc,),
        )


def db_counts():
    counts = {}
    for tbl in ["locations", "items", "movements", "menu_items", "sales"]:
        row = exec_sql(f"select count(*)::bigint as n from public.{tbl};", fetch="one")
        counts[tbl] = int(row["n"]) if row else 0
    return counts


def load_locations(active_only=True):
    if active_only:
        rows = exec_sql(
            "select name from public.locations where active=true order by name;",
            fetch="all",
        )
    else:
        rows = exec_sql("select name from public.locations order by name;", fetch="all")
    return [r["name"] for r in rows] if rows else []


def load_items(active_only=True):
    if active_only:
        rows = exec_sql(
            """
            select id, name, unit, par, active, created_at
            from public.items
            where active=true
            order by name;
            """,
            fetch="all",
        )
    else:
        rows = exec_sql(
            """
            select id, name, unit, par, active, created_at
            from public.items
            order by name;
            """,
            fetch="all",
        )
    return rows or []


def load_menu_items(active_only=True):
    if active_only:
        rows = exec_sql(
            """
            select id, name, price, active, created_at
            from public.menu_items
            where active=true
            order by name;
            """,
            fetch="all",
        )
    else:
        rows = exec_sql(
            """
            select id, name, price, active, created_at
            from public.menu_items
            order by name;
            """,
            fetch="all",
        )
    return rows or []


def stock_snapshot(location: str | None = None):
    """
    Current stock = sum(movements.qty) grouped by item (+ optional location)
    """
    if location:
        rows = exec_sql(
            """
            select
              i.id,
              i.name,
              i.unit,
              i.par,
              coalesce(sum(m.qty), 0) as on_hand
            from public.items i
            left join public.movements m
              on m.item_id = i.id and m.location = %s
            where i.active = true
            group by i.id, i.name, i.unit, i.par
            order by i.name;
            """,
            (location,),
            fetch="all",
        )
    else:
        rows = exec_sql(
            """
            select
              i.id,
              i.name,
              i.unit,
              i.par,
              coalesce(sum(m.qty), 0) as on_hand
            from public.items i
            left join public.movements m
              on m.item_id = i.id
            where i.active = true
            group by i.id, i.name, i.unit, i.par
            order by i.name;
            """,
            fetch="all",
        )
    return rows or []


# ---------------------------
# Pages
# ---------------------------

def page_dashboard():
    st.subheader("Dashboard")

    items = load_items(active_only=True)
    if not items:
        st.info("No items yet. Add items in Items tab.")
        return

    locs = load_locations(active_only=True)
    col1, col2 = st.columns([1, 2])
    with col1:
        loc = st.selectbox("Location", ["All locations"] + locs, index=0)

    rows = stock_snapshot(None if loc == "All locations" else loc)
    df = pd.DataFrame(rows)
    if df.empty:
        st.info("No stock movements yet. Use Adjust Stock to add starting stock.")
        return

    df["par"] = pd.to_numeric(df["par"], errors="coerce").fillna(0)
    df["on_hand"] = pd.to_numeric(df["on_hand"], errors="coerce").fillna(0)
    df["below_par"] = df["on_hand"] < df["par"]

    with col2:
        st.caption("Stock snapshot is calculated from Movements.")
        st.dataframe(
            df[["name", "unit", "on_hand", "par", "below_par"]],
            use_container_width=True,
            hide_index=True,
        )


def page_items():
    st.subheader("Items")

    rows = load_items(active_only=False)
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["id", "name", "unit", "par", "active", "created_at"])
    st.dataframe(df, use_container_width=True, hide_index=True)

    with st.expander("➕ Add / Update item", expanded=False):
        col1, col2, col3, col4 = st.columns([2, 1, 1, 1])

        with col1:
            name = st.text_input("Name", placeholder="e.g. Chicken flour")
        with col2:
            unit = st.text_input("Unit", placeholder="e.g. Bkt / Btl / Each")
        with col3:
            par = st.number_input("Par", min_value=0.0, value=0.0, step=1.0)
        with col4:
            active = st.checkbox("Active", value=True)

        if st.button("Save item", type="primary"):
            if not name.strip():
                st.error("Name is required.")
                return

            # Upsert by name (simple)
            exec_sql(
                """
                insert into public.items(name, unit, par, active)
                values (%s, %s, %s, %s)
                on conflict (id) do nothing;
                """,
                (name.strip(), unit.strip(), par, active),
            )
            st.success("Saved. Refreshing…")
            time.sleep(0.3)
            st.rerun()


def page_adjust_stock():
    st.subheader("Adjust Stock")

    items = load_items(active_only=True)
    if not items:
        st.info("Add items first in Items tab.")
        return

    locs = load_locations(active_only=True)
    if not locs:
        st.error("No locations found. DB init should have created them.")
        return

    item_map = {f"{r['name']} (#{r['id']})": r["id"] for r in items}

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        item_label = st.selectbox("Item", list(item_map.keys()))
    with col2:
        location = st.selectbox("Location", locs, index=0)
    with col3:
        qty = st.number_input("Qty change (+/-)", value=0.0, step=1.0)

    reason = st.selectbox("Reason", ["adjust", "purchase", "transfer", "waste", "count"], index=0)
    note = st.text_input("Note (optional)")

    if st.button("Record movement", type="primary"):
        item_id = item_map[item_label]
        exec_sql(
            """
            insert into public.movements(item_id, location, qty, reason, note)
            values (%s, %s, %s, %s, %s);
            """,
            (item_id, location, qty, reason, note.strip() if note else None),
        )
        st.success("Movement recorded. Dashboard should update now.")
        time.sleep(0.2)
        st.rerun()


def page_movements():
    st.subheader("Movements")

    rows = exec_sql(
        """
        select m.id, m.created_at, i.name as item, m.location, m.qty, m.reason, m.note
        from public.movements m
        join public.items i on i.id = m.item_id
        order by m.created_at desc
        limit 500;
        """,
        fetch="all",
    ) or []

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)


def page_menu_admin():
    st.subheader("Menu Admin")

    rows = load_menu_items(active_only=False)
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["id", "name", "price", "active", "created_at"])
    st.dataframe(df, use_container_width=True, hide_index=True)

    with st.expander("➕ Add / Update menu item", expanded=False):
        col1, col2, col3 = st.columns([2, 1, 1])
        with col1:
            name = st.text_input("Menu item name", placeholder="e.g. Chicken Burger")
        with col2:
            price = st.number_input("Price (NZD)", min_value=0.0, value=0.0, step=0.5)
        with col3:
            active = st.checkbox("Active", value=True)

        if st.button("Save menu item", type="primary"):
            if not name.strip():
                st.error("Name is required.")
                return
            exec_sql(
                """
                insert into public.menu_items(name, price, active)
                values (%s, %s, %s);
                """,
                (name.strip(), price, active),
            )
            st.success("Saved. Refreshing…")
            time.sleep(0.3)
            st.rerun()


def page_pos():
    st.subheader("POS")

    menu = load_menu_items(active_only=True)
    if not menu:
        st.info("No menu items yet. Add them in Menu Admin.")
        return

    locs = load_locations(active_only=True)
    if not locs:
        st.error("No locations found.")
        return

    menu_map = {f"{m['name']} (${float(m['price']):.2f})": m for m in menu}

    col1, col2, col3, col4 = st.columns([2, 1, 1, 2])
    with col1:
        menu_label = st.selectbox("Menu item", list(menu_map.keys()))
    with col2:
        qty = st.number_input("Qty", min_value=1.0, value=1.0, step=1.0)
    with col3:
        payment = st.selectbox("Payment", ["EFTPOS", "Cash", "Online"], index=0)
    with col4:
        location = st.selectbox("Location", locs, index=locs.index("Food Truck") if "Food Truck" in locs else 0)

    mi = menu_map[menu_label]
    price_each = float(mi["price"])
    event_name = st.text_input("Event name (optional)", placeholder="Electric Ave Day 1")

    line_total = price_each * float(qty)
    st.metric("Line total", f"${line_total:,.2f}")

    if st.button("Record sale", type="primary"):
        exec_sql(
            """
            insert into public.sales(menu_item_id, menu_item_name, qty, price_each, payment, location, event_name)
            values (%s, %s, %s, %s, %s, %s, %s);
            """,
            (mi["id"], mi["name"], qty, price_each, payment, location, event_name.strip() or None),
        )
        st.success("Sale recorded.")
        time.sleep(0.2)
        st.rerun()


def page_event_mode():
    st.subheader("Event Mode")

    st.caption("This is a simple event sales viewer. (You can build richer reporting in Supabase SQL/Charts too.)")

    event = st.text_input("Event name", placeholder="Electric Ave Day 1")
    if not event.strip():
        st.info("Enter an event name to view event sales.")
        return

    rows = exec_sql(
        """
        select
          date_trunc('minute', created_at) as time,
          menu_item_name,
          qty,
          (qty * price_each) as line_total,
          payment,
          location
        from public.sales
        where event_name = %s
        order by created_at desc
        limit 500;
        """,
        (event.strip(),),
        fetch="all",
    ) or []

    df = pd.DataFrame(rows)
    if df.empty:
        st.warning("No sales found for that event name (exact match).")
        return

    st.dataframe(df, use_container_width=True, hide_index=True)

    total = pd.to_numeric(df["line_total"], errors="coerce").fillna(0).sum()
    st.metric("Event total", f"${float(total):,.2f}")


# ---------------------------
# Main
# ---------------------------

def main():
    st.title(APP_TITLE)

    # Init DB early so locations exist and dashboard works
    try:
        init_db()
    except Exception as e:
        st.error("DB init failed. Check DATABASE_URL and Supabase pooler settings.")
        st.exception(e)
        st.stop()

    with st.expander("🛠️ DB connection test", expanded=False):
        try:
            c = db_counts()
            st.success("DB connected.")
            st.write({k: c[k] for k in ["locations", "items", "movements", "menu_items", "sales"]})
            # quick sanity
            locs = load_locations(active_only=False)
            st.caption(f"Locations: {locs}")
        except Exception as e:
            st.error("DB test failed.")
            st.exception(e)
            st.stop()

    # Simple navigation
    pages = {
        "Dashboard": page_dashboard,
        "Adjust Stock": page_adjust_stock,
        "Items": page_items,
        "Movements": page_movements,
        "Menu Admin": page_menu_admin,
        "POS": page_pos,
        "Event Mode": page_event_mode,
    }

    page = st.radio("Page", list(pages.keys()), horizontal=True, index=0)
    st.divider()
    pages[page]()


if __name__ == "__main__":
    main()
