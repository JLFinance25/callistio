"""PRISM tracing (required by the rules). One shared handler for the whole app.

Needs three values in .env (names from PRISM docs, blockconvey.com/docs):
  PRISMTRACE_HOST, PRISMTRACE_PROJECT_ID, PRISMTRACE_API_KEY
If they are missing, the app still runs, just without traces (and says so once).
"""
import os
import warnings

from dotenv import load_dotenv

load_dotenv()

handler = None
_needed = ("PRISMTRACE_HOST", "PRISMTRACE_PROJECT_ID", "PRISMTRACE_API_KEY")

if all(os.environ.get(k) for k in _needed):
    from prismtrace import FailMode, PRISMtraceCallbackHandler

    # Build once, share everywhere (PRISM docs: one handler per app, not per request).
    handler = PRISMtraceCallbackHandler(
        api_key=os.environ["PRISMTRACE_API_KEY"],
        project_id=os.environ["PRISMTRACE_PROJECT_ID"],
        host=os.environ["PRISMTRACE_HOST"],
        agent_name="what-changed",
        fail_mode=FailMode.OPEN,  # if PRISM is unreachable, the app keeps working
    )
else:
    missing = [k for k in _needed if not os.environ.get(k)]
    warnings.warn(f"PRISM tracing OFF: missing {', '.join(missing)} in .env")


def callbacks() -> list:
    """Pass as callbacks=... to every LangChain model and tool call."""
    return [handler] if handler else []


def session(session_id: str):
    """Group one user request's steps into one PRISM session: `with session(id): ...`"""
    import contextlib

    if handler is None:
        return contextlib.nullcontext()
    import prismtrace

    return prismtrace.session(session_id)


def close():
    if handler is not None:
        handler.close()
