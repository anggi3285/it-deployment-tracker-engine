from fastapi import FastAPI
from app.routers import units, notifications, metrics, summaries

app = FastAPI(
    title="IT Deployment Tracker Engine",
    description="Automated orchestration layer for device lifecycle & ITSM escalation",
    version="1.0.0"
)

# Registrasi Router Modular
app.include_router(units.router)
app.include_router(notifications.router)
app.include_router(metrics.router)
app.include_router(summaries.router)
