import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import psycopg
from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel

DATABASE_URL = os.environ.get("DATABASE_URL")

app = FastAPI(title="Unit Tracker (data dummy)")


@contextmanager
def get_conn():
    # Commit otomatis kalau sukses, rollback otomatis kalau ada error
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


# ---------- Model request ----------
class UnitIn(BaseModel):
    serial_number: str
    customer_id: int
    courier_id: int


class StatusIn(BaseModel):
    model_config = {"json_schema_extra": {"example": {"to_stage": "QC"}}}
    to_stage: str
    note: Optional[str] = None
    hold_reason: Optional[str] = None       # wajib kalau to_stage = On Hold
    scheduled_for: Optional[datetime] = None


# ---------- Endpoint ----------
@app.get("/health")
def health():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/units")
def list_units(stage: Optional[str] = None, limit: int = 100):
    sql = "SELECT * FROM units"
    params = []
    if stage:
        sql += " WHERE current_stage = %s"
        params.append(stage)
    sql += " ORDER BY po_at DESC LIMIT %s"
    params.append(limit)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


@app.post("/units", status_code=201)
def create_unit(body: UnitIn):
    with get_conn() as conn:
        try:
            unit = conn.execute(
                """INSERT INTO units (serial_number, customer_id, courier_id)
                   VALUES (%s, %s, %s) RETURNING *""",
                (body.serial_number, body.customer_id, body.courier_id),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "Serial number sudah ada")
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(422, "customer_id atau courier_id tidak ditemukan")
        conn.execute(
            "INSERT INTO status_events (unit_id, from_stage, to_stage) VALUES (%s, NULL, 'PO')",
            (unit["id"],),
        )
    return unit


@app.post("/units/{serial_number}/status")
def update_status(serial_number: str, body: StatusIn):
    # Semua langkah di bawah jalan dalam SATU transaksi
    with get_conn() as conn:
        unit = conn.execute(
            "SELECT * FROM units WHERE serial_number = %s FOR UPDATE", (serial_number,)
        ).fetchone()
        if not unit:
            raise HTTPException(404, "Unit tidak ditemukan")

        frm, to = unit["current_stage"], body.to_stage

        # 1. Validasi transisi lewat tabel allowed_transitions
        allowed = conn.execute(
            "SELECT 1 FROM allowed_transitions WHERE from_stage = %s AND to_stage = %s",
            (frm, to),
        ).fetchone()
        if not allowed:
            raise HTTPException(409, f"Transisi '{frm}' -> '{to}' tidak diizinkan")

        if to == "On Hold" and not body.hold_reason:
            raise HTTPException(422, "hold_reason wajib diisi saat On Hold")

        # 2. Hitung kolom yang ikut berubah
        arrived_at = unit["arrived_at"]
        if to == "Waiting Customer Schedule" and arrived_at is None:
            arrived_at = datetime.now(timezone.utc)   # titik awal reminder H+1..H+3

        reschedule_count = unit["reschedule_count"]
        if frm == "Scheduled" and to == "Waiting Customer Schedule":
            reschedule_count += 1

        hold_reason = body.hold_reason if to == "On Hold" else None

        scheduled_for = body.scheduled_for or unit["scheduled_for"]
        if to == "Waiting Customer Schedule":
            scheduled_for = None                       # jadwal lama batal

        # 3. Update unit + catat event
        updated = conn.execute(
            """UPDATE units
               SET current_stage = %s, arrived_at = %s, reschedule_count = %s,
                   hold_reason = %s, scheduled_for = %s
               WHERE id = %s RETURNING *""",
            (to, arrived_at, reschedule_count, hold_reason, scheduled_for, unit["id"]),
        ).fetchone()
        conn.execute(
            "INSERT INTO status_events (unit_id, from_stage, to_stage, note) VALUES (%s, %s, %s, %s)",
            (unit["id"], frm, to, body.note),
        )
    return updated


@app.get("/metrics")
def get_metrics():
    with get_conn() as conn:
        # 1. Internal lead time: PO -> Ready to Delivery
        internal_sql = """
            SELECT ROUND(AVG(EXTRACT(EPOCH FROM (rtd.changed_at - po.changed_at)) / 86400)::numeric, 2) AS avg_internal_days
            FROM status_events po
            JOIN status_events rtd ON po.unit_id = rtd.unit_id
            WHERE po.to_stage = 'PO' AND rtd.to_stage = 'Ready to Delivery'
        """
        internal_res = conn.execute(internal_sql).fetchone()

        # 2. Transit time per ekspedisi (In Transit -> Waiting Customer Schedule)
        transit_sql = """
            SELECT 
                c.name AS courier_name,
                c.max_ready_days,
                COUNT(DISTINCT u.id) AS total_units_shipped,
                ROUND(AVG(EXTRACT(EPOCH FROM (arr.changed_at - ship.changed_at)) / 86400)::numeric, 2) AS avg_transit_days
            FROM units u
            JOIN couriers c ON u.courier_id = c.id
            JOIN status_events ship ON ship.unit_id = u.id AND ship.to_stage = 'In Transit'
            JOIN status_events arr ON arr.unit_id = u.id AND arr.to_stage = 'Waiting Customer Schedule'
            GROUP BY c.id, c.name, c.max_ready_days
            ORDER BY c.id ASC
        """
        transit_res = conn.execute(transit_sql).fetchall()

        # 3. Customer response time: arrived_at -> Scheduled pertama
        cust_response_sql = """
            WITH first_schedule AS (
                SELECT unit_id, MIN(changed_at) AS first_scheduled_at
                FROM status_events
                WHERE to_stage = 'Scheduled'
                GROUP BY unit_id
            )
            SELECT ROUND(AVG(EXTRACT(EPOCH FROM (fs.first_scheduled_at - u.arrived_at)) / 86400)::numeric, 2) AS avg_response_days
            FROM units u
            JOIN first_schedule fs ON u.id = fs.unit_id
            WHERE u.arrived_at IS NOT NULL
        """
        cust_res = conn.execute(cust_response_sql).fetchone()

        # 4. Total dan distribusi reschedule
        reschedule_sql = """
            SELECT 
                SUM(reschedule_count) AS total_reschedules,
                COUNT(*) FILTER (WHERE reschedule_count > 0) AS units_with_reschedule
            FROM units
        """
        resched_res = conn.execute(reschedule_sql).fetchone()

    return {
        "internal_lead_time_days": float(internal_res["avg_internal_days"]) if internal_res and internal_res["avg_internal_days"] is not None else 0.0,
        "transit_time_by_courier": transit_res,
        "customer_response_time_days": float(cust_res["avg_response_days"]) if cust_res and cust_res["avg_response_days"] is not None else 0.0,
        "reschedule_metrics": resched_res
    }
