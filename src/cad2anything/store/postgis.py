"""Versioned spatial relationship model in PostgreSQL + PostGIS (local engineering CRS, SRID 0).

Authentication uses Microsoft Entra tokens; no passwords are stored. Connection settings come from
POSTGRES_HOST, POSTGRES_DATABASE, and POSTGRES_USER (a user principal name or managed identity name).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from cad2anything.model import (
    ChangeSet,
    Entity,
    EntityKind,
    Floor,
    Relation,
    RelationType,
    Revision,
    SpatialModel,
)

TOKEN_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS revision (
    rev_id text PRIMARY KEY,
    kind text NOT NULL,
    parent_rev_id text,
    effective_date date,
    source text,
    notes text,
    loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS floor (
    rev_id text NOT NULL REFERENCES revision ON DELETE CASCADE,
    code text NOT NULL,
    ordinal integer NOT NULL,
    name text,
    elevation_m double precision,
    height_m double precision,
    PRIMARY KEY (rev_id, code)
);
CREATE TABLE IF NOT EXISTS entity_version (
    rev_id text NOT NULL REFERENCES revision ON DELETE CASCADE,
    entity_id text NOT NULL,
    kind text NOT NULL,
    floor text,
    label text,
    attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom geometry NOT NULL,
    PRIMARY KEY (rev_id, entity_id)
);
CREATE INDEX IF NOT EXISTS entity_version_geom_idx ON entity_version USING gist (geom);
CREATE INDEX IF NOT EXISTS entity_version_kind_idx ON entity_version (rev_id, kind, floor);
CREATE TABLE IF NOT EXISTS spatial_relation (
    rev_id text NOT NULL REFERENCES revision ON DELETE CASCADE,
    from_id text NOT NULL,
    to_id text NOT NULL,
    type text NOT NULL,
    metrics jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS spatial_relation_from_idx ON spatial_relation (rev_id, from_id);
CREATE INDEX IF NOT EXISTS spatial_relation_to_idx ON spatial_relation (rev_id, to_id);
CREATE TABLE IF NOT EXISTS change_set (
    rev_from text NOT NULL,
    rev_to text NOT NULL,
    items jsonb NOT NULL,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rev_from, rev_to)
);
"""


def connection_settings() -> dict[str, str]:
    host = os.environ.get("POSTGRES_HOST") or os.environ.get("PGHOST")
    if not host:
        raise RuntimeError("POSTGRES_HOST is not set")
    return {
        "host": host,
        "dbname": os.environ.get("POSTGRES_DATABASE") or os.environ.get("PGDATABASE") or "cad2anything",
        "user": os.environ.get("POSTGRES_USER") or os.environ.get("PGUSER") or "",
    }


def entra_token() -> str:
    from azure.identity import DefaultAzureCredential

    client_id = os.environ.get("AZURE_CLIENT_ID")
    credential = (
        DefaultAzureCredential(managed_identity_client_id=client_id)
        if client_id
        else DefaultAzureCredential()
    )
    return credential.get_token(TOKEN_SCOPE).token


def connect(**overrides: Any) -> Any:
    import psycopg

    settings = connection_settings() | overrides
    if not settings.get("user"):
        raise RuntimeError("POSTGRES_USER is not set")
    password = os.environ.get("PGPASSWORD") or entra_token()
    return psycopg.connect(**settings, password=password, sslmode="require", connect_timeout=15)


def ensure_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()


def save_model(conn: Any, model: SpatialModel, source: str | None = None) -> dict[str, int]:
    rev = model.revision
    with conn.cursor() as cur:
        cur.execute("DELETE FROM revision WHERE rev_id = %s", (rev.rev_id,))
        cur.execute(
            "INSERT INTO revision (rev_id, kind, parent_rev_id, effective_date, source, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                rev.rev_id,
                str(rev.kind),
                rev.parent_rev_id,
                rev.effective_date,
                source or rev.source,
                rev.notes,
            ),
        )
        cur.executemany(
            "INSERT INTO floor (rev_id, code, ordinal, name, elevation_m, height_m) VALUES (%s, %s, %s, %s, %s, %s)",
            [(rev.rev_id, f.code, f.ordinal, f.name, f.elevation_m, f.height_m) for f in model.floors],
        )
        entity_sql = (
            "COPY entity_version (rev_id, entity_id, kind, floor, label, attributes, geom) FROM STDIN"
        )
        with cur.copy(entity_sql) as copy:
            for entity in model.entities.values():
                copy.write_row(
                    (
                        rev.rev_id,
                        entity.entity_id,
                        str(entity.kind),
                        entity.floor,
                        entity.label,
                        json.dumps(entity.attributes, ensure_ascii=False, default=str),
                        entity.geometry.wkt,
                    )
                )
        with cur.copy("COPY spatial_relation (rev_id, from_id, to_id, type, metrics) FROM STDIN") as copy:
            for relation in model.relations:
                copy.write_row(
                    (
                        rev.rev_id,
                        relation.from_id,
                        relation.to_id,
                        str(relation.type),
                        json.dumps(relation.metrics),
                    )
                )
    conn.commit()
    return {"entities": len(model.entities), "relations": len(model.relations), "floors": len(model.floors)}


def load_model(conn: Any, rev_id: str) -> SpatialModel:
    from shapely import wkt

    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, parent_rev_id, effective_date::text, source, notes FROM revision WHERE rev_id = %s",
            (rev_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(rev_id)
        revision = {
            "rev_id": rev_id,
            "kind": row[0],
            "parent_rev_id": row[1],
            "effective_date": row[2],
            "source": row[3],
            "notes": row[4] or "",
        }
        model = SpatialModel(revision=Revision.from_dict(revision))
        cur.execute(
            "SELECT code, ordinal, name, elevation_m, height_m FROM floor WHERE rev_id = %s ORDER BY ordinal",
            (rev_id,),
        )
        model.floors = [Floor(*r) for r in cur.fetchall()]
        cur.execute(
            "SELECT entity_id, kind, floor, label, attributes, ST_AsText(geom) FROM entity_version WHERE rev_id = %s",
            (rev_id,),
        )
        for entity_id, kind, floor, label, attributes, geom in cur.fetchall():
            model.add(
                Entity(entity_id, EntityKind(kind), floor, wkt.loads(geom), label, dict(attributes or {}))
            )
        cur.execute("SELECT from_id, to_id, type, metrics FROM spatial_relation WHERE rev_id = %s", (rev_id,))
        model.relations = [Relation(a, b, RelationType(t), dict(m or {})) for a, b, t, m in cur.fetchall()]
    return model


def save_change_set(conn: Any, change_set: ChangeSet, meta: dict[str, Any] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO change_set (rev_from, rev_to, items, meta) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (rev_from, rev_to) DO UPDATE "
            "SET items = EXCLUDED.items, meta = EXCLUDED.meta, loaded_at = now()",
            (
                change_set.rev_from,
                change_set.rev_to,
                json.dumps([i.to_dict() for i in change_set.items], ensure_ascii=False),
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )
    conn.commit()


def nearby_units(conn: Any, rev_id: str, unit_code: str, radius_m: float = 15.0) -> list[dict[str, Any]]:
    """Units on the same floor within ``radius_m`` of the given unit's boundary (PostGIS ST_DWithin)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.attributes->>'unit_code', o.attributes->>'brand_name', o.attributes->>'category',
                   round(ST_Distance(u.geom, o.geom)::numeric, 2), ST_Touches(u.geom, o.geom)
            FROM entity_version u
            JOIN entity_version o
              ON o.rev_id = u.rev_id AND o.kind = 'unit' AND o.floor = u.floor AND o.entity_id <> u.entity_id
             AND ST_DWithin(u.geom, o.geom, %s)
            WHERE u.rev_id = %s AND u.kind = 'unit' AND u.attributes->>'unit_code' = %s
            ORDER BY ST_Distance(u.geom, o.geom), 1
            """,
            (radius_m, rev_id, unit_code),
        )
        return [
            {"unit_code": c, "brand_name": b, "category": k, "distance_m": float(d), "touches": t}
            for c, b, k, d, t in cur.fetchall()
        ]


def sync_workspace(root: Path) -> dict[str, Any]:
    from cad2anything.workspace import Workspace

    workspace = Workspace(root)
    summary: dict[str, Any] = {"models": {}, "change_sets": 0}
    with connect() as conn:
        ensure_schema(conn)
        for revision in workspace.revisions():
            meta = workspace.model_meta(revision.rev_id)
            model = workspace.model(revision.rev_id)
            summary["models"][revision.rev_id] = save_model(conn, model, meta.get("source"))
        for path in sorted((Path(root) / "changes").glob("*__*.json")):
            rev_from, rev_to = path.stem.split("__")
            change_set = workspace.change_set(rev_from, rev_to)
            if change_set is not None:
                save_change_set(conn, change_set, workspace.change_meta(rev_from, rev_to))
                summary["change_sets"] += 1
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync the workspace spatial models into PostGIS.")
    parser.add_argument("command", choices=["schema", "sync"])
    parser.add_argument("--root", type=Path, default=Path("data/workspace"))
    args = parser.parse_args()
    if args.command == "schema":
        with connect() as conn:
            ensure_schema(conn)
        print("schema ok")
    else:
        print(json.dumps(sync_workspace(args.root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
