from datetime import datetime, timezone
from typing import Optional
import psycopg
from fastapi import APIRouter, HTTPException

from app.database import get_conn
from app.models import UnitIn, StatusIn

router = APIRouter(tags=["Units & Status"])


@router.get("/health")
def health_check():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@router.get("/couriers")
def list_couriers():
    with get_conn() as conn:
        return conn.execute("SELECT id, name, max_ready_days FROM couriers ORDER BY id ASC").fetchall()


@router.get("/units")
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


@router.post("/units", status_code=201)
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


@router.post("/units/{serial_number}/status")
def update_status(serial_number: str, body: StatusIn):
    with get_conn() as conn:
        unit = conn.execute(
            "SELECT * FROM units WHERE serial_number = %s FOR UPDATE", (serial_number,)
        ).fetchone()
        if not unit:
            raise HTTPException(404, "Unit tidak ditemukan")

        frm, to = unit["current_stage"], body.to_stage

        # 1. Validasi transisi lewat state machine
        allowed = conn.execute(
            "SELECT 1 FROM allowed_transitions WHERE from_stage = %s AND to_stage = %s",
            (frm, to),
        ).fetchone()
        if not allowed:
            raise HTTPException(409, f"Transisi '{frm}' -> '{to}' tidak diizinkan")

        if to == "On Hold" and not body.hold_reason:
            raise HTTPException(422, "hold_reason wajib diisi saat On Hold")

        # 2. Validasi penugasan kurir
        courier_id = body.courier_id or unit["courier_id"]
        if to in ("Ready to Delivery", "In Transit") and not courier_id:
            raise HTTPException(422, f"courier_id wajib ditentukan saat unit masuk tahap '{to}'")

        if body.courier_id:
            valid_courier = conn.execute("SELECT 1 FROM couriers WHERE id = %s", (body.courier_id,)).fetchone()
            if not valid_courier:
                raise HTTPException(422, f"courier_id '{body.courier_id}' tidak ditemukan")

        # 3. Hitung field turunan
        arrived_at = unit["arrived_at"]
        if to == "Waiting Customer Schedule" and arrived_at is None:
            arrived_at = datetime.now(timezone.utc)

        reschedule_count = unit["reschedule_count"]
        if frm == "Scheduled" and to == "Waiting Customer Schedule":
            reschedule_count += 1

        hold_reason = body.hold_reason if to == "On Hold" else None

        scheduled_for = body.scheduled_for or unit["scheduled_for"]
        if to == "Waiting Customer Schedule":
            scheduled_for = None

        # 4. Update unit & insert event audit log
        updated = conn.execute(
            """UPDATE units
               SET current_stage = %s, arrived_at = %s, reschedule_count = %s,
                   hold_reason = %s, scheduled_for = %s, courier_id = %s
               WHERE id = %s RETURNING *""",
            (to, arrived_at, reschedule_count, hold_reason, scheduled_for, courier_id, unit["id"]),
        ).fetchone()
        conn.execute(
            "INSERT INTO status_events (unit_id, from_stage, to_stage, note) VALUES (%s, %s, %s, %s)",
            (unit["id"], frm, to, body.note),
        )
    return updated
