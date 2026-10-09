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

        # 5. Metrik Eskalasi: Persentase dieskalasi & Rata-rata hari dari eskalasi ke Scheduled
        escalation_metrics_sql = """
            WITH first_escalations AS (
                SELECT unit_id, MIN(sent_at) AS first_escalated_at
                FROM notifications
                WHERE type LIKE 'escalation%'
                GROUP BY unit_id
            ),
            resolution_events AS (
                SELECT 
                    fe.unit_id,
                    fe.first_escalated_at,
                    MIN(se.changed_at) AS scheduled_after_escalation
                FROM first_escalations fe
                JOIN status_events se 
                  ON fe.unit_id = se.unit_id 
                 AND se.to_stage = 'Scheduled' 
                 AND se.changed_at >= fe.first_escalated_at
                GROUP BY fe.unit_id, fe.first_escalated_at
            )
            SELECT 
                (SELECT COUNT(DISTINCT unit_id) FROM first_escalations) AS total_escalated_units,
                (SELECT COUNT(*) FROM units) AS total_units,
                ROUND(
                    ((SELECT COUNT(DISTINCT unit_id) FROM first_escalations)::numeric / 
                     NULLIF((SELECT COUNT(*) FROM units), 0) * 100), 1
                )::float AS escalation_rate_pct,
                ROUND(
                    AVG(EXTRACT(EPOCH FROM (re.scheduled_after_escalation - re.first_escalated_at)) / 86400)::numeric, 1
                )::float AS avg_days_to_schedule_after_escalation
            FROM resolution_events re;
        """
        esc_res = conn.execute(escalation_metrics_sql).fetchone()

    return {
        "internal_lead_time_days": float(internal_res["avg_internal_days"]) if internal_res and internal_res["avg_internal_days"] is not None else 0.0,
        "transit_time_by_courier": transit_res,
        "customer_response_time_days": float(cust_res["avg_response_days"]) if cust_res and cust_res["avg_response_days"] is not None else 0.0,
        "reschedule_metrics": resched_res,
        "escalation_metrics": {
            "total_escalated_units": esc_res["total_escalated_units"] if esc_res else 0,
            "escalation_rate_pct": esc_res["escalation_rate_pct"] if esc_res and esc_res["escalation_rate_pct"] is not None else 0.0,
            "avg_days_to_schedule_after_escalation": esc_res["avg_days_to_schedule_after_escalation"] if esc_res and esc_res["avg_days_to_schedule_after_escalation"] is not None else 0.0
        }
    }


@router.get("/units/risks")
def get_unit_risk_scores(min_score: int = 10, limit: int = 50):
    """
    Hitung Skor Risiko Unit (0 - 100):
    - QC > 1 hari: +30 poin (Bottleneck QC internal)
    - Staging > 1 hari: +20 poin (Config delay)
    - PO > 1 hari: +15 poin (Procurement delay)
    - Ready to Delivery melebihi SLA kurir: +25 poin (Logistics delay)
    - Waiting Customer Schedule > 3 hari: +35 poin (Client stalled)
    - Setiap reschedule: +10 poin per kejadian
    - Stage On Hold: +40 poin
    """
    sql = """
        WITH latest_event AS (
            SELECT unit_id, MAX(changed_at) AS last_status_at
            FROM status_events
            GROUP BY unit_id
        ),
        scored_units AS (
            SELECT 
                u.id AS unit_id,
                u.serial_number,
                u.current_stage,
                cust.name AS customer_name,
                c.name AS courier_name,
                c.max_ready_days,
                u.reschedule_count,
                ROUND(EXTRACT(EPOCH FROM (now() - le.last_status_at)) / 86400, 1)::float AS days_in_stage,
                LEAST(100, (
                    -- Bobot stage internal
                    CASE 
                        WHEN u.current_stage = 'QC' AND le.last_status_at <= now() - INTERVAL '1 day' THEN 30
                        WHEN u.current_stage = 'Staging' AND le.last_status_at <= now() - INTERVAL '1 day' THEN 20
                        WHEN u.current_stage = 'PO' AND le.last_status_at <= now() - INTERVAL '1 day' THEN 15
                        ELSE 0
                    END +
                    -- Bobot pengiriman
                    CASE 
                        WHEN u.current_stage = 'Ready to Delivery' AND le.last_status_at <= now() - (COALESCE(c.max_ready_days, 3) || ' days')::interval THEN 25
                        ELSE 0
                    END +
                    -- Bobot customer & hold
                    CASE 
                        WHEN u.current_stage = 'Waiting Customer Schedule' AND le.last_status_at <= now() - INTERVAL '3 days' THEN 35
                        WHEN u.current_stage = 'On Hold' THEN 40
                        ELSE 0
                    END +
                    -- Bobot reschedule
                    (u.reschedule_count * 10)
                )) AS risk_score
            FROM units u
            JOIN latest_event le ON u.id = le.unit_id
            JOIN customers cust ON u.customer_id = cust.id
            LEFT JOIN couriers c ON u.courier_id = c.id
            WHERE u.current_stage != 'Deployed'
        )
        SELECT 
            unit_id,
            serial_number,
            current_stage,
            customer_name,
            courier_name,
            reschedule_count,
            days_in_stage,
            risk_score,
            CASE 
                WHEN risk_score >= 60 THEN 'CRITICAL'
                WHEN risk_score >= 35 THEN 'HIGH'
                WHEN risk_score >= 20 THEN 'MEDIUM'
                ELSE 'LOW'
            END AS risk_level
        FROM scored_units
        WHERE risk_score >= %s
        ORDER BY risk_score DESC, days_in_stage DESC
        LIMIT %s;
    """
    with get_conn() as conn:
        return conn.execute(sql, (min_score, limit)).fetchall()

