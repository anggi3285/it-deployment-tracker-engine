from fastapi import APIRouter
from app.database import get_conn
from app.models import NotificationLog

router = APIRouter(tags=["Notifications & Alerts"])


@router.get("/units/alerts/internal")
def get_internal_alerts():
    """1. PO, Staging, QC > 1 hari
       2. Ready to Delivery > couriers.max_ready_days"""
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


@router.get("/units/reminders")
def get_reminders():
    """Unit Waiting Customer Schedule umur H+1..H+3 yang belum dikirim reminder."""
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


@router.get("/units/escalations")
def get_escalations():
    """Unit >= 4 hari di Waiting Customer Schedule dengan interval ulang 3 hari."""
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


@router.post("/notifications/log", status_code=201)
def log_notification(body: NotificationLog):
    with get_conn() as conn:
        logged = conn.execute(
            """INSERT INTO notifications (unit_id, type, sent_to)
               VALUES (%s, %s, %s) RETURNING *""",
            (body.unit_id, body.type, body.sent_to),
        ).fetchone()
    return logged
