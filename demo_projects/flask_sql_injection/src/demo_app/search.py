import os
import sqlite3

DEFAULT_PRODUCTS = ("Red mug", "Blue notebook", "Green bottle")


def create_database() -> sqlite3.Connection:
    # check_same_thread=False: the Flask dev server handles requests on a worker
    # thread, while this connection is created on the main thread.
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    seed = os.environ.get("DEMO_SEED_PRODUCTS")
    names = seed.split(",") if seed else list(DEFAULT_PRODUCTS)
    connection.executemany("INSERT INTO products (name) VALUES (?)", [(name,) for name in names])
    return connection


def search_products(connection: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    return list(connection.execute(f"SELECT id, name FROM products WHERE name LIKE '%{query}%'"))
