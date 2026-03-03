import streamlit as st
import sqlite3
from datetime import datetime

st.set_page_config(page_title="JAEJU System", layout="wide")

DB_NAME = "jaeju_clean_v1.db"
LOCATIONS = ["Trailer", "Prep Kitchen"]

# ---------- DB ----------
conn = sqlite3.connect(DB_NAME, check_same_thread=False)
cur = conn.cursor()

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

conn.commit()

# ---------- SIDEBAR ----------
st.sidebar.title("JAEJU Control")
page = st.sidebar.radio("Navigate", [
    "Add Ingredient",
    "Add Product",
    "Build Recipe",
    "Stock Overview",
    "Adjust Stock",
    "Transfer Stock",
    "POS",
    "Sales Log"
])

# ---------- ADD INGREDIENT ----------
if page == "Add Ingredient":
    st.header("Add Ingredient")
    name = st.text_input("Ingredient Name")
    unit = st.text_input("Unit (kg, pcs, L)")
    par = st.number_input("Par Level", 0.0)

    if st.button("Add"):
        try:
            cur.execute(
                "INSERT INTO ingredients (name, unit, par_level) VALUES (?, ?, ?)",
                (name, unit, par)
            )
            conn.commit()

            ing_id = cur.lastrowid
            for loc in LOCATIONS:
                cur.execute(
                    "INSERT OR IGNORE INTO stock (ingredient_id, location, qty) VALUES (?, ?, 0)",
                    (ing_id, loc)
                )
            conn.commit()

            st.success("Ingredient added")
        except:
            st.error("Ingredient already exists")

# ---------- ADD PRODUCT ----------
if page == "Add Product":
    st.header("Add Product")
    name = st.text_input("Product Name")
    price = st.number_input("Sell Price", 0.0)

    if st.button("Add Product"):
        try:
            cur.execute(
                "INSERT INTO products (name, price) VALUES (?, ?)",
                (name, price)
            )
            conn.commit()
            st.success("Product added")
        except:
            st.error("Product already exists")

# ---------- BUILD RECIPE ----------
if page == "Build Recipe":
    st.header("Build Recipe")

    products = cur.execute("SELECT id, name FROM products").fetchall()
    ingredients = cur.execute("SELECT id, name FROM ingredients").fetchall()

    if products and ingredients:
        prod_dict = {name: id for id, name in products}
        ing_dict = {name: id for id, name in ingredients}

        selected_product = st.selectbox("Product", list(prod_dict.keys()))
        selected_ingredient = st.selectbox("Ingredient", list(ing_dict.keys()))
        qty = st.number_input("Qty per 1 sale", 0.0)

        if st.button("Add/Update"):
            cur.execute("""
            INSERT OR REPLACE INTO recipes (product_id, ingredient_id, qty_required)
            VALUES (?, ?, ?)
            """, (
                prod_dict[selected_product],
                ing_dict[selected_ingredient],
                qty
            ))
            conn.commit()
            st.success("Recipe updated")

# ---------- STOCK OVERVIEW ----------
if page == "Stock Overview":
    st.header("Stock Overview")
    location = st.selectbox("Location", LOCATIONS)

    data = cur.execute("""
    SELECT i.name, s.qty, i.unit, i.par_level
    FROM stock s
    JOIN ingredients i ON i.id = s.ingredient_id
    WHERE s.location = ?
    ORDER BY i.name
    """, (location,)).fetchall()

    for name, qty, unit, par in data:
        col1, col2, col3 = st.columns(3)
        col1.write(name)
        col2.write(f"{qty} {unit}")
        if qty < par:
            col3.error("LOW")
        else:
            col3.success("OK")

# ---------- ADJUST STOCK ----------
if page == "Adjust Stock":
    st.header("Adjust Stock")

    ingredients = cur.execute("SELECT id, name FROM ingredients").fetchall()
    ing_dict = {name: id for id, name in ingredients}

    selected = st.selectbox("Ingredient", list(ing_dict.keys()))
    location = st.selectbox("Location", LOCATIONS)
    change = st.number_input("Change (+/-)", value=0.0)

    if st.button("Apply"):
        cur.execute("""
        UPDATE stock
        SET qty = qty + ?
        WHERE ingredient_id = ? AND location = ?
        """, (change, ing_dict[selected], location))
        conn.commit()
        st.success("Stock updated")

# ---------- TRANSFER ----------
if page == "Transfer Stock":
    st.header("Transfer Prep → Trailer")

    ingredients = cur.execute("SELECT id, name FROM ingredients").fetchall()
    ing_dict = {name: id for id, name in ingredients}

    selected = st.selectbox("Ingredient", list(ing_dict.keys()))
    qty = st.number_input("Quantity", 0.0)

    if st.button("Transfer"):
        ing_id = ing_dict[selected]

        cur.execute("""
        UPDATE stock SET qty = qty - ?
        WHERE ingredient_id = ? AND location = 'Prep Kitchen'
        """, (qty, ing_id))

        cur.execute("""
        UPDATE stock SET qty = qty + ?
        WHERE ingredient_id = ? AND location = 'Trailer'
        """, (qty, ing_id))

        conn.commit()
        st.success("Transfer complete")

# ---------- POS ----------
if page == "POS":
    st.header("POS (Trailer)")

    products = cur.execute("SELECT id, name, price FROM products").fetchall()

    for pid, name, price in products:
        col1, col2, col3 = st.columns([3,1,1])
        col1.write(f"{name} - ${price:.2f}")
        qty = col2.number_input("Qty", min_value=0, value=1, key=f"qty_{pid}")

        if col3.button("Sell", key=f"sell_{pid}"):
            recipe = cur.execute("""
            SELECT ingredient_id, qty_required
            FROM recipes
            WHERE product_id = ?
            """, (pid,)).fetchall()

            for ing_id, qty_required in recipe:
                cur.execute("""
                UPDATE stock
                SET qty = qty - ?
                WHERE ingredient_id = ? AND location = 'Trailer'
                """, (qty_required * qty, ing_id))

            total = qty * price

            cur.execute("""
            INSERT INTO sales (product_id, qty, total, created_at)
            VALUES (?, ?, ?, ?)
            """, (pid, qty, total, datetime.now().isoformat()))

            conn.commit()
            st.success(f"Sold {qty} {name}")

# ---------- SALES LOG ----------
if page == "Sales Log":
    st.header("Sales Log")

    data = cur.execute("""
    SELECT p.name, s.qty, s.total, s.created_at
    FROM sales s
    JOIN products p ON p.id = s.product_id
    ORDER BY s.id DESC
    """).fetchall()

    for name, qty, total, time in data:
        st.write(f"{time} | {name} x{qty} | ${total:.2f}")
