import streamlit as st
import sqlite3

# Connect to DB
conn = sqlite3.connect("jaeju.db", check_same_thread=False)
cur = conn.cursor()

# Create items table
cur.execute("""
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    par_level REAL DEFAULT 0,
    price_nzd REAL DEFAULT 0,
    active INTEGER DEFAULT 1
);
""")

# Create stock table
cur.execute("""
CREATE TABLE IF NOT EXISTS stock (
    item_id INTEGER NOT NULL,
    location TEXT NOT NULL,
    qty REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, location),
    FOREIGN KEY (item_id) REFERENCES items(id)
);
""")

conn.commit()

st.title("JAEJU Stock System")

st.success("App running correctly ✅")
