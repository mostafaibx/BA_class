#!/usr/bin/env python3
"""
document_schema.py
==================
Profiles the generated raw dataset (raw/ba_raw.sqlite) and writes:

    SCHEMA.md   - per-table schema reference (types, keys, nullability, profile)
    schema.sql  - typed DDL (CREATE TABLE with PK/FK) for loading the raw layer
                  into a relational database

The physical facts (types, NULLs, distinct counts, ranges) are measured from
the data, so the documentation always matches the latest generated dataset.
Keys, grain and source are declared below in TABLES.

Run after generate_data.py:
    python document_schema.py
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent
DB = BASE / "raw" / "ba_raw.sqlite"

# -----------------------------------------------------------------------------
# Declared metadata: source system, grain, primary key, foreign keys.
# FK format: (local columns) -> (table, (referenced columns), note)
# -----------------------------------------------------------------------------
TABLES = {
    "customers": dict(
        source="ERP – customer master", grain="One row per customer (current state)",
        pk=["customer_id"], fks=[]),
    "products": dict(
        source="ERP – product master", grain="One row per SKU (current state)",
        pk=["sku"], fks=[]),
    "product_price_history": dict(
        source="ERP – price list", grain="One row per SKU per price-validity period",
        pk=["sku", "valid_from"], fks=[(["sku"], "products", ["sku"], "")]),
    "pickers": dict(
        source="HR / WMS – staff master", grain="One row per picker",
        pk=["picker_id"], fks=[]),
    "carriers": dict(
        source="TMS – carrier contracts", grain="One row per carrier",
        pk=["carrier_code"], fks=[]),
    "orders": dict(
        source="API / ERP – order header", grain="One row per received API order message",
        pk=["api_message_id"],
        note="`order_id` is the business key but is **not unique** (API re-sends). "
             "De-duplicate before using it as a key.",
        fks=[(["customer_id"], "customers", ["customer_id"], "")]),
    "order_lines": dict(
        source="ERP – order lines", grain="One row per order line",
        pk=["order_id", "line_no"],
        fks=[(["order_id"], "orders", ["order_id"], "orders.order_id is not unique in raw"),
             (["sku"], "products", ["sku"], "")]),
    "inventory_reservations": dict(
        source="ERP – stock allocation", grain="One row per reserved order line",
        pk=["reservation_id"],
        fks=[(["order_id", "line_no"], "order_lines", ["order_id", "line_no"], ""),
             (["sku"], "products", ["sku"], "")]),
    "backorders": dict(
        source="ERP – backorder notices", grain="One row per short line of a backordered order",
        pk=["backorder_id"],
        fks=[(["order_id", "line_no"], "order_lines", ["order_id", "line_no"], ""),
             (["sku"], "products", ["sku"], "")]),
    "inventory_snapshots": dict(
        source="ERP – end-of-day stock", grain="One row per SKU per day (periodic snapshot)",
        pk=["snapshot_date", "sku"], fks=[(["sku"], "products", ["sku"], "")]),
    "pick_tasks": dict(
        source="WMS – pick tasks", grain="One row per pick task (= reserved order line)",
        pk=["pick_task_id"],
        fks=[(["order_id", "line_no"], "order_lines", ["order_id", "line_no"], ""),
             (["picker_id"], "pickers", ["picker_id"], "NULL while task is QUEUED"),
             (["ordered_sku"], "products", ["sku"], ""),
             (["picked_sku"], "products", ["sku"], "differs from ordered_sku on a wrong pick")]),
    "qc_checks": dict(
        source="WMS – pack & QC", grain="One row per QC attempt of an order",
        pk=["qc_check_id"], unique=["order_id", "attempt_no"],
        fks=[(["order_id"], "orders", ["order_id"], "")]),
    "shipments": dict(
        source="Carrier / tracking feed", grain="One row per shipment (= one order)",
        pk=["shipment_id"], unique=["order_id"],
        fks=[(["order_id"], "orders", ["order_id"], ""),
             (["carrier_name"], "carriers", ["carrier_name"],
              "soft key – names are dirty, needs TRIM/UPPER mapping (not enforced in DDL)")]),
    "invoices": dict(
        source="Finance / ERP – invoicing", grain="One row per invoice (= one order)",
        pk=["invoice_id"], unique=["order_id"],
        fks=[(["order_id"], "orders", ["order_id"], ""),
             (["customer_id"], "customers", ["customer_id"], "")]),
    "handling_costs": dict(
        source="Finance – general-ledger cost postings",
        grain="One row per cost posting (daily per cost centre/type; rent & utilities monthly)",
        pk=["cost_entry_id"], fks=[]),
}

VARCHAR_STEPS = [2, 3, 5, 10, 20, 40, 60, 100, 200, 500]


def logical_type(col: str, decl: str, s: pd.Series) -> str:
    if col.endswith("_ts") or col.endswith("_at"):
        return "TIMESTAMP"
    if col.endswith("_date") or col in ("valid_from", "valid_to"):
        return "DATE"
    if decl == "INTEGER":
        return "INTEGER"
    if decl == "REAL":
        if col == "vat_rate":
            return "DECIMAL(4,2)"
        return "DECIMAL(12,2)" if col.endswith("_eur") else "DECIMAL(10,2)"
    max_len = int(s.dropna().astype(str).str.len().max() or 1)
    return f"VARCHAR({next(n for n in VARCHAR_STEPS if n >= max_len)})"


def profile_value(col: str, typ: str, s: pd.Series, key_cols: set) -> str:
    v = s.dropna()
    if v.empty:
        return "–"
    distinct = v.nunique()
    if typ.startswith("VARCHAR") and col not in key_cols and distinct <= 8:
        vals = sorted(v.astype(str).unique())
        return ", ".join(f"`{x}`" if x == x.strip() else f"`'{x}'`" for x in vals)
    if typ.startswith("VARCHAR") and distinct > 8 and v.astype(str).str.len().max() > 25:
        return f"e.g. `{v.iloc[0]}`"
    return f"`{v.min()}` … `{v.max()}`"


def key_role(col: str, meta: dict) -> str:
    roles = []
    if col in meta["pk"]:
        roles.append("PK")
    if col in meta.get("unique", []):
        roles.append("UQ")
    for cols, ref_t, ref_c, _ in meta["fks"]:
        if col in cols:
            roles.append(f"FK→{ref_t}")
    return ", ".join(roles)


def main():
    con = sqlite3.connect(DB)
    md, ddl = [], []
    md += ["# Schema Reference – BA raw dataset", "",
           f"_Generated by `document_schema.py` on {datetime.now():%Y-%m-%d %H:%M} from "
           f"`raw/ba_raw.sqlite`. Do not edit by hand; re-run the script after regenerating "
           f"the data._", "",
           "Business meaning of each column: see [DATA_DICTIONARY.md](DATA_DICTIONARY.md). "
           "Typed DDL: [schema.sql](schema.sql).", "",
           "**Legend:** PK = primary key · UQ = unique · FK→t = foreign key to table t · "
           "Nullable = NULLs observed in the data · Profile = allowed values (low-cardinality "
           "text) or min … max. Values shown as `'x '` contain leading/trailing spaces.", "",
           "## Overview", "",
           "| Table | Source system | Grain | Rows | Columns | Primary key |",
           "|---|---|---|---:|---:|---|"]
    ddl += ["-- schema.sql – typed DDL for the BA raw layer",
            f"-- Generated by document_schema.py on {datetime.now():%Y-%m-%d %H:%M}",
            "-- Generic ANSI SQL. FKs to orders.order_id are commented out because raw",
            "-- orders contain duplicate order_ids (API re-sends).", ""]
    body = []
    for t, meta in TABLES.items():
        df = pd.read_sql(f"SELECT * FROM {t}", con)
        decl = {r[1]: r[2] for r in con.execute(f"PRAGMA table_info({t})")}
        md.append(f"| [`{t}`](#{t}) | {meta['source']} | {meta['grain']} | {len(df):,} | "
                  f"{df.shape[1]} | `{', '.join(meta['pk'])}` |")

        key_cols = set(meta["pk"]) | {c for f in meta["fks"] for c in f[0]}
        body += ["", f"## `{t}`", "",
                 f"- **Source:** {meta['source']}",
                 f"- **Grain:** {meta['grain']}",
                 f"- **Rows:** {len(df):,}",
                 f"- **Primary key:** `{', '.join(meta['pk'])}`"]
        if meta.get("unique"):
            body.append(f"- **Unique:** `{', '.join(meta['unique'])}`")
        for cols, ref_t, ref_c, note in meta["fks"]:
            body.append(f"- **Foreign key:** `{', '.join(cols)}` → `{ref_t}({', '.join(ref_c)})`"
                        + (f" – _{note}_" if note else ""))
        if meta.get("note"):
            body.append(f"- **Note:** {meta['note']}")
        body += ["", "| # | Column | Type | Key | Nullable | NULL % | Distinct | Profile |",
                 "|---:|---|---|---|---|---:|---:|---|"]

        cols_ddl = []
        for i, col in enumerate(df.columns, 1):
            s = df[col]
            typ = logical_type(col, decl[col], s)
            nulls = int(s.isna().sum())
            body.append(f"| {i} | `{col}` | {typ} | {key_role(col, meta)} | "
                        f"{'YES' if nulls else 'NO'} | {nulls / len(df):.1%} | "
                        f"{s.nunique():,} | {profile_value(col, typ, s, key_cols)} |")
            cols_ddl.append(f"    {col:<24} {typ}{'' if nulls else ' NOT NULL'}")

        cons = [f"    CONSTRAINT pk_{t} PRIMARY KEY ({', '.join(meta['pk'])})"]
        if meta.get("unique"):
            cons.append(f"    CONSTRAINT uq_{t} UNIQUE ({', '.join(meta['unique'])})")
        for cols, ref_t, ref_c, note in meta["fks"]:
            line = (f"    CONSTRAINT fk_{t}_{'_'.join(cols)} FOREIGN KEY ({', '.join(cols)}) "
                    f"REFERENCES {ref_t} ({', '.join(ref_c)})")
            soft = (ref_t == "orders") or "soft key" in note
            cons.append(("--" + line[2:] + "   -- not enforceable on raw data") if soft else line)
        enforced = [c for c in cols_ddl + cons if not c.lstrip().startswith("--")]
        lines = []
        for c in cols_ddl + cons:
            is_last_enforced = c is enforced[-1]
            lines.append(c if c.lstrip().startswith("--") or is_last_enforced else c + ",")
        ddl += [f"CREATE TABLE {t} (", *lines, ");", ""]

    md += body
    (BASE / "SCHEMA.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (BASE / "schema.sql").write_text("\n".join(ddl), encoding="utf-8")
    print("Wrote SCHEMA.md and schema.sql")


if __name__ == "__main__":
    main()
