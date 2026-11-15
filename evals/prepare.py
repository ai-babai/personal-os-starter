"""Assemble a text-only evaluation input using Python's standard library."""

import argparse
from pathlib import Path


EVALS = Path(__file__).resolve().parent
INSTRUCTIONS = (
    "AGENTS.md",
    "INDEX.md",
    "onboarding/README.md",
    "onboarding/MIGRATION.md",
    "workspace/INDEX.md",
)


def body(path):
    return path.read_text(encoding="utf-8").split("---", 2)[2].strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, choices=sorted(p.name for p in (EVALS / "cases").iterdir() if p.is_dir()))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    text = body(EVALS / "PROMPT.md") + "\n\n# Сценарий\n\n"
    text += body(EVALS / "cases" / args.case / "input.md")
    text += "\n\n# Инструкции стартера\n\n"
    for relative in INSTRUCTIONS:
        text += "\n## " + relative + "\n\n"
        text += (args.source / relative).read_text(encoding="utf-8") + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        output.write(text)
    print(args.output)


if __name__ == "__main__":
    main()
