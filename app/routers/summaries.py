from datetime import datetime, timezone
from fastapi import APIRouter
from app.database import get_conn
from app.routers.metrics import get_metrics

router = APIRouter(tags=["Summaries & Reports"])


@router.get("/units/summary/morning")
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


@router.get("/units/summary/weekly")
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
