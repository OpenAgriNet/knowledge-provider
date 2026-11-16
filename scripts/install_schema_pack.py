"""Check an approved local schema bundle and optionally register its digest."""

import argparse
from pathlib import Path

from pipeline.metadata.schema_packs import install_schema_pack, load_schema_pack


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--install-for-network", metavar="NETWORK_ID")
    parser.add_argument("--verify-registry", action="store_true")
    args = parser.parse_args()
    if args.verify_registry and not args.install_for_network:
        parser.error("--verify-registry requires --install-for-network")
    pack = load_schema_pack(args.manifest)
    if args.verify_registry:
        from pipeline.network.network_registry import registry_for_network

        registration = registry_for_network(args.install_for_network).active_schema(pack.capability)
        if (registration.version, registration.artifact_url) != (pack.version, pack.entrypoint):
            parser.error("The approved bundle does not match the active Registry registration")
    if args.install_for_network:
        from pipeline.db import get_connection

        with get_connection() as conn:
            install_schema_pack(conn, args.install_for_network, pack)
    print(pack.digest)


if __name__ == "__main__":
    main()
