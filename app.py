import streamlit as st
import sqlite3
from datetime import datetime

st.set_page_config(page_title="JAEJU Pro System", layout="wide")

# ---------- DATABASE ----------
conn = sqlite3.connect("jaeju_pro.db", check_same_thread=False)
cur = conn.cursor()

# INGREDIENTS
cur.execute("""
CREATE TABLE IF NOT EXISTS ingredients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    unit TEXT,
    par_level REAL DEFAULT 0
)
""")

# PRODUCTS
cur.execute("""
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    price REAL NOT NULL
)
""")

# RECIPES (Product -> Ingredient mapping)
cur.execute("""
CREATE TABLE IF NOT EXISTS recipes (
    product_id INTEGER,
    ingredient_id INTEGER,
    qty_required REAL,
    PRIMARY KEY (product_id, ingredient_id)
)
""")

# STOCK
cur.execute("""
CREATE TABLE IF NOT EXISTS stock (
    ingredient_id INTEGER,
    location TEXT,
    qty REAL DEFAULT 0,
    PRIMARY KEY (ingredient_id, location)
)
""")

# SALES
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

LOCATIONS = ["Trailer", "Prep Kitchen"]

st.sidebar.title("JAEJU Pro")
page = st.sidebar.radio("Navigate", [
    "Add Ingredient",
    "Add Product",
    "Build Recipe",
    "Stock Overview",
    "Adjust Stock",
    "POS"
])

# ---------- ADD INGREDIENT ----------
if page == "Add Ingredient":
    st.header("Add Ingredient")
    name = st.text_input("Ingredient Name")
    unit = st.text_input("Unit (kg, piece, L, etc)")
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
                    "INSERT INTO stock (ingredient_id, location, qty) VALUES (?, ?, 0)",
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
        product_dict = {name: id for id, name in products}
        ingredient_dict = {name: id for id, name in ingredients}

        selected_product = st.selectbox("Product", list(product_dict.keys()))
        selected_ingredient = st.selectbox("Ingredient", list(ingredient_dict.keys()))
        qty = st.number_input("Quantity Required", 0.0)

        if st.button("Add To Recipe"):
            cur.execute("""
            INSERT OR REPLACE INTO recipes (product_id, ingredient_id, qty_required)
            VALUES (?, ?, ?)
            """, (
                product_dict[selected_product],
                ingredient_dict[selected_ingredient],
                qty
            ))
            conn.commit()
            st.success("Recipe updated")

# ---------- STOCK OVERVIEW ----------
if page == "Stock Overview":
    st.header("Stock Overview")

    data = cur.execute("""
    SELECT i.name, s.location, s.qty, i.par_level, i.unit
    FROM stock s
    JOIN ingredients i ON i.id = s.ingredient_id
    ORDER BY i.name
    """).fetchall()

    for name, location, qty, par, unit in data:
        col1, col2, col3, col4 = st.columns(4)
        col1.write(name)
        col2.write(location)
        col3.write(f"{qty} {unit}")
        if qty < par:
            col4.error("LOW")
        else:
            col4.success("OK")

# ---------- ADJUST STOCK ----------
if page == "Adjust Stock":
    st.header("Adjust Stock")

    ingredients = cur.execute("SELECT id, name FROM ingredients").fetchall()
    ing_dict = {name: id for id, name in ingredients}

    selected = st.selectbox("Ingredient", list(ing_dict.keys()))
    location = st.selectbox("Location", LOCATIONS)
    change = st.number_input("Change (+ or -)", value=0.0)

    if st.button("Update"):
        cur.execute("""
        UPDATE stock
        SET qty = qty + ?
        WHERE ingredient_id = ? AND location = ?
        """, (change, ing_dict[selected], location))
        conn.commit()
        st.success("Stock updated")

# ---------- POS ----------
if page == "POS":
    st.header("POS (Deducts Ingredients Automatically)")

    products = cur.execute("SELECT id, name, price FROM products").fetchall()

    for product_id, name, price in products:
        col1, col2, col3 = st.columns([3,1,1])
        col1.write(f"{name} - ${price:.2f}")
        qty = col2.number_input("Qty", 0, key=f"qty_{product_id}")

        if col3.button("Sell", key=f"sell_{product_id}"):
            total = qty * price

            # deduct ingredients
            recipe_items = cur.execute("""
            SELECT ingredient_id, qty_required
            FROM recipes
            WHERE product_id = ?
            """, (product_id,)).fetchall()

            for ing_id, qty_required in recipe_items:
                cur.execute("""
                UPDATE stock
                SET qty = qty - ?
                WHERE ingredient_id = ? AND location = 'Trailer'
                """, (qty_required * qty, ing_id))

            cur.execute("""
            INSERT INTO sales (product_id, qty, total, created_at)
            VALUES (?, ?, ?, ?)
            """, (product_id, qty, total, datetime.now().isoformat()))

            conn.commit()
            st.success(f"Sold {qty} {name}")
