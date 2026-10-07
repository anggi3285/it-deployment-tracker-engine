import os
import random
from datetime import datetime, timedelta, timezone
import psycopg2
from psycopg2.extras import execute_values

from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("POSTGRES_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("POSTGRES_PORT", 5432))
DB_NAME = os.getenv("POSTGRES_DB", "maindb")
DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "postgres")

def get_conn():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )

def generate_dummy_data():
    conn = get_conn()
    cur = conn.cursor()

    now = datetime.now(timezone.utc)
    random.seed(42)  # reproducible seed

    print("[*] 1. Menyiapkan master data...")
    # Master Couriers
    couriers_data = [
        ("CKB Logistics", 3),
        ("JNE Express Cargo", 4),
        ("Internal MST Fleet", 2)
    ]
    cur.execute("TRUNCATE TABLE status_events, notifications, units, customers, account_managers, couriers RESTART IDENTITY CASCADE;")
    
    cur.executemany("INSERT INTO couriers (name, max_ready_days) VALUES (%s, %s) RETURNING id;", couriers_data)
    cur.execute("SELECT id, name, max_ready_days FROM couriers;")
    couriers = cur.fetchall()

    # Master Account Managers
    am_data = [
        ("Budi Santoso", "-1001234567891"),
        ("Dewi Lestari", "-1001234567892"),
        ("Rian Hidayat", "-1001234567893")
    ]
    cur.executemany("INSERT INTO account_managers (name, telegram_chat_id) VALUES (%s, %s);", am_data)
    cur.execute("SELECT id FROM account_managers;")
    am_ids = [r[0] for r in cur.fetchall()]

    # Master Customers (10 perusahaan)
    customers_data = [
        ("PT Adaro Energy Tbk", random.choice(am_ids)),
        ("PT Bukit Asam Tbk", random.choice(am_ids)),
        ("PT Petrosea Tbk", random.choice(am_ids)),
        ("PT United Tractors Tbk", random.choice(am_ids)),
        ("PT Vale Indonesia", random.choice(am_ids)),
        ("PT Astra Agro Lestari", random.choice(am_ids)),
        ("PT Berau Coal Energy", random.choice(am_ids)),
        ("PT Indika Energy", random.choice(am_ids)),
        ("PT Pamapersada Nusantara", random.choice(am_ids)),
        ("PT Kalbe Farma Tbk", random.choice(am_ids))
    ]
    cur.executemany("INSERT INTO customers (name, account_manager_id) VALUES (%s, %s);", customers_data)
    cur.execute("SELECT id FROM customers;")
    cust_ids = [r[0] for r in cur.fetchall()]

    print("[*] 2. Menghasilkan 150 unit dengan simulasi lifecyle riil...")
    # Stage order & valid targets
    # 'PO' -> 'Staging' -> 'QC' -> 'Ready to Delivery' -> 'In Transit' -> 'Waiting Customer Schedule' -> 'Scheduled' -> 'Deployed'
    
    unit_rows = []
    events_rows = []
    notifications_rows = []

    for i in range(1, 151):
        sn = f"MST-{now.year % 100}{random.randint(10,99)}-{random.randint(100000, 999999)}"
        c_id = random.choice(cust_ids)
        cour = random.choice(couriers)
        cour_id = cour[0]

        # Tanggal PO dalam 60 hari terakhir
        days_ago = random.uniform(1.0, 58.0)
        po_time = now - timedelta(days=days_ago)

        current_time = po_time
        current_stage = "PO"
        
        arrived_at = None
        scheduled_for = None
        hold_reason = None
        reschedule_count = 0

        # Initial event PO
        unit_events = [(current_stage, current_time, "Unit PO diterbitkan")]

        # Kasus probabilistik
        is_slow_internal = (random.random() < 0.15)  # 15% telat internal
        is_fail_qc = (random.random() < 0.10)        # 10% gagal QC
        is_slow_customer = (random.random() < 0.25)  # 25% lambat schedule
        is_reschedule = (random.random() < 0.15)     # 15% reschedule
        is_hold = (random.random() < 0.08)           # 8% hold

        # Tentukan titik berhenti simulasi unit (biar ada di berbagai status saat ini)
        # Distribusi stop stage realistis
        stop_stage_choices = [
            'PO', 'Staging', 'QC', 'Ready to Delivery', 
            'In Transit', 'Waiting Customer Schedule', 'Scheduled', 
            'On Hold', 'Deployed'
        ]
        stop_stage_weights = [0.05, 0.08, 0.08, 0.09, 0.12, 0.18, 0.12, 0.06, 0.22]
        target_stop_stage = random.choices(stop_stage_choices, weights=stop_stage_weights)[0]

        # Simulasikan langkah per tahap
        # 1. PO -> Staging
        if target_stop_stage != 'PO':
            dur = random.uniform(2.0, 3.5) if is_slow_internal else random.uniform(0.3, 0.9)
            next_time = current_time + timedelta(days=dur)
            if next_time < now:
                current_time = next_time
                current_stage = 'Staging'
                unit_events.append((current_stage, current_time, "Unit masuk gudang staging"))

        # 2. Staging -> QC
        if current_stage == 'Staging' and target_stop_stage not in ['PO', 'Staging']:
            dur = random.uniform(1.8, 2.8) if is_slow_internal else random.uniform(0.2, 0.8)
            next_time = current_time + timedelta(days=dur)
            if next_time < now:
                current_time = next_time
                current_stage = 'QC'
                unit_events.append((current_stage, current_time, "Unit masuk tahap Quality Control"))

        # 2b. Simulasi QC Gagal (QC -> Staging -> QC)
        if current_stage == 'QC' and is_fail_qc and target_stop_stage not in ['PO', 'Staging']:
            fail_time = current_time + timedelta(hours=random.uniform(4, 12))
            if fail_time < now:
                unit_events.append(('Staging', fail_time, "QC Gagal: Masalah driver OS/Hardware, rework staging"))
                rework_time = fail_time + timedelta(days=random.uniform(0.8, 1.8))
                if rework_time < now:
                    current_time = rework_time
                    current_stage = 'QC'
                    unit_events.append(('QC', current_time, "QC Ulang setelah rework"))

        # 3. QC -> Ready to Delivery
        if current_stage == 'QC' and target_stop_stage not in ['PO', 'Staging', 'QC']:
            dur = random.uniform(0.3, 0.9)
            next_time = current_time + timedelta(days=dur)
            if next_time < now:
                current_time = next_time
                current_stage = 'Ready to Delivery'
                unit_events.append((current_stage, current_time, "Lolos QC, packing selesai, menunggu pickup ekspedisi"))

        # 4. Ready to Delivery -> In Transit
        if current_stage == 'Ready to Delivery' and target_stop_stage not in ['PO', 'Staging', 'QC', 'Ready to Delivery']:
            dur = random.uniform(1.0, 4.5)
            next_time = current_time + timedelta(days=dur)
            if next_time < now:
                current_time = next_time
                current_stage = 'In Transit'
                unit_events.append((current_stage, current_time, f"Pickup oleh {cour[1]}, dalam perjalanan"))

        # 5. In Transit -> Waiting Customer Schedule
        if current_stage == 'In Transit' and target_stop_stage not in ['PO', 'Staging', 'QC', 'Ready to Delivery', 'In Transit']:
            dur = random.uniform(1.0, 5.0)
            next_time = current_time + timedelta(days=dur)
            if next_time < now:
                current_time = next_time
                current_stage = 'Waiting Customer Schedule'
                arrived_at = current_time
                unit_events.append((current_stage, current_time, "Unit diterima di lokasi site customer. Menunggu jadwal konfirmasi user"))

        # 6. Waiting Customer Schedule -> Scheduled / On Hold
        if current_stage == 'Waiting Customer Schedule' and target_stop_stage not in ['PO', 'Staging', 'QC', 'Ready to Delivery', 'In Transit', 'Waiting Customer Schedule']:
            dur = random.uniform(4.0, 8.0) if is_slow_customer else random.uniform(0.5, 2.5)
            next_time = current_time + timedelta(days=dur)
            if next_time < now:
                current_time = next_time
                
                # Cek jika customer reschedule
                if is_reschedule:
                    reschedule_count += 1
                    current_stage = 'Scheduled'
                    scheduled_for = current_time + timedelta(days=random.randint(1, 4))
                    unit_events.append(('Scheduled', current_time, f"Jadwal deployment disepakati: {scheduled_for.strftime('%Y-%m-%d')}"))
                    
                    # Balik ke Waiting Customer Schedule
                    resched_time = current_time + timedelta(days=1)
                    if resched_time < now:
                        unit_events.append(('Waiting Customer Schedule', resched_time, "User berhalangan / izin dinas luar. Meminta reschedule."))
                        current_time = resched_time
                        current_stage = 'Waiting Customer Schedule'

                # Cek jika ditargetkan On Hold
                if is_hold or target_stop_stage == 'On Hold':
                    if current_stage != 'Scheduled':
                        current_stage = 'Scheduled'
                        scheduled_for = current_time + timedelta(days=1)
                        unit_events.append(('Scheduled', current_time, "Jadwal sementara"))
                    
                    hold_time = current_time + timedelta(hours=random.uniform(6, 24))
                    if hold_time < now:
                        current_time = hold_time
                        current_stage = 'On Hold'
                        hold_reason = random.choice(["Renovasi ruang IT kantor cabang", "User penerima cuti melahirkan", "Menunggu migrasi domain kantor"])
                        unit_events.append(('On Hold', current_time, f"On Hold: {hold_reason}"))
                else:
                    current_stage = 'Scheduled'
                    scheduled_for = current_time + timedelta(days=random.randint(1, 3))
                    unit_events.append(('Scheduled', current_time, f"Jadwal instalasi disetujui: {scheduled_for.strftime('%Y-%m-%d')}"))

        # 7. Scheduled -> Deployed
        if current_stage == 'Scheduled' and target_stop_stage == 'Deployed':
            dur = random.uniform(1.0, 3.0)
            next_time = current_time + timedelta(days=dur)
            if next_time < now:
                current_time = next_time
                current_stage = 'Deployed'
                unit_events.append(('Deployed', current_time, "Teknisi selesai instalasi onsite. BAST ditandatangani."))

        # Pastikan event terakhir identik dengan current_stage (Aturan Emas)
        last_event_stage = unit_events[-1][0]
        current_stage = last_event_stage

        unit_rows.append((
            i, sn, c_id, cour_id, current_stage, po_time, arrived_at, scheduled_for, hold_reason, reschedule_count, po_time
        ))

        # Buat format status_events
        prev_st = None
        for st, t_event, note in unit_events:
            events_rows.append((i, prev_st, st, t_event, note))
            prev_st = st

    print(f"[*] 3. Menyimpan {len(unit_rows)} unit dan {len(events_rows)} status_events ke PostgreSQL...")
    
    # Insert Units
    units_query = """
    INSERT INTO units (
        id, serial_number, customer_id, courier_id, current_stage, 
        po_at, arrived_at, scheduled_for, hold_reason, reschedule_count, created_at
    ) VALUES %s;
    """
    execute_values(cur, units_query, unit_rows)

    # Insert Status Events
    events_query = """
    INSERT INTO status_events (unit_id, from_stage, to_stage, changed_at, note)
    VALUES %s;
    """
    execute_values(cur, events_query, events_rows)

    # Generate sampel notifikasi reminder berdasarkan arrived_at
    notif_data = []
    for u in unit_rows:
        u_id = u[0]
        u_stage = u[4]
        u_arrived = u[6]
        if u_arrived and u_stage in ['Waiting Customer Schedule', 'Scheduled', 'Deployed']:
            # Cek delta hari
            diff_days = (now - u_arrived).days
            if diff_days >= 1:
                notif_data.append((u_id, 'reminder_h1', 'customer_pic@company.com', u_arrived + timedelta(days=1, hours=1)))
            if diff_days >= 2:
                notif_data.append((u_id, 'reminder_h2', 'customer_pic@company.com', u_arrived + timedelta(days=2, hours=1)))
            if diff_days >= 3:
                notif_data.append((u_id, 'reminder_h3', 'customer_pic@company.com', u_arrived + timedelta(days=3, hours=1)))
            if diff_days >= 4:
                notif_data.append((u_id, 'escalation_1', 'am_telegram_alert', u_arrived + timedelta(days=4, hours=2)))

    if notif_data:
        notif_query = "INSERT INTO notifications (unit_id, type, sent_to, sent_at) VALUES %s;"
        execute_values(cur, notif_query, notif_data)

    # Update sequence ID
    cur.execute("SELECT setval('units_id_seq', (SELECT MAX(id) FROM units));")
    cur.execute("SELECT setval('status_events_id_seq', (SELECT MAX(id) FROM status_events));")
    cur.execute("SELECT setval('notifications_id_seq', (SELECT COALESCE(MAX(id), 1) FROM notifications));")

    conn.commit()
    cur.close()
    conn.close()
    print("[+] Selesai! Data dummy sukses dimuat.")

if __name__ == '__main__':
    generate_dummy_data()
