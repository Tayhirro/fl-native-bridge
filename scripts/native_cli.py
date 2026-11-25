"""Use the native bridge directly without an MCP client. No desktop input."""
import argparse
import json
import sys

from fl_studio_mcp.utils.file_rpc import FileRPC


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", default="native.uiSnapshot")
    parser.add_argument("--params", default="{}", help="JSON object; no code is evaluated")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    rpc = FileRPC(pid=args.pid)
    if args.status:
        result = {"instances": FileRPC.instances(), "selected": str(rpc.root), "heartbeat": rpc.heartbeat()}
    else:
        result = rpc.send(args.action, json.loads(args.params), args.timeout)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if args.status or result.get("success") else 2


if __name__ == "__main__":
    sys.exit(main())
