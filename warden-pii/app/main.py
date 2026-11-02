"""warden-pii: masks personal data before any model sees an invoice (phase 7).

Stateless: it returns the masked text and the placeholder mapping, and keeps
neither. It runs on an internal network with no route to the internet."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .masking import build_analyzer, mask


class State:
    analyzer = None


state = State()


@asynccontextmanager
async def lifespan(_app):
    state.analyzer = build_analyzer()  # loads the spaCy model once
    yield


app = FastAPI(title="warden-pii", lifespan=lifespan)


class MaskRequest(BaseModel):
    text: str = Field(max_length=100_000)


@app.get("/health")
def health():
    return {"status": "ok" if state.analyzer else "loading"}


@app.post("/mask")
def mask_text(req: MaskRequest):
    return mask(state.analyzer, req.text)
