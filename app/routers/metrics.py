from fastapi import APIRouter
from app.database import get_conn

router = APIRouter(tags=["Metrics & Analytics"])


@router.get("/metrics")
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
