"""ASGI entry point: FastAPI with Socket.IO mounted (spec 13.2)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import all_routers
from app.config import settings
from app.core.checks import run_startup_checks
from app.sockets import handlers  # noqa: F401  (modules register themselves)
from app.sockets.manager import sio


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    run_startup_checks()
    yield


app = FastAPI(title=settings.APP_NAME, version=settings.VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router in all_routers():
    app.include_router(_router, prefix="/api/v1")

application = socketio.ASGIApp(
    socketio_server=sio, other_asgi_app=app, socketio_path="socket.io"
)
