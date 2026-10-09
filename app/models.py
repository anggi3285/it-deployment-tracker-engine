from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class UnitIn(BaseModel):
    serial_number: str
    customer_id: int
    courier_id: Optional[int] = None  # Opsional saat PO/Staging


class StatusIn(BaseModel):
    model_config = {"json_schema_extra": {"example": {"to_stage": "QC"}}}
    to_stage: str
    note: Optional[str] = None
    courier_id: Optional[int] = None  # Wajib saat ke Ready to Delivery / In Transit
    hold_reason: Optional[str] = None  # Wajib saat to_stage = On Hold
    scheduled_for: Optional[datetime] = None


class NotificationLog(BaseModel):
    unit_id: int
    type: str
    sent_to: Optional[str] = None
