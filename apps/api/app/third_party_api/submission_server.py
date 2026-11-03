"""Private stdio MCP server. One operation, one accepted proposal, three attempts."""

import json
import sys
from pathlib import Path
from typing import Any

# Launched with -I: import only this installed application's source tree.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pydantic import ValidationError

from app.third_party_api.skill_submission import TOOLS, InvalidSubmission, correction, expand


def submit(directory: Path, args: dict[str, Any]) -> dict[str, Any]:
    def reply(message: str, error: bool) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": message}], "isError": error}

    accepted = directory / "accepted.json"
    if (directory / "unsafe").exists():
        return reply("Unsafe submission; stop.", True)
    if accepted.exists():
        return reply("Already accepted. Finish now.", False)
    for n in range(1, 4):
        try:
            attempt = (directory / f"submission-{n}.json").open("x", encoding="utf-8")
            break
        except FileExistsError:
            continue
    else:
        return reply("Submission limit reached. Finish now.", True)
    record = json.loads((directory / "input.json").read_text(encoding="utf-8"))
    with attempt:
        try:
            result = expand(record["operation"], record["data"], args)
        except (ValidationError, InvalidSubmission) as exc:
            message = correction(exc)
        except ValueError:
            (directory / "unsafe").touch()
            json.dump({"accepted": False, "error": "Unsafe submission"}, attempt)
            return reply("Unsafe submission; stop.", True)
        else:
            accepted.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            json.dump({"accepted": True}, attempt)
            return reply("Accepted. Finish now.", False)
        json.dump({"accepted": False, "error": message}, attempt)
    return reply(
        message
        + (
            ". Resubmit the complete corrected proposal."
            if n < 3
            else ". Submission limit reached. Finish now."
        ),
        True,
    )


def main() -> None:
    directory = Path(sys.argv[1])
    operation = json.loads((directory / "input.json").read_text(encoding="utf-8"))["operation"]
    name, schema = TOOLS[operation]
    for line in sys.stdin.buffer:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "serverInfo": {"name": "skill", "version": "1"},
                "capabilities": {"tools": {}},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": "Submit the whole proposal; correct errors. Three attempts.",
                        "inputSchema": schema.model_json_schema(),
                    }
                ]
            }
        elif method == "tools/call" and request["params"]["name"] == name:
            result = submit(directory, request["params"]["arguments"])
        elif method == "ping":
            result = {}
        else:
            response["error"] = {"code": -32601, "message": "Unknown method"}
            print(json.dumps(response), flush=True)
            continue
        response["result"] = result
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
