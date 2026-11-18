"""Document identity shared by new index points and older frozen projections."""


def indexed_identity(record: dict) -> dict | None:
    identity = record.get("document_identity")
    if isinstance(identity, dict):
        return identity
    if record.get("scheme_code"):
        return {
            "code": record["scheme_code"],
            "name": record.get("scheme_name") or record.get("name"),
            "aliases": record.get("scheme_aliases") or [],
        }
    return None
