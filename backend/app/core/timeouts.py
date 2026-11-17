"""A hard deadline for calls into third-party clients that may hang (search, vector store).

The call runs in a daemon thread and the caller stops waiting after `seconds`; the abandoned thread cannot keep the
process alive and its late result is discarded. The caller's context (the active trace) travels with the call.
"""
import contextvars
import threading


class CallTimeout(TimeoutError):
    pass


def run_with_timeout(fn, seconds: float, what: str):
    ctx = contextvars.copy_context()
    box: dict = {}

    def target():
        try:
            box["value"] = ctx.run(fn)
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller
            box["error"] = e

    t = threading.Thread(target=target, daemon=True, name=f"deadline-{what}")
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise CallTimeout(f"{what} did not answer within {seconds:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]
