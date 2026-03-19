#!/usr/bin/env python3
"""Initialize the database with all tables."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import init_db

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully.")
