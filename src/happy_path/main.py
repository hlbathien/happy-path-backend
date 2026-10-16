from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from happy_path.api.routes import router
from happy_path.clients.goong import GoongClient
from happy_path.config import DB_PATH
from happy_path.core.graph import CorridorRegistry

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not getattr(app.state, "registry", None) and DB_PATH.exists():
        app.state.registry = CorridorRegistry(DB_PATH)
    if not getattr(app.state, "goong", None):
        app.state.goong = GoongClient()
    yield
    if getattr(app.state, "goong", None):
        await app.state.goong.aclose()

app = FastAPI(
    title="VMAP Happy Path Autopilot API (Freeze-Spec v3.0)",
    version="3.0.0",
    description="Zero-cost Pareto-optimal road-trip synthesis backend for Vietnam (HCMC <-> Da Lat, Nha Trang, Vung Tau) with Physical Highway Interchanges, 48-Hour Schedule Validity, EV Fast-Charging, VETC Tolls & Transactional Session Mutations.",
    openapi_version="3.1.0",
    lifespan=lifespan,
)

if DB_PATH.exists():
    app.state.registry = CorridorRegistry(DB_PATH)
app.state.goong = GoongClient()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
