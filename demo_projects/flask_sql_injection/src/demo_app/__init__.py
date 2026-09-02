"""Deliberately vulnerable Flask demo used by PatchCage tests."""

from demo_app.app import create_app
from demo_app.search import create_database, search_products

__all__ = ["create_app", "create_database", "search_products"]
