"""Nicrron models, traced by PRISM. Every model call goes through here (Nicrron is required).

Nicrron is OpenAI-compatible: the OpenAI client with a different base URL and key.
LangChain's ChatOpenAI is used so PRISM's handler records each call as a step.
Keys come from .env only.
"""
import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from backend.tracing import callbacks

load_dotenv()

BASE_URL = os.environ.get("NICRRON_BASE_URL", "https://api.nicrron.ai/v1")
MODEL_FAST = os.environ.get("MODEL_FAST", "anthropic/claude-haiku-5-5")      # reading / extraction
MODEL_STRONG = os.environ.get("MODEL_STRONG", "anthropic/claude-sonnet-5-5")  # comparing / writing


def model(name: str = MODEL_FAST) -> ChatOpenAI:
    # temperature=0 lets Nicrron cache repeat answers (faster rehearsed demos)
    return ChatOpenAI(model=name, base_url=BASE_URL, api_key=os.environ["NICRRON_API_KEY"],
                      temperature=0, callbacks=callbacks())


def chat(prompt: str, model_name: str = MODEL_FAST, system: str | None = None) -> str:
    messages = ([("system", system)] if system else []) + [("human", prompt)]
    return model(model_name).invoke(messages).content
