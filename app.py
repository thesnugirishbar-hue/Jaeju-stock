import streamlit as st
import sqlite3
from datetime import datetime, date
import pandas as pd

st.set_page_config(page_title="JAEJU POS", layout="wide")

DB_NAME = "jaeju_master_v1.db"
LOCATIONS = ["Trailer", "Prep Kitchen"]

conn = sqlite3.connect(DB_NAME, check_same_thread=False)
cur = conn.cursor()

# ---------- TABLES ----------
cur.execute("""
CREATE TABLE IF NOT EXISTS ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    unit TEXT,
    par_level REAL DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    price REAL
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS recipes (
    product_id INTEGER,
    ingredient_id INTEGER,
    qty_required REAL,
    PRIMARY KEY (product_id, ingredient_id)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS stock (
    ingredient_id INTEGER,
    location TEXT,
    qty REAL DEFAULT 0,
    PRIMARY KEY (ingredient_id, location)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER,
    qty REAL,
    total REAL,
    created_at TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient_id INTEGER,
    qty REAL,
    status TEXT,
    created_at TEXT
)
""")

conn.commit()

# ---------- SIDEBAR ----------
st.sidebar.title("JAEJU Control")
page = st.sidebar.radio("Navigate", [
    "POS",
    "Sales Reports",
    "Stock",
    "Orders",
    "Admin"
])

# ======================================================
# POS
# ======================================================
if page == "POS":

    st.title("JAEJU POS")

    products = cur.execute("SELECT id, name, price FROM products").fetchall()
    cols = st.columns(3)

    for i, (pid, name, price) in enumerate(products):
        with cols[i % 3]:
            if st.button(f"{name}\n${price:.2f}", use_container_width=True):

                recipe = cur.execute("""
                SELECT ingredient_id, qty_required
                FROM recipes
                WHERE product_id = ?
                """, (pid,)).fetchall()

                # stock check
                for ing_id, qty_required in recipe:
                    trailer_stock = cur.execute("""
                    SELECT qty FROM stock
                    WHERE ingredient_id = ? AND location = 'Trailer'
                    """, (ing_id,)).fetchone()[0]

                    if trailer_stock < qty_required:
                        st.error(f"Not enough stock for {name}")
                        break
                else:
                    for ing_id, qty_required in recipe:
                        cur.execute("""
                        UPDATE stock
                        SET qty = qty - ?
                        WHERE ingredient_id = ? AND location = 'Trailer'
                        """, (qty_required, ing_id))

                    cur.execute("""
                    INSERT INTO sales (product_id, qty, total, created_at)
                    VALUES (?, 1, ?, ?)
                    """, (pid, price, datetime.now().isoformat()))

                    conn.commit()
                    st.success(f"Sold {name}")

# ======================================================
# SALES REPORTS
# ======================================================
if page == "Sales Reports":

    st.title("Sales Reports")

    data = cur.execute("""
    SELECT p.name, s.qty, s.total, s.created_at
    FROM sales s
    JOIN products p ON p.id = s.product_id
    """).fetchall()

    if data:
        df = pd.DataFrame(data, columns=["product", "qty", "total", "created_at"])
        df["created_at"] = pd.to_datetime(df["created_at"])
        df["date"] = df["created_at"].dt.date

        selected_date = st.date_input("Select Date", date.today())
        filtered = df[df["date"] == selected_date]

        st.metric("Revenue", f"${filtered['total'].sum():.2f}")
        st.metric("Items Sold", f"{filtered['qty'].sum():.0f}")

        st.subheader("Revenue by Product")
        summary = filtered.groupby("product")["total"].sum().reset_index()
        st.dataframe(summary, use_container_width=True)

    else:
        st.info("No sales yet.")

# ======================================================
# STOCK
# ======================================================
if page == "Stock":

    st.title("Stock Overview")

    location = st.selectbox("Location", LOCATIONS)

    data = cur.execute("""
    SELECT i.name, s.qty, i.unit
    FROM stock s
    JOIN ingredients i ON i.id = s.ingredient_id
    WHERE s.location = ?
    """, (location,)).fetchall()

    for name, qty, unit in data:
        st.write(f"{name} — {qty} {unit}")

# ======================================================
# ORDERS
# ======================================================
if page == "Orders":

    st.title("Orders")

    ingredients = cur.execute("SELECT id, name FROM ingredients").fetchall()
    ing_dict = {name: id for id, name in ingredients}

    selected = st.selectbox("Ingredient", list(ing_dict.keys()))
    qty = st.number_input("Quantity", 0.0)

    if st.button("Create Order"):
        cur.execute("""
        INSERT INTO orders (ingredient_id, qty, status, created_at)
        VALUES (?, ?, 'Pending', ?)
        """, (ing_dict[selected], qty, datetime.now().isoformat()))
        conn.commit()
        st.success("Order created")

    st.subheader("Pending Orders")

    pending = cur.execute("""
    SELECT o.id, i.name, o.qty, o.ingredient_id
    FROM orders o
    JOIN ingredients i ON i.id = o.ingredient_id
    WHERE o.status = 'Pending'
    """).fetchall()

    for order_id, name, qty, ing_id in pending:
        col1, col2 = st.columns([3,1])
        col1.write(f"{name} - {qty}")

        if col2.button("Fulfil", key=f"f_{order_id}"):

            cur.execute("""
            UPDATE stock SET qty = qty - ?
            WHERE ingredient_id = ? AND location = 'Prep Kitchen'
            """, (qty, ing_id))

            cur.execute("""
            UPDATE stock SET qty = qty + ?
            WHERE ingredient_id = ? AND location = 'Trailer'
            """, (qty, ing_id))

            cur.execute("""
            UPDATE orders SET status = 'Completed'
            WHERE id = ?
            """, (order_id,))

            conn.commit()
            st.success("Order fulfilled")

# ======================================================
# ADMIN
# ======================================================
if page == "Admin":

    st.title("Admin")

    st.subheader("Add Ingredient")
    name = st.text_input("Ingredient Name")
    unit = st.text_input("Unit")
    if st.button("Add Ingredient"):
        try:
            cur.execute("INSERT INTO ingredients (name, unit) VALUES (?, ?)", (name, unit))
            conn.commit()
            st.success("Ingredient added")
        except:
            st.error("Exists")

    st.subheader("Add Product")
    pname = st.text_input("Product Name")
    price = st.number_input("Price", 0.0)
    if st.button("Add Product"):
        try:
            cur.execute("INSERT INTO products (name, price) VALUES (?, ?)", (pname, price))
            conn.commit()
            st.success("Product added")
        except:
            st.error("Exists")

    st.subheader("Build Recipe")

    products = cur.execute("SELECT id, name FROM products").fetchall()
    ingredients = cur.execute("SELECT id, name FROM ingredients").fetchall()

    if products and ingredients:
        prod_dict = {name: id for id, name in products}
        ing_dict = {name: id for id, name in ingredients}

        selected_product = st.selectbox("Product", list(prod_dict.keys()))
        selected_ingredient = st.selectbox("Ingredient", list(ing_dict.keys()))
        qty = st.number_input("Qty per sale", 0.0)

        if st.button("Add To Recipe"):
            cur.execute("""
            INSERT OR REPLACE INTO recipes (product_id, ingredient_id, qty_required)
            VALUES (?, ?, ?)
            """, (prod_dict[selected_product], ing_dict[selected_ingredient], qty))
            conn.commit()
            st.success("Recipe updated")    PRIMARY KEY (product_id, ingredient_id)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS stock (
    ingredient_id INTEGER,
    location TEXT,
    qty REAL DEFAULT 0,
    PRIMARY KEY (ingredient_id, location)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER,
    qty REAL,
    total REAL,
    created_at TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient_id INTEGER,
    qty REAL,
    status TEXT,
    created_at TEXT
)
""")

conn.commit()

# ---------- SIDEBAR ----------
st.sidebar.title("JAEJU Control")
page = st.sidebar.radio("Navigate", [
    "POS",
    "Sales Reports",
    "Stock Overview",
    "Create Order",
    "Fulfil Orders",
    "Admin"
])

# ==========================================================
# 🟢 IPAD BIG BUTTON POS
# ==========================================================
if page == "POS":

    st.title("JAEJU POS (Trailer)")

    products = cur.execute("SELECT id, name, price FROM products").fetchall()

    cols = st.columns(3)

    for i, (pid, name, price) in enumerate(products):
        with cols[i % 3]:
            if st.button(f"{name}\n${price:.2f}", use_container_width=True):
                qty = 1

                recipe = cur.execute("""
                SELECT ingredient_id, qty_required
                FROM recipes
                WHERE product_id = ?
                """, (pid,)).fetchall()

                # Stock check
                for ing_id, qty_required in recipe:
                    trailer_stock = cur.execute("""
                    SELECT qty FROM stock
                    WHERE ingredient_id = ? AND location = 'Trailer'
                    """, (ing_id,)).fetchone()[0]

                    if trailer_stock < qty_required:
                        st.error(f"Not enough stock for {name}")
                        break
                else:
                    # Deduct
                    for ing_id, qty_required in recipe:
                        cur.execute("""
                        UPDATE stock
                        SET qty = qty - ?
                        WHERE ingredient_id = ? AND location = 'Trailer'
                        """, (qty_required, ing_id))

                    cur.execute("""
                    INSERT INTO sales (product_id, qty, total, created_at)
                    VALUES (?, ?, ?, ?)
                    """, (pid, 1, price, datetime.now().isoformat()))

                    conn.commit()
                    st.success(f"Sold {name}")

# ==========================================================
# 📊 SALES REPORTS
# ==========================================================
if page == "Sales Reports":

    st.title("Sales Reports")

    sales_data = cur.execute("""
    SELECT p.name, s.qty, s.total, s.created_at
    FROM sales s
    JOIN products p ON p.id = s.product_id
    """).fetchall()

    if not sales_data:
        st.info("No sales yet.")
    else:
        df = pd.DataFrame(sales_data, columns=["product", "qty", "total", "created_at"])
        df["created_at"] = pd.to_datetime(df["created_at"])
        df["date"] = df["created_at"].dt.date

        selected_date = st.date_input("Select Date", date.today())
        filtered = df[df["date"] == selected_date]

        col1, col2 = st.columns(2)
        col1.metric("Total Revenue", f"${filtered['total'].sum():.2f}")
        col2.metric("Total Items Sold", f"{filtered['qty'].sum():.0f}")

        st.subheader("Revenue by Product")
        product_summary = filtered.groupby("product")["total"].sum().reset_index()
        st.dataframe(product_summary, use_container_width=True)

        st.subheader("Transactions")
        st.dataframe(filtered.sort_values("created_at", ascending=False), use_container_width=True)

# ==========================================================
# STOCK OVERVIEW
# ==========================================================
if page == "Stock Overview":
    st.title("Stock Overview")

    location = st.selectbox("Location", LOCATIONS)

    data = cur.execute("""
    SELECT i.name, s.qty, i.unit
    FROM stock s
    JOIN ingredients i ON i.id = s.ingredient_id
    WHERE s.location = ?
    ORDER BY i.name
    """, (location,)).fetchall()

    for name, qty, unit in data:
        st.write(f"{name} — {qty} {unit}")

# ==========================================================
# CREATE ORDER
# ==========================================================
if page == "Create Order":
    st.title("Create Order (Trailer → Prep)")

    ingredients = cur.execute("SELECT id, name FROM ingredients").fetchall()
    ing_dict = {name: id for id, name in ingredients}

    selected = st.selectbox("Ingredient", list(ing_dict.keys()))
    qty = st.number_input("Quantity", 0.0)

    if st.button("Create Order"):
        cur.execute("""
        INSERT INTO orders (ingredient_id, qty, status, created_at)
        VALUES (?, ?, 'Pending', ?)
        """, (ing_dict[selected], qty, datetime.now().isoformat()))
        conn.commit()
        st.success("Order created")

# ==========================================================
# FULFIL ORDERS
# ==========================================================
if page == "Fulfil Orders":
    st.title("Pending Orders")

    orders = cur.execute("""
    SELECT o.id, i.name, o.qty, o.ingredient_id
    FROM orders o
    JOIN ingredients i ON i.id = o.ingredient_id
    WHERE o.status = 'Pending'
    """).fetchall()

    for order_id, name, qty, ing_id in orders:
        col1, col2 = st.columns([3,1])
        col1.write(f"{name} - {qty}")

        if col2.button("Fulfil", key=f"f_{order_id}"):

            prep_stock = cur.execute("""
            SELECT qty FROM stock
            WHERE ingredient_id = ? AND location = 'Prep Kitchen'
            """, (ing_id,)).fetchone()[0]

            if prep_stock < qty:
                st.error("Not enough Prep stock")
            else:
                cur.execute("""
                UPDATE stock SET qty = qty - ?
                WHERE ingredient_id = ? AND location = 'Prep Kitchen'
                """, (qty, ing_id))

                cur.execute("""
                UPDATE stock SET qty = qty + ?
                WHERE ingredient_id = ? AND location = 'Trailer'
                """, (qty, ing_id))

                cur.execute("""
                UPDATE orders SET status = 'Completed'
                WHERE id = ?
                """, (order_id,))

                conn.commit()
                st.success("Order fulfilled")

# ==========================================================
# ADMIN PAGE
# ==========================================================
if page == "Admin":
    st.title("Admin Tools")

    st.write("Use previous version for adding ingredients, products, recipes.")
