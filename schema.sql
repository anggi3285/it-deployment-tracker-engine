-- Skema Proyek 1: Tracker unit PO sampai deploy (data dummy)

-- Aturan per tahap
CREATE TABLE stage_rules (
  stage          TEXT PRIMARY KEY,
  sort_order     INT NOT NULL,
  owner          TEXT NOT NULL CHECK (owner IN ('internal','ekspedisi','customer','closed')),
  max_days       INT,            -- batas hari untuk tahap internal (NULL = tanpa batas / ikut couriers)
  reminder_days  INT[],          -- hari reminder untuk tahap customer
  action         TEXT NOT NULL   -- alert_internal / alert_idle / track_only / remind_customer / none
);

-- Alur transisi yang diizinkan (dipakai API untuk validasi)
CREATE TABLE allowed_transitions (
  from_stage TEXT REFERENCES stage_rules(stage),
  to_stage   TEXT REFERENCES stage_rules(stage),
  PRIMARY KEY (from_stage, to_stage)
);

CREATE TABLE couriers (
  id             SERIAL PRIMARY KEY,
  name           TEXT NOT NULL UNIQUE,
  max_ready_days INT NOT NULL DEFAULT 4   -- batas Ready to Delivery per ekspedisi
);

CREATE TABLE account_managers (
  id               SERIAL PRIMARY KEY,
  name             TEXT NOT NULL,
  telegram_chat_id TEXT
);

CREATE TABLE customers (
  id                 SERIAL PRIMARY KEY,
  name               TEXT NOT NULL,
  account_manager_id INT REFERENCES account_managers(id)
);

CREATE TABLE units (
  id               SERIAL PRIMARY KEY,
  serial_number    TEXT NOT NULL UNIQUE,
  customer_id      INT REFERENCES customers(id),
  courier_id       INT REFERENCES couriers(id),
  current_stage    TEXT NOT NULL REFERENCES stage_rules(stage) DEFAULT 'PO',
  po_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  arrived_at       TIMESTAMPTZ,            -- unit sampai di customer, titik awal reminder
  scheduled_for    TIMESTAMPTZ,
  hold_reason      TEXT,
  reschedule_count INT NOT NULL DEFAULT 0,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Setiap perubahan status dicatat di sini (sumber semua metrik durasi)
CREATE TABLE status_events (
  id         BIGSERIAL PRIMARY KEY,
  unit_id    INT NOT NULL REFERENCES units(id) ON DELETE CASCADE,
  from_stage TEXT REFERENCES stage_rules(stage),
  to_stage   TEXT NOT NULL REFERENCES stage_rules(stage),
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  note       TEXT
);

-- Log reminder dan eskalasi (mencegah kirim ganda)
CREATE TABLE notifications (
  id      BIGSERIAL PRIMARY KEY,
  unit_id INT NOT NULL REFERENCES units(id) ON DELETE CASCADE,
  type    TEXT NOT NULL,   -- reminder_h1 / reminder_h2 / reminder_h3 / escalation_1 / escalation_2 ...
  sent_to TEXT,
  sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_units_stage       ON units(current_stage);
CREATE INDEX idx_events_unit_time  ON status_events(unit_id, changed_at);
CREATE INDEX idx_notif_unit_type   ON notifications(unit_id, type);

-- Seed: aturan tahap (angka contoh, ubah sesuka lu)
INSERT INTO stage_rules (stage, sort_order, owner, max_days, reminder_days, action) VALUES
('PO',                        1, 'internal',  1,    NULL,      'alert_internal'),
('Staging',                   2, 'internal',  1,    NULL,      'alert_internal'),
('QC',                        3, 'internal',  1,    NULL,      'alert_internal'),
('Ready to Delivery',         4, 'internal',  NULL, NULL,      'alert_idle'),       -- batas dari couriers.max_ready_days
('In Transit',                5, 'ekspedisi', NULL, NULL,      'track_only'),
('Waiting Customer Schedule', 6, 'customer',  NULL, '{1,2,3}', 'remind_customer'),  -- eskalasi AM di H+4
('Scheduled',                 7, 'customer',  NULL, NULL,      'track_only'),
('On Hold',                   8, 'customer',  NULL, NULL,      'track_only'),
('Deployed',                  9, 'closed',    NULL, NULL,      'none');

-- Seed: transisi yang diizinkan
INSERT INTO allowed_transitions (from_stage, to_stage) VALUES
('PO','Staging'),
('Staging','QC'),
('QC','Ready to Delivery'),
('QC','Staging'),                              -- QC gagal
('Ready to Delivery','In Transit'),
('In Transit','Waiting Customer Schedule'),
('Waiting Customer Schedule','Scheduled'),
('Scheduled','Deployed'),
('Scheduled','Waiting Customer Schedule'),     -- reschedule
('Scheduled','On Hold'),
('On Hold','Scheduled');
