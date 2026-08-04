from fastapi import Depends, FastAPI

from app.auth import AuthenticatedUser, get_current_user
from app.db import engine

app = FastAPI(
    title="Employee Directory API",
    description="Internal employee directory with permission-filtered natural-language search.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict:
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "unreachable"
    return {"status": "ok" if db_status == "ok" else "degraded", "database": db_status}


@app.get("/auth/whoami", response_model=AuthenticatedUser)
def whoami(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    return user
