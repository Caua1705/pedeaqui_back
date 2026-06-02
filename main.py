from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.endpoints import admin_orders, health, menu, orders, restaurants
from src.core.config import settings


app = FastAPI(
    title=settings.APP_NAME,
    description="Backend API for Rapidex white-label restaurant ordering.",
    version="0.1.0",
)

origins = [
    "https://pederapidex.com",
    "https://www.pederapidex.com",

    "http://localhost:5500",
    "http://127.0.0.1:5500",

    "http://localhost:5501",
    "http://127.0.0.1:5501",

    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(restaurants.router)
app.include_router(menu.router)
app.include_router(orders.router)
app.include_router(admin_orders.router)