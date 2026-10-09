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
    courier_id: Optional[int] = None        # Boleh kosong saat PO/Staging


class StatusIn(BaseModel):
    model_config = {"json_schema_extra": {"example": {"to_stage": "QC"}}}
    to_stage: str
    note: Optional[str] = None
    courier_id: Optional[int] = None        # Wajib diisi saat to_stage = Ready to Delivery / In Transit
    hold_reason: Optional[str] = None       # wajib kalau to_stage = On Hold
    scheduled_for: Optional[datetime] = None


# ---------- Endpoint ----------
@app.get("/couriers")
def list_couriers():
    with get_conn() as conn:
        return conn.execute("SELECT id, name, max_ready_days FROM couriers ORDER BY id ASC").fetchall()


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


@app.get("/units/summary/morning")
def get_morning_summary():
    with get_conn() as conn:
        # 1. Total unit per tahap
        stage_counts = conn.execute("""
            SELECT sr.stage, sr.sort_order, COUNT(u.id) AS total
            FROM stage_rules sr
            LEFT JOIN units u ON sr.stage = u.current_stage
            GROUP BY sr.stage, sr.sort_order
            ORDER BY sr.sort_order ASC;
        """).fetchall()

        # 2. Unit telat internal (PO, Staging, QC > 1 hari)
        internal_overdue = conn.execute("""
            WITH latest_event AS (
                SELECT unit_id, MAX(changed_at) AS last_status_at
                FROM status_events
                GROUP BY unit_id
            )
            SELECT u.serial_number, u.current_stage,
                   ROUND(EXTRACT(EPOCH FROM (now() - le.last_status_at)) / 86400, 1)::float AS days_stuck
            FROM units u
            JOIN latest_event le ON u.id = le.unit_id
            WHERE u.current_stage IN ('PO', 'Staging', 'QC')
              AND le.last_status_at <= now() - INTERVAL '1 day'
            ORDER BY days_stuck DESC;
        """).fetchall()

        # 3. Unit idle di Ready to Delivery (melebihi max_ready_days kurir)
        idle_delivery = conn.execute("""
            WITH latest_event AS (
                SELECT unit_id, MAX(changed_at) AS last_status_at
                FROM status_events
                GROUP BY unit_id
            )
            SELECT u.serial_number, c.name AS courier_name, c.max_ready_days,
                   ROUND(EXTRACT(EPOCH FROM (now() - le.last_status_at)) / 86400, 1)::float AS days_idle
            FROM units u
            JOIN latest_event le ON u.id = le.unit_id
            LEFT JOIN couriers c ON u.courier_id = c.id
            WHERE u.current_stage = 'Ready to Delivery'
              AND le.last_status_at <= now() - (COALESCE(c.max_ready_days, 3) || ' days')::interval
            ORDER BY days_idle DESC;
        """).fetchall()

        # 4. Total aktif dan deployed
        total_units = conn.execute("SELECT COUNT(*) AS count FROM units;").fetchone()["count"]

    return {
        "total_units": total_units,
        "stages": stage_counts,
        "internal_overdue": internal_overdue,
        "idle_delivery": idle_delivery,
    }


@app.get("/units/alerts/internal")
def get_internal_alerts():
    # 1. PO, Staging, QC > 1 hari
    # 2. Ready to Delivery > couriers.max_ready_days
    sql = """
        WITH latest_event AS (
            SELECT unit_id, MAX(changed_at) AS last_status_at
            FROM status_events
            GROUP BY unit_id
        )
        SELECT 
            u.id AS unit_id,
            u.serial_number,
            u.current_stage,
            c.name AS courier_name,
            c.max_ready_days,
            ROUND(EXTRACT(EPOCH FROM (now() - le.last_status_at)) / 86400, 1)::float AS days_in_stage,
            CASE 
                WHEN u.current_stage IN ('PO', 'Staging', 'QC') THEN 1
                WHEN u.current_stage = 'Ready to Delivery' THEN COALESCE(c.max_ready_days, 3)
            END AS max_allowed_days
        FROM units u
        JOIN latest_event le ON u.id = le.unit_id
        LEFT JOIN couriers c ON u.courier_id = c.id
        WHERE 
            (u.current_stage IN ('PO', 'Staging', 'QC') AND le.last_status_at <= now() - INTERVAL '1 day')
            OR
            (u.current_stage = 'Ready to Delivery' AND le.last_status_at <= now() - (COALESCE(c.max_ready_days, 3) || ' days')::interval)
        ORDER BY days_in_stage DESC;
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


@app.get("/units/reminders")
def get_reminders():
    # Ambil unit Waiting Customer Schedule umur H+1, H+2, H+3
    # yang BELUM pernah dikirimi reminder pada tipe hari tersebut
    sql = """
        WITH waiting_units AS (
            SELECT 
                u.id AS unit_id,
                u.serial_number,
                u.customer_id,
                c.name AS customer_name,
                u.arrived_at,
                FLOOR(EXTRACT(EPOCH FROM (now() - u.arrived_at)) / 86400)::int AS days_waiting
            FROM units u
            JOIN customers c ON u.customer_id = c.id
            WHERE u.current_stage = 'Waiting Customer Schedule'
              AND u.arrived_at IS NOT NULL
        )
        SELECT 
            w.unit_id,
            w.serial_number,
            w.customer_id,
            w.customer_name,
            w.days_waiting,
            'reminder_h' || w.days_waiting AS notif_type
        FROM waiting_units w
        LEFT JOIN notifications n 
          ON w.unit_id = n.unit_id 
         AND n.type = ('reminder_h' || w.days_waiting)
        WHERE w.days_waiting BETWEEN 1 AND 3
          AND n.id IS NULL
        ORDER BY w.days_waiting ASC, w.unit_id ASC;
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


@app.get("/units/escalations")
def get_escalations():
    # Ambil unit >= 4 hari di Waiting Customer Schedule
    # Aturan: H+4 eskalasi pertama, ulangi tiap 3 hari jika belum ada jadwal
    sql = """
        WITH latest_escalation AS (
            SELECT unit_id, MAX(sent_at) AS last_sent_at
            FROM notifications
            WHERE type LIKE 'escalation%'
            GROUP BY unit_id
        )
        SELECT 
            u.id AS unit_id,
            u.serial_number,
            c.name AS customer_name,
            am.name AS am_name,
            COALESCE(am.telegram_chat_id, '7704084297') AS am_telegram_id,
            FLOOR(EXTRACT(EPOCH FROM (now() - u.arrived_at)) / 86400)::int AS days_waiting,
            le.last_sent_at,
            CASE 
                WHEN le.last_sent_at IS NULL THEN 'escalation_1'
                ELSE 'escalation_repeat'
            END AS notif_type
        FROM units u
        JOIN customers c ON u.customer_id = c.id
        JOIN account_managers am ON c.account_manager_id = am.id
        LEFT JOIN latest_escalation le ON u.id = le.unit_id
        WHERE u.current_stage = 'Waiting Customer Schedule'
          AND u.arrived_at <= now() - INTERVAL '4 days'
          AND (
              le.last_sent_at IS NULL 
              OR le.last_sent_at <= now() - INTERVAL '3 days'
          )
        ORDER BY days_waiting DESC;
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


class NotificationLog(BaseModel):
    unit_id: int
    type: str
    sent_to: Optional[str] = None


@app.post("/notifications/log", status_code=201)
def log_notification(body: NotificationLog):
    with get_conn() as conn:
        logged = conn.execute(
            """INSERT INTO notifications (unit_id, type, sent_to)
               VALUES (%s, %s, %s) RETURNING *""",
            (body.unit_id, body.type, body.sent_to),
        ).fetchone()
    return logged


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

        # Validasi Kurir saat masuk Ready to Delivery atau In Transit
        courier_id = body.courier_id or unit["courier_id"]
        if to in ("Ready to Delivery", "In Transit") and not courier_id:
            raise HTTPException(422, f"courier_id wajib ditentukan saat unit masuk tahap '{to}'")

        if body.courier_id:
            # Validasi ID kurir ada di DB
            valid_courier = conn.execute("SELECT 1 FROM couriers WHERE id = %s", (body.courier_id,)).fetchone()
            if not valid_courier:
                raise HTTPException(422, f"courier_id '{body.courier_id}' tidak ditemukan")

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
                   hold_reason = %s, scheduled_for = %s, courier_id = %s
               WHERE id = %s RETURNING *""",
            (to, arrived_at, reschedule_count, hold_reason, scheduled_for, courier_id, unit["id"]),
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


@app.get("/units/summary/weekly")
def get_weekly_summary():
    with get_conn() as conn:
        # 1. Total unit & breakdown status
        stages = conn.execute("""
            SELECT current_stage, COUNT(*) AS count
            FROM units
            GROUP BY current_stage
            ORDER BY count DESC
        """).fetchall()

        total_units = conn.execute("SELECT COUNT(*) AS total FROM units").fetchone()["total"]
        deployed_units = conn.execute("SELECT COUNT(*) AS count FROM units WHERE current_stage = 'Deployed'").fetchone()["count"]
        on_hold_units = conn.execute("SELECT COUNT(*) AS count FROM units WHERE current_stage = 'On Hold'").fetchone()["count"]
        in_progress = total_units - (deployed_units + on_hold_units)

        # 2. Lead times & metrics
        metrics = get_metrics()

        # 3. Ekspedisi performance
        couriers = conn.execute("""
            SELECT 
                c.name,
                c.max_ready_days,
                COUNT(u.id) FILTER (WHERE u.current_stage = 'Ready to Delivery') AS pending_pickup,
                COUNT(u.id) FILTER (WHERE u.current_stage = 'In Transit') AS in_transit,
                COUNT(u.id) FILTER (WHERE u.current_stage = 'Deployed') AS delivered
            FROM couriers c
            LEFT JOIN units u ON u.courier_id = c.id
            GROUP BY c.id, c.name, c.max_ready_days
            ORDER BY c.name ASC
        """).fetchall()

        # 4. Top bottleneck customers (paling lama di Waiting Customer Schedule)
        stuck_customers = conn.execute("""
            SELECT 
                cust.name AS customer_name,
                am.name AS am_name,
                COUNT(u.id) AS waiting_units,
                ROUND(AVG(EXTRACT(EPOCH FROM (NOW() - u.arrived_at)) / 86400)::numeric, 1) AS avg_waiting_days
            FROM units u
            JOIN customers cust ON u.customer_id = cust.id
            JOIN account_managers am ON cust.account_manager_id = am.id
            WHERE u.current_stage = 'Waiting Customer Schedule' AND u.arrived_at IS NOT NULL
            GROUP BY cust.id, cust.name, am.name
            ORDER BY waiting_units DESC, avg_waiting_days DESC
            LIMIT 5
        """).fetchall()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "total_units": total_units,
            "deployed": deployed_units,
            "in_progress": in_progress,
            "on_hold": on_hold_units,
            "deployment_rate_pct": round((deployed_units / total_units * 100), 1) if total_units else 0
        },
        "stage_distribution": {s["current_stage"]: s["count"] for s in stages},
        "kpi_lead_times": {
            "internal_lead_days": metrics["internal_lead_time_days"],
            "customer_response_days": metrics["customer_response_time_days"],
            "total_reschedules": metrics["reschedule_metrics"]["total_reschedules"] or 0
        },
        "courier_performance": couriers,
        "bottlenecks": stuck_customers
    }

