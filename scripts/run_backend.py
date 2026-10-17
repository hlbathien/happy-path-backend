#!/usr/bin/env python3
"""Start with Uvicorn's native .env handling and a non-destructive database upgrade."""
from pathlib import Path
import os
import uvicorn

ROOT = Path(__file__).resolve().parent.parent


def main():
    # Config loads .env before the application/config modules are imported.
    config = uvicorn.Config('happy_path.main:app', host=os.getenv('HAPPY_PATH_HOST', '0.0.0.0'), port=8000,
                            env_file=ROOT / '.env' if (ROOT / '.env').exists() else None)
    from happy_path.config import DB_PATH
    from seed_db import build_database
    from migrate_db import migrate_database
    if not DB_PATH.exists():
        build_database(DB_PATH)
    migrate_database(DB_PATH)
    uvicorn.Server(config).run()


if __name__ == '__main__':
    main()
