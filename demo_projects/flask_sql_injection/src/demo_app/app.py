import sqlite3

from flask import Flask, jsonify, request

from demo_app.search import create_database, search_products


def create_app(connection: sqlite3.Connection | None = None) -> Flask:
    app = Flask(__name__)
    database = connection or create_database()

    @app.get("/search")
    def search() -> tuple[object, int] | object:
        query = request.args.get("query")
        if query is None:
            return jsonify({"error": "query is required"}), 400
        rows = search_products(database, query)
        return jsonify([dict(row) for row in rows])

    return app
