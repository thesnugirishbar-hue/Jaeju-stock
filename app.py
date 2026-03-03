import streamlit as st
import sqlite3
from datetime import datetime, date

# ----------------------------
# CONFIG
# ----------------------------
st.set_page_config(page_title="JAEJU Stock & POS", layout="wide")

DB_NAME = "jaeju_pro.db"
LOCATIONS = ["Trailer", "Prep Kitchen"]

# ----------------------------
# DB HELPERS
# ----------------------------
def get_conn():
    return sqlite3.connect(DB_NAME, check_same_thread=False)

def init_db(conn: sqlite3.Connection):
    cur = conn.cursor()

    # Ingredients (raw stock)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ingredients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        unit TEXT DEFAULT '',
        par_level REAL DEFAULT 0
    )
    """)

    # Products (POS items)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        price REAL NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1
    )
    """)

    # Recipes (bill of materials): product -> ingredient qty
    cur.execute("""
    CREATE TABLE IF NOT EXISTS recipes (
        product_id INTEGER NOT NULL,
        ingredient_id INTEGER NOT NULL,
        qty_required REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (product_id, ingredient_id)
    )
    """)

    # Stock per location
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stock (
        ingredient_id INTEGER NOT NULL,
        location TEXT NOT NULL,
        qty REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (ingredient_id, location)
    )
    """)

    # Sales header/lines simplified (one row per product sale click)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        location TEXT NOT NULL,
        product_id INTEGER NOT NULL,
        qty REAL NOT NULL,
        total REAL NOT NULL
    )
    """)

    # Transfers (prep -> trailer, etc.)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        ingredient_id INTEGER NOT NULL,
        from_location TEXT NOT NULL,
        to_location TEXT NOT NULL,
        qty REAL NOT NULL,
        note TEXT DEFAULT ''
    )
    """)

    conn.commit()

def fetch_all(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()

def fetch_one(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchone()

def exec_sql(conn, sql, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()

def ensure_stock_rows(conn, ingredient_id: int):
    for loc in LOCATIONS:
        exec_sql(
            conn,
            "INSERT OR IGNORE INTO stock (ingredient_id, location, qty) VALUES (?, ?, 0)",
            (ingredient_id, loc),
        )

def get_stock_qty(conn, ingredient_id: int, location: str) -> float:
    row = fetch_one(
        conn,
        "SELECT qty FROM stock WHERE ingredient_id = ? AND location = ?",
        (ingredient_id, location),
    )
    return float(row[0]) if row else 0.0

def set_stock_qty(conn, ingredient_id: int, location: str, qty: float):
    exec_sql(
        conn,
        "UPDATE stock SET qty = ? WHERE ingredient_id = ? AND location = ?",
        (qty, ingredient_id, location),
    )

def adjust_stock(conn, ingredient_id: int, location: str, delta: float):
    current = get_stock_qty(conn, ingredient_id, location)
    set_stock_qty(conn, ingredient_id, location, current + delta)

def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()

# ----------------------------
# INIT
# ----------------------------
conn = get_conn()
init_db(conn)

# ----------------------------
# UI
# ----------------------------
st.sidebar.title("JAEJU Stock & POS")

page = st.sidebar.radio("Navigate", [
    "Dashboard",
    "Ingredients",
    "Products",
    "Recipes",
    "Stock Overview",
    "Adjust Stock",
    "Transfer Stock",
    "POS",
    "Reports"
])

# ----------------------------
# DASHBOARD
# ----------------------------
if page == "Dashboard":
    st.title("Dashboard")

    colA, colB, colC = st.columns(3)

    # Counts
    ing_count = fetch_one(conn, "SELECT COUNT(*) FROM ingredients")[0]
    prod_count = fetch_one(conn, "SELECT COUNT(*) FROM products WHERE active = 1")[0]
    low_count = fetch_one(conn, """
        SELECT COUNT(*)
        FROM stock s
        JOIN ingredients i ON i.id = s.ingredient_id
        WHERE s.location = 'Prep Kitchen' AND s.qty < i.par_level
    """)[0]

    colA.metric("Ingredients", ing_count)
    colB.metric("Active Products", prod_count)
    colC.metric("LOW in Prep Kitchen", low_count)

    st.subheader("Today’s Sales (Trailer)")
    today = date.today().isoformat()
    today_total = fetch_one(conn, """
        SELECT COALESCE(SUM(total), 0)
        FROM sales
        WHERE location = 'Trailer' AND substr(created_at, 1, 10) = ?
    """, (today, ))[0]
    st.metric("Revenue", f"${float(today_total):.2f}")

    st.subheader("Recent Activity")
    recent_sales = fetch_all(conn, """
        SELECT s.created_at, p.name, s.qty, s.total
        FROM sales s
        JOIN products p ON p.id = s.product_id
        ORDER BY s.id DESC
        LIMIT 8
    """)
    if recent_sales:
        st.dataframe(
            [{"time": r[0], "product": r[1], "qty": r[2], "total": r[3]} for r in recent_sales],
            use_container_width=True
        )
    else:
        st.info("No sales yet.")

# ----------------------------
# INGREDIENTS
# ----------------------------
if page == "Ingredients":
    st.title("Ingredients (Raw Stock)")

    with st.expander("Add Ingredient", expanded=True):
        c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
        name = c1.text_input("Ingredient name", placeholder="Chicken thigh (raw)")
        unit = c2.text_input("Unit", placeholder="kg / pcs / L")
        par = c3.number_input("Par level", min_value=0.0, value=0.0, step=0.1)
        add = c4.button("Add", use_container_width=True)

        if add:
            if not name.strip():
                st.error("Ingredient name is required.")
            else:
                try:
                    exec_sql(conn, "INSERT INTO ingredients (name, unit, par_level) VALUES (?, ?, ?)",
                             (name.strip(), unit.strip(), float(par)))
                    ing_id = fetch_one(conn, "SELECT id FROM ingredients WHERE name = ?", (name.strip(), ))[0]
                    ensure_stock_rows(conn, ing_id)
                    st.success("Ingredient added.")
                except sqlite3.IntegrityError:
                    st.error("That ingredient name already exists.")

    st.subheader("Ingredient List")
    rows = fetch_all(conn, "SELECT id, name, unit, par_level FROM ingredients ORDER BY name")
    if rows:
        st.dataframe(
            [{"id": r[0], "name": r[1], "unit": r[2], "par_level": r[3]} for r in rows],
            use_container_width=True
        )
    else:
        st.info("No ingredients yet.")

# ----------------------------
# PRODUCTS
# ----------------------------
if page == "Products":
    st.title("Products (POS Items)")

    with st.expander("Add Product", expanded=True):
        c1, c2, c3 = st.columns([3, 1, 1])
        pname = c1.text_input("Product name", placeholder="KFC Box")
        price = c2.number_input("Price", min_value=0.0, value=0.0, step=0.5)
        addp = c3.button("Add", use_container_width=True)

        if addp:
            if not pname.strip():
                st.error("Product name is required.")
            else:
                try:
                    exec_sql(conn, "INSERT INTO products (name, price, active) VALUES (?, ?, 1)",
                             (pname.strip(), float(price)))
                    st.success("Product added.")
                except sqlite3.IntegrityError:
                    st.error("That product name already exists.")

    st.subheader("Products")
    prows = fetch_all(conn, "SELECT id, name, price, active FROM products ORDER BY name")
    if prows:
        # Quick toggle active
        for pid, n, pr, act in prows:
            c1, c2, c3, c4 = st.columns([4, 1, 1, 1])
            c1.write(n)
            c2.write(f"${float(pr):.2f}")
            new_act = c3.checkbox("Active", value=bool(act), key=f"act_{pid}")
            if c4.button("Save", key=f"save_prod_{pid}"):
                exec_sql(conn, "UPDATE products SET active = ? WHERE id = ?", (1 if new_act else 0, pid))
                st.success(f"Saved {n}")
    else:
        st.info("No products yet.")

# ----------------------------
# RECIPES
# ----------------------------
if page == "Recipes":
    st.title("Recipes (Product → Ingredients)")

    products = fetch_all(conn, "SELECT id, name FROM products ORDER BY name")
    ingredients = fetch_all(conn, "SELECT id, name, unit FROM ingredients ORDER BY name")

    if not products:
        st.warning("Add at least one product first.")
    elif not ingredients:
        st.warning("Add ingredients first.")
    else:
        prod_map = {p[1]: p[0] for p in products}
        ing_map = {f"{i[1]} ({i[2]})" if i[2] else i[1]: i[0] for i in ingredients}

        selected_prod_name = st.selectbox("Choose product", list(prod_map.keys()))
        selected_prod_id = prod_map[selected_prod_name]

        st.subheader(f"Recipe for: {selected_prod_name}")

        # Show current recipe lines
        current = fetch_all(conn, """
            SELECT r.ingredient_id, i.name, i.unit, r.qty_required
            FROM recipes r
            JOIN ingredients i ON i.id = r.ingredient_id
            WHERE r.product_id = ?
            ORDER BY i.name
        """, (selected_prod_id,))

        if current:
            st.dataframe(
                [{"ingredient": f"{r[1]} ({r[2]})" if r[2] else r[1], "qty_required": r[3]} for r in current],
                use_container_width=True
            )
        else:
            st.info("No ingredients in this recipe yet.")

        st.divider()

        c1, c2, c3 = st.columns([4, 2, 1])
        selected_ing_label = c1.selectbox("Add/Update ingredient", list(ing_map.keys()))
        qty_req = c2.number_input("Qty required per 1 sale", min_value=0.0, value=0.0, step=0.01)
        if c3.button("Add/Update", use_container_width=True):
            ing_id = ing_map[selected_ing_label]
            exec_sql(conn, """
                INSERT OR REPLACE INTO recipes (product_id, ingredient_id, qty_required)
                VALUES (?, ?, ?)
            """, (selected_prod_id, ing_id, float(qty_req)))
            ensure_stock_rows(conn, ing_id)
            st.success("Recipe updated. Refresh page if needed.")

        # Remove ingredient
        if current:
            remove_labels = [f"{r[1]} ({r[2]})" if r[2] else r[1] for r in current]
            rsel = st.selectbox("Remove ingredient from recipe", remove_labels)
            if st.button("Remove selected ingredient"):
                # Map name back to id
                name_only = rsel.split(" (")[0]
                ing_id = fetch_one(conn, "SELECT id FROM ingredients WHERE name = ?", (name_only,))[0]
                exec_sql(conn, "DELETE FROM recipes WHERE product_id = ? AND ingredient_id = ?",
                         (selected_prod_id, ing_id))
                st.success("Removed.")

# ----------------------------
# STOCK OVERVIEW
# ----------------------------
if page == "Stock Overview":
    st.title("Stock Overview")

    loc = st.selectbox("Location", LOCATIONS)

    rows = fetch_all(conn, """
        SELECT i.name, i.unit, i.par_level, s.qty
        FROM stock s
        JOIN ingredients i ON i.id = s.ingredient_id
        WHERE s.location = ?
        ORDER BY i.name
    """, (loc,))

    if rows:
        data = []
        for name, unit, par, qty in rows:
            status = "LOW" if float(qty) < float(par) else "OK"
            data.append({
                "ingredient": name,
                "qty": qty,
                "unit": unit,
                "par_level": par,
                "status": status
            })
        st.dataframe(data, use_container_width=True)
    else:
        st.info("No stock rows yet. Add ingredients first.")

# ----------------------------
# ADJUST STOCK
# ----------------------------
if page == "Adjust Stock":
    st.title("Adjust Stock (Manual)")

    ingredients = fetch_all(conn, "SELECT id, name, unit FROM ingredients ORDER BY name")
    if not ingredients:
        st.warning("Add ingredients first.")
    else:
        ing_labels = [f"{i[1]} ({i[2]})" if i[2] else i[1] for i in ingredients]
        ing_map = {ing_labels[idx]: ingredients[idx][0] for idx in range(len(ingredients))}

        c1, c2, c3 = st.columns([4, 2, 2])
        ing_label = c1.selectbox("Ingredient", ing_labels)
        location = c2.selectbox("Location", LOCATIONS)
        delta = c3.number_input("Change (+ add / - remove)", value=0.0, step=0.1)

        if st.button("Apply change"):
            ing_id = ing_map[ing_label]
            ensure_stock_rows(conn, ing_id)

            current = get_stock_qty(conn, ing_id, location)
            new_qty = current + float(delta)

            # Allow negative? Usually no.
            if new_qty < 0:
                st.error(f"Not allowed: would make stock negative ({new_qty}).")
            else:
                set_stock_qty(conn, ing_id, location, new_qty)
                st.success(f"Updated. New qty: {new_qty}")

# ----------------------------
# TRANSFER STOCK (Prep → Trailer order/requisition)
# ----------------------------
if page == "Transfer Stock":
    st.title("Transfer Stock (Prep Kitchen ⇄ Trailer)")

    ingredients = fetch_all(conn, "SELECT id, name, unit FROM ingredients ORDER BY name")
    if not ingredients:
        st.warning("Add ingredients first.")
    else:
        ing_labels = [f"{i[1]} ({i[2]})" if i[2] else i[1] for i in ingredients]
        ing_map = {ing_labels[idx]: ingredients[idx][0] for idx in range(len(ingredients))}

        c1, c2, c3, c4 = st.columns([4, 2, 2, 4])
        ing_label = c1.selectbox("Ingredient", ing_labels)
        from_loc = c2.selectbox("From", LOCATIONS, index=1)  # default Prep Kitchen
        to_loc = c3.selectbox("To", LOCATIONS, index=0)      # default Trailer
        note = c4.text_input("Note (optional)", placeholder="e.g., Electric Ave top-up")

        qty = st.number_input("Quantity to transfer", min_value=0.0, value=0.0, step=0.1)

        # Show current balances
        ing_id = ing_map[ing_label]
        ensure_stock_rows(conn, ing_id)
        colA, colB = st.columns(2)
        colA.info(f"{from_loc} stock: {get_stock_qty(conn, ing_id, from_loc)}")
        colB.info(f"{to_loc} stock: {get_stock_qty(conn, ing_id, to_loc)}")

        if st.button("Transfer"):
            if from_loc == to_loc:
                st.error("From and To cannot be the same.")
            elif qty <= 0:
                st.error("Quantity must be greater than 0.")
            else:
                available = get_stock_qty(conn, ing_id, from_loc)
                if qty > available:
                    st.error(f"Not enough stock in {from_loc}. Available: {available}")
                else:
                    # move
                    set_stock_qty(conn, ing_id, from_loc, available - qty)
                    set_stock_qty(conn, ing_id, to_loc, get_stock_qty(conn, ing_id, to_loc) + qty)

                    exec_sql(conn, """
                        INSERT INTO transfers (created_at, ingredient_id, from_location, to_location, qty, note)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (now_iso(), ing_id, from_loc, to_loc, float(qty), note.strip()))

                    st.success("Transfer complete.")

# ----------------------------
# POS
# ----------------------------
if page == "POS":
    st.title("POS (Sells Products, Deducts Ingredients)")

    location = st.selectbox("Selling location", ["Trailer"])  # keep simple for now

    products = fetch_all(conn, "SELECT id, name, price FROM products WHERE active = 1 ORDER BY name")
    if not products:
        st.warning("Add products first.")
    else:
        st.caption("Tip: If a product has no recipe, it will record the sale but deduct nothing.")

        # Make it easier to tap on mobile
        for pid, pname, price in products:
            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([4, 1.5, 1.5, 2])
                c1.markdown(f"### {pname}")
                c2.metric("Price", f"${float(price):.2f}")
                qty = c3.number_input("Qty", min_value=0, value=1, step=1, key=f"pos_qty_{pid}")

                # Pre-check recipe and stock sufficiency
                recipe = fetch_all(conn, """
                    SELECT r.ingredient_id, i.name, i.unit, r.qty_required
                    FROM recipes r
                    JOIN ingredients i ON i.id = r.ingredient_id
                    WHERE r.product_id = ?
                """, (pid,))

                can_sell = True
                shortage_msgs = []

                for ing_id, iname, unit, per_sale in recipe:
                    need = float(per_sale) * float(qty)
                    have = get_stock_qty(conn, ing_id, location)
                    if need > have:
                        can_sell = False
                        shortage_msgs.append(f"- {iname}: need {need} {unit}, have {have} {unit}")

                if recipe and not can_sell:
                    c4.error("Not enough stock")
                    with st.expander("Shortage details"):
                        st.write("\n".join(shortage_msgs))
                else:
                    sell = c4.button("Sell", key=f"sell_{pid}", use_container_width=True)

                    if sell:
                        # Deduct ingredients (if recipe exists)
                        for ing_id, iname, unit, per_sale in recipe:
                            need = float(per_sale) * float(qty)
                            have = get_stock_qty(conn, ing_id, location)
                            # safety check
                            if need > have:
                                st.error(f"Blocked: {iname} insufficient at time of sale.")
                                break
                            set_stock_qty(conn, ing_id, location, have - need)
                        else:
                            total = float(qty) * float(price)
                            exec_sql(conn, """
                                INSERT INTO sales (created_at, location, product_id, qty, total)
                                VALUES (?, ?, ?, ?, ?)
                            """, (now_iso(), location, pid, float(qty), total))
                            st.success(f"Sold {qty} × {pname} (${total:.2f})")

# ----------------------------
# REPORTS
# ----------------------------
if page == "Reports":
    st.title("Reports")

    st.subheader("Sales (Last 50)")
    sales = fetch_all(conn, """
        SELECT s.created_at, s.location, p.name, s.qty, s.total
        FROM sales s
        JOIN products p ON p.id = s.product_id
        ORDER BY s.id DESC
        LIMIT 50
    """)
    if sales:
        st.dataframe(
            [{"time": r[0], "location": r[1], "product": r[2], "qty": r[3], "total": r[4]} for r in sales],
            use_container_width=True
        )
    else:
        st.info("No sales yet.")

    st.subheader("Transfers (Last 50)")
    trans = fetch_all(conn, """
        SELECT t.created_at, i.name, t.from_location, t.to_location, t.qty, t.note
        FROM transfers t
        JOIN ingredients i ON i.id = t.ingredient_id
        ORDER BY t.id DESC
        LIMIT 50
    """)
    if trans:
        st.dataframe(
            [{"time": r[0], "ingredient": r[1], "from": r[2], "to": r[3], "qty": r[4], "note": r[5]} for r in trans],
            use_container_width=True
        )
    else:
        st.info("No transfers yet.")
