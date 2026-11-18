"""Minimal coding agent: one Responses loop, a bash tool and a write tool.

Standard library only. It exists as a floor for comparisons: anything a full
harness costs beyond this is the harness, not the task.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

TOOLS = [
    {"type": "function", "name": "bash", "description": "Run a shell command in the working directory.",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"type": "function", "name": "read_file", "description": "Read a file.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"type": "function", "name": "write_file", "description": "Write a file (absolute or relative path).",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"]}},
]


def call_tool(name, arguments):
    try:
        args = json.loads(arguments)
        if name == "bash":
            done = subprocess.run(args["command"], shell=True, capture_output=True, text=True, timeout=120)
            return f"exit={done.returncode}\n{done.stdout[-8000:]}{done.stderr[-4000:]}"
        if name == "read_file":
            with open(args["path"]) as stream:
                return stream.read()[-16000:]
        if name == "write_file":
            path = args["path"]
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w") as stream:
                stream.write(args["content"])
            return f"wrote {len(args['content'])} bytes to {path}"
        return f"unknown tool {name}"
    except Exception as error:  # noqa: BLE001 - tool errors go back to the model
        return f"error: {type(error).__name__}: {error}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--prompt", required=True)
    # The plan's request budget is the real limit; this only stops a runaway loop (session-250 needs 251 turns).
    parser.add_argument("--max-turns", type=int, default=300)
    a = parser.parse_args()
    url = os.environ["SWARM_BASE_URL"].rstrip("/") + "/responses"
    history = [{"role": "user", "content": a.prompt}]
    for _ in range(a.max_turns):
        body = {"model": a.model, "input": history, "tools": TOOLS, "reasoning": {"effort": a.effort},
                "max_output_tokens": int(os.environ.get("SWARM_MAX_OUTPUT_TOKENS", "16384")), "stream": False}
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=300) as response:
            reply = json.loads(response.read())
        if reply.get("status") != "completed":
            print(json.dumps({"error": "incomplete_response", "status": reply.get("status")}))
            return 2
        calls = [item for item in reply.get("output", []) if item.get("type") == "function_call"]
        if not calls:
            text = "".join(part.get("text", "") for item in reply.get("output", []) if item.get("type") == "message"
                           for part in item.get("content", []))
            print(json.dumps({"final": text}))
            return 0
        for call in calls:
            history.append(call)
            history.append({"type": "function_call_output", "call_id": call["call_id"],
                            "output": call_tool(call["name"], call["arguments"])})
    print(json.dumps({"error": "max_turns"}))
    return 3


if __name__ == "__main__":
    sys.exit(main())
