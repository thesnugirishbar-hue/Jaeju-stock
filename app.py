import streamlit as st
import sqlite3
from datetime import datetime

st.set_page_config(page_title="JAEJU Stock & POS", layout="wide")

# ---------- DATABASE ----------
conn = sqlite3.connect("jaeju.db", check_same_thread=False)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    price REAL DEFAULT 0,
    par_level REAL DEFAULT 0,
    active INTEGER DEFAULT 1
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS stock (
    item_id INTEGER,
    location TEXT,
    qty REAL DEFAULT 0,
    PRIMARY KEY (item_id, location)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    qty REAL,
    total REAL,
    created_at TEXT
)
""")

conn.commit()

# ---------- SIDEBAR ----------
st.sidebar.title("JAEJU Control")
page = st.sidebar.radio("Navigate", [
    "Add Item",
    "Stock Overview",
    "Adjust Stock",
    "Transfer Stock",
    "POS"
])

LOCATIONS = ["Trailer", "Prep Kitchen"]

# ---------- ADD ITEM ----------
if page == "Add Item":
    st.header("Add New Item")

    name = st.text_input("Item Name")
    price = st.number_input("Sell Price", 0.0)
    par = st.number_input("Par Level", 0.0)

    if st.button("Add Item"):
        try:
            cur.execute(
                "INSERT INTO items (name, price, par_level) VALUES (?, ?, ?)",
                (name, price, par)
            )
            conn.commit()

            item_id = cur.lastrowid

            for loc in LOCATIONS:
                cur.execute(
                    "INSERT INTO stock (item_id, location, qty) VALUES (?, ?, 0)",
                    (item_id, loc)
                )

            conn.commit()
            st.success("Item added successfully")

        except sqlite3.IntegrityError:
            st.error("Item name already exists")

# ---------- STOCK OVERVIEW ----------
if page == "Stock Overview":
    st.header("Stock Overview")

    data = cur.execute("""
    SELECT i.name, s.location, s.qty, i.par_level
    FROM stock s
    JOIN items i ON i.id = s.item_id
    ORDER BY i.name
    """).fetchall()

    for row in data:
        name, location, qty, par = row
        col1, col2, col3, col4 = st.columns(4)
        col1.write(name)
        col2.write(location)
        col3.write(qty)
        if qty < par:
            col4.error("LOW")
        else:
            col4.success("OK")

# ---------- ADJUST STOCK ----------
if page == "Adjust Stock":
    st.header("Adjust Stock")

    items = cur.execute("SELECT id, name FROM items").fetchall()
    item_dict = {name: id for id, name in items}

    selected_name = st.selectbox("Select Item", list(item_dict.keys()))
    location = st.selectbox("Location", LOCATIONS)
    change = st.number_input("Change (+ or -)", value=0.0)

    if st.button("Update"):
        item_id = item_dict[selected_name]
        cur.execute("""
        UPDATE stock
        SET qty = qty + ?
        WHERE item_id = ? AND location = ?
        """, (change, item_id, location))
        conn.commit()
        st.success("Stock updated")

# ---------- TRANSFER STOCK ----------
if page == "Transfer Stock":
    st.header("Transfer Between Locations")

    items = cur.execute("SELECT id, name FROM items").fetchall()
    item_dict = {name: id for id, name in items}

    selected_name = st.selectbox("Item", list(item_dict.keys()))
    from_loc = st.selectbox("From", LOCATIONS)
    to_loc = st.selectbox("To", LOCATIONS)
    qty = st.number_input("Quantity", 0.0)

    if st.button("Transfer"):
        item_id = item_dict[selected_name]

        cur.execute("""
        UPDATE stock SET qty = qty - ?
        WHERE item_id = ? AND location = ?
        """, (qty, item_id, from_loc))

        cur.execute("""
        UPDATE stock SET qty = qty + ?
        WHERE item_id = ? AND location = ?
        """, (qty, item_id, to_loc))

        conn.commit()
        st.success("Transfer complete")

# ---------- POS ----------
if page == "POS":
    st.header("Simple POS (Trailer)")

    items = cur.execute(
        "SELECT id, name, price FROM items WHERE active = 1"
    ).fetchall()

    for item_id, name, price in items:
        col1, col2, col3 = st.columns([3,1,1])
        col1.write(f"{name} - ${price:.2f}")

        qty = col2.number_input(
            "Qty",
            min_value=0,
            step=1,
            key=f"qty_{item_id}"
        )

        if col3.button("Sell", key=f"sell_{item_id}"):
            total = qty * price

            cur.execute("""
            INSERT INTO sales (item_id, qty, total, created_at)
            VALUES (?, ?, ?, ?)
            """, (item_id, qty, total, datetime.now().isoformat()))

            cur.execute("""
            UPDATE stock
            SET qty = qty - ?
            WHERE item_id = ? AND location = 'Trailer'
            """, (qty, item_id))

            conn.commit()

            st.success(f"Sold {qty} {name}")
