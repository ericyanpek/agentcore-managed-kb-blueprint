"""Compare two corpus manifests to derive the change set for a release."""


def diff_manifests(previous: dict | None, current: dict) -> dict:
    """Return added/modified/deleted document entries.

    `previous` is None for an initial load, in which case every document in
    `current` is reported as added.
    """
    previous_documents = {
        item["file"]: item for item in (previous or {}).get("documents", [])
    }
    current_documents = {item["file"]: item for item in current["documents"]}

    added = sorted(set(current_documents) - set(previous_documents))
    deleted = sorted(set(previous_documents) - set(current_documents))
    modified = sorted(
        name
        for name in set(current_documents) & set(previous_documents)
        if (
            current_documents[name]["contentSha256"]
            != previous_documents[name]["contentSha256"]
            or current_documents[name]["metadataSha256"]
            != previous_documents[name]["metadataSha256"]
        )
    )
    return {
        "added": [current_documents[name] for name in added],
        "modified": [current_documents[name] for name in modified],
        "deleted": [previous_documents[name] for name in deleted],
    }
