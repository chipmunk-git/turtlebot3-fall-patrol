from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import models
from app.config import MAP_DIR, SNAPSHOT_DIR, STATIC_DIR
from app.database import engine
from app.routers import events, ignore_zones, logs, map, status

SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
MAP_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    models.Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Fall Patrol API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(events.router)
app.include_router(ignore_zones.router)
app.include_router(status.router)
app.include_router(map.router)
app.include_router(logs.router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/snapshots", StaticFiles(directory=SNAPSHOT_DIR), name="snapshots")
app.mount("/maps", StaticFiles(directory=MAP_DIR), name="maps")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
