"""Generate the four canonical Draft 2020-12 contracts from the runtime models."""

import argparse
import json
from pathlib import Path

from app.schemas.evaluator import EvaluatorRequest, EvaluatorResponse
from app.schemas.interviewer import InterviewerRequest, InterviewerResponse
from app.schemas.meta import EvaluatorErrorResponse, InterviewerErrorResponse

DRAFT = "https://json-schema.org/draft/2020-12/schema"
ROOT = Path(__file__).resolve().parents[1]


def input_schema(model):
    return {"$schema": DRAFT, **model.model_json_schema(mode="validation")}


def output_schema(success, error):
    definitions = {}
    branches = []
    for model, name in (
        (success, success.__name__),
        (error, success.__name__.removesuffix("Response") + "ErrorResponse"),
    ):
        schema = model.model_json_schema(mode="validation")
        definitions.update(schema.pop("$defs", {}))
        definitions[name] = schema
        branches.append({"$ref": f"#/$defs/{name}"})
    return {
        "$schema": DRAFT,
        "title": f"{success.__name__} HTTP contract v2",
        "oneOf": branches,
        "$defs": definitions,
    }


def schemas():
    return {
        "interviewer_input.schema.json": input_schema(InterviewerRequest),
        "interviewer_output.schema.json": output_schema(
            InterviewerResponse, InterviewerErrorResponse
        ),
        "evaluator_input.schema.json": input_schema(EvaluatorRequest),
        "evaluator_output.schema.json": output_schema(EvaluatorResponse, EvaluatorErrorResponse),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if committed contracts differ")
    args = parser.parse_args()
    for name, schema in schemas().items():
        path = ROOT / name
        if args.check:
            if not path.is_file() or json.loads(path.read_text()) != schema:
                raise SystemExit(f"Contract out of date: {name}; run python -m app.contracts")
        else:
            path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
    print("Four contract schemas are up to date")


if __name__ == "__main__":
    main()
