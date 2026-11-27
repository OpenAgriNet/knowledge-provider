"""Prepare the checked ICAR AgricultureAdvisory bundle and intake mapping."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from pipeline.intake.webhook_mapping import JsonataMapping
from pipeline.metadata.schema_packs import load_schema_pack

EXAMPLE = Path(__file__).resolve().parents[1] / "docs/examples/icar-agriculture-advisory"
MAX_ARTIFACT_BYTES = 512 * 1024


def prepare_pack(directory: Path, network_id: str) -> None:
    configuration = json.loads((EXAMPLE / "pack-sources.json").read_text(encoding="utf-8"))
    directory.mkdir(parents=True)
    for filename, source in configuration["sources"].items():
        contents = subprocess.run(
            ["curl", "-q", "-fsSL", "--max-time", "20", "--max-filesize", str(MAX_ARTIFACT_BYTES), source["url"]],
            check=True, capture_output=True,
        ).stdout
        if len(contents) > MAX_ARTIFACT_BYTES or hashlib.sha256(contents).hexdigest() != source["sha256"]:
            raise ValueError(f"Schema source content does not match the pinned artifact: {filename}")
        (directory / filename).write_bytes(contents)

    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps(configuration["manifest"], indent=2) + "\n", encoding="utf-8")
    pack = load_schema_pack(manifest)
    expression = (EXAMPLE / "icar-advisory.jsonata").read_text(encoding="utf-8")
    expression = expression.replace('"__SCHEMA_DIGEST__"', json.dumps(pack.digest))
    expression = expression.replace('"__NETWORK_ID__"', json.dumps(network_id))
    mapping = JsonataMapping(runtime="jsonata-v1", expression=expression)
    (directory / "templates.json").write_text(
        json.dumps({"icar-agriculture-advisory": mapping.model_dump()}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(pack.digest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="New directory for the checked bundle and mapping")
    parser.add_argument("--network-id", required=True, help="Schema registry namespace, independent of publication")
    args = parser.parse_args()
    if not args.network_id or len(args.network_id) > 128 or any(character.isspace() for character in args.network_id):
        parser.error("--network-id must be a nonempty identifier of at most 128 characters")
    prepare_pack(args.directory, args.network_id)


if __name__ == "__main__":
    main()
