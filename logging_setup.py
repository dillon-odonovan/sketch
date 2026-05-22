import contextvars
import logging

# Set per-interaction at the top of each slash command. Reads carry through
# `await` boundaries and `asyncio.to_thread` (Python 3.11 copies the context
# into worker threads), so logs emitted deep inside the Sheets client also
# pick up the active trace ID. Defaults to "-" outside any request so startup
# and gateway-level log lines render cleanly.
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default="-"
)


class _TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get()
        return True


def configure() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] [trace=%(trace_id)s] %(name)s: %(message)s"
    ))
    handler.addFilter(_TraceIdFilter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
