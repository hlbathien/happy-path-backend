#!/usr/bin/env python3
"""Add the audit repair schema and configured road metadata without replacing live state."""
import argparse
import json
import sqlite3
from pathlib import Path

from happy_path.config import DB_PATH
from happy_path.core.graph import CorridorRegistry

ROOT = Path(__file__).resolve().parent.parent


def migrate_database(path: Path) -> None:
    path = CorridorRegistry._resolve_runtime_db_path(path.resolve())
    # mode=rw fails closed: a typo must not create an empty production database.
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=15, isolation_level=None)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=15000')
        conn.execute('BEGIN IMMEDIATE')
        additions = {
            'corridor_branches': {'real_branch_km': 'REAL NOT NULL DEFAULT 0', 'branch_waypoints_json': "TEXT NOT NULL DEFAULT '[]'"},
            'pois': {'is_24h': 'INTEGER NOT NULL DEFAULT 0', 'ev_max_kw': 'INTEGER NOT NULL DEFAULT 0',
                     'ev_stall_count': 'INTEGER NOT NULL DEFAULT 0', 'ev_connector_type': "TEXT NOT NULL DEFAULT 'NONE'",
                     'google_place_id': "TEXT NOT NULL DEFAULT ''", 'google_rating': 'REAL',
                     'user_rating_count': 'INTEGER NOT NULL DEFAULT 0'},
            'trip_sessions': {'version': 'INTEGER NOT NULL DEFAULT 1'},
        }
        for table, columns in additions.items():
            existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
            if not existing:
                raise RuntimeError(f'Missing required table: {table}')
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        # Execute DDL one statement at a time: executescript would commit the transaction early.
        for statement in (ROOT / 'data/schema.sql').read_text(encoding='utf-8').split(';'):
            if statement.strip() and not statement.strip().startswith('PRAGMA'):
                conn.execute(statement)
        conn.execute('UPDATE pois SET is_24h=1 WHERE open_minute=0 AND close_minute=1440')
        seed = json.loads((ROOT / 'data/seed_corridors.json').read_text(encoding='utf-8'))
        for corridor in seed['corridors']:
            cid = corridor['corridor_id']
            for branch in corridor['branches']:
                conn.execute('UPDATE corridor_branches SET real_branch_km=?,branch_waypoints_json=? WHERE corridor_id=? AND branch_id=?',
                             (branch.get('real_branch_km', 0), json.dumps(branch.get('branch_waypoints', [])), cid, branch['branch_id']))
            for plaza in corridor.get('toll_plazas', []):
                conn.execute('INSERT INTO toll_plazas VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(plaza_id) DO UPDATE SET '
                             'branch_id=excluded.branch_id,corridor_km=excluded.corridor_km,fee_vnd=excluded.fee_vnd,source_url=excluded.source_url',
                             (plaza['plaza_id'], cid, plaza['branch_id'], plaza['name_vi'], plaza['corridor_km'],
                              plaza['lat'], plaza['lng'], plaza['fee_vnd'], plaza.get('operator', 'VETC/ePass'), plaza['source_url']))
        conn.execute('COMMIT')
    except Exception:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()
    print(f'[OK] Migrated {path}; existing POIs, evidence, edges and sessions preserved.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-path', type=Path, default=DB_PATH)
    migrate_database(parser.parse_args().db_path)
