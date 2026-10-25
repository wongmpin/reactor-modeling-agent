from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from reactor_agent.agent.model import DeepSeekProvider, ModelUnavailable
from reactor_agent.domain.models import ReactorModelSpec
from reactor_agent.hysys.worker import HysysWorker
from reactor_agent.mcp_server import main as mcp_main
from reactor_agent.service import ReactorAgentService


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    parser = argparse.ArgumentParser(prog="reactor-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hysys-check")
    sub.add_parser("worker-once")
    sub.add_parser("worker")
    execute = sub.add_parser("run-spec")
    execute.add_argument("path")
    execute.add_argument("--test-data-confirmed", action="store_true", help="Explicitly accept the test assumptions in the supplied example file.")
    sub.add_parser("model-check")
    sub.add_parser("mcp")
    make = sub.add_parser("empty-case")
    make.add_argument("destination")
    validate = sub.add_parser("validate")
    validate.add_argument("spec_json")
    sub.add_parser("serve-ui")
    args = parser.parse_args()

    if args.command == "hysys-check":
        worker = HysysWorker()
        try:
            print(json.dumps(worker.inspect().model_dump(mode="json"), indent=2))
        finally:
            worker.stop()
    elif args.command == "worker":
        from reactor_agent.queue_runner import run_consumer
        run_consumer()
    elif args.command == "run-spec":
        if not args.test_data_confirmed:
            parser.error("Review all assumptions in the specification; use --test-data-confirmed only for reviewed test inputs.")
        service = ReactorAgentService()
        spec = ReactorModelSpec.model_validate_json(Path(args.path).read_text(encoding="utf-8"))
        task = service.create_task(spec.user_request)
        output = service.process_request(task.task_id, spec_json=spec.model_dump_json())
        print(json.dumps(output, ensure_ascii=False, indent=2), flush=True)
        if output["status"] != "spec_validated":
            sys.exit(4)
        queue_id = service.queue_execution(task.task_id)
        outcome = service.run_one_queued(queue_id)
        if outcome is None:
            print(json.dumps({"queue_id": queue_id, "message": "Background consumer owns this run; view task history in the UI."}))
        else:
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
            if outcome["status"] != "validated":
                sys.exit(5)
    elif args.command == "worker-once":
        service = ReactorAgentService()
        print(json.dumps(service.run_one_queued(), indent=2, ensure_ascii=False))
    elif args.command == "model-check":
        try:
            result = DeepSeekProvider().check_connectivity()
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if not result.get("ok"):
                sys.exit(3)
        except ModelUnavailable as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2, ensure_ascii=False))
            sys.exit(2)
    elif args.command == "mcp":
        mcp_main()
    elif args.command == "empty-case":
        worker = HysysWorker()
        try:
            print(json.dumps(worker.create_empty_case(args.destination), indent=2))
        finally:
            worker.stop()
    elif args.command == "validate":
        service = ReactorAgentService()
        print(json.dumps(service.validate_spec_json(Path(args.spec_json).read_text(encoding="utf-8")), indent=2, ensure_ascii=False))
    elif args.command == "serve-ui":
        import streamlit.web.cli
        streamlit.web.cli.main_run(["reactor_agent/ui.py", "--server.address=127.0.0.1"])


if __name__ == "__main__":
    main()
