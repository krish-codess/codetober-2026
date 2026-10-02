"""FastAPI service (skeleton: health only; endpoints arrive in Phase 5)."""

from fastapi import FastAPI

app = FastAPI(title="GOLD STANDARD API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
