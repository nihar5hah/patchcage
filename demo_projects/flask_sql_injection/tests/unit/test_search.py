import sqlite3

import pytest
from demo_app import create_app, create_database, search_products


@pytest.fixture
def database() -> sqlite3.Connection:
    connection = create_database()
    yield connection
    connection.close()


def test_search_returns_matching_product(database: sqlite3.Connection) -> None:
    rows = search_products(database, "notebook")

    assert [row["name"] for row in rows] == ["Blue notebook"]


def test_search_returns_empty_list_for_unknown_product(database: sqlite3.Connection) -> None:
    assert search_products(database, "missing") == []


def test_http_endpoint_requires_query(database: sqlite3.Connection) -> None:
    client = create_app(database).test_client()

    response = client.get("/search")

    assert response.status_code == 400
    assert response.get_json() == {"error": "query is required"}
