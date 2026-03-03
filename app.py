import streamlit as st
import sqlite3
from datetime import datetime, date

st.set_page_config(page_title="JAEJU Stock + POS", layout="wide")

# Fresh DB name to avoid old-schema crashes (this resets data)
DB_NAME = "jaeju_master_v2.db"
LOCATIONS = ["Trailer", "Prep Kitchen"]

# -----------------------------
# DB setup
# -----------------------------
conn = sqlite3.connect(DB_NAME, check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    unit TEXT DEFAULT '',
    par_level REAL DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    price REAL NOT NULL DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS recipes (
    product_id INTEGER NOT NULL,
    ingredient_id INTEGER NOT NULL,
    qty_required REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, ingredient_id)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS stock (
    ingredient_id INTEGER NOT NULL,
    location TEXT NOT NULL,
    qty REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (ingredient_id, location)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient_id INTEGER NOT NULL,
    qty REAL NOT NULL,
    status TEXT NOT NULL,          -- Pending / Completed
    created_at TEXT NOT NULL
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    qty REAL NOT NULL,
    total REAL NOT NULL,
    created_at TEXT NOT NULL
)
""")

conn.commit()

def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()

def ensure_stock_rows(ingredient_id: int):
    for loc in LOCATIONS:
        cur.execute(
            "INSERT OR IGNORE INTO stock (ingredient_id, location, qty) VALUES (?, ?, 0)",
            (ingredient_id, loc)
        )
    conn.commit()

def get_stock(ingredient_id: int, location: str) -> float:
    row = cur.execute(
        "SELECT qty FROM stock WHERE ingredient_id = ? AND location = ?",
        (ingredient_id, location)
    ).fetchone()
    return float(row[0]) if row else 0.0

# -----------------------------
# Sidebar
# -----------------------------
st.sidebar.title("JAEJU Control")
page = st.sidebar.radio("Navigate", [
    "POS (iPad)",
    "Sales Reports",
    "Orders (Create + Fulfil)",
    "Stock Overview",
    "Adjust Stock",
    "Admin (Add + Recipes)"
])

# =========================================================
# POS (iPad big buttons)
# =========================================================
if page == "POS (iPad)":
    st.title("POS (Trailer)")

    products = cur.execute("SELECT id, name, price FROM products ORDER BY name").fetchall()
    if not products:
        st.warning("No products yet. Go to Admin → Add Product.")
    else:
        # Simple qty selector (applies to next tap)
        qty = st.number_input("Quantity", min_value=1, value=1, step=1)

        st.caption("Tap a product to sell. This deducts ingredient stock from Trailer using the recipe.")

        cols = st.columns(3)
        for i, (pid, name, price) in enumerate(products):
            with cols[i % 3]:
                if st.button(f"{name}\n${price:.2f}", use_container_width=True):
                    # Get recipe
                    recipe = cur.execute("""
                        SELECT r.ingredient_id, r.qty_required, i.name, i.unit
                        FROM recipes r
                        JOIN ingredients i ON i.id = r.ingredient_id
                        WHERE r.product_id = ?
                    """, (pid,)).fetchall()

                    # Check stock for all ingredients first
                    ok = True
                    shortages = []
                    for ing_id, per_sale, ing_name, unit in recipe:
                        need = float(per_sale) * float(qty)
                        have = get_stock(int(ing_id), "Trailer")
                        if need > have:
                            ok = False
                            shortages.append(f"{ing_name}: need {need:g} {unit}, have {have:g} {unit}")

                    if recipe and not ok:
                        st.error("Not enough stock to sell this item.")
                        with st.expander("Show shortages"):
                            for s in shortages:
                                st.write("• " + s)
                    else:
                        # Deduct ingredients (if recipe exists)
                        for ing_id, per_sale, ing_name, unit in recipe:
                            need = float(per_sale) * float(qty)
                            cur.execute("""
                                UPDATE stock
                                SET qty = qty - ?
                                WHERE ingredient_id = ? AND location = 'Trailer'
                            """, (need, ing_id))

                        # Record sale
                        total = float(qty) * float(price)
                        cur.execute("""
                            INSERT INTO sales (product_id, qty, total, created_at)
                            VALUES (?, ?, ?, ?)
                        """, (pid, float(qty), total, now_iso()))
                        conn.commit()

                        st.success(f"Sold {qty} × {name} (${total:.2f})")

# =========================================================
# Sales Reports (not just log)
# =========================================================
if page == "Sales Reports":
    st.title("Sales Reports")

    # Date selector
    selected_date = st.date_input("Date", date.today())
    d = selected_date.isoformat()

    # KPIs
    revenue = cur.execute("""
        SELECT COALESCE(SUM(total), 0)
        FROM sales
        WHERE substr(created_at, 1, 10) = ?
    """, (d,)).fetchone()[0]

    units = cur.execute("""
        SELECT COALESCE(SUM(qty), 0)
        FROM sales
        WHERE substr(created_at, 1, 10) = ?
    """, (d,)).fetchone()[0]

    c1, c2 = st.columns(2)
    c1.metric("Revenue", f"${float(revenue):.2f}")
    c2.metric("Units sold", f"{float(units):.0f}")

    st.subheader("Revenue by Product")
    by_product = cur.execute("""
        SELECT p.name, COALESCE(SUM(s.total), 0) AS revenue, COALESCE(SUM(s.qty), 0) AS units
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE substr(s.created_at, 1, 10) = ?
        GROUP BY p.name
        ORDER BY revenue DESC
    """, (d,)).fetchall()

    if by_product:
        st.dataframe(
            [{"product": r[0], "revenue": float(r[1]), "units": float(r[2])} for r in by_product],
            use_container_width=True
        )
    else:
        st.info("No sales for this date.")

    st.subheader("Transactions")
    tx = cur.execute("""
        SELECT s.created_at, p.name, s.qty, s.total
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE substr(s.created_at, 1, 10) = ?
        ORDER BY s.id DESC
        LIMIT 200
    """, (d,)).fetchall()

    if tx:
        st.dataframe(
            [{"time": r[0], "product": r[1], "qty": float(r[2]), "total": float(r[3])} for r in tx],
            use_container_width=True
        )

# =========================================================
# Orders (Create + Fulfil)
# =========================================================
if page == "Orders (Create + Fulfil)":
    st.title("Orders (Trailer → Prep)")

    ingredients = cur.execute("SELECT id, name, unit FROM ingredients ORDER BY name").fetchall()
    if not ingredients:
        st.warning("Add ingredients first (Admin).")
    else:
        ing_labels = [f"{i[1]} ({i[2]})" if i[2] else i[1] for i in ingredients]
        ing_map = {ing_labels[idx]: int(ingredients[idx][0]) for idx in range(len(ingredients))}

        st.subheader("Create Order")
        c1, c2, c3 = st.columns([4, 2, 2])
        ing_label = c1.selectbox("Ingredient", ing_labels)
        qty = c2.number_input("Qty needed", min_value=0.0, value=0.0, step=0.1)
        if c3.button("Create", use_container_width=True):
            if qty <= 0:
                st.error("Qty must be > 0.")
            else:
                cur.execute("""
                    INSERT INTO orders (ingredient_id, qty, status, created_at)
                    VALUES (?, ?, 'Pending', ?)
                """, (ing_map[ing_label], float(qty), now_iso()))
                conn.commit()
                st.success("Order created (Pending).")

        st.divider()
        st.subheader("Pending Orders (Fulfil from Prep)")

        pending = cur.execute("""
            SELECT o.id, o.ingredient_id, i.name, i.unit, o.qty, o.created_at
            FROM orders o
            JOIN ingredients i ON i.id = o.ingredient_id
            WHERE o.status = 'Pending'
            ORDER BY o.id ASC
        """).fetchall()

        if not pending:
            st.info("No pending orders.")
        else:
            for oid, ing_id, name, unit, oqty, created_at in pending:
                ensure_stock_rows(int(ing_id))
                prep_have = get_stock(int(ing_id), "Prep Kitchen")
                trailer_have = get_stock(int(ing_id), "Trailer")

                col1, col2, col3, col4 = st.columns([5, 2, 2, 2])
                col1.write(f"#{oid} • {name} • {oqty:g} {unit} • {created_at}")
                col2.write(f"Prep: {prep_have:g}")
                col3.write(f"Trailer: {trailer_have:g}")

                if col4.button("Fulfil", key=f"fulfil_{oid}", use_container_width=True):
                    if float(oqty) > prep_have:
                        st.error(f"Not enough Prep stock to fulfil (have {prep_have:g}).")
                    else:
                        # Move stock Prep -> Trailer
                        cur.execute("""
                            UPDATE stock SET qty = qty - ?
                            WHERE ingredient_id = ? AND location = 'Prep Kitchen'
                        """, (float(oqty), int(ing_id)))
                        cur.execute("""
                            UPDATE stock SET qty = qty + ?
                            WHERE ingredient_id = ? AND location = 'Trailer'
                        """, (float(oqty), int(ing_id)))

                        cur.execute("UPDATE orders SET status = 'Completed' WHERE id = ?", (oid,))
                        conn.commit()
                        st.success(f"Fulfilled order #{oid}")

# =========================================================
# Stock Overview
# =========================================================
if page == "Stock Overview":
    st.title("Stock Overview")

    location = st.selectbox("Location", LOCATIONS)
    rows = cur.execute("""
        SELECT i.name, i.unit, i.par_level, s.qty
        FROM stock s
        JOIN ingredients i ON i.id = s.ingredient_id
        WHERE s.location = ?
        ORDER BY i.name
    """, (location,)).fetchall()

    if not rows:
        st.info("No stock yet. Add ingredients in Admin.")
    else:
        data = []
        for name, unit, par, qty in rows:
            data.append({
                "ingredient": name,
                "qty": float(qty),
                "unit": unit,
                "par_level": float(par),
                "status": "LOW" if float(qty) < float(par) else "OK"
            })
        st.dataframe(data, use_container_width=True)

# =========================================================
# Adjust Stock
# =========================================================
if page == "Adjust Stock":
    st.title("Adjust Stock")

    ingredients = cur.execute("SELECT id, name, unit FROM ingredients ORDER BY name").fetchall()
    if not ingredients:
        st.warning("Add ingredients first (Admin).")
    else:
        ing_labels = [f"{i[1]} ({i[2]})" if i[2] else i[1] for i in ingredients]
        ing_map = {ing_labels[idx]: int(ingredients[idx][0]) for idx in range(len(ingredients))}

        c1, c2, c3 = st.columns([4, 2, 2])
        ing_label = c1.selectbox("Ingredient", ing_labels)
        location = c2.selectbox("Location", LOCATIONS)
        delta = c3.number_input("Change (+ add / - remove)", value=0.0, step=0.1)

        if st.button("Apply change"):
            ing_id = ing_map[ing_label]
            ensure_stock_rows(ing_id)

            current = get_stock(ing_id, location)
            new_qty = current + float(delta)

            if new_qty < 0:
                st.error(f"Blocked: would go negative ({new_qty:g}).")
            else:
                cur.execute("""
                    UPDATE stock SET qty = ?
                    WHERE ingredient_id = ? AND location = ?
                """, (float(new_qty), ing_id, location))
                conn.commit()
                st.success(f"Updated {ing_label} in {location} to {new_qty:g}")

# =========================================================
# Admin (Add + Recipes)
# =========================================================
if page == "Admin (Add + Recipes)":
    st.title("Admin")

    st.subheader("Add Ingredient")
    c1, c2, c3, c4 = st.columns([4, 2, 2, 2])
    ing_name = c1.text_input("Ingredient name", placeholder="Chicken thigh (raw)")
    ing_unit = c2.text_input("Unit", placeholder="kg / pcs / L")
    ing_par = c3.number_input("Par level", min_value=0.0, value=0.0, step=0.1)
    if c4.button("Add Ingredient", use_container_width=True):
        if not ing_name.strip():
            st.error("Ingredient name required.")
        else:
            try:
                cur.execute(
                    "INSERT INTO ingredients (name, unit, par_level) VALUES (?, ?, ?)",
                    (ing_name.strip(), ing_unit.strip(), float(ing_par))
                )
                conn.commit()
                ing_id = cur.execute("SELECT id FROM ingredients WHERE name = ?", (ing_name.strip(),)).fetchone()[0]
                ensure_stock_rows(int(ing_id))
                st.success("Ingredient added.")
            except sqlite3.IntegrityError:
                st.error("Ingredient already exists.")

    st.divider()

    st.subheader("Add Product")
    p1, p2, p3 = st.columns([4, 2, 2])
    prod_name = p1.text_input("Product name", placeholder="KFC Burger")
    prod_price = p2.number_input("Price", min_value=0.0, value=0.0, step=0.5)
    if p3.button("Add Product", use_container_width=True):
        if not prod_name.strip():
            st.error("Product name required.")
        else:
            try:
                cur.execute(
                    "INSERT INTO products (name, price) VALUES (?, ?)",
                    (prod_name.strip(), float(prod_price))
                )
                conn.commit()
                st.success("Product added.")
            except sqlite3.IntegrityError:
                st.error("Product already exists.")

    st.divider()

    st.subheader("Build / Update Recipe")
    products = cur.execute("SELECT id, name FROM products ORDER BY name").fetchall()
    ingredients = cur.execute("SELECT id, name, unit FROM ingredients ORDER BY name").fetchall()

    if not products:
        st.info("Add products first.")
    elif not ingredients:
        st.info("Add ingredients first.")
    else:
        prod_map = {p[1]: int(p[0]) for p in products}
        ing_labels = [f"{i[1]} ({i[2]})" if i[2] else i[1] for i in ingredients]
        ing_map = {ing_labels[idx]: int(ingredients[idx][0]) for idx in range(len(ingredients))}

        r1, r2, r3 = st.columns([4, 4, 2])
        sel_prod = r1.selectbox("Product", list(prod_map.keys()))
        sel_ing = r2.selectbox("Ingredient", ing_labels)
        qty_req = r3.number_input("Qty per 1 sale", min_value=0.0, value=0.0, step=0.01)

        if st.button("Add/Update recipe line"):
            pid = prod_map[sel_prod]
            iid = ing_map[sel_ing]
            ensure_stock_rows(iid)

            cur.execute("""
                INSERT OR REPLACE INTO recipes (product_id, ingredient_id, qty_required)
                VALUES (?, ?, ?)
            """, (pid, iid, float(qty_req)))
            conn.commit()
            st.success("Recipe updated.")

        # Show recipe for selected product
        st.subheader(f"Current recipe: {sel_prod}")
        pid = prod_map[sel_prod]
        lines = cur.execute("""
            SELECT i.name, i.unit, r.qty_required
            FROM recipes r
            JOIN ingredients i ON i.id = r.ingredient_id
            WHERE r.product_id = ?
            ORDER BY i.name
        """, (pid,)).fetchall()

        if lines:
            st.dataframe(
                [{"ingredient": f"{a} ({u})" if u else a, "qty_required": float(q)} for a, u, q in lines],
                use_container_width=True
            )
        else:
            st.info("No recipe lines yet for this product.")
