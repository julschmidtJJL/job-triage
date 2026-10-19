"""One-shot helper: print the NOTION_DATA_SOURCE_ID for the configured job board database.

Usage:
    python scripts/get_notion_data_source_id.py

Requires NOTION_API_KEY and NOTION_JOB_BOARD_DB_ID in .env (or the environment).
Paste the printed ID into the GitHub secret NOTION_DATA_SOURCE_ID.
"""
import os
import sys

from dotenv import load_dotenv
from notion_client import Client

load_dotenv()

api_key = os.environ.get("NOTION_API_KEY", "").strip()
db_id = os.environ.get("NOTION_JOB_BOARD_DB_ID", "").strip()

if not api_key or not db_id:
    sys.exit("Missing NOTION_API_KEY or NOTION_JOB_BOARD_DB_ID in environment / .env")

db = Client(auth=api_key).databases.retrieve(db_id)

sources = db.get("data_sources", [])
if not sources:
    sys.exit("No data_sources returned — is your notion-client >=3.0 and the DB shared with the integration?")

print(f"Database: {db.get('title', [{}])[0].get('plain_text', '<untitled>')}")
print(f"Found {len(sources)} data source(s):\n")
for ds in sources:
    print(f"  name: {ds.get('name', '<unnamed>')}")
    print(f"  id:   {ds.get('id')}\n")

if len(sources) == 1:
    print(f"Set this as the GitHub secret NOTION_DATA_SOURCE_ID:\n  {sources[0]['id']}")
