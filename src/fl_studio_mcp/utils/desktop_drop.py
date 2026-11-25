"""Isolated OLE worker. Never starts, stops or saves FL Studio."""

import json
import sys
import ctypes
import os
import time

# OLE creates its tracking HWND during OleInitialize. It must inherit physical
# DPI awareness from process startup, rather than a later thread-only override.
if os.name == "nt":
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

def main():
    def trace(phase, **details):
        print(json.dumps({"phase": phase, "time": time.monotonic(), **details}), file=sys.stderr, flush=True)

    trace("helper_started")
    try:
        from fl_studio_mcp.utils.desktop import _drop_file_in_process
        from fl_studio_mcp.utils.file_rpc import select_instance

        trace("helper_imported")
        request = json.loads(sys.argv[1])
        select_instance(request.pop("pid"))
        trace("instance_selected")
        result = _drop_file_in_process(**request, trace=trace)
    except Exception as exc:
        result = {
            "success": False,
            "submitted": None,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "requires_native_readback": True,
        }
    print(json.dumps(result, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
