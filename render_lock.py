"""Matplotlib is process-global: serialize live and archive rendering."""
from functools import wraps
from threading import RLock

RENDER_LOCK=RLock()


def serialized_render(function):
    @wraps(function)
    def render(*args,**kwargs):
        with RENDER_LOCK:return function(*args,**kwargs)
    return render
