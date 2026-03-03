https://jaeju-stock-bm2nofvxrxqjsdhxdf2m28.streamlit.app/            par_level REAL DEFAULT 0,
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

        conn.commit()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


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
            cur.execute("INSERT OR IGNORE INTO stock(item_id, location, qty) VALUES (?, ?, 0)",
                        (item_id, loc))
        conn.commit()


def add_item(name: str, unit: str, par_level: float, price_nzd: float, active: int = 1):
    name = name.strip()
    if not name:
        raise ValueError("Item name cannot be empty.")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO items(name, unit, par_level, price_nzd, active)
            VALUES (?, ?, ?, ?, ?)
        """, (name, unit, par_level, price_nzd, active))
        item_id = cur.lastrowid
        conn.commit()
    ensure_stock_rows_for_item(item_id)


def set_item_active(item_id: int, active: bool):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE items SET active = ? WHERE id = ?",
                    (1 if active else 0, item_id))
        conn.commit()


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


def get_orders_df(limit=50):
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


st.set_page_config(
    page_title="JAEJU Stock",
    page_icon="jaeju-logo.jpg",
    layout="wide"
)
init_db()

st.title("JAEJU Stock + Orders (Truck ↔ Prep Kitchen)")

tabs = st.tabs(["Dashboard", "Items", "Adjust Stock", "Orders", "Movements"])

with tabs[0]:
    pivot = get_stock_pivot()
    if pivot.empty:
        st.info("No items yet. Go to Items first.")
    else:
        st.dataframe(pivot[["name", "unit", "par_level", LOC_TRUCK, LOC_PREP, "Below PAR?"]],
                     use_container_width=True, hide_index=True)

with tabs[1]:
    st.subheader("Add items")
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    name = c1.text_input("Item name")
    unit = c2.text_input("Unit (kg / pcs / L)")
    par = c3.number_input("PAR level (Prep)", min_value=0.0, value=0.0, step=0.5)
    price = c4.number_input("Price NZD", min_value=0.0, value=0.0, step=0.1)

    if st.button("Add item", type="primary"):
        try:
            add_item(name, unit.strip() or "unit", float(par), float(price))
            st.success("Added.")
            st.rerun()
        except Exception as e:
            st.error(str(e))

with tabs[2]:
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
        reason = c4.text_input("Reason (delivery / wastage / recount)")

        if st.button("Apply adjustment", type="primary"):
            try:
                adjust_stock(int(item_map[item_name]), location, float(delta),
                             reason.strip() or "Manual adjustment", "manual", None)
                st.success("Updated.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

with tabs[3]:
    st.subheader("Truck → Prep orders")
    items = get_items_df(active_only=True)
    if items.empty:
        st.info("Add items first.")
    else:
        item_map = dict(zip(items["name"], items["id"]))
        note = st.text_input("Order note (optional)")

        c1, c2, c3 = st.columns([2, 1, 1])
        line_item = c1.selectbox("Item to add", list(item_map.keys()))
        line_qty = c2.number_input("Qty", min_value=0.0, value=0.0, step=0.5)
        add_line_btn = c3.button("Add line")

        if "draft_lines" not in st.session_state:
            st.session_state["draft_lines"] = []

        if add_line_btn:
            if line_qty <= 0:
                st.warning("Qty must be > 0")
            else:
                st.session_state["draft_lines"].append((line_item, float(line_qty)))
                st.success("Line added.")

        if st.session_state["draft_lines"]:
            st.dataframe(pd.DataFrame(st.session_state["draft_lines"], columns=["Item", "Qty"]),
                         use_container_width=True, hide_index=True)

            c1, c2 = st.columns(2)
            if c1.button("Clear draft"):
                st.session_state["draft_lines"] = []
                st.rerun()

            if c2.button("Create order", type="primary"):
                try:
                    order_id = create_order(note)
                    for item_name, qty in st.session_state["draft_lines"]:
                        add_order_line(order_id, int(item_map[item_name]), qty)
                    st.session_state["draft_lines"] = []
                    st.success(f"Order #{order_id} created.")
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

with tabs[4]:
    st.subheader("Movements log")
    mv = get_movements_df()
    if mv.empty:
        st.info("No movements yet.")
    else:
        st.dataframe(mv, use_container_width=True, hide_index=True)
