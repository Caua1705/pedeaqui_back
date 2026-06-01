from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.endpoints import admin_orders, health, menu, orders, restaurants
from src.core.config import settings


app = FastAPI(
    title=settings.APP_NAME,
    description="Backend API for PedeAqui white-label restaurant ordering.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(restaurants.router)
app.include_router(menu.router)
app.include_router(orders.router)
app.include_router(admin_orders.router)
